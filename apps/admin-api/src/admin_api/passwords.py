"""Password hashes for ``users.password_hash``: scrypt from the standard library.

The stored form is ``scrypt$<n>$<r>$<p>$<salt b64>$<hash b64>``, so the cost parameters can
be raised later without breaking existing hashes. Hashing takes tens of milliseconds of
CPU on purpose; async callers run it with ``asyncio.to_thread`` to keep the loop free.
"""

import base64
import hashlib
import hmac
import secrets
from functools import cache

_N, _R, _P = 2**15, 8, 1
_SALT_BYTES = 16
_HASH_BYTES = 32
# scrypt needs 128 * n * r bytes (32 MiB here); OpenSSL's default cap is exactly that.
_MAXMEM = 64 * 1024 * 1024


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode(), salt=salt, n=n, r=r, p=p, maxmem=_MAXMEM, dklen=_HASH_BYTES
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _scrypt(password, salt, _N, _R, _P)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check; a malformed ``stored`` value is a mismatch, not an error."""
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest, validate=True)
        actual = _scrypt(password, base64.b64decode(salt, validate=True), int(n), int(r), int(p))
    except ValueError:
        return False
    return hmac.compare_digest(actual, expected)


@cache
def dummy_hash() -> str:
    """A hash to check against when the email is unknown, so the response takes as long
    as for a wrong password and does not reveal which emails exist."""
    return hash_password(secrets.token_urlsafe(16))
