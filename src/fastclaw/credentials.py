"""Authenticated encryption for recoverable provider credentials."""

from __future__ import annotations

import base64
import ctypes
import os
import secrets
import stat
from ctypes import wintypes
from pathlib import Path
from typing import Any, Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_PREFIX = "fcsec:v1:"
_DPAPI_PREFIX = "dpapi:v1:"
_KEY_BYTES = 32
_CRYPTPROTECT_UI_FORBIDDEN: Final = 0x1


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


class CredentialError(RuntimeError):
    """Raised when encrypted credentials cannot be safely handled."""


class CredentialCipher:
    """Encrypt credentials with a host key kept outside the database."""

    def __init__(
        self,
        data_root: Path,
        configured_key: str = "",
        *,
        platform_name: str | None = None,
    ) -> None:
        self._data_root = data_root
        self._configured_key = configured_key.strip()
        self._platform_name = platform_name or os.name
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
        payload = self._key_file_payload(key)
        try:
            descriptor = os.open(key_path, flags, 0o600)
        except FileExistsError:
            return self._read_key_file(key_path)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        return key

    def _read_key_file(self, key_path: Path) -> bytes:
        file_stat = key_path.lstat()
        if not stat.S_ISREG(file_stat.st_mode):
            raise CredentialError("master.key must be a regular file")
        if self._platform_name != "nt" and file_stat.st_mode & 0o077:
            raise CredentialError("master.key must be a regular file with mode 0600")
        value = key_path.read_text().strip()
        if self._platform_name == "nt":
            if value.startswith(_DPAPI_PREFIX):
                return self._unprotect_windows_key(value.removeprefix(_DPAPI_PREFIX))
            # Upgrade a raw key copied from a POSIX host to user-bound DPAPI.
            key = self._decode_key(value)
            self._replace_key_file(key_path, self._key_file_payload(key))
            return key
        return self._decode_key(value)

    def _key_file_payload(self, key: bytes) -> bytes:
        if self._platform_name == "nt":
            protected = self._protect_windows_key(key)
            return f"{_DPAPI_PREFIX}{base64.urlsafe_b64encode(protected).decode()}".encode()
        return base64.urlsafe_b64encode(key)

    @staticmethod
    def _replace_key_file(key_path: Path, payload: bytes) -> None:
        temporary = key_path.with_name(f".{key_path.name}.{secrets.token_hex(6)}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(key_path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _protect_windows_key(key: bytes) -> bytes:
        crypt32, kernel32 = CredentialCipher._windows_libraries()
        source_buffer = ctypes.create_string_buffer(key)
        source = _DataBlob(
            len(key),
            ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        protected = _DataBlob()
        success = crypt32.CryptProtectData(
            ctypes.byref(source),
            "FastClaw provider master key",
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(protected),
        )
        if not success:
            raise CredentialError(f"Windows DPAPI encryption failed: {kernel32.GetLastError()}")
        try:
            return ctypes.string_at(protected.pbData, protected.cbData)
        finally:
            kernel32.LocalFree(protected.pbData)

    @staticmethod
    def _unprotect_windows_key(value: str) -> bytes:
        crypt32, kernel32 = CredentialCipher._windows_libraries()
        try:
            protected_bytes = base64.urlsafe_b64decode(value.encode())
        except ValueError as exc:
            raise CredentialError("invalid Windows DPAPI master key") from exc
        protected_buffer = ctypes.create_string_buffer(protected_bytes)
        protected = _DataBlob(
            len(protected_bytes),
            ctypes.cast(protected_buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        plaintext = _DataBlob()
        success = crypt32.CryptUnprotectData(
            ctypes.byref(protected),
            None,
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(plaintext),
        )
        if not success:
            raise CredentialError(f"Windows DPAPI decryption failed: {kernel32.GetLastError()}")
        try:
            key = ctypes.string_at(plaintext.pbData, plaintext.cbData)
        finally:
            kernel32.LocalFree(plaintext.pbData)
        if len(key) != _KEY_BYTES:
            raise CredentialError("Windows DPAPI master key has an invalid length")
        return key

    @staticmethod
    def _windows_libraries() -> tuple[Any, Any]:
        if os.name != "nt":
            raise CredentialError("Windows DPAPI is unavailable on this platform")
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            raise CredentialError("Windows DPAPI is unavailable")
        return windll.crypt32, windll.kernel32
