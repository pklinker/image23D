import hashlib
import secrets

KEY_PREFIX = "i23d"


def generate_api_key() -> str:
    return f"{KEY_PREFIX}_{secrets.token_urlsafe(32)}"


def hash_api_key(key: str) -> str:
    # High-entropy random tokens (256 bits from token_urlsafe(32)) don't need
    # a slow KDF like bcrypt -- a fast hash is fine and lets a lookup by hash
    # stay a plain indexed equality query.
    return hashlib.sha256(key.encode()).hexdigest()
