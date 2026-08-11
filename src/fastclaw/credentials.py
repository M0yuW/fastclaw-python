"""Authenticated encryption for recoverable provider credentials."""

from __future__ import annotations

import base64
import os
import secrets
import stat
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_PREFIX = "fcsec:v1:"
_KEY_BYTES = 32


class CredentialError(RuntimeError):
    """Raised when encrypted credentials cannot be safely handled."""


class CredentialCipher:
    """Encrypt credentials with a host key kept outside the database."""

    def __init__(self, data_root: Path, configured_key: str = "") -> None:
        self._data_root = data_root
        self._configured_key = configured_key.strip()
        self._key: bytes | None = None

    def encrypt(self, plaintext: str, *, context: str) -> str:
        if not plaintext:
            return ""
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self._load_key()).encrypt(
            nonce,
            plaintext.encode(),
            context.encode(),
        )
        token = base64.urlsafe_b64encode(nonce + ciphertext).decode()
        return f"{_PREFIX}{token}"

    def decrypt(self, token: str, *, context: str) -> str:
        if not token:
            return ""
        if not token.startswith(_PREFIX):
            raise CredentialError("unsupported encrypted credential format")
        try:
            payload = base64.urlsafe_b64decode(token.removeprefix(_PREFIX).encode())
            plaintext = AESGCM(self._load_key()).decrypt(
                payload[:12],
                payload[12:],
                context.encode(),
            )
        except (InvalidTag, ValueError) as exc:
            raise CredentialError("provider credential cannot be decrypted") from exc
        return plaintext.decode()

    def _load_key(self) -> bytes:
        if self._key is not None:
            return self._key
        if self._configured_key:
            self._key = self._decode_key(self._configured_key)
            return self._key
        self._key = self._load_or_create_key_file()
        return self._key

    @staticmethod
    def _decode_key(value: str) -> bytes:
        try:
            key = base64.urlsafe_b64decode(value.encode())
        except ValueError as exc:
            raise CredentialError("FASTCLAW_MASTER_KEY must be URL-safe base64") from exc
        if len(key) != _KEY_BYTES:
            raise CredentialError("FASTCLAW_MASTER_KEY must encode exactly 32 bytes")
        return key

    def _load_or_create_key_file(self) -> bytes:
        self._data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        key_path = self._data_root / "master.key"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        key = secrets.token_bytes(_KEY_BYTES)
        try:
            descriptor = os.open(key_path, flags, 0o600)
        except FileExistsError:
            return self._read_key_file(key_path)
        try:
            os.write(descriptor, base64.urlsafe_b64encode(key))
        finally:
            os.close(descriptor)
        return key

    def _read_key_file(self, key_path: Path) -> bytes:
        file_stat = key_path.lstat()
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_mode & 0o077:
            raise CredentialError("master.key must be a regular file with mode 0600")
        return self._decode_key(key_path.read_text().strip())
