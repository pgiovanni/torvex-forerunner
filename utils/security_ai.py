"""Security AI — sealed, tiered case assessment for AltGuard verdicts.

Design: altguard/docs/MULTI-SERVER-DESIGN.md §6-§7. The paid ladder:

    standard   one written assessment per flagged case
    advanced   assessment + adversarial second pass on holds (skeptic rules)
    elite      advocate / skeptic / judge panel

The reviewer is SEALED by construction, and the seals live in this module:

1. **In:** the model only ever receives signal classes and confidence bands
   (built gate-side), with identities replaced by placeholders (Account A/B/…)
   BEFORE the prompt is assembled. No prose reasons, no IPs, no fingerprint
   data, no city/ISP — none of that is even reachable from here.
2. **No human-authored text enters the prompt.** Every prompt line is
   machine-generated from enums; there is no path for a ban reason, nickname
   or message to ride in. The AI cannot be spoken to.
3. **Out:** placeholders are substituted back locally from the case's own
   allow-list; a hallucinated "Account Z" renders as an unknown-account marker.
   An output lint then strips anything shaped like an IP, a long hex string or
   a Discord id that is not on the allow-list.

Entitlements are operator-granted only (same stance as Logging Pro): a row in
security_ai.db, never a key in security_config — a remote admin must not be
able to grant themselves a paid tier. Fair-use case caps are per calendar
month, env-tunable.

Pure module: sqlite3 + stdlib only, no discord import; the model provider is
passed in, so every decision here is testable with a mock.
"""

import contextlib
import os
import re
import sqlite3
import string
import time

TIERS = ("standard", "advanced", "elite")

DB_PATH = os.environ.get(
    "SECURITY_AI_DB",
    os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "security_ai.db")),
)

# Per-tier model ids (env-overridable per the testing plan) and monthly
# fair-use case caps. Caps bound the operator's compute cost, not the value —
# a quiet server never notices them.
MODELS = {
    "standard": os.environ.get("SECURITY_AI_MODEL_STANDARD", "anthropic/claude-haiku-4.5"),
    "advanced": os.environ.get("SECURITY_AI_MODEL_ADVANCED", "anthropic/claude-sonnet-5"),
    "elite": os.environ.get("SECURITY_AI_MODEL_ELITE", "anthropic/claude-fable-5"),
}


def _cap(tier, default):
    try:
        return int(os.environ.get(f"SECURITY_AI_CASES_{tier.upper()}", default))
    except ValueError:
        return default


CASE_CAPS = {
    "standard": _cap("standard", 60),
    "advanced": _cap("advanced", 150),
    "elite": _cap("elite", 400),
}

MAX_TOKENS = 700


# ── entitlements ─────────────────────────────────────────────────────────


@contextlib.contextmanager
def _conn(db=None):
    c = sqlite3.connect(db or DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS security_ai (
        guild_id   INTEGER PRIMARY KEY,
        tier       TEXT NOT NULL,
        expires_ts INTEGER,
        note       TEXT,
        granted_ts INTEGER NOT NULL
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS case_counter (
        guild_id INTEGER NOT NULL,
        month    TEXT NOT NULL,
        count    INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, month)
    )""")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def grant(guild_id, tier, days, note="", db=None, now=None):
    """Operator grant. days <= 0 means no expiry. Returns the stored row."""
    tier = str(tier).strip().lower()
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r} — one of {TIERS}")
    now = int(now if now is not None else time.time())
    expires = now + int(days) * 86400 if int(days) > 0 else None
    with _conn(db) as c:
        c.execute(
            """INSERT INTO security_ai (guild_id, tier, expires_ts, note, granted_ts)
               VALUES (?,?,?,?,?)
               ON CONFLICT(guild_id) DO UPDATE SET
                 tier=excluded.tier, expires_ts=excluded.expires_ts,
                 note=excluded.note, granted_ts=excluded.granted_ts""",
            (int(guild_id), tier, expires, str(note or ""), now),
        )
    return {"guild_id": int(guild_id), "tier": tier, "expires_ts": expires, "note": note}


def revoke(guild_id, db=None):
    with _conn(db) as c:
        c.execute("DELETE FROM security_ai WHERE guild_id=?", (int(guild_id),))


def entitlement(guild_id, db=None):
    """The raw row, expired or not — for /security-ai-status honesty."""
    with _conn(db) as c:
        row = c.execute("SELECT * FROM security_ai WHERE guild_id=?", (int(guild_id),)).fetchone()
    return dict(row) if row else None


def tier_for(guild_id, db=None, now=None) -> str | None:
    """The tier this guild is entitled to RIGHT NOW, or None."""
    row = entitlement(guild_id, db=db)
    if not row:
        return None
    now = now if now is not None else time.time()
    if row["expires_ts"] is not None and row["expires_ts"] < now:
        return None
    return row["tier"]


def _month_key(now=None):
    return time.strftime("%Y-%m", time.gmtime(now if now is not None else time.time()))


def cases_used(guild_id, db=None, now=None) -> int:
    with _conn(db) as c:
        row = c.execute("SELECT count FROM case_counter WHERE guild_id=? AND month=?",
                        (int(guild_id), _month_key(now))).fetchone()
    return int(row["count"]) if row else 0


def take_case(guild_id, tier, db=None, now=None) -> bool:
    """Reserve one case against the month's fair-use cap. False = cap reached
    (the caller reports 'monthly review budget reached', never half-runs)."""
    cap = CASE_CAPS.get(tier, 0)
    month = _month_key(now)
    with _conn(db) as c:
        c.execute("INSERT OR IGNORE INTO case_counter (guild_id, month, count) VALUES (?,?,0)",
                  (int(guild_id), month))
        row = c.execute("SELECT count FROM case_counter WHERE guild_id=? AND month=?",
                        (int(guild_id), month)).fetchone()
        if int(row["count"]) >= cap:
            return False
        c.execute("UPDATE case_counter SET count=count+1 WHERE guild_id=? AND month=?",
                  (int(guild_id), month))
    return True


# ── case building (the input seal) ───────────────────────────────────────

# Human wording for the wire enums. Rendering here means the gate's reason
# vocabulary never needs to reach the model or the copy.
_SIGNAL_TEXT = {
    "device_match": "Device fingerprint matches {accounts} — {band} confidence",
    "device_near_miss": "A device on file is similar but below the match threshold ({band})",
    "device_no_match": "No device match on file — first or unique device",
    "environment_clean": "Browser environment looks ordinary",
    "environment_masked": "Location appears masked — browser locale disagrees with the network",
    "connection_residential": "Connection is an ordinary residential line",
    "connection_mobile": "Connection is a mobile carrier",
    "connection_anonymizer": "Connection is an anonymizer (VPN, proxy or Tor)",
    "connection_datacenter": "Connection is a datacenter or hosting provider",
    "fingerprint_spoofed": "The browser fingerprint shows signs of deliberate spoofing",
}


def signal_lines(signals):
    """Human lines for a result card (accounts render as Discord mentions).
    Same vocabulary as the sealed case, so a mod and the model read the same
    evidence — the mod just sees real names."""
    out = []
    for sig in signals or []:
        text = _SIGNAL_TEXT.get(sig.get("class"))
        if not text:
            continue
        accounts = ", ".join(f"<@{a}>" for a in (sig.get("accounts") or []))
        out.append("• " + text.format(accounts=accounts or "another account",
                                      band=sig.get("band", "unstated")))
    return out


def build_case(uid, signals, verdict, local_facts=None):
    """One flagged verdict -> (case_text, placeholder_map).

    `signals` is the gate's sealed list: [{"class": ..., "band": ...,
    "accounts": [uid, ...]}, ...]. `local_facts` are booleans the REQUESTING
    guild owns (its own ban list / membership) — never free text.

    placeholder_map maps "A", "B", ... -> real uid strings, and stays local:
    it is used to substitute the model's output, never shown to the model.
    """
    facts = dict(local_facts or {})
    letters = iter(string.ascii_uppercase)
    mapping = {next(letters): str(uid)}
    for sig in signals or []:
        for acct in sig.get("accounts") or []:
            acct = str(acct)
            if acct not in mapping.values():
                mapping[next(letters)] = acct
    by_uid = {v: k for k, v in mapping.items()}

    lines = ["Case type: new member flagged by a verification gate.",
             "Subject: Account A.",
             f"Scorer verdict: {'hold for review' if verdict != 'pass' else 'pass'}."]
    for sig in signals or []:
        cls = sig.get("class")
        text = _SIGNAL_TEXT.get(cls)
        if text is None:
            continue  # unknown class: drop, never forward
        accounts = [f"Account {by_uid[str(a)]}" for a in (sig.get("accounts") or [])
                    if str(a) in by_uid]
        lines.append("Signal: " + text.format(
            accounts=", ".join(accounts) or "another account",
            band=sig.get("band", "unstated")))
    if facts.get("matched_account_banned_here"):
        lines.append("Local fact: a matched account is on THIS server's own ban list.")
    if facts.get("matched_account_member_here"):
        lines.append("Local fact: a matched account is currently a member of this server.")
    if facts.get("matched_account_left"):
        lines.append("Local fact: a matched account was previously seen here and has left.")
    if facts.get("matched_account_cleared"):
        lines.append("Local fact: a matched account was previously reviewed here and "
                     "released by this server's moderators (a known false-positive shape).")
    return "\n".join(lines), mapping


# ── prompts (fixed, machine-only) ────────────────────────────────────────

_BASE_RULES = (
    "You are a security reviewer for a Discord verification system. You receive "
    "an abstract case: signal classes with coarse confidence bands, account "
    "placeholders (Account A, Account B, ...), and local facts. That is ALL that "
    "exists — you have no other data, and you must not invent accounts, "
    "percentages, IP addresses or identifiers. Refer to accounts ONLY by their "
    "placeholders. Alt accounts are not an offense by themselves; ban evasion "
    "against THIS server's own ban list is. Shared devices can be households, "
    "internet cafés or families. Anonymizers are common and legitimate; they "
    "raise uncertainty, not guilt."
)

PROMPT_ASSESS = _BASE_RULES + (
    " Write a short assessment for this server's moderators: (1) what the "
    "evidence actually shows, (2) the strongest innocent explanation, (3) the "
    "strongest same-person/evasion explanation, (4) end with exactly one line "
    "'Recommendation: HOLD' or 'Recommendation: RELEASE' followed by a one-"
    "clause reason. Under 180 words."
)

PROMPT_SKEPTIC = _BASE_RULES + (
    " You are the adversarial second pass. Below is a case and a draft "
    "assessment that recommends holding the member. Attack the recommendation: "
    "what would make this a false positive? If the hold does not survive your "
    "attack, overrule it. End with exactly one line 'Recommendation: HOLD' or "
    "'Recommendation: RELEASE' followed by a one-clause reason. Under 150 words."
)

PROMPT_ADVOCATE = _BASE_RULES + (
    " You argue ONE side only: make the strongest honest case that this is the "
    "same person or deliberate evasion. Do not conclude with a recommendation. "
    "Under 120 words."
)

PROMPT_DEFENSE = _BASE_RULES + (
    " You argue ONE side only: make the strongest honest case that this is "
    "innocent (household, coincidence, generic hardware, ordinary privacy "
    "tooling). Do not conclude with a recommendation. Under 120 words."
)

PROMPT_JUDGE = _BASE_RULES + (
    " You are the judge. Below are the case and two opposing arguments. Weigh "
    "them and rule. End with exactly one line 'Recommendation: HOLD' or "
    "'Recommendation: RELEASE' followed by a one-clause reason. Under 150 words."
)


def recommendation_in(text) -> str | None:
    m = re.search(r"Recommendation:\s*(HOLD|RELEASE)", text or "", re.IGNORECASE)
    return m.group(1).upper() if m else None


# ── output lint (the output seal) ────────────────────────────────────────

_IP4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_IP6_RE = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{1,4}\b")
# Requires at least one a-f: a pure-digit run is a Discord id, and those are
# judged against the case's allow-list instead of stripped blindly.
_HEX_RE = re.compile(r"\b(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{12,}\b")
_ID_RE = re.compile(r"\b\d{17,20}\b")
_PLACEHOLDER_RE = re.compile(r"\bAccount ([A-Z])\b")


def lint_output(text, mapping):
    """Substitute placeholders from the allow-list; strip everything that has
    no business in the output. Returns (clean_text, flagged: bool)."""
    flagged = False

    def sub_account(m):
        nonlocal flagged
        uid = mapping.get(m.group(1))
        if uid is None:
            flagged = True
            return "an account not in this case"
        return f"<@{uid}>"

    out = _PLACEHOLDER_RE.sub(sub_account, text or "")
    allow = set(mapping.values())

    def strip_id(m):
        nonlocal flagged
        if m.group(0) in allow:
            return m.group(0)
        flagged = True
        return "▮▮"

    out = _ID_RE.sub(strip_id, out)
    for pat in (_IP4_RE, _IP6_RE, _HEX_RE):
        out, n = pat.subn("▮▮", out)
        flagged = flagged or bool(n)
    return out, flagged


# ── orchestration ────────────────────────────────────────────────────────


async def assess(chat, tier, case_text):
    """Run the tier's pass structure. `chat(system, user) -> str` is the sealed
    model call (model choice, keys and metering live with the caller).

    Returns (final_text, passes_run, recommendation).
    """
    if tier == "standard":
        text = await chat(PROMPT_ASSESS, case_text)
        return text, 1, recommendation_in(text)

    if tier == "advanced":
        first = await chat(PROMPT_ASSESS, case_text)
        rec = recommendation_in(first)
        if rec != "HOLD":
            return first, 1, rec
        second = await chat(PROMPT_SKEPTIC, f"{case_text}\n\n--- Draft assessment ---\n{first}")
        rec2 = recommendation_in(second) or rec
        combined = (f"{first}\n\n**Adversarial second pass**\n{second}")
        return combined, 2, rec2

    if tier == "elite":
        advocate = await chat(PROMPT_ADVOCATE, case_text)
        defense = await chat(PROMPT_DEFENSE, case_text)
        verdict = await chat(
            PROMPT_JUDGE,
            f"{case_text}\n\n--- Argument for evasion ---\n{advocate}"
            f"\n\n--- Argument for innocence ---\n{defense}")
        combined = (f"**For evasion**\n{advocate}\n\n**For innocence**\n{defense}"
                    f"\n\n**Ruling**\n{verdict}")
        return combined, 3, recommendation_in(verdict)

    raise ValueError(f"unknown tier {tier!r}")
