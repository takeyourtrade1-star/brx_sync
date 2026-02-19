"""
Encryption manager for CardTrader tokens using Fernet symmetric encryption.
"""
from typing import Optional

from cryptography.fernet import Fernet

from app.core.config import get_settings

settings = get_settings()


class EncryptionManager:
    """Manages encryption/decryption of sensitive data using Fernet."""

    def __init__(self):
        key_str = settings.FERNET_KEY
        if not key_str:
            raise ValueError("FERNET_KEY not configured")

        try:
            self.fernet = Fernet(key_str.encode("utf-8"))
        except Exception as e:
            raise ValueError(f"Invalid Fernet key format: {e}")

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a plaintext string."""
        return self.fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt a ciphertext string."""
        return self.fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")


# Global instance
_encryption_manager: Optional[EncryptionManager] = None


def get_encryption_manager() -> EncryptionManager:
    """Get or create the global encryption manager instance."""
    global _encryption_manager
    if _encryption_manager is None:
        _encryption_manager = EncryptionManager()
    return _encryption_manager
