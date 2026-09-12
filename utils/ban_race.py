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
  * AFK costs a life: a survivor who cast nothing in a round loses one at
    round close (shields don't apply, no kill credit) — you play or you bleed;
  * the STORM takes a life from the survivor who sent the fewest chat messages each round from
    `storm_from_round` on, whenever 3+ are alive — hiding is not a strategy;
  * the top killer (2+ kills, unique max) carries a BOUNTY: finishing them is
    worth two extra shots;
  * one PURGE round per race (picked at start) gives everyone three shots;
  * drops are GUARANTEED every round and SCALE WITH THE PLAYERS STILL IN:
    alive × `drops_per_player` (min 1, capped so drops never overlap a
    `drop_window`), spread evenly over the middle of the timer — twenty
    players get a busy channel, three get one drop; sudden death adds a
    SUPER drop each round: nuke (everyone else takes 1 at close), full
    heal, arsenal (+3 shots) — instant on grab — and, one super in ten, the
    GOLDEN APPLE: +2 lives that go ABOVE max (capped at max+2), the only
    way past the cap (Paul 9/12, Minecraft reference);
  * killing blows pay a shield (or a shot if you already hold one) — unless
    the victim was AFK that round: no shield, no shot, no kill credit, no
    bounty. Shooting someone who isn't playing is free, so it pays nothing.

Power-ups (dropped in the channel, first click takes it):
  shield / shot / overload (take 1 to deal 2) / transfuse (give 1, lose 1) /
  patch (heal 1) / medkit (heal 2, forfeit this round's vote — can't be used
  after voting, and the AFK penalty doesn't apply that round).

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
    mode="ghost",            # ghost = marked out only; real = actual bans (opt-in)
    backfire=0.10,
    sudden_death_at=5,       # overwritten at start from the head-count unless the host set it (9/12)
    min_account_days=7,
    min_players=3,           # lobby needs this many before Start works
    shot_cap=3,
    storm_from_round=2,
    drops_per_player=0.5,    # drops per round = alive players × this (min 1; Paul 9/12: "scale with active users")
    drop_window=10,          # seconds a drop stays grabbable
)

MODES = ("real", "ghost")
MIN_PLAYERS = 3   # floor for the setting; below this the game has no decisions

# Every power-up says what it gives AND what it costs (Paul 9/12: "make sure
# every power up has its benefits and weaknesses listed"). The blurb is shown
# on the drop, in the kit, and in the lobby — one string, ✅ then ❌.
POWERUPS = {
    "shield":    ("🛡️", "Shield",
                  "✅ Eats the next shot fired at you, whole. "
                  "❌ One at a time (a second becomes a Patch); OFF in sudden death; "
                  "doesn't stop the AFK penalty or the storm."),
    "shot":      ("🔫", "Extra shot",
                  "✅ One more shot in the bank — fire twice in a round. "
                  "❌ Still 1 damage each; the bank caps out, so hoarding wastes it."),
    "overload":  ("💥", "Overload",
                  "✅ Your shot deals 2 instead of 1 — a full-health kill in two rounds, or a finisher now. "
                  "❌ Burns 1 of YOUR lives the moment it fires (it can kill you) and rides on a banked shot."),
    "transfuse": ("💉", "Transfuse",
                  "✅ Give someone 1 life — keep an ally in. "
                  "❌ You lose 1; refused on your last life; it does NOT count as your vote, so you still owe a shot."),
    "patch":     ("🩹", "Patch",
                  "✅ Heal 1, instantly, no strings. "
                  "❌ Can't go above max lives — wasted at full health."),
    "medkit":    ("🏥", "Medkit",
                  "✅ Heal 2, instantly. "
                  "❌ You sit this round's vote out (can't use it after you've fired); wasted at full health."),
}
# Drop tiers (Paul 9/12): "the ones that have a pitfall should be more common;
# the ones that don't have pitfalls are rares." A pitfall is a COST you pay
# (a life, your vote) — not a mere limit like "one shield at a time".
#   common — bites back: Overload (burns a life), Transfuse (costs a life),
#            Medkit (costs the vote)
#   rare   — no strings: Shield, Extra shot, Patch
# Weights live on the tier, so re-tiering a power-up is a one-word edit.
TIERS = {
    "common": ("⚪", "Common", 3),
    "rare":   ("🟡", "Rare",   1),
}
POWERUP_TIER = {
    "overload": "common", "transfuse": "common", "medkit": "common",
    "shield": "rare", "shot": "rare", "patch": "rare",
}
# Within a tier a kind can be rarer still. Extra shots also arrive from
# bounties, purge rounds and Arsenal, so the drop itself is halved (Paul 9/12:
# "extra shot appearing way too much").
KIND_WEIGHT = {"shot": 0.5}
DROP_WEIGHTS = {kind: TIERS[tier][2] * KIND_WEIGHT.get(kind, 1) for kind, tier in POWERUP_TIER.items()}


def tier_of(kind):
    """(emoji, label) of a power-up's drop tier."""
    e, label, _ = TIERS[POWERUP_TIER[kind]]
    return e, label
USABLE = ("overload", "transfuse")   # the two a player has to aim
SELF_USE = ("patch", "medkit")       # used on yourself, instantly
ITEM_COLS = ("overload", "transfuse", "patch", "medkit")

# SUPER drops: sudden death only, one per round on top of the regular ones,
# and they fire the moment they're grabbed — no inventory, no aiming.
SUPER = {
    "nuke":     ("🧨", "NUKE",
                 "✅ Every other survivor takes 1 when the round closes — every kill is yours. "
                 "❌ It's your shot for the round, it lands at close (not now), and it makes you everyone's target."),
    "fullheal": ("💖", "Full heal",
                 "✅ Straight back to max lives. "
                 "❌ No shields exist in sudden death — lives are all you've got."),
    "arsenal":  ("🔫", "Arsenal",
                 "✅ +3 shots, right now. "
                 "❌ Still one target per shot, and unfired shots die with you."),
    "goldapple": ("🍎", "Golden apple",
                  "✅ +2 lives that go ABOVE max — the only thing in the race that does. "
                  "❌ SUPER RARE: one super in ten, sudden death only; a shot still takes 1 (it's a buffer, "
                  "not armor), heals can't stack past max+2."),
}
GOLDAPPLE_OVER = 2          # how far above max lives a golden apple can take you
# Golden apple is LEGENDARY — one super drop in ten (Paul: "SUPER rare, only at
# the sudden death finale"; supers only ever fall in sudden death).
SUPER_WEIGHTS = {"nuke": 3, "fullheal": 3, "arsenal": 3, "goldapple": 1}

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
                patch       INTEGER NOT NULL DEFAULT 0,
                medkit      INTEGER NOT NULL DEFAULT 0,
                skip_round  INTEGER,                 -- round whose vote a medkit forfeited
                PRIMARY KEY (race_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS shots (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                race_id    INTEGER NOT NULL,
                round_no   INTEGER NOT NULL,
                shooter_id TEXT NOT NULL,
                target_id  TEXT NOT NULL,
                kind       TEXT NOT NULL,             -- shot|overload|transfuse|nuke
                ts         REAL NOT NULL,
                seq        INTEGER,                   -- shot 1, shot 2 … per shooter per round (banked shots only)
                extra      INTEGER NOT NULL DEFAULT 0, -- 1 = beyond the round's allowance (a drop/reward paid for it)
                result     TEXT,                      -- hit|kill|shielded|backfire|wasted|self … written at close
                prev_target_id TEXT                   -- the ORIGINAL aim when the shot was re-aimed
            );
            CREATE INDEX IF NOT EXISTS idx_shots_round ON shots(race_id, round_no);
            CREATE TABLE IF NOT EXISTS log (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                race_id  INTEGER NOT NULL,
                round_no INTEGER NOT NULL,
                ts       REAL NOT NULL,
                text     TEXT NOT NULL,
                kind     TEXT,                        -- grab|use|resolve|open … (9/12; older rows NULL)
                user_id  TEXT                         -- the player a grab/use belongs to
            );
        """)
        # columns added after the first deploy (9/11 evening): idempotent
        for table, cols in (
            ("players", (("patch", "INTEGER NOT NULL DEFAULT 0"),
                         ("medkit", "INTEGER NOT NULL DEFAULT 0"),
                         ("skip_round", "INTEGER"))),
            ("shots", (("seq", "INTEGER"),
                       ("extra", "INTEGER NOT NULL DEFAULT 0"),
                       ("result", "TEXT"),
                       ("prev_target_id", "TEXT"))),
            ("log", (("kind", "TEXT"),
                     ("user_id", "TEXT"))),
        ):
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            for col, ddl in cols:
                if col not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


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
    s["min_players"] = max(MIN_PLAYERS, int(s["min_players"]))
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


def latest_race(guild_id, db=None):
    """The guild's most recent race in any state — for post-mortems."""
    init(db)
    with _conn(db) as c:
        return _race_row(c.execute(
            "SELECT * FROM races WHERE guild_id=? ORDER BY id DESC LIMIT 1",
            (str(guild_id),)).fetchone())


def guild_races(guild_id, limit=10, db=None):
    """The guild's races, newest first — for looking back."""
    init(db)
    with _conn(db) as c:
        rows = c.execute("SELECT * FROM races WHERE guild_id=? ORDER BY id DESC LIMIT ?",
                         (str(guild_id), limit)).fetchall()
    return [_race_row(r) for r in rows]


def alltime(guild_id, db=None):
    """Career numbers across every FINISHED race in the guild (aborted ones
    never crowned anyone, so they don't count). Returns
      races   — finished races, newest first, each with `winner_names`
      players — {user_id: {name, races, wins, kills, outs, rounds, shots}}
                rounds = rounds survived (died_round, or the race length if
                they were standing at the end); name = latest seen.
    Only structured columns are used — nothing is parsed out of log text."""
    init(db)
    with _conn(db) as c:
        races = [_race_row(r) for r in c.execute(
            "SELECT * FROM races WHERE guild_id=? AND status='finished' ORDER BY id DESC",
            (str(guild_id),)).fetchall()]
        if not races:
            return {"races": [], "players": {}}
        ids = [r["id"] for r in races]
        q = ",".join("?" * len(ids))
        prow = c.execute(f"SELECT * FROM players WHERE race_id IN ({q}) ORDER BY race_id DESC",
                         ids).fetchall()
        shots = {(str(r["race_id"]), r["shooter_id"]): r["n"] for r in c.execute(
            f"SELECT race_id, shooter_id, COUNT(*) n FROM shots WHERE race_id IN ({q})"
            " AND kind IN ('shot','overload','nuke') GROUP BY race_id, shooter_id", ids).fetchall()}
    by_id = {r["id"]: r for r in races}
    names = {}
    players = {}
    for p in prow:
        p = dict(p)
        race = by_id[p["race_id"]]
        uid = p["user_id"]
        names.setdefault(uid, p["name"])
        st = players.setdefault(uid, {"name": p["name"], "races": 0, "wins": 0, "kills": 0,
                                      "outs": 0, "rounds": 0, "shots": 0, "history": []})
        won = uid in race["winner_ids"]
        survived = p["died_round"] if p["died_round"] else race["round_no"]
        st["races"] += 1
        st["wins"] += 1 if won else 0
        st["kills"] += p["kills"] or 0
        st["outs"] += 0 if p["alive"] else 1
        st["rounds"] += survived or 0
        st["shots"] += shots.get((str(p["race_id"]), uid), 0)
        st["history"].append({"race_id": p["race_id"], "won": won, "kills": p["kills"] or 0,
                              "out_round": p["died_round"], "rounds": race["round_no"],
                              "finished_at": race["finished_at"]})
    for r in races:
        r["winner_names"] = [names.get(w, w) for w in r["winner_ids"]]
    return {"races": races, "players": players}


def leaderboard(players, limit=15):
    """Players ordered by wins, then kills, then races — the all-time table."""
    rows = sorted(players.items(), key=lambda kv: (-kv[1]["wins"], -kv[1]["kills"], -kv[1]["races"],
                                                   kv[1]["name"].lower()))
    return [(uid, st) for uid, st in rows[:limit]]


def lobby_races(db=None):
    init(db)
    with _conn(db) as c:
        return [_race_row(r) for r in c.execute("SELECT * FROM races WHERE status='lobby'")]


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
                " bounty=?, alive=?, died_round=?, banned=?, patch=?, medkit=?, skip_round=?"
                " WHERE race_id=? AND user_id=?",
                (p["lives"], p["shots"], p["shield"], p["overload"], p["transfuse"], p["kills"],
                 p["bounty"], p["alive"], p["died_round"], p["banned"], p.get("patch", 0),
                 p.get("medkit", 0), p.get("skip_round"), race_id, str(p["user_id"])))


def update_player(race_id, user_id, db=None, **fields):
    cols = ", ".join(f"{k}=?" for k in fields)
    with _conn(db) as c:
        c.execute(f"UPDATE players SET {cols} WHERE race_id=? AND user_id=?",
                  (*fields.values(), race_id, str(user_id)))


BANKED = ("shot", "overload")   # the kinds that spend a banked shot — these get numbered


def cast(race_id, round_no, shooter_id, target_id, kind="shot", now=None, db=None, allowance=1):
    """Record a cast. Banked shots (shot/overload) are numbered per shooter per
    round — shot 1, shot 2 … — and flagged `extra` past `allowance` (1 normally,
    3 in a purge round), so the story can say WHICH shot did what and which
    ones a drop or reward paid for. Returns {id, seq, extra}."""
    with _conn(db) as c:
        seq, extra = None, 0
        if kind in BANKED:
            n = c.execute("SELECT COUNT(*) FROM shots WHERE race_id=? AND round_no=? AND shooter_id=?"
                          " AND kind IN ('shot','overload')",
                          (race_id, round_no, str(shooter_id))).fetchone()[0]
            seq = n + 1
            extra = 1 if seq > max(1, allowance) else 0
        cur = c.execute("INSERT INTO shots (race_id, round_no, shooter_id, target_id, kind, ts, seq, extra)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (race_id, round_no, str(shooter_id), str(target_id), kind, now or time.time(),
                         seq, extra))
        return {"id": cur.lastrowid, "seq": seq, "extra": extra}


def retarget(race_id, round_no, shooter_id, target_id, db=None):
    """Re-aim the shooter's most recent plain shot this round. Returns the
    moved row's {seq, prev_target_id} — or None if there was nothing to move.
    The ORIGINAL aim is kept in prev_target_id (first re-aim wins) so the
    story can show the change of mind."""
    with _conn(db) as c:
        r = c.execute("SELECT id, seq, target_id, prev_target_id FROM shots WHERE race_id=? AND round_no=?"
                      " AND shooter_id=? AND kind='shot' ORDER BY id DESC LIMIT 1",
                      (race_id, round_no, str(shooter_id))).fetchone()
        if not r:
            return None
        prev = r["prev_target_id"] or r["target_id"]
        c.execute("UPDATE shots SET target_id=?, prev_target_id=? WHERE id=?",
                  (str(target_id), prev, r["id"]))
        return {"seq": r["seq"], "prev_target_id": prev}


def mark_shots(results, db=None):
    """Write each shot's outcome back after the round resolves:
    {shot_id: result}."""
    if not results:
        return
    with _conn(db) as c:
        c.executemany("UPDATE shots SET result=? WHERE id=?",
                      [(res, sid) for sid, res in results.items()])


def shots(race_id, round_no, db=None):
    with _conn(db) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM shots WHERE race_id=? AND round_no=? ORDER BY id", (race_id, round_no))]


def log(race_id, round_no, text, now=None, db=None, kind=None, user_id=None):
    """`kind` = grab|use|resolve|open (what sort of line); `user_id` = whose
    grab/use it is. Both optional — resolution lines name several people."""
    with _conn(db) as c:
        c.execute("INSERT INTO log (race_id, round_no, ts, text, kind, user_id) VALUES (?,?,?,?,?,?)",
                  (race_id, round_no, now or time.time(), text, kind,
                   str(user_id) if user_id is not None else None))


def player_log(race_id, user_id, db=None):
    """Every logged line that mentions the player, in order: resolution
    lines (hits, shields, kills, AFK, storm), drops grabbed, items used."""
    with _conn(db) as c:
        rows = c.execute("SELECT round_no, ts, text, kind FROM log WHERE race_id=?"
                         " AND (user_id=? OR text LIKE ?) ORDER BY id",
                         (race_id, str(user_id), f"%<@{user_id}>%")).fetchall()
    return [dict(r) for r in rows]


def powerup_digest(race_id, user_id, db=None):
    """What one player grabbed and used across a race — the header line of
    their story. Grabs come from tagged log rows (9/12+); uses from the shots
    table (overload/transfuse/nuke casts, extra shots fired) plus tagged
    self-use lines (patch/medkit); shields that popped from the resolve text.
    Returns {"grabbed": {name: n}, "used": {name: n}}."""
    uid = str(user_id)
    grabbed, used = {}, {}

    def bump(d, k, n=1):
        d[k] = d.get(k, 0) + n

    with _conn(db) as c:
        for r in c.execute("SELECT text FROM log WHERE race_id=? AND kind='grab' AND user_id=?", (race_id, uid)):
            name = _bold_name(r["text"])
            if name:
                bump(grabbed, name)
        for r in c.execute("SELECT text FROM log WHERE race_id=? AND kind='use' AND user_id=?", (race_id, uid)):
            name = _bold_name(r["text"])
            if name:
                bump(used, name)
        for r in c.execute("SELECT kind, COUNT(*) n FROM shots WHERE race_id=? AND shooter_id=?"
                           " AND kind IN ('overload','transfuse','nuke') GROUP BY kind", (race_id, uid)):
            bump(used, {"overload": "Overload", "transfuse": "Transfuse", "nuke": "Nuke"}[r["kind"]], r["n"])
        n = c.execute("SELECT COUNT(*) FROM shots WHERE race_id=? AND shooter_id=? AND extra=1",
                      (race_id, uid)).fetchone()[0]
        if n:
            bump(used, "Extra shot", n)
        n = c.execute("SELECT COUNT(*) FROM log WHERE race_id=? AND text LIKE ?",
                      (race_id, f"%{m(uid)}'s **shield** ate%")).fetchone()[0]
        if n:
            bump(used, "Shield", n)
    return {"grabbed": grabbed, "used": used}


def _bold_name(text):
    """The first **bold** span of a log line — grab/use lines bold the item."""
    i = text.find("**")
    if i < 0:
        return None
    j = text.find("**", i + 2)
    return text[i + 2:j] if j > i else None


def digest_line(d):
    """'🎒 grabbed 3 (Shield ×2, Patch) · used 2 (Shield, Extra shot)' or ''."""
    def part(label, counts):
        if not counts:
            return None
        items = ", ".join(f"{k} ×{n}" if n > 1 else k for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
        return f"{label} {sum(counts.values())} ({items})"
    bits = [b for b in (part("grabbed", d["grabbed"]), part("used", d["used"])) if b]
    return ("🎒 " + " · ".join(bits)) if bits else ""


def race_log(race_id, db=None):
    """The whole round log, in order — the open chart."""
    with _conn(db) as c:
        rows = c.execute("SELECT round_no, ts, text FROM log WHERE race_id=? ORDER BY id",
                         (race_id,)).fetchall()
    return [dict(r) for r in rows]


def by_round(log_rows):
    """[(round_no, [lines])] in round order."""
    grouped = {}
    for lg in log_rows:
        grouped.setdefault(lg["round_no"], []).append(lg["text"])
    return [(r, grouped[r]) for r in sorted(grouped)]


def fit_rounds(story, max_fields=24, max_chars=5200, field_chars=1000):
    """Newest rounds first, to fit a Discord embed (25 fields, 6000 chars,
    1024 per field). Returns ([(round_no, text)] in round order, how many of
    the earliest rounds didn't fit)."""
    out, total = [], 0
    for r, lines in reversed(story):
        text = "\n".join(lines)
        if len(text) > field_chars:
            text = text[:field_chars - 2] + " …"
        if len(out) >= max_fields or total + len(text) > max_chars:
            break
        out.append((r, text))
        total += len(text)
    out.reverse()
    return out, len(story) - len(out)


def player_shots(race_id, user_id, db=None):
    """Shots the player cast or was aimed at, in cast order."""
    with _conn(db) as c:
        rows = c.execute("SELECT round_no, shooter_id, target_id, kind, ts, seq, extra, result,"
                         " prev_target_id FROM shots"
                         " WHERE race_id=? AND (shooter_id=? OR target_id=?) ORDER BY id",
                         (race_id, str(user_id), str(user_id))).fetchall()
    return [dict(r) for r in rows]


CAST_VERB = {"shot": "🎯 fired at", "overload": "💥 overloaded at", "transfuse": "💉 transfused",
             "nuke": "🧨 armed a NUKE"}
RESULT_TAG = {"hit": " → hit", "kill": " → **KILL**", "shielded": " → eaten by a shield",
              "backfire": " → backfired", "wasted": " → wasted (already gone)", "self": " → hit themselves",
              "transfused": " → done", "late": " → too late", "nuke": " → went off"}


def timeline(user_id, shot_rows, log_rows):
    """Per-round story of one player: what they aimed at (their casts, in
    order — intent, even when a shot later backfired or was wasted), then what
    the log says happened to and around them (hits, shields popping, kills,
    the AFK penalty, the storm, drops grabbed, items used). Returns
    [(round_no, [lines])] in round order; rounds with nothing are skipped.
    Shots aimed AT the player are deliberately not listed as casts — they were
    secret at the time and the resolution line already says who hit them."""
    uid = str(user_id)
    by_round = {}
    mine = [sh for sh in shot_rows if sh["shooter_id"] == uid]
    # shots get a number only in rounds where the player fired more than one —
    # "shot 1 / shot 2 (extra)" is the whole point (Paul 9/12), a lone shot reads better bare
    banked_per_round = {}
    for sh in mine:
        if sh["kind"] in BANKED and sh.get("seq"):
            banked_per_round[sh["round_no"]] = banked_per_round.get(sh["round_no"], 0) + 1
    for sh in mine:
        verb = CAST_VERB.get(sh["kind"], f"cast {sh['kind']} at")
        line = verb if sh["kind"] == "nuke" else f"{verb} {m(sh['target_id'])}"
        if sh["kind"] in BANKED and sh.get("seq") and banked_per_round.get(sh["round_no"], 0) > 1:
            tag = f"Shot {sh['seq']}" + (" (extra)" if sh.get("extra") else "")
            line = f"{line[:1]} **{tag}** — {line[2:]}" if line[:1] else f"**{tag}** — {line}"
        elif sh["kind"] in BANKED and sh.get("extra"):
            line += " *(extra shot)*"
        if sh.get("prev_target_id") and sh["prev_target_id"] != sh["target_id"]:
            line += f" (re-aimed from {m(sh['prev_target_id'])})"
        if sh.get("result"):
            line += RESULT_TAG.get(sh["result"], "")
        by_round.setdefault(sh["round_no"], []).append(line)
    for lg in log_rows:
        by_round.setdefault(lg["round_no"], []).append(lg["text"])
    return [(r, by_round[r]) for r in sorted(by_round)]


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


def recommended_lives(n_players):
    """Lives that keep a race of n players interesting (Paul 9/12: "scale
    lives with the amount of people playing — a recommended value, not
    forced"). Every survivor fires once a round, so the more players the more
    incoming fire per round; 3 lives was right for 10 and thin for 15.
      3 → 2 · 6 → 3 · 10 → 4 · 15 → 5 · 20 → 7 · 32+ → 10
    Soft cap 10 (Paul 9/12: "it can go above 6, idk if that's too much" —
    ten lives is ~10+ rounds, half an hour at 3 min; raise MAX_RECOMMENDED
    if a huge lobby wants longer). A host who sets `lives:` explicitly
    always wins over this. There is NO cap on players."""
    return max(2, min(MAX_RECOMMENDED_LIVES, 2 + max(0, n_players) // 4))


MAX_RECOMMENDED_LIVES = 10


def recommended_sudden_death(n_players):
    """Alive count at which sudden death starts (shields off, half-length
    rounds). A fixed 5 meant a 6-player race was in sudden death from its
    second round (Paul 9/12: "sudden-deathing every single round, that's not
    normal"). About a quarter of the starters, never below 2, never above 5:
      6 → 2 · 8 → 2 · 12 → 3 · 15 → 4 · 20+ → 5"""
    return max(2, min(5, -(-max(0, n_players) // 4)))


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


def drop_count(alive, per_player, round_secs, drop_window):
    """How many regular drops a round gets: alive players × per_player,
    never fewer than one, never so many that two are grabbable at once
    (the 10–85 % window divided by the grab window)."""
    n = int(round(max(0, alive) * per_player))
    cap = max(1, int(0.75 * round_secs // max(1, drop_window)))
    return max(1, min(cap, n))


def drop_schedule(rng, round_secs, alive, per_player, sudden, drop_window=10):
    """When this round's drops appear: a list of (seconds_into_round, kind,
    is_super). Regular drops are guaranteed and scale with the players still
    alive (see drop_count). They're spread over the middle of the timer in
    equal slots with jitter inside each — steady traffic instead of a random
    clump — so none lands on the open or the close. Sudden death adds one
    SUPER drop."""
    n = drop_count(alive, per_player, round_secs, drop_window)
    lo, hi = 0.10 * round_secs, 0.85 * round_secs
    slot = (hi - lo) / n
    out = []
    for i in range(n):
        out.append((lo + slot * (i + rng.random()), roll_drop(rng), False))
    if sudden:
        out.append((rng.uniform(0.20, 0.70) * round_secs, roll_drop(rng, SUPER_WEIGHTS), True))
    out.sort(key=lambda t: t[0])
    return out


def grant_super(p, kind, shot_cap, max_lives):
    """Instant super effects. `nuke` is not handled here — it's a cast, the
    caller records the shot row. Returns the feed line."""
    if kind == "fullheal":
        p["lives"] = max(p["lives"], max_lives)      # never lowers a golden-appled player
        return f"💖 {m(p['user_id'])} grabbed **Full heal** — back to {p['lives']} lives."
    if kind == "goldapple":
        p["lives"] = min(max_lives + GOLDAPPLE_OVER, p["lives"] + 2)
        return f"🍎 {m(p['user_id'])} bit the **GOLDEN APPLE** — **{p['lives']}** lives, above the cap."
    if kind == "arsenal":
        p["shots"] = min(shot_cap + 3, p["shots"] + 3)
        return f"🔫 {m(p['user_id'])} grabbed **Arsenal** — three more shots."
    raise ValueError(kind)


def grant(p, kind, shot_cap):
    """Hand a power-up to a player. A second shield turns into a Patch so the
    'one held at a time' rule never wastes a grab (was an extra shot until
    9/12 — shots were coming from everywhere)."""
    if kind == "shield":
        if p["shield"]:
            p["patch"] = p.get("patch", 0) + 1
            return f"🛡️ {m(p['user_id'])} already holds a shield — it became a **Patch**."
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
    if kind == "patch":
        p["patch"] = p.get("patch", 0) + 1
        return f"🩹 {m(p['user_id'])} grabbed a **Patch** — heal 1."
    if kind == "medkit":
        p["medkit"] = p.get("medkit", 0) + 1
        return f"🏥 {m(p['user_id'])} grabbed a **Medkit** — heal 2, skip a vote."
    raise ValueError(kind)


def use_self(p, kind, round_no, max_lives, voted_this_round):
    """Patch / Medkit: instant, on yourself. Returns (ok, text). Mutates p."""
    if p is None or not p["alive"]:
        return False, "You're out of the race."
    if kind == "patch":
        if p.get("patch", 0) <= 0:
            return False, "You don't hold a Patch."
        if p["lives"] >= max_lives:
            return False, "You're at full lives — save it."
        p["patch"] -= 1
        p["lives"] = min(max_lives, p["lives"] + 1)
        return True, f"🩹 Patched up — **{p['lives']}** lives."
    if kind == "medkit":
        if p.get("medkit", 0) <= 0:
            return False, "You don't hold a Medkit."
        if p.get("skip_round") == round_no:
            return False, "You already sat this round out."
        if voted_this_round:
            return False, "You've already voted this round — a Medkit costs the vote, so it's next round or never."
        if p["lives"] >= max_lives:
            return False, "You're at full lives — save it."
        p["medkit"] -= 1
        p["lives"] = min(max_lives, p["lives"] + 2)
        p["skip_round"] = round_no
        return True, f"🏥 Medkit used — **{p['lives']}** lives. No shooting for you this round."
    return False, "That isn't something you use on yourself."


def cast_error(p, target, kind="shot", round_no=None):
    """Why this cast is illegal — or None. `target` may be None for a bad id."""
    if p is None or not p["alive"]:
        return "You're out of the race."
    if kind in ("shot", "overload") and round_no is not None and p.get("skip_round") == round_no:
        return "You used a Medkit this round — no shooting until the next one."
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
                  max_lives, shot_cap, afk=True):
    """Apply every shot cast this round, then the storm, then re-place the
    bounty. Mutates `rows` in place. Returns a dict:
      lines   — announcement lines in resolution order (mentions inline)
      dead    — user_ids eliminated this round, in order
      killers — {victim_id: killer_id} for kill credit (storm/self deaths absent)
      winners — [] while the race goes on; [uid] for a winner; several = draw
      results — {shot_id: outcome} for rows that carry an id (hit|kill|
                shielded|backfire|wasted|self|transfused|late|nuke)
    `sudden` disables shields. `msgs` is {user_id: messages this round} for the
    storm. `afk` charges a life to every survivor who cast no shot/overload.
    Shooters who die mid-batch still fire (dead man's shot)."""
    P = {p["user_id"]: p for p in rows}
    lines, dead, killers, results = [], [], {}, {}
    order = list(shot_rows)
    rng.shuffle(order)

    # "A's shot" vs "A's shot 2 (extra)": number a shooter's banked shots only
    # when they fired more than one this round (Paul 9/12: "shot 1 here and
    # shot 2 here")
    fired = {}
    for s in shot_rows:
        if s["kind"] in BANKED and s.get("seq"):
            fired[s["shooter_id"]] = fired.get(s["shooter_id"], 0) + 1

    def shot_name(s):
        sid = s["shooter_id"]
        if s["kind"] in BANKED and s.get("seq") and fired.get(sid, 0) > 1:
            return f"{m(sid)}'s shot {s['seq']}" + (" (extra)" if s.get("extra") else "")
        if s["kind"] in BANKED and s.get("extra"):
            return f"{m(sid)}'s extra shot"
        return f"{m(sid)}'s shot"

    def record(s, res):
        if s.get("id") is not None:
            results[s["id"]] = res

    # who actually played this round: cast a shot/overload/nuke, or sat it
    # out on a Medkit. Everyone else is AFK — the penalty hits them at close,
    # and killing them pays nothing (no free kills on people who aren't here).
    voted = {s["shooter_id"] for s in shot_rows if s["kind"] in ("shot", "overload", "nuke")}

    def is_afk(p):
        return p["user_id"] not in voted and p.get("skip_round") != round_no

    rewards = []   # (shooter, victim) — paid AFTER every shot has landed

    def reward_kill(shooter, victim):
        victim_id = victim["user_id"]
        dead.append(victim_id)
        if shooter is victim:
            return
        killers[victim_id] = shooter["user_id"]      # attribution only
        if is_afk(victim):
            lines.append(f"   ↳ {m(victim_id)} was AFK — no reward for {m(shooter['user_id'])}.")
            return
        shooter["kills"] += 1
        rewards.append((shooter, victim))

    def pay(shooter, victim):
        if shooter["shield"]:
            shooter["patch"] = shooter.get("patch", 0) + 1
            lines.append(f"   ↳ {m(shooter['user_id'])} gets a Patch for the kill (already shielded).")
        else:
            shooter["shield"] = 1
            lines.append(f"   ↳ {m(shooter['user_id'])} gets a shield for the kill.")
        if victim["bounty"]:
            shooter["shots"] = min(shot_cap + 2, shooter["shots"] + 2)
            lines.append(f"   ↳ 🎯 **Bounty claimed** — {m(shooter['user_id'])} banks two more shots.")

    def hit(shooter, victim, dmg, via):
        """One damage application. `via` names the source for the line.
        Returns the outcome word for the shot record."""
        vid = victim["user_id"]
        if not victim["alive"]:
            return "wasted"
        if victim["shield"] and not sudden:
            victim["shield"] = 0
            lines.append(f"🛡️ {m(vid)}'s **shield** ate {via}.")
            return "shielded"
        victim["lives"] = max(0, victim["lives"] - dmg)
        if victim["lives"] > 0:
            who = "themselves" if victim is shooter else m(vid)
            lines.append(f"🩸 {via_cap(via)} hit {who} — **{victim['lives']}** "
                         f"{'life' if victim['lives'] == 1 else 'lives'} left.")
            return "self" if victim is shooter else "hit"
        by = "their own shot" if victim is shooter else via
        _die(victim, round_no, lines, f"⛔ {m(vid)} was **BANNED** by {by}.")
        reward_kill(shooter, victim)
        return "self" if victim is shooter else "kill"

    def via_cap(t):
        return t[0].upper() + t[1:] if t else t

    for s in order:
        shooter, target = P.get(s["shooter_id"]), P.get(s["target_id"])
        if shooter is None or target is None:
            continue
        kind = s["kind"]
        sid, tid = shooter["user_id"], target["user_id"]

        if kind == "nuke":
            lines.append(f"🧨 {m(sid)}'s **NUKE** goes off.")
            for v in rows:
                if v is not shooter and v["alive"]:
                    hit(shooter, v, 1, f"{m(sid)}'s nuke")
            record(s, "nuke")
            continue

        if kind == "transfuse":
            if not target["alive"]:
                lines.append(f"💉 {m(sid)} tried to transfuse {m(tid)} — too late, they're gone.")
                record(s, "late")
                continue
            if shooter["lives"] <= 0:
                lines.append(f"💉 {m(sid)} had nothing left to give {m(tid)}.")
                record(s, "late")
                continue
            record(s, "transfused")
            shooter["lives"] -= 1
            target["lives"] = max(target["lives"], min(max_lives, target["lives"] + 1))
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
        name = shot_name(s)
        backfired = False
        if rng.random() < backfire:
            victim = shooter
            backfired = True
            lines.append(f"🔥 **Backfire!** {name} at {m(tid)} hit themselves.")
        if not victim["alive"]:
            if victim is shooter:
                record(s, "backfire")
                continue
            lines.append(f"💨 {via_cap(name)} at {m(tid)} — already gone. Wasted.")
            record(s, "wasted")
            continue
        out = hit(shooter, victim, dmg, name)
        record(s, "backfire" if backfired else out)

    # kill rewards land only now: a shield earned this round must not eat a
    # shot that was fired this round (resolution is simultaneous)
    for shooter, victim in rewards:
        pay(shooter, victim)

    # AFK: didn't vote this round = one life gone, shields don't apply
    afk_hit = set()
    if afk:
        for p in alive(rows):
            if not is_afk(p):
                continue
            afk_hit.add(p["user_id"])
            p["lives"] -= 1
            if p["lives"] > 0:
                lines.append(f"😴 {m(p['user_id'])} didn't vote — loses a life. **{p['lives']}** left.")
            else:
                _die(p, round_no, lines, f"😴 {m(p['user_id'])} didn't vote and had nothing left. **Eliminated.**")
                dead.append(p["user_id"])

    # the storm: fewest-chat-messages survivor bleeds one life, shields don't help;
    # it skips anyone the AFK penalty already hit this round
    if storm and len(alive(rows)) >= 3:
        cands = [p for p in alive(rows) if p["user_id"] not in afk_hit] or alive(rows)
        low = min(msgs.get(p["user_id"], 0) for p in cands)
        pool = [p for p in cands if msgs.get(p["user_id"], 0) == low]
        v = rng.choice(pool)
        v["lives"] -= 1
        if v["lives"] > 0:
            lines.append(f"🌪️ The **storm** took a life from {m(v['user_id'])} (sent the fewest chat messages this round) — "
                         f"**{v['lives']}** left.")
        else:
            _die(v, round_no, lines, f"🌪️ The **storm** swept {m(v['user_id'])} away (sent the fewest chat messages this round). "
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
    return {"lines": lines, "dead": uniq, "killers": killers, "winners": winners, "results": results}


def standings(rows):
    """Two lists for the board: survivors (kills desc, lives desc) and the
    fallen (latest death first). Shields are deliberately NOT included."""
    live = sorted(alive(rows), key=lambda p: (-p["kills"], -p["lives"], p["name"].lower()))
    fallen = sorted([p for p in rows if not p["alive"]],
                    key=lambda p: (-(p["died_round"] or 0), p["name"].lower()))
    return live, fallen
