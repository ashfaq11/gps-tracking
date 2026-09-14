"""
Password hashing and sign-in tokens.

Kept apart from the repository so that the storage layer never sees a
plaintext password: a repository takes a hash, and only this module knows how
one is made.
"""

import hashlib
import hmac
import secrets

import bcrypt

# bcrypt truncates at 72 bytes and, in some versions, raises above it. Reject
# early rather than silently ignoring the tail of a long passphrase.
MAX_PASSWORD_BYTES = 72
MIN_PASSWORD_LENGTH = 8


def hash_password(password: str) -> str:
    """bcrypt hash, salt included, safe to store."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """
    Constant-time check of a password against a stored hash.

    Returns False rather than raising on a malformed hash: a corrupt row
    should fail the sign-in, not the request.
    """
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


def new_token() -> str:
    """A fresh opaque session token. 32 bytes of urandom, URL-safe."""
    return secrets.token_urlsafe(32)


def token_fingerprint(token: str) -> str:
    """
    What gets stored for a token.

    SHA-256 rather than bcrypt: this is a 256-bit random string, not a
    human-chosen password, so there is nothing to brute-force and no reason
    to pay a KDF's cost on every authenticated request. Hashing it at all is
    what stops a leaked table from handing over live sessions.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_match(supplied: str, stored_hash: str) -> bool:
    """Compare fingerprints without leaking timing information."""
    return hmac.compare_digest(token_fingerprint(supplied), stored_hash)
