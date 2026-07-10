"""SQLite database layer for ClassRally.

Provides user accounts, classes/groups, and game history persistence.
Uses only stdlib (sqlite3). Thread-safe via WAL mode + one connection per call.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
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


def _hash_password(password: str) -> str:
    """SHA256 hash of password. Matches existing AdminAuth pattern."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


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
    finally:
        conn.close()


# --- User CRUD ---

def create_user(nickname: str, password: str, role: str = "student",
                avatar_id: int | None = None) -> dict:
    """Register a new user. Returns {id, nickname, role, avatar_id} or raises ValueError."""
    nickname = nickname.strip()
    if not nickname or len(nickname) > 24:
        raise ValueError("Prezdivka musi mit 1-24 znaku.")
    if not password or len(password) < 4:
        raise ValueError("Heslo musi mit alespon 4 znaky.")
    if role not in ("student", "teacher"):
        raise ValueError("Role musi byt 'student' nebo 'teacher'.")

    if avatar_id is None:
        avatar_id = random.randint(1, 20)
    else:
        avatar_id = max(1, min(20, int(avatar_id)))

    pw_hash = _hash_password(password)
    conn = get_conn()
    try:
        try:
            cur = conn.execute(
                "INSERT INTO users (nickname, password_hash, role, avatar_id) VALUES (?, ?, ?, ?)",
                (nickname, pw_hash, role, avatar_id),
            )
            conn.commit()
            return {"id": cur.lastrowid, "nickname": nickname, "role": role, "avatar_id": avatar_id}
        except sqlite3.IntegrityError:
            raise ValueError(f"Prezdivka '{nickname}' je jiz obsazena.")
    finally:
        conn.close()


def authenticate_user(nickname: str, password: str) -> dict | None:
    """Check credentials. Returns user dict or None."""
    pw_hash = _hash_password(password)
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, nickname, role, total_score, games_played, avatar_id FROM users "
            "WHERE nickname = ? COLLATE NOCASE AND password_hash = ?",
            (nickname.strip(), pw_hash),
        ).fetchone()
        if row:
            return dict(row)
        return None
    finally:
        conn.close()


def get_user(user_id: int) -> dict | None:
    """Get user by ID."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, nickname, role, total_score, games_played, avatar_id, created_at "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
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

def create_session(user_id: int) -> str:
    """Create a new session token for a user."""
    token = uuid.uuid4().hex
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
            (token, user_id, time.time()),
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
        row = conn.execute(
            "SELECT user_id, created_at FROM sessions WHERE token = ?",
            (token,),
        ).fetchone()
        if not row:
            return None
        if time.time() - row["created_at"] > SESSION_TTL:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
            return None
        return row["user_id"]
    finally:
        conn.close()


def delete_session(token: str) -> None:
    """Invalidate a session token."""
    conn = get_conn()
    try:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
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
    if not new_password or len(new_password) < 4:
        raise ValueError("Heslo musi mit alespon 4 znaky.")

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


def change_password(user_id: int, old_password: str, new_password: str) -> bool:
    """User changes their own password. Verifies old password first."""
    if not new_password or len(new_password) < 4:
        raise ValueError("Heslo musi mit alespon 4 znaky.")
    old_hash = _hash_password(old_password)
    new_hash = _hash_password(new_password)
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM users WHERE id = ? AND password_hash = ?",
            (user_id, old_hash),
        ).fetchone()
        if not row:
            raise ValueError("Soucasne heslo neni spravne.")
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (new_hash, user_id))
        conn.commit()
        return True
    finally:
        conn.close()


def set_user_role(target_user_id: int, new_role: str, acting_teacher_id: int) -> bool:
    """Teacher changes a user's role. Cannot change own role."""
    if new_role not in ("student", "teacher"):
        raise ValueError("Role musi byt 'student' nebo 'teacher'.")
    if target_user_id == acting_teacher_id:
        raise ValueError("Nemuzes zmenit svoji vlastni roli.")
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM users WHERE id = ?",
                           (target_user_id,)).fetchone()
        if not row:
            raise ValueError("Uzivatel nenalezen.")
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


def teacher_reset_password(target_user_id: int, new_password: str,
                           teacher_id: int) -> bool:
    """Teacher resets any user's password (global, not class-restricted)."""
    if target_user_id == teacher_id:
        raise ValueError("Pouzijte zmenu vlastniho hesla.")
    if not new_password or len(new_password) < 4:
        raise ValueError("Heslo musi mit alespon 4 znaky.")
    conn = get_conn()
    try:
        row = conn.execute("SELECT id FROM users WHERE id = ?",
                           (target_user_id,)).fetchone()
        if not row:
            raise ValueError("Uzivatel nenalezen.")
        pw_hash = _hash_password(new_password)
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (pw_hash, target_user_id))
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
        # Remove from all classes first
        conn.execute("DELETE FROM class_members WHERE user_id = ?",
                     (target_user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (target_user_id,))
        conn.commit()
        return True
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
            "question_duration_sec, player_count, played_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                game_id,
                class_id,
                game_record.get("bank_name", ""),
                game_record.get("total_questions", 0),
                game_record.get("question_duration_sec", 20),
                game_record.get("player_count", 0),
                game_record.get("timestamp", datetime.now().isoformat()),
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


def create_selfpaced_session(bank_name: str, questions: list) -> str:
    """Create a new self-paced session. Returns session_id."""
    session_id = uuid.uuid4().hex[:12]
    now = time.time()
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO selfpaced_sessions (id, bank_name, questions, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, bank_name, json.dumps(questions, ensure_ascii=False),
             now, now + SELFPACED_SESSION_TTL),
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
            "SELECT id, bank_name, questions, created_at, expires_at "
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
