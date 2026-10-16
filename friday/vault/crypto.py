"""Authenticated encryption with libsodium (XChaCha20-Poly1305 IETF AEAD).

Every ciphertext is bound to associated data (e.g. the secret's name), so a
blob copied from one row to another fails to decrypt.
"""

from __future__ import annotations

import base64
import binascii

from nacl import bindings
from nacl.encoding import RawEncoder
from nacl.exceptions import CryptoError
from nacl.hash import blake2b
from nacl.utils import random as random_bytes

from friday.config import ConfigError, Settings

KEY_BYTES = bindings.crypto_aead_xchacha20poly1305_ietf_KEYBYTES
NONCE_BYTES = bindings.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES
VERSION = b"\x01"
KEYRING_USERNAME = "master-key"


class DecryptionError(Exception):
    pass


class Cipher:
    def __init__(self, key: bytes):
        if len(key) != KEY_BYTES:
            raise ValueError(f"key must be {KEY_BYTES} bytes")
        self._key = bytes(key)

    def __repr__(self) -> str:
        return "Cipher(<key redacted>)"

    def subcipher(self, purpose: str) -> Cipher:
        """Derive an independent key for one purpose (secrets, files, sessions...)."""
        return Cipher(self.derive_bytes(purpose, KEY_BYTES))

    def derive_bytes(self, purpose: str, size: int = 32) -> bytes:
        person = purpose.encode()
        if len(person) > 16:
            raise ValueError("purpose must be at most 16 bytes")
        return blake2b(b"", key=self._key, person=person, digest_size=size, encoder=RawEncoder)

    def encrypt(self, plaintext: bytes, *, aad: bytes) -> bytes:
        nonce = random_bytes(NONCE_BYTES)
        ct = bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(plaintext, VERSION + aad, nonce, self._key)
        return VERSION + nonce + ct

    def decrypt(self, blob: bytes, *, aad: bytes) -> bytes:
        if blob[:1] != VERSION or len(blob) < 1 + NONCE_BYTES:
            raise DecryptionError("unknown ciphertext format")
        nonce, ct = blob[1 : 1 + NONCE_BYTES], blob[1 + NONCE_BYTES :]
        try:
            return bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(ct, VERSION + aad, nonce, self._key)
        except CryptoError as e:
            raise DecryptionError("decryption failed (wrong key, tampered data, or mismatched name)") from e


def generate_key_b64() -> str:
    return base64.b64encode(random_bytes(KEY_BYTES)).decode()


def decode_key(text: str) -> bytes:
    text = text.strip()
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            key = decoder(text + "=" * (-len(text) % 4))
        except (binascii.Error, ValueError):
            continue
        if len(key) == KEY_BYTES:
            return key
    raise ConfigError(f"master key must be {KEY_BYTES} bytes, base64-encoded")


def load_master_key(settings: Settings) -> bytes:
    if settings.master_key is not None:
        return decode_key(settings.master_key.get_secret_value())
    import keyring

    try:
        stored = keyring.get_password(settings.keyring_service, KEYRING_USERNAME)
    except Exception as e:  # no keyring backend (e.g. headless Linux)
        raise ConfigError(f"FRIDAY_MASTER_KEY is unset and the OS keyring is unavailable: {e}") from e
    if not stored:
        raise ConfigError("No master key. Run `friday keygen --keyring` or set FRIDAY_MASTER_KEY.")
    return decode_key(stored)


def store_master_key_in_keyring(settings: Settings, key_b64: str) -> None:
    import keyring

    if keyring.get_password(settings.keyring_service, KEYRING_USERNAME):
        raise ConfigError("A master key already exists in the keyring; refusing to overwrite it.")
    keyring.set_password(settings.keyring_service, KEYRING_USERNAME, key_b64)
