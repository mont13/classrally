"""SQLite database layer for ClassRally.

Provides user accounts, classes/groups, and game history persistence.
Uses only stdlib (sqlite3). Thread-safe via WAL mode + one connection per call.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import random
import re
import secrets
import sqlite3
import string
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# Database location — overridable via QUIZ_DATA_DIR env (e.g. isolated smoke
# test runs) or set_db_path() for tests
_DB_DIR = Path(os.environ["QUIZ_DATA_DIR"]).resolve() if os.environ.get("QUIZ_DATA_DIR") else Path(__file__).resolve().parent / "data"
_DB_PATH: Path = _DB_DIR / "classrally.db"

# Session TTL: 8 hours
SESSION_TTL = 28800

# Teacher registration code (set from CLI / env)
TEACHER_CODE: str = ""

# Lock for migrations (one-time)
_migration_lock = threading.Lock()
_migrated = False


def set_db_path(path: str | Path) -> None:
    """Override DB path (for testing)."""
    global _DB_PATH, _DB_DIR
    _DB_PATH = Path(path)
    _DB_DIR = _DB_PATH.parent


# Password hashing: salted PBKDF2-HMAC-SHA256. Older databases store unsalted
# SHA-256 hex digests; those still verify and are upgraded on the next login.
PBKDF2_ITERATIONS = 600_000
_PBKDF2_PREFIX = "pbkdf2_sha256"

# Minimum password length by role (teachers manage classes and question banks).
MIN_PASSWORD_LEN = {"teacher": 8, "student": 4}


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"{_PBKDF2_PREFIX}${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    if stored.startswith(_PBKDF2_PREFIX + "$"):
        try:
            _, iterations, salt_hex, digest_hex = stored.split("$")
            digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                         bytes.fromhex(salt_hex), int(iterations))
        except ValueError:
            return False
        return hmac.compare_digest(digest.hex(), digest_hex)
    legacy = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return hmac.compare_digest(legacy, stored)


_DUMMY_HASH: str | None = None


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None or _needs_rehash(_DUMMY_HASH):
        _DUMMY_HASH = _hash_password(secrets.token_hex(8))
    return _DUMMY_HASH


def _email_taken(conn: sqlite3.Connection, email: str, except_user: int | None = None) -> bool:
    """True when another account already has this address CONFIRMED."""
    row = conn.execute(
        "SELECT 1 FROM users WHERE email = ? COLLATE NOCASE AND email_verified_at IS NOT NULL "
        "AND id IS NOT ?", (email, except_user),
    ).fetchone()
    return row is not None


def _needs_rehash(stored: str) -> bool:
    return not stored.startswith(f"{_PBKDF2_PREFIX}${PBKDF2_ITERATIONS}$")


def _check_password_policy(password: str, role: str) -> None:
    minimum = MIN_PASSWORD_LEN.get(role, MIN_PASSWORD_LEN["student"])
    if not password or len(password) < minimum:
        raise ValueError(f"Heslo musi mit alespon {minimum} znaku.")


# Plain ASCII addresses only (internationalised domains in punycode): nothing
# that could be read as a second address, a display name or a header break.
_LOCAL_ATOM = r"[a-z0-9!#$%&'*+/=?^_`{|}~-]+"
_EMAIL_RE = re.compile(
    rf"^(?=[^@]{{1,64}}@){_LOCAL_ATOM}(?:\.{_LOCAL_ATOM})*"
    r"@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)


def normalize_email(email: str) -> str:
    """Return a lower-cased e-mail address or raise ValueError."""
    email = (email or "").strip().lower()
    if len(email) > 254 or not _EMAIL_RE.match(email):
        raise ValueError("Neplatna e-mailova adresa.")
    return email


def get_conn() -> sqlite3.Connection:
    """Get a new connection (one per request, thread-safe)."""
    _DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create tables if they don't exist. Called once at server startup."""
    conn = get_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                nickname      TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL DEFAULT 'student',
                total_score   INTEGER NOT NULL DEFAULT 0,
                games_played  INTEGER NOT NULL DEFAULT 0,
                avatar_id     INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token      TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS classes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                join_code  TEXT NOT NULL UNIQUE,
                teacher_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS class_members (
                class_id  INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
                user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                joined_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (class_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS games (
                id                   TEXT PRIMARY KEY,
                class_id             INTEGER REFERENCES classes(id),
                bank_name            TEXT,
                total_questions      INTEGER NOT NULL,
                question_duration_sec INTEGER NOT NULL,
                player_count         INTEGER NOT NULL,
                played_at            TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS game_players (
                game_id     TEXT NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                user_id     INTEGER REFERENCES users(id),
                player_name TEXT NOT NULL,
                score       INTEGER NOT NULL DEFAULT 0,
                rank        INTEGER NOT NULL,
                PRIMARY KEY (game_id, player_name)
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_class_members_user ON class_members(user_id);
            CREATE INDEX IF NOT EXISTS idx_game_players_user ON game_players(user_id);
            CREATE INDEX IF NOT EXISTS idx_games_class ON games(class_id);

            CREATE TABLE IF NOT EXISTS selfpaced_sessions (
                id           TEXT PRIMARY KEY,
                bank_name    TEXT NOT NULL,
                questions    TEXT NOT NULL,
                created_at   REAL NOT NULL,
                expires_at   REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS selfpaced_results (
                session_id   TEXT NOT NULL REFERENCES selfpaced_sessions(id) ON DELETE CASCADE,
                player_name  TEXT NOT NULL,
                player_id    TEXT NOT NULL,
                user_id      INTEGER REFERENCES users(id),
                score        INTEGER NOT NULL DEFAULT 0,
                total        INTEGER NOT NULL DEFAULT 0,
                answers      TEXT NOT NULL DEFAULT '[]',
                finished_at  REAL,
                PRIMARY KEY (session_id, player_id)
            );

            CREATE INDEX IF NOT EXISTS idx_selfpaced_results_session ON selfpaced_results(session_id);
            CREATE INDEX IF NOT EXISTS idx_selfpaced_results_user ON selfpaced_results(user_id);
        """)
        conn.commit()

        # Migration: add avatar_id column if missing (for existing databases)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "avatar_id" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN avatar_id INTEGER NOT NULL DEFAULT 1")
            conn.commit()

        # Migration: data ownership (several teachers on one server) and the
        # homework player secret (only its hash is stored)
        game_cols = [row[1] for row in conn.execute("PRAGMA table_info(games)").fetchall()]
        if "owner_id" not in game_cols:
            conn.execute("ALTER TABLE games ADD COLUMN owner_id INTEGER")
        sp_cols = [row[1] for row in conn.execute("PRAGMA table_info(selfpaced_sessions)").fetchall()]
        if "owner_id" not in sp_cols:
            conn.execute("ALTER TABLE selfpaced_sessions ADD COLUMN owner_id INTEGER")
        spr_cols = [row[1] for row in conn.execute("PRAGMA table_info(selfpaced_results)").fetchall()]
        if "secret_hash" not in spr_cols:
            conn.execute("ALTER TABLE selfpaced_results ADD COLUMN secret_hash TEXT")

        # Migration: optional e-mail (password reset, invitations)
        if "email" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
        if "email_verified_at" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN email_verified_at REAL")
        conn.executescript("""
            -- Only a CONFIRMED address is unique: an unconfirmed one may sit on
            -- several accounts, so nobody can block someone else's address and
            -- setting an address never reveals whether it is already in use.
            DROP INDEX IF EXISTS idx_users_email;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_verified
                ON users(email COLLATE NOCASE) WHERE email_verified_at IS NOT NULL;

            -- One-time links sent by e-mail; only the SHA-256 of the token is stored.
            -- purpose: 'reset' (password), 'verify' (e-mail), 'invite' (new teacher)
            CREATE TABLE IF NOT EXISTS user_tokens (
                token_hash  TEXT PRIMARY KEY,
                purpose     TEXT NOT NULL,
                user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
                email       TEXT,
                created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at  REAL NOT NULL,
                expires_at  REAL NOT NULL,
                used_at     REAL
            );
            CREATE INDEX IF NOT EXISTS idx_user_tokens_user ON user_tokens(user_id);

            -- Exams (pisemky): results are written as students answer, so a
            -- restart loses nothing. The questions are stored as they were in
            -- the exam: later edits of the bank never change old results.
            CREATE TABLE IF NOT EXISTS exam_sessions (
                id          TEXT PRIMARY KEY,
                owner_id    INTEGER,
                bank_name   TEXT,
                questions   TEXT NOT NULL,
                config      TEXT NOT NULL,
                opened_at   REAL NOT NULL,
                ended_at    REAL,
                interrupted INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS exam_results (
                session_id     TEXT NOT NULL REFERENCES exam_sessions(id) ON DELETE CASCADE,
                player_id      TEXT NOT NULL,
                name           TEXT NOT NULL,
                answers        TEXT NOT NULL DEFAULT '{}',
                score          INTEGER,
                grade          INTEGER,
                submitted_at   REAL,
                auto_submitted INTEGER NOT NULL DEFAULT 0,
                blur_count     INTEGER NOT NULL DEFAULT 0,
                away_ms        REAL NOT NULL DEFAULT 0,
                updated_at     REAL NOT NULL,
                PRIMARY KEY (session_id, player_id)
            );
            CREATE INDEX IF NOT EXISTS idx_exam_sessions_owner ON exam_sessions(owner_id);
        """)
        conn.commit()
    finally:
        conn.close()


# --- User CRUD ---

def create_user(nickname: str, password: str, role: str = "student",
                avatar_id: int | None = None, email: str | None = None,
                email_verified: bool = False) -> dict:
    """Register a new user. Returns {id, nickname, role, avatar_id, email} or raises ValueError."""
    nickname = nickname.strip()
    if not nickname or len(nickname) > 24:
        raise ValueError("Prezdivka musi mit 1-24 znaku.")
    if role not in ("student", "teacher"):
        raise ValueError("Role musi byt 'student' nebo 'teacher'.")
    _check_password_policy(password, role)
    email = normalize_email(email) if email else None
    verified_at = time.time() if (email and email_verified) else None

    if avatar_id is None:
        avatar_id = random.randint(1, 20)
    else:
        avatar_id = max(1, min(20, int(avatar_id)))

    pw_hash = _hash_password(password)
    conn = get_conn()
    try:
        # Only reachable with a confirmed address (teacher invitation link).
        if verified_at and _email_taken(conn, email):
            raise ValueError("E-mail uz pouziva jiny ucet.")
        try:
            cur = conn.execute(
                "INSERT INTO users (nickname, password_hash, role, avatar_id, email, email_verified_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (nickname, pw_hash, role, avatar_id, email, verified_at),
            )
            conn.commit()
            return {"id": cur.lastrowid, "nickname": nickname, "role": role,
                    "avatar_id": avatar_id, "email": email,
                    "email_verified": verified_at is not None}
        except sqlite3.IntegrityError:
            raise ValueError(f"Prezdivka '{nickname}' je jiz obsazena.")
    finally:
        conn.close()


def authenticate_user(nickname: str, password: str) -> dict | None:
    """Check credentials. Returns user dict or None.

    A password stored in the old unsalted format is re-hashed on success.
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, nickname, role, total_score, games_played, avatar_id, password_hash "
            "FROM users WHERE nickname = ? COLLATE NOCASE",
            (nickname.strip(),),
        ).fetchone()
        if not row:
            _verify_password(password, _dummy_hash())  # same cost as a real check
            return None
        if not _verify_password(password, row["password_hash"]):
            return None
        if _needs_rehash(row["password_hash"]):
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (_hash_password(password), row["id"]))
            conn.commit()
        user = dict(row)
        del user["password_hash"]
        return user
    finally:
        conn.close()


def get_user(user_id: int) -> dict | None:
    """Get user by ID."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, nickname, role, total_score, games_played, avatar_id, created_at, "
            "email, email_verified_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        user = dict(row)
        user["email_verified"] = user.pop("email_verified_at") is not None
        return user
    finally:
        conn.close()


def get_user_profile(user_id: int) -> dict | None:
    """Get user profile with class memberships and recent games."""
    user = get_user(user_id)
    if not user:
        return None

    conn = get_conn()
    try:
        # Classes
        classes = conn.execute("""
            SELECT c.id, c.name, c.join_code, c.teacher_id,
                   (SELECT COUNT(*) FROM class_members WHERE class_id = c.id) as member_count
            FROM classes c
            LEFT JOIN class_members cm ON cm.class_id = c.id AND cm.user_id = ?
            WHERE c.teacher_id = ? OR cm.user_id = ?
        """, (user_id, user_id, user_id)).fetchall()

        # Recent games (last 20)
        games = conn.execute("""
            SELECT gp.game_id, gp.score, gp.rank, g.played_at, g.total_questions,
                   g.player_count, g.bank_name
            FROM game_players gp
            JOIN games g ON g.id = gp.game_id
            WHERE gp.user_id = ?
            ORDER BY g.played_at DESC
            LIMIT 20
        """, (user_id,)).fetchall()

        user["classes"] = [dict(c) for c in classes]
        user["recent_games"] = [dict(g) for g in games]
        return user
    finally:
        conn.close()


def update_avatar(user_id: int, avatar_id: int) -> bool:
    """Update user's avatar. Returns True if updated."""
    avatar_id = max(1, min(20, int(avatar_id)))
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE users SET avatar_id = ? WHERE id = ?",
            (avatar_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# --- Sessions ---

def _session_key(token: str) -> str:
    """Sessions are stored as SHA-256 of the token (a DB leak is not a login)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id: int) -> str:
    """Create a new session token for a user."""
    token = secrets.token_hex(24)
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
            (_session_key(token), user_id, time.time()),
        )
        conn.commit()
        return token
    finally:
        conn.close()


def validate_session(token: str | None) -> int | None:
    """Validate session token. Returns user_id or None."""
    if not token:
        return None
    conn = get_conn()
    try:
        key = _session_key(token)
        row = conn.execute(
            "SELECT user_id, created_at FROM sessions WHERE token = ?",
            (key,),
        ).fetchone()
        if not row:
            return None
        if time.time() - row["created_at"] > SESSION_TTL:
            conn.execute("DELETE FROM sessions WHERE token = ?", (key,))
            conn.commit()
            return None
        return row["user_id"]
    finally:
        conn.close()


def delete_session(token: str) -> None:
    """Invalidate a session token."""
    conn = get_conn()
    try:
        conn.execute("DELETE FROM sessions WHERE token = ?", (_session_key(token),))
        conn.commit()
    finally:
        conn.close()


def cleanup_expired_sessions() -> None:
    """Remove expired sessions."""
    conn = get_conn()
    try:
        conn.execute(
            "DELETE FROM sessions WHERE created_at < ?",
            (time.time() - SESSION_TTL,),
        )
        conn.commit()
    finally:
        conn.close()


# --- Classes ---

def _generate_join_code() -> str:
    """Generate a 6-character alphanumeric join code."""
    chars = string.ascii_uppercase + string.digits
    # Exclude ambiguous: 0/O, 1/I/L
    chars = chars.replace("O", "").replace("0", "").replace("I", "").replace("L", "").replace("1", "")
    return "".join(random.choices(chars, k=6))


def create_class(name: str, teacher_id: int) -> dict:
    """Create a new class. Returns {id, name, join_code}."""
    name = name.strip()
    if not name or len(name) > 100:
        raise ValueError("Nazev tridy musi mit 1-100 znaku.")

    # Verify teacher role
    user = get_user(teacher_id)
    if not user or user["role"] != "teacher":
        raise ValueError("Pouze ucitel muze vytvaret tridy.")

    conn = get_conn()
    try:
        # Try up to 10 times to get a unique code
        for _ in range(10):
            code = _generate_join_code()
            try:
                cur = conn.execute(
                    "INSERT INTO classes (name, join_code, teacher_id) VALUES (?, ?, ?)",
                    (name, code, teacher_id),
                )
                conn.commit()
                return {"id": cur.lastrowid, "name": name, "join_code": code}
            except sqlite3.IntegrityError:
                continue
        raise ValueError("Nepodarilo se vygenerovat unikatni kod tridy.")
    finally:
        conn.close()


def join_class(join_code: str, user_id: int) -> dict:
    """Student joins a class by code. Returns class info."""
    join_code = join_code.strip().upper()
    conn = get_conn()
    try:
        cls = conn.execute(
            "SELECT id, name, join_code, teacher_id FROM classes WHERE join_code = ?",
            (join_code,),
        ).fetchone()
        if not cls:
            raise ValueError("Trida s timto kodem neexistuje.")

        try:
            conn.execute(
                "INSERT INTO class_members (class_id, user_id) VALUES (?, ?)",
                (cls["id"], user_id),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass  # Already a member — OK

        return dict(cls)
    finally:
        conn.close()


def list_user_classes(user_id: int) -> list[dict]:
    """List classes for a user (teacher sees owned, student sees joined)."""
    conn = get_conn()
    try:
        user = get_user(user_id)
        if not user:
            return []

        rows = conn.execute("""
            SELECT DISTINCT c.id, c.name, c.join_code, c.teacher_id, c.created_at,
                   (SELECT COUNT(*) FROM class_members WHERE class_id = c.id) as member_count,
                   (SELECT nickname FROM users WHERE id = c.teacher_id) as teacher_name
            FROM classes c
            LEFT JOIN class_members cm ON cm.class_id = c.id
            WHERE c.teacher_id = ? OR cm.user_id = ?
            ORDER BY c.created_at DESC
        """, (user_id, user_id)).fetchall()

        return [dict(r) for r in rows]
    finally:
        conn.close()


def can_view_class(class_id: int, user_id: int, teacher_only: bool = False) -> bool:
    """The class teacher and (unless teacher_only) the class members may see a class."""
    conn = get_conn()
    try:
        if teacher_only:
            row = conn.execute("SELECT 1 FROM classes WHERE id = ? AND teacher_id = ?",
                               (class_id, user_id)).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM classes c WHERE c.id = ? AND (c.teacher_id = ? OR EXISTS "
                "(SELECT 1 FROM class_members m WHERE m.class_id = c.id AND m.user_id = ?))",
                (class_id, user_id, user_id),
            ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_class_members(class_id: int) -> list[dict]:
    """Get class members with their stats."""
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT u.id, u.nickname, u.total_score, u.games_played, cm.joined_at
            FROM class_members cm
            JOIN users u ON u.id = cm.user_id
            WHERE cm.class_id = ?
            ORDER BY u.nickname COLLATE NOCASE
        """, (class_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_class_history(class_id: int, limit: int = 50) -> list[dict]:
    """Get game history for a class."""
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT g.id, g.bank_name, g.total_questions, g.player_count, g.played_at
            FROM games g
            WHERE g.class_id = ?
            ORDER BY g.played_at DESC
            LIMIT ?
        """, (class_id, limit)).fetchall()

        result = []
        for g in rows:
            game = dict(g)
            players = conn.execute("""
                SELECT player_name, score, rank FROM game_players
                WHERE game_id = ? ORDER BY rank
            """, (g["id"],)).fetchall()
            game["players"] = [dict(p) for p in players]
            result.append(game)
        return result
    finally:
        conn.close()


def get_class_progress(class_id: int) -> list[dict]:
    """Get per-student score progression over time for a class."""
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT u.id as user_id, u.nickname,
                   gp.score, gp.rank, g.played_at, g.id as game_id
            FROM class_members cm
            JOIN users u ON u.id = cm.user_id
            LEFT JOIN game_players gp ON gp.user_id = u.id
            LEFT JOIN games g ON g.id = gp.game_id AND g.class_id = ?
            WHERE cm.class_id = ?
            ORDER BY u.nickname, g.played_at
        """, (class_id, class_id)).fetchall()

        # Group by student
        students: dict[int, dict] = {}
        for r in rows:
            uid = r["user_id"]
            if uid not in students:
                students[uid] = {
                    "user_id": uid,
                    "nickname": r["nickname"],
                    "games": [],
                }
            if r["game_id"]:
                students[uid]["games"].append({
                    "game_id": r["game_id"],
                    "score": r["score"],
                    "rank": r["rank"],
                    "played_at": r["played_at"],
                })

        return list(students.values())
    finally:
        conn.close()


def delete_class(class_id: int, teacher_id: int) -> bool:
    """Delete a class (only owner can delete)."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM classes WHERE id = ? AND teacher_id = ?",
            (class_id, teacher_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def teacher_exists() -> bool:
    """Check if at least one teacher account exists in the DB."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM users WHERE role = 'teacher' LIMIT 1"
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def reset_user_password(user_id: int, new_password: str, teacher_id: int) -> bool:
    """Teacher resets a student's password. Returns True if updated.

    Only allows resetting students who are in one of the teacher's classes.
    """
    _check_password_policy(new_password, "student")

    conn = get_conn()
    try:
        # Verify the target user is a student in one of teacher's classes
        row = conn.execute("""
            SELECT u.id FROM users u
            JOIN class_members cm ON cm.user_id = u.id
            JOIN classes c ON c.id = cm.class_id
            WHERE u.id = ? AND u.role = 'student' AND c.teacher_id = ?
            LIMIT 1
        """, (user_id, teacher_id)).fetchone()
        if not row:
            raise ValueError("Student nenalezen ve vasich tridach.")

        pw_hash = _hash_password(new_password)
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                      (pw_hash, user_id))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def kick_class_member(class_id: int, user_id: int, teacher_id: int) -> bool:
    """Remove a student from a class. Only the class owner can do this."""
    conn = get_conn()
    try:
        # Verify ownership
        cls = conn.execute(
            "SELECT id FROM classes WHERE id = ? AND teacher_id = ?",
            (class_id, teacher_id),
        ).fetchone()
        if not cls:
            raise ValueError("Trida nenalezena nebo nejste vlastnik.")

        cur = conn.execute(
            "DELETE FROM class_members WHERE class_id = ? AND user_id = ?",
            (class_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_user(user_id: int, teacher_id: int) -> bool:
    """Teacher deletes a student account. Only for students in teacher's classes."""
    conn = get_conn()
    try:
        # Verify the target is a student in one of teacher's classes
        row = conn.execute("""
            SELECT u.id FROM users u
            JOIN class_members cm ON cm.user_id = u.id
            JOIN classes c ON c.id = cm.class_id
            WHERE u.id = ? AND u.role = 'student' AND c.teacher_id = ?
            LIMIT 1
        """, (user_id, teacher_id)).fetchone()
        if not row:
            raise ValueError("Student nenalezen ve vasich tridach.")

        conn.execute("DELETE FROM users WHERE id = ? AND role = 'student'",
                      (user_id,))
        conn.commit()
        return True
    finally:
        conn.close()


class WrongPassword(ValueError):
    """The current password given to confirm a change is wrong."""


def change_password(user_id: int, old_password: str, new_password: str,
                    keep_session: str | None = None) -> dict:
    """User changes their own password. Verifies old password first.

    Ends all other sessions and voids pending reset links. Returns the user.
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT password_hash, role FROM users WHERE id = ?", (user_id,),
        ).fetchone()
        if not row or not _verify_password(old_password, row["password_hash"]):
            raise WrongPassword("Soucasne heslo neni spravne.")
        _check_password_policy(new_password, row["role"])
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (_hash_password(new_password), user_id))
        conn.execute("DELETE FROM sessions WHERE user_id = ? AND token IS NOT ?",
                     (user_id, _session_key(keep_session) if keep_session else None))
        conn.execute("UPDATE user_tokens SET used_at = ? WHERE user_id = ? AND purpose = 'reset' "
                     "AND used_at IS NULL", (time.time(), user_id))
        conn.commit()
    finally:
        conn.close()
    return get_user(user_id)


# With several teachers on one server, no teacher may take over another one.
_TEACHER_PROTECTED = ("Ucitelsky ucet muze zmenit jen jeho majitel, nebo spravce serveru "
                      "(python3 server.py --set-password / --set-role).")


def set_user_role(target_user_id: int, new_role: str, acting_teacher_id: int) -> bool:
    """Teacher changes a user's role. Cannot change own role."""
    if new_role not in ("student", "teacher"):
        raise ValueError("Role musi byt 'student' nebo 'teacher'.")
    if target_user_id == acting_teacher_id:
        raise ValueError("Nemuzes zmenit svoji vlastni roli.")
    conn = get_conn()
    try:
        row = conn.execute("SELECT id, role FROM users WHERE id = ?",
                           (target_user_id,)).fetchone()
        if not row:
            raise ValueError("Uzivatel nenalezen.")
        if row["role"] == "teacher":
            raise ValueError(_TEACHER_PROTECTED)
        conn.execute("UPDATE users SET role = ? WHERE id = ?",
                     (new_role, target_user_id))
        conn.commit()
        return True
    finally:
        conn.close()


def list_all_users() -> list[dict]:
    """List all users (for teacher admin view)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, nickname, role, total_score, games_played, avatar_id "
            "FROM users ORDER BY role DESC, nickname COLLATE NOCASE"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_users_for_teacher(teacher_id: int) -> list[dict]:
    """The teacher and the students in their classes (multi-teacher servers)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, nickname, role, total_score, games_played, avatar_id FROM users u "
            "WHERE u.id = ? OR EXISTS (SELECT 1 FROM class_members m JOIN classes c "
            "ON c.id = m.class_id WHERE m.user_id = u.id AND c.teacher_id = ?) "
            "ORDER BY role DESC, nickname COLLATE NOCASE",
            (teacher_id, teacher_id),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def is_only_student_of(student_id: int, teacher_id: int) -> bool:
    """The account sits in this teacher's classes and in no class of another teacher.

    On multi-teacher servers only then may the teacher reset its password, delete
    it or change its role — a student shared with a colleague is not theirs alone.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT c.teacher_id FROM class_members m JOIN classes c ON c.id = m.class_id "
            "WHERE m.user_id = ?", (student_id,),
        ).fetchall()
        return {r["teacher_id"] for r in rows} == {teacher_id}
    finally:
        conn.close()


def is_student_of(student_id: int, teacher_id: int) -> bool:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT 1 FROM class_members m JOIN classes c ON c.id = m.class_id "
            "WHERE m.user_id = ? AND c.teacher_id = ?", (student_id, teacher_id),
        ).fetchone() is not None
    finally:
        conn.close()


def teacher_reset_password(target_user_id: int, new_password: str,
                           teacher_id: int) -> bool:
    """Teacher resets any user's password (global, not class-restricted)."""
    if target_user_id == teacher_id:
        raise ValueError("Pouzijte zmenu vlastniho hesla.")
    conn = get_conn()
    try:
        row = conn.execute("SELECT id, role FROM users WHERE id = ?",
                           (target_user_id,)).fetchone()
        if not row:
            raise ValueError("Uzivatel nenalezen.")
        if row["role"] == "teacher":
            raise ValueError(_TEACHER_PROTECTED)
        _check_password_policy(new_password, row["role"])
        pw_hash = _hash_password(new_password)
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (pw_hash, target_user_id))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (target_user_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def delete_user_by_teacher(target_user_id: int, teacher_id: int) -> bool:
    """Teacher deletes any user account (except themselves)."""
    if target_user_id == teacher_id:
        raise ValueError("Nemuzes smazat svuj vlastni ucet.")
    conn = get_conn()
    try:
        row = conn.execute("SELECT id, role FROM users WHERE id = ?",
                           (target_user_id,)).fetchone()
        if not row:
            raise ValueError("Uzivatel nenalezen.")
        if row["role"] == "teacher":
            raise ValueError(_TEACHER_PROTECTED)
        # Remove from all classes first
        conn.execute("DELETE FROM class_members WHERE user_id = ?",
                     (target_user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (target_user_id,))
        conn.commit()
        return True
    finally:
        conn.close()


# --- E-mail and one-time tokens (password reset, e-mail verification, invites) ---

TOKEN_TTL = {"reset": 3600, "verify": 2 * 86400, "invite": 7 * 86400}


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_user_token(purpose: str, user_id: int | None = None, email: str | None = None,
                      created_by: int | None = None) -> str:
    """Create a one-time token and return it (only its hash is stored)."""
    if purpose not in TOKEN_TTL:
        raise ValueError(f"unknown token purpose: {purpose}")
    raw = secrets.token_urlsafe(32)
    now = time.time()
    conn = get_conn()
    try:
        conn.execute("DELETE FROM user_tokens WHERE expires_at < ?", (now - 86400,))
        conn.execute(
            "INSERT INTO user_tokens (token_hash, purpose, user_id, email, created_by,"
            " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_token_hash(raw), purpose, user_id, email, created_by, now, now + TOKEN_TTL[purpose]),
        )
        conn.commit()
        return raw
    finally:
        conn.close()


def peek_user_token(raw: str, purpose: str) -> dict | None:
    """Return a valid (unused, unexpired) token row without consuming it."""
    if not raw:
        return None
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT purpose, user_id, email, created_by, expires_at FROM user_tokens "
            "WHERE token_hash = ? AND purpose = ? AND used_at IS NULL AND expires_at > ?",
            (_token_hash(raw), purpose, time.time()),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _consume_token(conn: sqlite3.Connection, raw: str, purpose: str) -> dict | None:
    """Atomically mark a valid token as used; return its row or None."""
    if not raw:
        return None
    now = time.time()
    cur = conn.execute(
        "UPDATE user_tokens SET used_at = ? WHERE token_hash = ? AND purpose = ? "
        "AND used_at IS NULL AND expires_at > ?",
        (now, _token_hash(raw), purpose, now),
    )
    if cur.rowcount != 1:
        return None
    row = conn.execute(
        "SELECT purpose, user_id, email, created_by FROM user_tokens WHERE token_hash = ?",
        (_token_hash(raw),),
    ).fetchone()
    return dict(row)


def set_email(user_id: int, email: str, password: str) -> dict:
    """Set or change the user's e-mail (unverified until the link is opened).

    Requires the current password so a stolen session cannot redirect password
    resets to another mailbox.
    """
    email = normalize_email(email)
    conn = get_conn()
    try:
        row = conn.execute("SELECT password_hash, email, email_verified_at FROM users WHERE id = ?",
                           (user_id,)).fetchone()
        if not row or not _verify_password(password, row["password_hash"]):
            raise WrongPassword("Soucasne heslo neni spravne.")
        if row["email"] == email:
            return {"email": email, "email_verified": row["email_verified_at"] is not None,
                    "changed": False}
        conn.execute("UPDATE users SET email = ?, email_verified_at = NULL WHERE id = ?",
                     (email, user_id))
        conn.commit()
        return {"email": email, "email_verified": False, "changed": True}
    finally:
        conn.close()


def verify_email_with_token(raw: str) -> dict | None:
    """Confirm an e-mail address. Returns the user or None (invalid/expired link).

    Raises ValueError when another account has already confirmed the address
    (only the mailbox owner, who holds the link, learns that).
    """
    tok = peek_user_token(raw, "verify")
    if not tok:
        return None
    conn = get_conn()
    try:
        if _email_taken(conn, tok["email"], except_user=tok["user_id"]):
            raise ValueError("Tato adresa je uz potvrzena u jineho uctu.")
        tok = _consume_token(conn, raw, "verify")
        if not tok:
            conn.commit()
            return None
        try:
            cur = conn.execute(
                "UPDATE users SET email_verified_at = ? WHERE id = ? AND email = ? COLLATE NOCASE",
                (time.time(), tok["user_id"], tok["email"]),
            )
        except sqlite3.IntegrityError:  # confirmed elsewhere in the meantime
            conn.rollback()
            raise ValueError("Tato adresa je uz potvrzena u jineho uctu.")
        conn.commit()
        if cur.rowcount != 1:
            return None  # the address was changed after the link was sent
        return get_user(tok["user_id"])
    finally:
        conn.close()


def find_user_for_reset(login: str) -> dict | None:
    """Find an account by nickname or e-mail that has a verified e-mail address."""
    login = (login or "").strip()
    if not login:
        return None
    # "@" means an address: a nickname shaped like someone's address must not
    # capture resets requested with that address.
    field, value = ("email", login.lower()) if "@" in login else ("nickname", login)
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, nickname, role, email FROM users WHERE email_verified_at IS NOT NULL "
            f"AND email IS NOT NULL AND {field} = ? COLLATE NOCASE",
            (value,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def reset_password_with_token(raw: str, new_password: str) -> dict | None:
    """Set a new password from a reset link. Logs the user out everywhere.

    Returns the user or None when the link is invalid, used or expired.
    Raises ValueError (without consuming the link) for a too short password.
    """
    tok = peek_user_token(raw, "reset")
    if not tok:
        return None
    user = get_user(tok["user_id"])
    if not user:
        return None
    _check_password_policy(new_password, user["role"])
    conn = get_conn()
    try:
        tok = _consume_token(conn, raw, "reset")
        if not tok:
            conn.commit()
            return None
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (_hash_password(new_password), tok["user_id"]))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (tok["user_id"],))
        conn.execute("UPDATE user_tokens SET used_at = ? WHERE user_id = ? AND purpose = 'reset' "
                     "AND used_at IS NULL", (time.time(), tok["user_id"]))
        conn.commit()
        return user
    finally:
        conn.close()


def accept_invite(raw: str, nickname: str, password: str, avatar_id: int | None = None) -> dict | None:
    """Create a teacher account from an invitation link (e-mail counts as verified).

    Returns the new user, None for an invalid/used/expired link; raises
    ValueError for invalid account data (the link stays usable).
    """
    tok = peek_user_token(raw, "invite")
    if not tok:
        return None
    user = create_user(nickname, password, "teacher", avatar_id=avatar_id,
                       email=tok["email"], email_verified=True)
    conn = get_conn()
    try:
        if not _consume_token(conn, raw, "invite"):
            # Lost a race with another acceptance of the same link: undo.
            conn.execute("DELETE FROM users WHERE id = ?", (user["id"],))
            conn.commit()
            return None
        conn.commit()
        return user
    finally:
        conn.close()


def admin_set_password(nickname: str, new_password: str) -> dict:
    """Set a password from the server console (emergency access). Logs the user out."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT id, nickname, role FROM users WHERE nickname = ? COLLATE NOCASE",
                           (nickname.strip(),)).fetchone()
        if not row:
            raise ValueError(f"Ucet '{nickname}' neexistuje.")
        _check_password_policy(new_password, row["role"])
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (_hash_password(new_password), row["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def admin_set_role(nickname: str, role: str) -> dict:
    """Set an account's role from the server console (e.g. remove a teacher). Logs the user out."""
    if role not in ("student", "teacher"):
        raise ValueError("Role musi byt 'student' nebo 'teacher'.")
    conn = get_conn()
    try:
        row = conn.execute("SELECT id, nickname FROM users WHERE nickname = ? COLLATE NOCASE",
                           (nickname.strip(),)).fetchone()
        if not row:
            raise ValueError(f"Ucet '{nickname}' neexistuje.")
        conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, row["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        conn.commit()
        return {"id": row["id"], "nickname": row["nickname"], "role": role}
    finally:
        conn.close()


# --- Game Recording ---

def save_game_to_db(game_record: dict, class_id: int | None = None,
                    user_map: dict[str, int] | None = None) -> None:
    """Save a completed game to the database.

    Args:
        game_record: Dict with id, timestamp, total_questions, etc.
        class_id: Optional class ID to associate the game with.
        user_map: Optional mapping of player_id -> user_id for registered players.
    """
    conn = get_conn()
    try:
        game_id = game_record["id"]
        # Check if already saved
        existing = conn.execute("SELECT id FROM games WHERE id = ?", (game_id,)).fetchone()
        if existing:
            return

        conn.execute(
            "INSERT INTO games (id, class_id, bank_name, total_questions, "
            "question_duration_sec, player_count, played_at, owner_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                game_id,
                class_id,
                game_record.get("bank_name", ""),
                game_record.get("total_questions", 0),
                game_record.get("question_duration_sec", 20),
                game_record.get("player_count", 0),
                game_record.get("timestamp", datetime.now().isoformat()),
                game_record.get("owner_id"),
            ),
        )

        user_map = user_map or {}
        for player in game_record.get("players", []):
            # Try to find user_id from player_id mapping
            player_user_id = None
            player_id = player.get("player_id")
            if player_id and player_id in user_map:
                player_user_id = user_map[player_id]

            conn.execute(
                "INSERT OR IGNORE INTO game_players "
                "(game_id, user_id, player_name, score, rank) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    game_id,
                    player_user_id,
                    player["name"],
                    player.get("score", 0),
                    player.get("rank", 0),
                ),
            )

        # Update cumulative scores for registered users
        for player in game_record.get("players", []):
            player_id = player.get("player_id")
            if player_id and player_id in user_map:
                uid = user_map[player_id]
                conn.execute(
                    "UPDATE users SET total_score = total_score + ?, "
                    "games_played = games_played + 1 WHERE id = ?",
                    (player.get("score", 0), uid),
                )

        conn.commit()
    finally:
        conn.close()


def get_user_game_history(user_id: int, limit: int = 50) -> list[dict]:
    """Get game history for a specific user."""
    conn = get_conn()
    try:
        rows = conn.execute("""
            SELECT gp.game_id, gp.score, gp.rank, gp.player_name,
                   g.played_at, g.total_questions, g.player_count, g.bank_name
            FROM game_players gp
            JOIN games g ON g.id = gp.game_id
            WHERE gp.user_id = ?
            ORDER BY g.played_at DESC
            LIMIT ?
        """, (user_id, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- Self-Paced Sessions ---

# TTL for self-paced homework sessions: 7 days
SELFPACED_SESSION_TTL = 7 * 24 * 3600


def create_selfpaced_session(bank_name: str, questions: list, owner_id: int | None = None) -> str:
    """Create a new self-paced session. Returns session_id."""
    session_id = uuid.uuid4().hex[:12]
    now = time.time()
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO selfpaced_sessions (id, bank_name, questions, created_at, expires_at, owner_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, bank_name, json.dumps(questions, ensure_ascii=False),
             now, now + SELFPACED_SESSION_TTL, owner_id),
        )
        conn.commit()
        return session_id
    finally:
        conn.close()


def get_selfpaced_session(session_id: str) -> dict | None:
    """Get a self-paced session by ID. Returns None if not found or expired."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, bank_name, questions, created_at, expires_at, owner_id "
            "FROM selfpaced_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        if time.time() > row["expires_at"]:
            return None
        return {
            "id": row["id"],
            "bank_name": row["bank_name"],
            "questions": json.loads(row["questions"]),
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "owner_id": row["owner_id"],
        }
    finally:
        conn.close()


def upsert_selfpaced_progress(session_id: str, player_id: str, player_name: str,
                               user_id: int | None, score: int, total: int,
                               answers: list, finished: bool = False) -> None:
    """Insert or update a student's progress in a self-paced session."""
    conn = get_conn()
    try:
        finished_at = time.time() if finished else None
        existing = conn.execute(
            "SELECT finished_at FROM selfpaced_results WHERE session_id = ? AND player_id = ?",
            (session_id, player_id),
        ).fetchone()

        if existing:
            # Don't overwrite a finished result
            if existing["finished_at"] is not None:
                return
            conn.execute(
                "UPDATE selfpaced_results SET player_name=?, score=?, total=?, answers=?, "
                "finished_at=?, user_id=? WHERE session_id=? AND player_id=?",
                (player_name, score, total, json.dumps(answers), finished_at,
                 user_id, session_id, player_id),
            )
        else:
            conn.execute(
                "INSERT INTO selfpaced_results "
                "(session_id, player_id, player_name, user_id, score, total, answers, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, player_id, player_name, user_id, score, total,
                 json.dumps(answers), finished_at),
            )

        # Update cumulative user stats when finished
        if finished and user_id is not None:
            conn.execute(
                "UPDATE users SET total_score = total_score + ?, "
                "games_played = games_played + 1 WHERE id = ?",
                (score, user_id),
            )

        conn.commit()
    finally:
        conn.close()


def get_selfpaced_progress(session_id: str, player_id: str) -> dict | None:
    """Get a single student's progress."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT session_id, player_id, player_name, user_id, score, total, "
            "answers, finished_at FROM selfpaced_results WHERE session_id=? AND player_id=?",
            (session_id, player_id),
        ).fetchone()
        if not row:
            return None
        return {
            "session_id": row["session_id"],
            "player_id": row["player_id"],
            "player_name": row["player_name"],
            "user_id": row["user_id"],
            "score": row["score"],
            "total": row["total"],
            "answers": json.loads(row["answers"]),
            "finished_at": row["finished_at"],
        }
    finally:
        conn.close()


def list_selfpaced_sessions(owner_id: int | None = None) -> list[dict]:
    """Homework links that have not expired yet, newest first (all or one teacher's)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT s.id, s.bank_name, s.created_at, s.expires_at, s.owner_id, "
            "(SELECT COUNT(*) FROM selfpaced_results r WHERE r.session_id = s.id) AS players, "
            "(SELECT COUNT(*) FROM selfpaced_results r WHERE r.session_id = s.id "
            " AND r.finished_at IS NOT NULL) AS finished "
            "FROM selfpaced_sessions s WHERE s.expires_at > ? AND (? IS NULL OR s.owner_id = ?) "
            "ORDER BY s.created_at DESC",
            (time.time(), owner_id, owner_id),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_selfpaced_secret(session_id: str, player_id: str, secret: str) -> None:
    conn = get_conn()
    try:
        conn.execute("UPDATE selfpaced_results SET secret_hash = ? WHERE session_id = ? AND player_id = ?",
                     (hashlib.sha256(secret.encode("utf-8")).hexdigest(), session_id, player_id))
        conn.commit()
    finally:
        conn.close()


def selfpaced_secret_ok(session_id: str, player_id: str, secret: str | None) -> bool:
    """True when the homework player's secret matches (sessions from before the
    secret existed have none stored and stay usable by player_id alone)."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT secret_hash FROM selfpaced_results WHERE session_id = ? AND player_id = ?",
                           (session_id, player_id)).fetchone()
    finally:
        conn.close()
    if not row:
        return False
    if row["secret_hash"] is None:
        return True
    return bool(secret) and hmac.compare_digest(
        row["secret_hash"], hashlib.sha256(secret.encode("utf-8")).hexdigest())


def list_selfpaced_results(session_id: str) -> list[dict]:
    """List all results for a self-paced session, sorted by score desc."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT player_name, score, total, finished_at FROM selfpaced_results "
            "WHERE session_id = ? ORDER BY score DESC, player_name COLLATE NOCASE",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def cleanup_expired_selfpaced_sessions() -> None:
    """Remove expired self-paced sessions (and cascade-delete their results)."""
    conn = get_conn()
    try:
        conn.execute(
            "DELETE FROM selfpaced_sessions WHERE expires_at < ?",
            (time.time(),),
        )
        conn.commit()
    finally:
        conn.close()


# --- Exams (pisemky) ---

def create_exam_session(session_id: str, owner_id: int | None, bank_name: str,
                        questions: list, config: dict, opened_at: float) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO exam_sessions (id, owner_id, bank_name, questions, config, opened_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, owner_id, bank_name, json.dumps(questions, ensure_ascii=False),
             json.dumps(config, ensure_ascii=False), opened_at),
        )
        conn.commit()
    finally:
        conn.close()


def save_exam_result(session_id: str, player_id: str, row: dict) -> None:
    """Insert or update one student's state in an exam (answers, grade, proctoring)."""
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO exam_results (session_id, player_id, name, answers, score, grade, submitted_at, "
            "auto_submitted, blur_count, away_ms, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id, player_id) DO UPDATE SET name = excluded.name, "
            "answers = excluded.answers, score = excluded.score, grade = excluded.grade, "
            "submitted_at = excluded.submitted_at, auto_submitted = excluded.auto_submitted, "
            "blur_count = excluded.blur_count, away_ms = excluded.away_ms, updated_at = excluded.updated_at",
            (session_id, player_id, row["name"], json.dumps(row.get("answers") or {}, ensure_ascii=False),
             row.get("score"), row.get("grade"), row.get("submitted_at"),
             1 if row.get("auto_submitted") else 0, int(row.get("blur_count") or 0),
             float(row.get("away_ms") or 0.0), time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def reopen_exam_session(session_id: str, config: dict) -> bool:
    """An ended exam opened again (latecomers): running again, with current settings.

    False when the saved exam no longer exists (the teacher deleted it).
    """
    conn = get_conn()
    try:
        cur = conn.execute("UPDATE exam_sessions SET ended_at = NULL, interrupted = 0, config = ? WHERE id = ?",
                           (json.dumps(config, ensure_ascii=False), session_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def update_exam_session_config(session_id: str, config: dict) -> None:
    conn = get_conn()
    try:
        conn.execute("UPDATE exam_sessions SET config = ? WHERE id = ?",
                     (json.dumps(config, ensure_ascii=False), session_id))
        conn.commit()
    finally:
        conn.close()


def end_exam_session(session_id: str, ended_at: float, interrupted: bool = False) -> None:
    conn = get_conn()
    try:
        conn.execute("UPDATE exam_sessions SET ended_at = ?, interrupted = ? WHERE id = ?",
                     (ended_at, 1 if interrupted else 0, session_id))
        conn.commit()
    finally:
        conn.close()


def _exam_session_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "owner_id": row["owner_id"], "bank_name": row["bank_name"] or "",
        "questions": json.loads(row["questions"]), "config": json.loads(row["config"]),
        "opened_at": row["opened_at"], "ended_at": row["ended_at"], "interrupted": bool(row["interrupted"]),
    }


def _exam_result_row(row: sqlite3.Row) -> dict:
    return {
        "player_id": row["player_id"], "name": row["name"], "answers": json.loads(row["answers"] or "{}"),
        "score": row["score"], "grade": row["grade"], "submitted_at": row["submitted_at"],
        "auto_submitted": bool(row["auto_submitted"]), "blur_count": row["blur_count"],
        "away_ms": row["away_ms"], "updated_at": row["updated_at"],
    }


def get_exam_session(session_id: str) -> dict | None:
    """One exam with all its student rows, or None."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM exam_sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            return None
        session = _exam_session_row(row)
        session["results"] = [_exam_result_row(r) for r in conn.execute(
            "SELECT * FROM exam_results WHERE session_id = ? ORDER BY name COLLATE NOCASE", (session_id,))]
        return session
    finally:
        conn.close()


def list_exam_sessions(owner_id: int | None = None) -> list[dict]:
    """Exams newest first (one teacher's, or all), with student counts and the average."""
    conn = get_conn()
    try:
        where, args = ("WHERE s.owner_id = ?", (owner_id,)) if owner_id is not None else ("", ())
        rows = conn.execute(f"""
            SELECT s.id, s.owner_id, s.bank_name, s.questions, s.opened_at, s.ended_at, s.interrupted,
                   COUNT(r.player_id) AS students,
                   SUM(CASE WHEN r.submitted_at IS NOT NULL OR r.auto_submitted = 1 THEN 1 ELSE 0 END) AS graded,
                   AVG(r.score) AS avg_score
            FROM exam_sessions s LEFT JOIN exam_results r ON r.session_id = s.id
            {where}
            GROUP BY s.id ORDER BY s.opened_at DESC
        """, args).fetchall()
        out = []
        for row in rows:
            total = len(json.loads(row["questions"]))
            avg = row["avg_score"]
            out.append({
                "id": row["id"], "owner_id": row["owner_id"], "bank_name": row["bank_name"] or "",
                "opened_at": row["opened_at"], "ended_at": row["ended_at"],
                "interrupted": bool(row["interrupted"]), "total_questions": total,
                "students": row["students"], "graded": row["graded"] or 0,
                "avg_percent": round(avg / total * 100) if avg is not None and total else None,
            })
        return out
    finally:
        conn.close()


def list_open_exam_sessions() -> list[dict]:
    """Exams without an end time, with their student rows (for closing after a restart)."""
    conn = get_conn()
    try:
        ids = [r["id"] for r in conn.execute("SELECT id FROM exam_sessions WHERE ended_at IS NULL")]
    finally:
        conn.close()
    return [s for s in (get_exam_session(i) for i in ids) if s is not None]


def delete_exam_session(session_id: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM exam_sessions WHERE id = ?", (session_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# --- Migration ---

def migrate_json_history(history_dir: Path) -> int:
    """Import existing JSON history files into the database. Idempotent."""
    global _migrated
    with _migration_lock:
        if _migrated:
            return 0
        _migrated = True

    if not history_dir.is_dir():
        return 0

    count = 0
    for fp in sorted(history_dir.glob("game_*.json")):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                record = json.load(f)
            if "id" not in record:
                # Extract ID from filename
                record["id"] = fp.stem.replace("game_", "")
            save_game_to_db(record)
            count += 1
        except (json.JSONDecodeError, KeyError, sqlite3.Error):
            continue  # Skip invalid files

    return count
