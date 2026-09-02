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


def decrypt(token: bytes) -> str:
    return _get().decrypt(bytes(token)).decode("utf-8")
