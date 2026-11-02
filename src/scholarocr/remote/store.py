"""Persistent state for remote mode: encrypted BYO keys, OAuth clients, codes and tokens.

* Model API keys are encrypted at rest with Fernet (AES-128-CBC + HMAC-SHA256) under a key derived
  from ``SCHOLAROCR_REMOTE_SECRET``. They are never logged and never returned by any tool.
* OAuth codes and tokens are stored only as SHA-256 hashes.
* A user id is derived from the API key itself (HMAC), so reconnecting with the same key returns to
  the same sandbox and no ScholarOCR account is needed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_keys (
  user_id TEXT PRIMARY KEY, provider TEXT NOT NULL, ciphertext BLOB, operator INTEGER NOT NULL DEFAULT 0,
  key_hint TEXT, created_at REAL NOT NULL, last_used REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_clients (client_id TEXT PRIMARY KEY, data TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_pending (req_id TEXT PRIMARY KEY, data TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_codes (code_hash TEXT PRIMARY KEY, data TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_tokens (
  token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, grant_id TEXT NOT NULL, user_id TEXT NOT NULL,
  client_id TEXT NOT NULL, scopes TEXT NOT NULL, resource TEXT, expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tokens_user ON oauth_tokens(user_id);
CREATE INDEX IF NOT EXISTS ix_tokens_grant ON oauth_tokens(grant_id);
"""


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def derive_key(secret: str, label: str) -> bytes:
    return hmac.new(secret.encode(), label.encode(), hashlib.sha256).digest()


@dataclass(frozen=True)
class UserKey:
    user_id: str
    provider: str
    api_key: str | None  # None = this user runs on the operator's key (operator mode only)
    operator: bool
    key_hint: str | None


class AuthStore:
    def __init__(self, db_path: Path, secret: str):
        from cryptography.fernet import Fernet

        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(base64.urlsafe_b64encode(derive_key(secret, "scholarocr/key-encryption/v1")))
        self._id_key = derive_key(secret, "scholarocr/user-id/v1")
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
            finally:
                conn.close()

    # ------------------------------------------------------------------ users / keys
    def user_id_for_key(self, provider: str, api_key: str) -> str:
        mac = hmac.new(self._id_key, f"{provider}:{api_key}".encode(), hashlib.sha256).hexdigest()
        return f"u_{mac[:24]}"

    def put_key(self, user_id: str, provider: str, api_key: str | None, *, operator: bool = False) -> None:
        now = time.time()
        ct = self._fernet.encrypt(api_key.encode()) if api_key else None
        hint = f"...{api_key[-4:]}" if api_key and len(api_key) >= 12 else None
        with self._conn() as c:
            c.execute("INSERT INTO user_keys(user_id, provider, ciphertext, operator, key_hint, created_at, last_used) "
                      "VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET provider=excluded.provider, "
                      "ciphertext=excluded.ciphertext, operator=excluded.operator, key_hint=excluded.key_hint, "
                      "last_used=excluded.last_used", (user_id, provider, ct, int(operator), hint, now, now))

    def get_key(self, user_id: str) -> UserKey | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM user_keys WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            return None
        api_key = None
        if row["ciphertext"] is not None:
            from cryptography.fernet import InvalidToken

            try:
                api_key = self._fernet.decrypt(row["ciphertext"]).decode()
            except InvalidToken:  # secret rotated: the stored key is unusable
                return None
        return UserKey(user_id, row["provider"], api_key, bool(row["operator"]), row["key_hint"])

    def touch_user(self, user_id: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE user_keys SET last_used=? WHERE user_id=?", (time.time(), user_id))

    def idle_users(self, older_than: float) -> list[str]:
        with self._conn() as c:
            return [r[0] for r in c.execute("SELECT user_id FROM user_keys WHERE last_used < ?", (older_than,))]

    def delete_user(self, user_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM user_keys WHERE user_id=?", (user_id,))
            c.execute("DELETE FROM oauth_tokens WHERE user_id=?", (user_id,))

    # ------------------------------------------------------------------ oauth clients
    def put_client(self, client_id: str, data: dict[str, Any]) -> None:
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO oauth_clients(client_id, data, created_at) VALUES(?,?,?)",
                      (client_id, json.dumps(data), time.time()))

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT data FROM oauth_clients WHERE client_id=?", (client_id,)).fetchone()
        return json.loads(row[0]) if row else None

    # ------------------------------------------------------------------ pending authorizations
    def put_pending(self, req_id: str, data: dict[str, Any], ttl_s: float) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO oauth_pending(req_id, data, expires_at) VALUES(?,?,?)",
                      (req_id, json.dumps(data), time.time() + ttl_s))

    def get_pending(self, req_id: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT data, expires_at FROM oauth_pending WHERE req_id=?", (req_id,)).fetchone()
        if row is None or row["expires_at"] < time.time():
            return None
        return json.loads(row["data"])

    def pop_pending(self, req_id: str) -> dict[str, Any] | None:
        data = self.get_pending(req_id)
        with self._conn() as c:
            c.execute("DELETE FROM oauth_pending WHERE req_id=?", (req_id,))
        return data

    # ------------------------------------------------------------------ codes
    def put_code(self, code: str, data: dict[str, Any], expires_at: float) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO oauth_codes(code_hash, data, expires_at) VALUES(?,?,?)",
                      (sha256_hex(code), json.dumps(data), expires_at))

    def get_code(self, code: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT data, expires_at FROM oauth_codes WHERE code_hash=?", (sha256_hex(code),)).fetchone()
        if row is None or row["expires_at"] < time.time():
            return None
        return json.loads(row["data"])

    def delete_code(self, code: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM oauth_codes WHERE code_hash=?", (sha256_hex(code),))

    # ------------------------------------------------------------------ tokens
    def put_token(self, token: str, *, kind: str, grant_id: str, user_id: str, client_id: str, scopes: list[str],
                  resource: str | None, expires_at: float) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO oauth_tokens(token_hash, kind, grant_id, user_id, client_id, scopes, resource, "
                      "expires_at) VALUES(?,?,?,?,?,?,?,?)",
                      (sha256_hex(token), kind, grant_id, user_id, client_id, json.dumps(scopes), resource, expires_at))

    def get_token(self, token: str, kind: str) -> dict[str, Any] | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM oauth_tokens WHERE token_hash=? AND kind=?", (sha256_hex(token), kind)).fetchone()
        if row is None or row["expires_at"] < time.time():
            return None
        d = dict(row)
        d["scopes"] = json.loads(d["scopes"])
        return d

    def delete_grant(self, grant_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM oauth_tokens WHERE grant_id=?", (grant_id,))

    def purge_expired(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        n = 0
        with self._conn() as c:
            for table in ("oauth_pending", "oauth_codes", "oauth_tokens"):
                n += c.execute(f"DELETE FROM {table} WHERE expires_at < ?", (now,)).rowcount
        return n
