"""User management with SQLite — admin/user roles."""

import os
import sqlite3
import hashlib
import time
from datetime import datetime
from pathlib import Path

DB_PATH = os.getenv("USER_DB_PATH", "/root/h5-chat/bridge/users.db")


def _get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db():
    """Create tables if not exist."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user',   -- 'admin' or 'user'
            display_name TEXT DEFAULT '',
            created_at REAL NOT NULL,
            last_login REAL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL DEFAULT 0,           -- 0 = never expires
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            agent_id TEXT NOT NULL DEFAULT 'main',
            session_key TEXT NOT NULL UNIQUE,
            title TEXT DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            active INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_user_active ON sessions(user_id, agent_id, active);
    """)
    conn.commit()
    conn.close()


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


# ── User CRUD ──────────────────────────────────────────────────

def create_user(username: str, password: str, role: str = "user", display_name: str = "") -> dict | None:
    """Create a new user. Returns user dict or None if username exists."""
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, display_name, created_at) VALUES (?, ?, ?, ?, ?)",
            (username, _hash_password(password), role, display_name or username, time.time()),
        )
        conn.commit()
        return get_user_by_username(username)
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()


def get_user_by_username(username: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_users() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute("SELECT id, username, role, display_name, created_at, last_login, enabled FROM users").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_user(user_id: int) -> bool:
    conn = _get_conn()
    conn.execute("DELETE FROM tokens WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return True


def update_user(user_id: int, **kwargs) -> bool:
    """Update user fields. Allowed: role, display_name, enabled, password."""
    allowed = {"role", "display_name", "enabled"}
    sets = []
    vals = []
    for k, v in kwargs.items():
        if k == "password":
            sets.append("password_hash = ?")
            vals.append(_hash_password(v))
        elif k in allowed:
            sets.append(f"{k} = ?")
            vals.append(v)
    if not sets:
        return False
    vals.append(user_id)
    conn = _get_conn()
    conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    conn.close()
    return True


# ── Auth / Token ───────────────────────────────────────────────

def authenticate(username: str, password: str) -> str | None:
    """Verify username+password, return a new API token. None if auth fails."""
    user = get_user_by_username(username)
    if not user or not user["enabled"]:
        return None
    if user["password_hash"] != _hash_password(password):
        return None

    # Generate token
    import secrets
    token = secrets.token_urlsafe(32)

    conn = _get_conn()
    conn.execute(
        "INSERT INTO tokens (user_id, token, created_at) VALUES (?, ?, ?)",
        (user["id"], token, time.time()),
    )
    conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (time.time(), user["id"]))
    conn.commit()
    conn.close()
    return token


def verify_token(token: str) -> dict | None:
    """Verify API token, return user dict. None if invalid."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT u.* FROM users u JOIN tokens t ON u.id = t.user_id WHERE t.token = ? AND u.enabled = 1",
        (token,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def revoke_token(token: str) -> bool:
    conn = _get_conn()
    conn.execute("DELETE FROM tokens WHERE token = ?", (token,))
    conn.commit()
    conn.close()
    return True


def logout_all(user_id: int) -> bool:
    """Revoke all tokens for a user."""
    conn = _get_conn()
    conn.execute("DELETE FROM tokens WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    return True


# ── Init ───────────────────────────────────────────────────────

def _make_session_key(username: str, agent_id: str) -> str:
    """Generate a human-readable session key."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"agent:{agent_id}:h5-{username}-{ts}"


def _get_username(user_id: int) -> str:
    """Look up username by user_id."""
    conn = _get_conn()
    row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return row["username"] if row else str(user_id)


def _migrate_legacy_sessions():
    """One-time migration: convert old PRIMARY KEY sessions to new auto-increment schema.
    If the old table exists without 'id' column, recreate it.
    """
    conn = _get_conn()
    try:
        # Check if 'id' column exists
        cols = conn.execute("PRAGMA table_info(sessions)").fetchall()
        col_names = [c[1] for c in cols]
        if 'id' not in col_names:
            # Old schema — migrate data
            rows = conn.execute("SELECT user_id, agent_id, session_key, updated_at FROM sessions").fetchall()
            conn.execute("DROP TABLE sessions")
            conn.execute("""
                CREATE TABLE sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    agent_id TEXT NOT NULL DEFAULT 'main',
                    session_key TEXT NOT NULL UNIQUE,
                    title TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_user_active ON sessions(user_id, agent_id, active);
            """)
            for r in rows:
                conn.execute(
                    "INSERT INTO sessions (user_id, agent_id, session_key, title, created_at, updated_at, active) VALUES (?, ?, ?, '', ?, ?, 1)",
                    (r['user_id'], r['agent_id'], r['session_key'], r['updated_at'], r['updated_at']),
                )
            conn.commit()
            print("[users] Migrated legacy sessions table")
    except Exception as e:
        print(f"[users] Session migration check: {e}")
    finally:
        conn.close()


def get_or_create_session(user_id: int, agent_id: str = "main") -> str:
    """Get active session key for user, or create one if none active."""
    _migrate_legacy_sessions()
    conn = _get_conn()
    row = conn.execute(
        "SELECT session_key FROM sessions WHERE user_id = ? AND agent_id = ? AND active = 1",
        (user_id, agent_id),
    ).fetchone()
    if row:
        conn.close()
        return row["session_key"]
    # No active session — find any existing one and activate it
    row = conn.execute(
        "SELECT session_key FROM sessions WHERE user_id = ? AND agent_id = ? ORDER BY updated_at DESC LIMIT 1",
        (user_id, agent_id),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE sessions SET active = 1, updated_at = ? WHERE user_id = ? AND agent_id = ? AND session_key = ?",
            (time.time(), user_id, agent_id, row["session_key"]),
        )
        conn.commit()
        conn.close()
        return row["session_key"]
    # No sessions at all — create new
    username = _get_username(user_id)
    sk = _make_session_key(username, agent_id)
    conn.execute(
        "INSERT INTO sessions (user_id, agent_id, session_key, title, created_at, updated_at, active) VALUES (?, ?, ?, '', ?, ?, 1)",
        (user_id, agent_id, sk, time.time(), time.time()),
    )
    conn.commit()
    conn.close()
    return sk


def new_session(user_id: int, agent_id: str = "main", title: str = "") -> str:
    """Create a new session and set it as active. Returns new session_key."""
    _migrate_legacy_sessions()
    username = _get_username(user_id)
    sk = _make_session_key(username, agent_id)
    now = time.time()
    conn = _get_conn()
    # Deactivate all existing sessions for this user+agent
    conn.execute(
        "UPDATE sessions SET active = 0 WHERE user_id = ? AND agent_id = ?",
        (user_id, agent_id),
    )
    # Insert new active session
    conn.execute(
        "INSERT INTO sessions (user_id, agent_id, session_key, title, created_at, updated_at, active) VALUES (?, ?, ?, ?, ?, ?, 1)",
        (user_id, agent_id, sk, title or "", now, now),
    )
    conn.commit()
    conn.close()
    return sk


def list_sessions(user_id: int, agent_id: str = "main") -> list[dict]:
    """List all sessions for user, ordered by created_at desc (creation time is fixed)."""
    _migrate_legacy_sessions()
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, session_key, title, created_at, updated_at, active FROM sessions WHERE user_id = ? AND agent_id = ? ORDER BY created_at DESC",
        (user_id, agent_id),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def switch_session(user_id: int, agent_id: str, session_key: str) -> str | None:
    """Switch active session. Returns session_key on success, None if not found."""
    _migrate_legacy_sessions()
    conn = _get_conn()
    # Verify session belongs to user
    row = conn.execute(
        "SELECT id FROM sessions WHERE user_id = ? AND agent_id = ? AND session_key = ?",
        (user_id, agent_id, session_key),
    ).fetchone()
    if not row:
        conn.close()
        return None
    # Deactivate all, activate target
    conn.execute(
        "UPDATE sessions SET active = 0 WHERE user_id = ? AND agent_id = ?",
        (user_id, agent_id),
    )
    conn.execute(
        "UPDATE sessions SET active = 1, updated_at = ? WHERE user_id = ? AND agent_id = ? AND session_key = ?",
        (time.time(), user_id, agent_id, session_key),
    )
    conn.commit()
    conn.close()
    return session_key


def delete_session(user_id: int, agent_id: str, session_key: str) -> bool:
    """Delete a session. Cannot delete the active session (use switch first)."""
    _migrate_legacy_sessions()
    conn = _get_conn()
    row = conn.execute(
        "SELECT active FROM sessions WHERE user_id = ? AND agent_id = ? AND session_key = ?",
        (user_id, agent_id, session_key),
    ).fetchone()
    if not row:
        conn.close()
        return False
    if row["active"]:
        conn.close()
        return False  # Cannot delete active session
    conn.execute(
        "DELETE FROM sessions WHERE user_id = ? AND agent_id = ? AND session_key = ?",
        (user_id, agent_id, session_key),
    )
    conn.commit()
    conn.close()
    return True


def rename_session(user_id: int, agent_id: str, session_key: str, title: str) -> bool:
    """Rename a session title."""
    conn = _get_conn()
    conn.execute(
        "UPDATE sessions SET title = ?, updated_at = ? WHERE user_id = ? AND agent_id = ? AND session_key = ?",
        (title, time.time(), user_id, agent_id, session_key),
    )
    conn.commit()
    conn.close()
    return True


def touch_session(user_id: int, agent_id: str, session_key: str) -> None:
    """Update session's updated_at (called when a message is sent)."""
    conn = _get_conn()
    conn.execute(
        "UPDATE sessions SET updated_at = ? WHERE user_id = ? AND agent_id = ? AND session_key = ?",
        (time.time(), user_id, agent_id, session_key),
    )
    conn.commit()
    conn.close()


def bootstrap():
    """Create tables and ensure default admin exists."""
    init_db()
    admin = get_user_by_username("admin")
    if not admin:
        create_user("admin", "admin123", role="admin", display_name="管理员")
        print("[users] Default admin created: admin / admin123")
