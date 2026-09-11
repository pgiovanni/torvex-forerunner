"""Last to survive — the ban race. Engine + store, no discord import.

The pitch Paul gives players is the whole rulebook: "you've got three lives,
there are power-ups, pick who you're going for and shoot, see you after the
round ends." Everything below exists so the bot can enforce that sentence
without anyone reading more than it.

Why the bot fires the shots instead of handing out Ban Members: Discord will
not let a member ban anyone whose top role is equal to or above their own, so
a flat "everyone can ban" role means nobody can ban anybody. The bot sits at
the top of the hierarchy and executes on players' behalf — which is also what
makes rules possible at all (lives, hidden shots, power-ups) instead of a
ten-second click race.

Round model (all resolution is SIMULTANEOUS at round close):
  * every living player gets one shot per round, banked up to `shot_cap`;
  * shots are cast privately and resolve together in a random order — so a
    player who dies this round still fires (dead man's shot) and two people
    focusing one target is how you kill someone in a single round;
  * a shot costs one life; at zero the bot bans you (mode "real") or just
    marks you out (mode "ghost");
  * shields eat one whole shot and are hidden until they pop; none work in
    sudden death (alive <= `sudden_death_at`, rounds also run at half length);
  * `backfire` fraction of shots hit the shooter instead;
  * the STORM takes a life from the least active survivor each round from
    `storm_from_round` on, whenever 3+ are alive — hiding is not a strategy;
  * the top killer (2+ kills, unique max) carries a BOUNTY: finishing them is
    worth two extra shots;
  * one PURGE round per race (picked at start) gives everyone three shots;
  * killing blows pay a shield (or a shot if you already hold one).

Power-ups (dropped in the channel, first click takes it):
  shield / shot / overload (take 1 to deal 2) / transfuse (give 1, lose 1).

Everything is scoped by guild through the race row, and a guild can only have
one race in lobby or running at a time.
"""
import json
import os
import random
import sqlite3
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("TORVEX_BANRACE_DB") or os.path.join(ROOT, "ban_race.db")

DEFAULT_CHANNEL = "last-to-survive"

DEFAULTS = dict(
    lives=3,
    round_secs=180,
    mode="real",             # real = actual bans, ghost = marked out only
    backfire=0.10,
    sudden_death_at=5,
    min_account_days=7,
    shot_cap=3,
    storm_from_round=2,
    drop_chance=0.6,         # chance a round spawns a power-up drop
    drop_window=10,          # seconds a drop stays grabbable
)

MODES = ("real", "ghost")
MIN_PLAYERS = 3

POWERUPS = {
    "shield":    ("🛡️", "Shield",     "Eats the next shot fired at you. One held at a time — a second becomes a shot."),
    "shot":      ("🔫", "Extra shot", "One more shot in the bank."),
    "overload":  ("💥", "Overload",   "Take 1 damage to deal 2. Costs a shot."),
    "transfuse": ("💉", "Transfuse",  "Give someone 1 life, lose 1 yourself."),
}
DROP_WEIGHTS = {"shield": 3, "shot": 3, "overload": 2, "transfuse": 2}
USABLE = ("overload", "transfuse")   # the two a player has to aim

ACTIVE = ("lobby", "running")


# ── store ─────────────────────────────────────────────────────────────────────

def _conn(db=None):
    c = sqlite3.connect(db or DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init(db=None):
    with _conn(db) as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS races (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id      TEXT NOT NULL,
                channel_id    TEXT NOT NULL,
                host_id       TEXT NOT NULL,
                status        TEXT NOT NULL,          -- lobby|running|finished|aborted
                settings      TEXT NOT NULL,          -- json
                round_no      INTEGER NOT NULL DEFAULT 0,
                round_ends_at REAL,
                purge_round   INTEGER,
                lobby_msg_id  TEXT,
                round_msg_id  TEXT,
                invite_url    TEXT,
                created_at    REAL NOT NULL,
                started_at    REAL,
                finished_at   REAL,
                winner_ids    TEXT                    -- json list
            );
            CREATE INDEX IF NOT EXISTS idx_race_guild ON races(guild_id, status);
            CREATE TABLE IF NOT EXISTS players (
                race_id     INTEGER NOT NULL,
                user_id     TEXT NOT NULL,
                name        TEXT NOT NULL,
                lives       INTEGER NOT NULL,
                shots       INTEGER NOT NULL DEFAULT 0,
                shield      INTEGER NOT NULL DEFAULT 0,
                overload    INTEGER NOT NULL DEFAULT 0,
                transfuse   INTEGER NOT NULL DEFAULT 0,
                kills       INTEGER NOT NULL DEFAULT 0,
                bounty      INTEGER NOT NULL DEFAULT 0,
                alive       INTEGER NOT NULL DEFAULT 1,
                died_round  INTEGER,
                banned      INTEGER NOT NULL DEFAULT 0,
                joined_at   REAL NOT NULL,
                PRIMARY KEY (race_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS shots (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                race_id    INTEGER NOT NULL,
                round_no   INTEGER NOT NULL,
                shooter_id TEXT NOT NULL,
                target_id  TEXT NOT NULL,
                kind       TEXT NOT NULL,             -- shot|overload|transfuse
                ts         REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_shots_round ON shots(race_id, round_no);
            CREATE TABLE IF NOT EXISTS log (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                race_id  INTEGER NOT NULL,
                round_no INTEGER NOT NULL,
                ts       REAL NOT NULL,
                text     TEXT NOT NULL
            );
        """)


def _race_row(r):
    if r is None:
        return None
    d = dict(r)
    d["settings"] = json.loads(d["settings"] or "{}")
    d["winner_ids"] = json.loads(d["winner_ids"] or "[]")
    return d


def create_race(guild_id, channel_id, host_id, settings=None, now=None, db=None):
    """One race in lobby/running per guild. Returns the race dict or raises
    ValueError when one is already on."""
    init(db)
    now = now or time.time()
    s = dict(DEFAULTS)
    s.update({k: v for k, v in (settings or {}).items() if v is not None})
    if s["mode"] not in MODES:
        raise ValueError("mode must be real or ghost")
    with _conn(db) as c:
        row = c.execute("SELECT id FROM races WHERE guild_id=? AND status IN ('lobby','running')",
                        (str(guild_id),)).fetchone()
        if row:
            raise ValueError("a race is already on in this server")
        cur = c.execute(
            "INSERT INTO races (guild_id, channel_id, host_id, status, settings, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (str(guild_id), str(channel_id), str(host_id), "lobby", json.dumps(s), now))
        rid = cur.lastrowid
    return get_race(rid, db)


def get_race(race_id, db=None):
    with _conn(db) as c:
        return _race_row(c.execute("SELECT * FROM races WHERE id=?", (race_id,)).fetchone())


def active_race(guild_id, db=None):
    init(db)
    with _conn(db) as c:
        return _race_row(c.execute(
            "SELECT * FROM races WHERE guild_id=? AND status IN ('lobby','running')"
            " ORDER BY id DESC LIMIT 1", (str(guild_id),)).fetchone())


def running_races(db=None):
    init(db)
    with _conn(db) as c:
        return [_race_row(r) for r in c.execute("SELECT * FROM races WHERE status='running'")]


def update_race(race_id, db=None, **fields):
    if "winner_ids" in fields:
        fields["winner_ids"] = json.dumps([str(x) for x in fields["winner_ids"]])
    if "settings" in fields:
        fields["settings"] = json.dumps(fields["settings"])
    cols = ", ".join(f"{k}=?" for k in fields)
    with _conn(db) as c:
        c.execute(f"UPDATE races SET {cols} WHERE id=?", (*fields.values(), race_id))


def join(race_id, user_id, name, lives, now=None, db=None):
    with _conn(db) as c:
        c.execute("INSERT OR IGNORE INTO players (race_id, user_id, name, lives, joined_at)"
                  " VALUES (?,?,?,?,?)", (race_id, str(user_id), name, lives, now or time.time()))


def leave(race_id, user_id, db=None):
    with _conn(db) as c:
        return c.execute("DELETE FROM players WHERE race_id=? AND user_id=?",
                         (race_id, str(user_id))).rowcount


def players(race_id, db=None):
    with _conn(db) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM players WHERE race_id=? ORDER BY joined_at", (race_id,))]


def player(race_id, user_id, db=None):
    with _conn(db) as c:
        r = c.execute("SELECT * FROM players WHERE race_id=? AND user_id=?",
                      (race_id, str(user_id))).fetchone()
        return dict(r) if r else None


def save_players(race_id, rows, db=None):
    with _conn(db) as c:
        for p in rows:
            c.execute(
                "UPDATE players SET lives=?, shots=?, shield=?, overload=?, transfuse=?, kills=?,"
                " bounty=?, alive=?, died_round=?, banned=? WHERE race_id=? AND user_id=?",
                (p["lives"], p["shots"], p["shield"], p["overload"], p["transfuse"], p["kills"],
                 p["bounty"], p["alive"], p["died_round"], p["banned"], race_id, str(p["user_id"])))


def update_player(race_id, user_id, db=None, **fields):
    cols = ", ".join(f"{k}=?" for k in fields)
    with _conn(db) as c:
        c.execute(f"UPDATE players SET {cols} WHERE race_id=? AND user_id=?",
                  (*fields.values(), race_id, str(user_id)))


def cast(race_id, round_no, shooter_id, target_id, kind="shot", now=None, db=None):
    with _conn(db) as c:
        c.execute("INSERT INTO shots (race_id, round_no, shooter_id, target_id, kind, ts)"
                  " VALUES (?,?,?,?,?,?)",
                  (race_id, round_no, str(shooter_id), str(target_id), kind, now or time.time()))


def retarget(race_id, round_no, shooter_id, target_id, db=None):
    """Re-aim the shooter's most recent plain shot this round. Returns True if
    there was one to move."""
    with _conn(db) as c:
        r = c.execute("SELECT id FROM shots WHERE race_id=? AND round_no=? AND shooter_id=?"
                      " AND kind='shot' ORDER BY id DESC LIMIT 1",
                      (race_id, round_no, str(shooter_id))).fetchone()
        if not r:
            return False
        c.execute("UPDATE shots SET target_id=? WHERE id=?", (str(target_id), r["id"]))
        return True


def shots(race_id, round_no, db=None):
    with _conn(db) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM shots WHERE race_id=? AND round_no=? ORDER BY id", (race_id, round_no))]


def log(race_id, round_no, text, now=None, db=None):
    with _conn(db) as c:
        c.execute("INSERT INTO log (race_id, round_no, ts, text) VALUES (?,?,?,?)",
                  (race_id, round_no, now or time.time(), text))


# ── engine (pure) ─────────────────────────────────────────────────────────────

def m(uid):
    return f"<@{uid}>"


def alive(rows):
    return [p for p in rows if p["alive"]]


def is_sudden_death(rows, at):
    return len(alive(rows)) <= at


def join_error(created_at, now, min_days, is_bot=False, bannable=True, mode="real"):
    """Why this account can't enter — or None."""
    if is_bot:
        return "Bots don't race."
    age_days = (now - created_at) / 86400.0
    if age_days < min_days:
        return (f"Accounts need to be at least **{min_days} days** old to enter "
                f"(yours is {age_days:.0f}). The prize attracts alts; this keeps it fair.")
    if mode == "real" and not bannable:
        return ("The bot can't ban you (owner, or your top role is above the bot's), so you "
                "can't be eliminated — hosts like that watch, they don't play.")
    return None


def pick_purge_round(rng, n_players):
    """A purge round somewhere in the early-middle of the race, never round 1."""
    hi = max(2, min(4, n_players // 4 + 2))
    return rng.randint(2, hi)


def open_round(rows, round_no, purge_round, shot_cap):
    """Bank the round's shot for every survivor. Returns the lines to announce."""
    lines = []
    purge = round_no == purge_round
    for p in alive(rows):
        if purge:
            p["shots"] = max(p["shots"], 3)
        else:
            p["shots"] = min(shot_cap, p["shots"] + 1)
    if purge:
        lines.append("🩸 **PURGE ROUND** — everyone has three shots. Nobody is safe.")
    return lines


def roll_drop(rng, weights=None):
    w = weights or DROP_WEIGHTS
    kinds = list(w)
    return rng.choices(kinds, weights=[w[k] for k in kinds], k=1)[0]


def grant(p, kind, shot_cap):
    """Hand a power-up to a player. A second shield turns into a shot so the
    'one held at a time' rule never wastes a grab."""
    if kind == "shield":
        if p["shield"]:
            p["shots"] = min(shot_cap + 1, p["shots"] + 1)
            return f"🛡️ {m(p['user_id'])} already holds a shield — it became an extra shot."
        p["shield"] = 1
        return f"🛡️ {m(p['user_id'])} grabbed a **shield**."
    if kind == "shot":
        p["shots"] = min(shot_cap + 1, p["shots"] + 1)
        return f"🔫 {m(p['user_id'])} grabbed an **extra shot**."
    if kind == "overload":
        p["overload"] += 1
        return f"💥 {m(p['user_id'])} grabbed **Overload** — take 1 to deal 2."
    if kind == "transfuse":
        p["transfuse"] += 1
        return f"💉 {m(p['user_id'])} grabbed **Transfuse** — give 1, lose 1."
    raise ValueError(kind)


def cast_error(p, target, kind="shot"):
    """Why this cast is illegal — or None. `target` may be None for a bad id."""
    if p is None or not p["alive"]:
        return "You're out of the race."
    if target is None or not target["alive"]:
        return "That player isn't in the race (or is already gone)."
    if target["user_id"] == p["user_id"] and kind != "transfuse":
        return "Shooting yourself is what backfire is for."
    if target["user_id"] == p["user_id"]:
        return "Transfuse gives a life to someone ELSE."
    if kind == "shot" and p["shots"] <= 0:
        return None  # caller may re-aim; it decides
    if kind == "overload":
        if p["overload"] <= 0:
            return "You don't hold an Overload."
        if p["shots"] <= 0:
            return "Overload rides on a shot and you're out of shots this round."
    if kind == "transfuse":
        if p["transfuse"] <= 0:
            return "You don't hold a Transfuse."
        if p["lives"] <= 1:
            return "Transfuse costs a life and you only have one."
    return None


def spend(p, kind):
    """Debit the cast from the player's bank/inventory."""
    if kind == "shot":
        p["shots"] -= 1
    elif kind == "overload":
        p["shots"] -= 1
        p["overload"] -= 1
    elif kind == "transfuse":
        p["transfuse"] -= 1


def _die(p, round_no, lines, how):
    p["lives"] = 0
    p["alive"] = 0
    p["died_round"] = round_no
    lines.append(how)


def resolve_round(rows, shot_rows, round_no, rng, *, backfire, sudden, storm, msgs,
                  max_lives, shot_cap):
    """Apply every shot cast this round, then the storm, then re-place the
    bounty. Mutates `rows` in place. Returns a dict:
      lines   — announcement lines in resolution order (mentions inline)
      dead    — user_ids eliminated this round, in order
      killers — {victim_id: killer_id} for kill credit (storm/self deaths absent)
      winners — [] while the race goes on; [uid] for a winner; several = draw
    `sudden` disables shields. `msgs` is {user_id: messages this round} for the
    storm. Shooters who die mid-batch still fire (dead man's shot)."""
    P = {p["user_id"]: p for p in rows}
    lines, dead, killers = [], [], {}
    order = list(shot_rows)
    rng.shuffle(order)

    rewards = []   # (shooter, victim) — paid AFTER every shot has landed

    def reward_kill(shooter, victim):
        victim_id = victim["user_id"]
        dead.append(victim_id)
        if shooter is victim:
            return
        shooter["kills"] += 1
        killers[victim_id] = shooter["user_id"]
        rewards.append((shooter, victim))

    def pay(shooter, victim):
        if shooter["shield"]:
            shooter["shots"] = min(shot_cap + 1, shooter["shots"] + 1)
            lines.append(f"   ↳ {m(shooter['user_id'])} gets an extra shot for the kill.")
        else:
            shooter["shield"] = 1
            lines.append(f"   ↳ {m(shooter['user_id'])} gets a shield for the kill.")
        if victim["bounty"]:
            shooter["shots"] = min(shot_cap + 2, shooter["shots"] + 2)
            lines.append(f"   ↳ 🎯 **Bounty claimed** — {m(shooter['user_id'])} banks two more shots.")

    for s in order:
        shooter, target = P.get(s["shooter_id"]), P.get(s["target_id"])
        if shooter is None or target is None:
            continue
        kind = s["kind"]
        sid, tid = shooter["user_id"], target["user_id"]

        if kind == "transfuse":
            if not target["alive"]:
                lines.append(f"💉 {m(sid)} tried to transfuse {m(tid)} — too late, they're gone.")
                continue
            if shooter["lives"] <= 0:
                lines.append(f"💉 {m(sid)} had nothing left to give {m(tid)}.")
                continue
            shooter["lives"] -= 1
            target["lives"] = min(max_lives, target["lives"] + 1)
            lines.append(f"💉 {m(sid)} **transfused** {m(tid)} — {m(tid)} up to {target['lives']}, "
                         f"{m(sid)} down to {shooter['lives']}.")
            if shooter["lives"] == 0 and shooter["alive"]:
                _die(shooter, round_no, lines, f"⛔ {m(sid)} gave their last life away. **Eliminated.**")
                dead.append(sid)
            continue

        dmg = 1
        if kind == "overload":
            dmg = 2
            shooter["lives"] -= 1
            lines.append(f"💥 {m(sid)} **overloaded** — burns a life to fire double.")
            if shooter["lives"] <= 0 and shooter["alive"]:
                _die(shooter, round_no, lines, f"⛔ {m(sid)} burned out on their own overload. **Eliminated.**")
                dead.append(sid)

        victim = target
        if rng.random() < backfire:
            victim = shooter
            lines.append(f"🔥 **Backfire!** {m(sid)}'s shot at {m(tid)} hit themselves.")
        if not victim["alive"]:
            if victim is shooter:
                continue
            lines.append(f"💨 {m(sid)} shot at {m(tid)} — already gone. Wasted.")
            continue
        vid = victim["user_id"]
        if victim["shield"] and not sudden:
            victim["shield"] = 0
            lines.append(f"🛡️ {m(vid)}'s **shield** ate {m(sid)}'s shot.")
            continue
        victim["lives"] = max(0, victim["lives"] - dmg)
        if victim["lives"] > 0:
            who = "themselves" if victim is shooter else m(vid)
            lines.append(f"🩸 {m(sid)} shot {who} — **{victim['lives']}** "
                         f"{'life' if victim['lives'] == 1 else 'lives'} left.")
        else:
            by = "their own shot" if victim is shooter else m(sid)
            _die(victim, round_no, lines, f"⛔ {m(vid)} was **BANNED** by {by}.")
            reward_kill(shooter, victim)

    # kill rewards land only now: a shield earned this round must not eat a
    # shot that was fired this round (resolution is simultaneous)
    for shooter, victim in rewards:
        pay(shooter, victim)

    # the storm: least active survivor bleeds one life, shields don't help
    if storm and len(alive(rows)) >= 3:
        cands = alive(rows)
        low = min(msgs.get(p["user_id"], 0) for p in cands)
        pool = [p for p in cands if msgs.get(p["user_id"], 0) == low]
        v = rng.choice(pool)
        v["lives"] -= 1
        if v["lives"] > 0:
            lines.append(f"🌪️ The **storm** took a life from {m(v['user_id'])} (quietest this round) — "
                         f"**{v['lives']}** left.")
        else:
            _die(v, round_no, lines, f"🌪️ The **storm** swept {m(v['user_id'])} away (quietest this round). "
                                     f"**Eliminated.**")
            dead.append(v["user_id"])

    # bounty: unique top killer with 2+
    survivors = alive(rows)
    for p in rows:
        p["bounty"] = 0
    if survivors:
        top = max(p["kills"] for p in survivors)
        holders = [p for p in survivors if p["kills"] == top]
        if top >= 2 and len(holders) == 1:
            holders[0]["bounty"] = 1
            lines.append(f"🎯 **Bounty** on {m(holders[0]['user_id'])} ({top} kills) — "
                         f"finish them for two extra shots.")

    winners = []
    if len(survivors) == 1:
        winners = [survivors[0]["user_id"]]
    elif not survivors:
        winners = [p["user_id"] for p in rows if p.get("died_round") == round_no]

    # de-dupe dead while keeping order (a player can't die twice, but be safe)
    seen, uniq = set(), []
    for d in dead:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return {"lines": lines, "dead": uniq, "killers": killers, "winners": winners}


def standings(rows):
    """Two lists for the board: survivors (kills desc, lives desc) and the
    fallen (latest death first). Shields are deliberately NOT included."""
    live = sorted(alive(rows), key=lambda p: (-p["kills"], -p["lives"], p["name"].lower()))
    fallen = sorted([p for p in rows if not p["alive"]],
                    key=lambda p: (-(p["died_round"] or 0), p["name"].lower()))
    return live, fallen
