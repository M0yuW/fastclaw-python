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
