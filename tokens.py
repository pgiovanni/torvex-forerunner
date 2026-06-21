"""Signed-token helpers. IDENTICAL copy lives in ../gate/tokens.py.
Both sides MUST share the same ALTGUARD_SECRET so tokens validate."""
import base64

from itsdangerous import URLSafeTimedSerializer

SALT = "altguard-verify-v1"


def pack(token: str) -> str:
    """Wrap a signed token into a single dot-free blob so Discord's auth-token
    scanner doesn't flag the verify link (the signed form looks like a token)."""
    return base64.urlsafe_b64encode(token.encode()).decode().rstrip("=")


def unpack(packed: str) -> str:
    pad = "=" * (-len(packed) % 4)
    return base64.urlsafe_b64decode(packed + pad).decode()


def _serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt=SALT)


def make_token(secret: str, uid, gid, ov: bool = False) -> str:
    """Create a short-lived signed token binding a Discord user+guild."""
    return _serializer(secret).dumps({"uid": str(uid), "gid": str(gid), "ov": bool(ov)})


def read_token(secret: str, token: str, max_age: int = 1800) -> dict:
    """Validate + decode. Raises itsdangerous.BadSignature / SignatureExpired."""
    return _serializer(secret).loads(token, max_age=max_age)
