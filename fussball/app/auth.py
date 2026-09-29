"""Login für genau einen Benutzer: Passwort (PBKDF2) + optional TOTP (2FA).

Umgebungsvariablen (erzeugen mit `python -m fussball set-password`):
  APP_PASSWORD_HASH  pbkdf2_sha256$<iterationen>$<salt>$<hash>
  APP_TOTP_SECRET    Base32-Secret für Google Authenticator & Co. (optional, empfohlen)
  SESSION_SECRET     zufälliger Schlüssel zum Signieren des Session-Cookies
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque

import pyotp

ITERATIONS = 390_000
MAX_ATTEMPTS = 5
WINDOW_SECONDS = 15 * 60


def hash_password(password: str, salt: bytes | None = None, iterations: int = ITERATIONS) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations, base64.b64encode(salt).decode(), base64.b64encode(digest).decode()
    )


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, iterations, salt, digest = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    calc = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
    return hmac.compare_digest(calc, base64.b64decode(digest))


def verify_totp(code: str, secret: str | None) -> bool:
    if not secret:
        return True  # 2FA nicht eingerichtet
    return pyotp.TOTP(secret).verify((code or "").strip().replace(" ", ""), valid_window=1)


class RateLimiter:
    """Max. 5 fehlgeschlagene Logins pro IP in 15 Minuten."""

    def __init__(self, max_attempts: int = MAX_ATTEMPTS, window: int = WINDOW_SECONDS):
        self.max_attempts, self.window = max_attempts, window
        self.failures: dict[str, deque] = defaultdict(deque)

    def blocked(self, key: str) -> bool:
        q = self.failures[key]
        while q and q[0] < time.time() - self.window:
            q.popleft()
        return len(q) >= self.max_attempts

    def fail(self, key: str) -> None:
        self.failures[key].append(time.time())

    def reset(self, key: str) -> None:
        self.failures.pop(key, None)


def credentials() -> tuple[str | None, str | None]:
    return os.getenv("APP_PASSWORD_HASH"), os.getenv("APP_TOTP_SECRET") or None


def session_secret() -> str:
    secret = os.getenv("SESSION_SECRET")
    if not secret:
        # Ohne festen Schlüssel werden Sessions beim Neustart ungültig (sicherer Standard).
        secret = secrets.token_urlsafe(32)
        os.environ["SESSION_SECRET"] = secret
    return secret


def new_totp_secret() -> tuple[str, str]:
    secret = pyotp.random_base32()
    uri = pyotp.TOTP(secret).provisioning_uri(name="owner", issuer_name="PeaceJudge")
    return secret, uri
