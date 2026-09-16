"""Credential vault: Fernet (AES-128-CBC + HMAC-SHA256) symmetric encryption
for user-provided credentials stored in the `connections` table.

The key comes from `SINGULARITY_VAULT_KEY` (a urlsafe-base64 32-byte Fernet key).
In local/dev — where that env var is empty — a key is generated once and cached
in-process, with a loud warning. Production MUST set the env var; otherwise keys
rotate on every restart and stored credentials become undecryptable.
"""

import os

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

_DEV_KEY_FILE = os.path.join(os.path.dirname(__file__), "..", "data", ".vault_key")
_fernet: Fernet | None = None


def _dev_key() -> bytes:
    """Persist a generated key for local runs so creds survive restarts."""
    path = os.path.abspath(_DEV_KEY_FILE)
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return fh.read().strip()
    key = Fernet.generate_key()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(key)
    return key


def get_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet
    configured = get_settings().vault_key.strip()
    key = configured.encode() if configured else _dev_key()
    _fernet = Fernet(key)
    return _fernet


def encrypt(plaintext: str) -> str:
    return get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    try:
        return get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("stored credential could not be decrypted (vault key changed?)") from exc


def generate_key() -> str:
    """Helper for operators: produce a fresh SINGULARITY_VAULT_KEY value."""
    return Fernet.generate_key().decode("utf-8")