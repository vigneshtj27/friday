"""RFC 6238 TOTP, generated locally from a stored seed."""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time
from urllib.parse import parse_qs, unquote, urlparse

ALGOS = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256, "SHA512": hashlib.sha512}


def parse_seed(seed: str) -> tuple[bytes, int, int, str]:
    """Accept a base32 secret or an otpauth:// URI. Returns (key, digits, period, algorithm)."""
    seed = seed.strip()
    digits, period, algo = 6, 30, "SHA1"
    if seed.startswith("otpauth://"):
        q = parse_qs(urlparse(seed).query)
        secret = unquote(q["secret"][0])
        digits = int(q.get("digits", ["6"])[0])
        period = int(q.get("period", ["30"])[0])
        algo = q.get("algorithm", ["SHA1"])[0].upper()
    else:
        secret = seed
    secret = secret.replace(" ", "").replace("-", "").upper()
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    return key, digits, period, algo


def hotp(key: bytes, counter: int, digits: int = 6, algo: str = "SHA1") -> str:
    mac = hmac.new(key, struct.pack(">Q", counter), ALGOS[algo]).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**digits).zfill(digits)


def totp(seed: str, *, at: float | None = None) -> tuple[str, int]:
    """Returns (code, seconds remaining in this window)."""
    key, digits, period, algo = parse_seed(seed)
    now = time.time() if at is None else at
    return hotp(key, int(now // period), digits, algo), period - int(now % period)
