"""Provider keys at rest (FR-17): Fernet-encrypted with ``ROZMOVA_FERNET_MASTER_KEY``.

admin-api encrypts a key once on ``PUT /bots/{id}/credentials`` and never returns it; only
``last4`` is shown. The gateway decrypts it in memory when a call loads the bot. Decrypted
keys stay ``SecretStr``, so they print as ``**********`` in logs, reprs and dumps.
Generate a master key with ``python -c "from cryptography.fernet import Fernet;
print(Fernet.generate_key().decode())"``.
"""

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from rozmova_core.settings import Settings


class CredentialError(Exception):
    """A stored key cannot be decrypted: the master key changed or the row is corrupt."""


class CredentialCipher:
    def __init__(self, master_key: SecretStr) -> None:
        self._fernet = Fernet(master_key.get_secret_value().encode())

    @classmethod
    def from_settings(cls, settings: Settings) -> "CredentialCipher | None":
        """``None`` without a master key: provider keys cannot be stored or read."""
        key = settings.fernet_master_key
        return cls(key) if key is not None else None

    def encrypt(self, secret: SecretStr) -> str:
        return self._fernet.encrypt(secret.get_secret_value().encode()).decode()

    def decrypt(self, ciphertext: str) -> SecretStr:
        try:
            return SecretStr(self._fernet.decrypt(ciphertext.encode()).decode())
        except InvalidToken as exc:
            # InvalidToken has no message; nothing about the key leaks from here.
            raise CredentialError("cannot decrypt the provider key") from exc


def last4(secret: SecretStr) -> str:
    return secret.get_secret_value()[-4:]
