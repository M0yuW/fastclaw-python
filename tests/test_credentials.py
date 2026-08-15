from __future__ import annotations

import base64
import os
from pathlib import Path

import pytest

from fastclaw.credentials import CredentialCipher, CredentialError


def test_credential_cipher_round_trip_and_context_binding(tmp_path: Path) -> None:
    cipher = CredentialCipher(tmp_path)

    encrypted = cipher.encrypt("provider-secret", context="cfg_1")

    assert encrypted.startswith("fcsec:v1:")
    assert "provider-secret" not in encrypted
    assert cipher.decrypt(encrypted, context="cfg_1") == "provider-secret"
    with pytest.raises(CredentialError, match="cannot be decrypted"):
        cipher.decrypt(encrypted, context="cfg_2")
    assert (os.stat(tmp_path / "master.key").st_mode & 0o777) == 0o600


def test_configured_master_key_does_not_create_key_file(tmp_path: Path) -> None:
    master_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    cipher = CredentialCipher(tmp_path, master_key)

    encrypted = cipher.encrypt("provider-secret", context="cfg_1")

    assert cipher.decrypt(encrypted, context="cfg_1") == "provider-secret"
    assert not (tmp_path / "master.key").exists()


def test_windows_key_file_uses_user_bound_protection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        CredentialCipher,
        "_protect_windows_key",
        staticmethod(lambda key: b"protected:" + key[::-1]),
    )
    monkeypatch.setattr(
        CredentialCipher,
        "_unprotect_windows_key",
        staticmethod(
            lambda value: base64.urlsafe_b64decode(value.encode()).removeprefix(b"protected:")[::-1]
        ),
    )
    cipher = CredentialCipher(tmp_path, platform_name="nt")

    encrypted = cipher.encrypt("provider-secret", context="cfg_1")
    key_file = (tmp_path / "master.key").read_text()

    assert key_file.startswith("dpapi:v1:")
    assert "provider-secret" not in key_file
    restarted = CredentialCipher(tmp_path, platform_name="nt")
    assert restarted.decrypt(encrypted, context="cfg_1") == "provider-secret"


def test_windows_imports_and_reprotects_posix_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = os.urandom(32)
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "master.key").write_text(base64.urlsafe_b64encode(key).decode())
    monkeypatch.setattr(
        CredentialCipher,
        "_protect_windows_key",
        staticmethod(lambda value: b"protected:" + value),
    )
    cipher = CredentialCipher(tmp_path, platform_name="nt")

    encrypted = cipher.encrypt("provider-secret", context="cfg_1")

    assert (tmp_path / "master.key").read_text().startswith("dpapi:v1:")
    assert cipher.decrypt(encrypted, context="cfg_1") == "provider-secret"
