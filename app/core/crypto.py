"""Encryption for CardTrader credentials with online key rotation support."""
from typing import Optional

from cryptography.fernet import Fernet, MultiFernet

from app.core.config import get_settings

settings = get_settings()


class EncryptionManager:
    """Manages encryption/decryption of sensitive data using Fernet."""

    def __init__(self):
        key_str = settings.FERNET_KEY
        if not key_str:
            raise ValueError("FERNET_KEY not configured")

        try:
            self._primary = Fernet(key_str.encode("utf-8"))
            self.fernet = MultiFernet(
                [self._primary, *(Fernet(key) for key in settings.fernet_previous_keys)]
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid Fernet key configuration") from exc

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a plaintext string."""
        return self.fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt a ciphertext string."""
        return self.fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")

    def rotate(self, ciphertext: str) -> str:
        """Re-encrypt an existing token with the primary key."""
        return self.fernet.rotate(ciphertext.encode("utf-8")).decode("utf-8")

    def encrypt_at_rest_secret(self, plaintext: str) -> str:
        """Mark encrypted values so legacy plaintext webhook secrets remain readable."""
        return "fernet:" + self.encrypt(plaintext)

    def decrypt_at_rest_secret(self, stored: str) -> str:
        if stored.startswith("fernet:"):
            return self.decrypt(stored.removeprefix("fernet:"))
        return stored

    def rotate_at_rest_secret(self, stored: str) -> str:
        if stored.startswith("fernet:"):
            return "fernet:" + self.rotate(stored.removeprefix("fernet:"))
        return self.encrypt_at_rest_secret(stored)


# Global instance
_encryption_manager: Optional[EncryptionManager] = None


def get_encryption_manager() -> EncryptionManager:
    """Get or create the global encryption manager instance."""
    global _encryption_manager
    if _encryption_manager is None:
        _encryption_manager = EncryptionManager()
    return _encryption_manager
