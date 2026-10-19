"""Provider keys encrypt and decrypt with the master key and never print."""

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from rozmova_core.credentials import CredentialCipher, CredentialError, last4
from rozmova_core.settings import Settings

KEY = SecretStr("sk-proj-very-secret-key-a1b2")


def cipher() -> CredentialCipher:
    return CredentialCipher(SecretStr(Fernet.generate_key().decode()))


def test_round_trip() -> None:
    box = cipher()

    ciphertext = box.encrypt(KEY)

    assert KEY.get_secret_value() not in ciphertext
    assert box.decrypt(ciphertext) == KEY


def test_decrypted_key_does_not_print() -> None:
    box = cipher()

    decrypted = box.decrypt(box.encrypt(KEY))

    assert KEY.get_secret_value() not in f"{decrypted} {decrypted!r}"


def test_another_master_key_cannot_decrypt() -> None:
    ciphertext = cipher().encrypt(KEY)

    with pytest.raises(CredentialError):
        cipher().decrypt(ciphertext)


def test_no_master_key_no_cipher() -> None:
    assert CredentialCipher.from_settings(Settings(fernet_master_key=None)) is None
    master = SecretStr(Fernet.generate_key().decode())
    assert CredentialCipher.from_settings(Settings(fernet_master_key=master)) is not None


def test_last4() -> None:
    assert last4(KEY) == "a1b2"
