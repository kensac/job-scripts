from __future__ import annotations

import os

from cryptography.fernet import Fernet

_fernet: Fernet | None = None


def _get() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(os.environ["APP_ENCRYPTION_KEY"])
    return _fernet


def encrypt(value: str) -> bytes:
    return _get().encrypt(value.encode("utf-8"))


def decrypt(token: bytes, *, ttl: int | None = None) -> str:
    """Decrypt a value encrypted by encrypt().

    `ttl` bounds how old the ciphertext may be, in seconds. Fernet stamps a
    timestamp into every token and authenticates it along with the payload, so
    a caller that needs a value to expire - an OAuth state parameter handed to
    a browser and handed back - gets expiry from the same primitive that gives
    it integrity, rather than from a second scheme layered on top.
    """
    return _get().decrypt(bytes(token), ttl=ttl).decode("utf-8")
