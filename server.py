#!/usr/bin/env python3
"""Local classroom quiz server (Kahoot-style) with host/player/admin views.

Features:
- Teacher admin portal with optional password protection
- Question CRUD editor
- AI question generation via Ollama (configurable model/port)
- Scoring history persistence
- Configurable question/reveal timing
- Security: player secrets, host token, rate-limited login
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import ipaddress
import json
import math
import mimetypes
import os
import re
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError

from qrgen import generate_qr_svg
import db as user_db
from ws import WSConnection, WSConnectionManager, ws_handshake_response

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
AUDIO_DIR = STATIC_DIR / "audio"
UPLOADS_DIR = STATIC_DIR / "uploads"
QUESTIONS_DIR = BASE_DIR / "questions"
HISTORY_DIR = BASE_DIR / "history"
DEFAULT_QUESTIONS_FILE = "default.json"
QUESTION_DURATION_SEC = 20
REVEAL_DURATION_SEC = 5

# Ensure directories exist
QUESTIONS_DIR.mkdir(exist_ok=True)
HISTORY_DIR.mkdir(exist_ok=True)

# Explicitly register audio MIME types that may be absent in minimal Docker images
# (python:3.12-slim ships without /etc/mime.types, so .ogg and .m4a return None).
mimetypes.add_type("audio/mpeg", ".mp3")
mimetypes.add_type("audio/ogg", ".ogg")
mimetypes.add_type("audio/wav", ".wav")
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("audio/aac", ".aac")
mimetypes.add_type("audio/webm", ".weba")

# Game ID format: 12 hex chars
GAME_ID_RE = re.compile(r"^[a-f0-9]{12}$")


# --- Admin auth ---

class AdminAuth:
    """Optional password protection for admin portal."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._password_hash: str | None = None
        self._sessions: dict[str, float] = {}
        self.SESSION_TTL = 3600 * 8  # 8 hours
        self._login_attempts: dict[str, list[float]] = {}
        self.MAX_ATTEMPTS = 5
        self.ATTEMPT_WINDOW = 300  # 5 minutes

    def set_password(self, password: str | None) -> None:
        with self._lock:
            if password:
                self._password_hash = hashlib.sha256(password.encode()).hexdigest()
            else:
                self._password_hash = None

    @property
    def enabled(self) -> bool:
        return self._password_hash is not None

    def _check_rate_limit(self, client_ip: str) -> bool:
        """Return True if login attempt is allowed."""
        now = time.time()
        attempts = self._login_attempts.get(client_ip, [])
        # Clean old attempts
        attempts = [t for t in attempts if now - t < self.ATTEMPT_WINDOW]
        self._login_attempts[client_ip] = attempts
        return len(attempts) < self.MAX_ATTEMPTS

    def _record_attempt(self, client_ip: str) -> None:
        now = time.time()
        if client_ip not in self._login_attempts:
            self._login_attempts[client_ip] = []
        self._login_attempts[client_ip].append(now)

    def check_password(self, password: str, client_ip: str = "") -> str | None:
        with self._lock:
            if self._password_hash is None:
                return self._create_session()
            if client_ip and not self._check_rate_limit(client_ip):
                return None
            if client_ip:
                self._record_attempt(client_ip)
            if hashlib.sha256(password.encode()).hexdigest() == self._password_hash:
                # Clear attempts on success
                self._login_attempts.pop(client_ip, None)
                return self._create_session()
            return None

    def _create_session(self) -> str:
        token = uuid.uuid4().hex
        self._sessions[token] = time.time()
        return token

    def validate_session(self, token: str | None) -> bool:
        with self._lock:
            if self._password_hash is None:
                return True
            if not token:
                return False
            created = self._sessions.get(token)
            if not created:
                return False
            if time.time() - created > self.SESSION_TTL:
                del self._sessions[token]
                return False
            return True


ADMIN_AUTH = AdminAuth()

# Host token - generated at startup, required for host actions
HOST_TOKEN: str = uuid.uuid4().hex


# --- Question bank management ---

def _migrate_legacy_questions() -> None:
    """Migrate old single-file questions to questions/ directory."""
    legacy = BASE_DIR / "questions_virtualbox_ubuntu_docker.json"
    target = QUESTIONS_DIR / "virtualbox_ubuntu_docker.json"
    if legacy.exists() and not target.exists():
        import shutil
        shutil.copy2(legacy, target)


def list_question_banks() -> list[dict[str, Any]]:
    _migrate_legacy_questions()
    banks = []
    for f in sorted(QUESTIONS_DIR.iterdir()):
        if f.suffix == ".json" and f.is_file():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                count = len(data) if isinstance(data, list) else 0
            except Exception:
                count = 0
            banks.append({
                "filename": f.name,
                "name": f.stem.replace("_", " ").title(),
                "question_count": count,
            })
    return banks


def _validate_question(q: dict, idx: int) -> None:
    """Validate a single question dict. Supports multiple question types."""
    if not isinstance(q, dict):
        raise RuntimeError(f"question {idx} is not object")
    if "id" not in q or "prompt" not in q:
        raise RuntimeError(f"question {idx} missing 'id' or 'prompt'")
    if not isinstance(q["prompt"], str) or not q["prompt"].strip():
        raise RuntimeError(f"question {idx} has empty prompt")

    q_type = q.get("type", "choice")

    if q_type in ("choice", "truefalse"):
        for key in ("options", "correct_index"):
            if key not in q:
                raise RuntimeError(f"question {idx} ({q_type}) missing '{key}'")
        if not isinstance(q["options"], list) or len(q["options"]) < 2:
            raise RuntimeError(f"question {idx} has invalid options")
        if q_type == "truefalse" and len(q["options"]) != 2:
            raise RuntimeError(f"question {idx} truefalse must have exactly 2 options")
        if len(q["options"]) > 6:
            raise RuntimeError(f"question {idx} has too many options (max 6)")
        for oi, opt in enumerate(q["options"]):
            if not isinstance(opt, str):
                raise RuntimeError(f"question {idx} option {oi} is not a string")
        ci = q["correct_index"]
        if not isinstance(ci, int) or ci < 0 or ci >= len(q["options"]):
            raise RuntimeError(f"question {idx} correct_index {ci} out of range")

    elif q_type == "multiselect":
        if "options" not in q or "correct_indices" not in q:
            raise RuntimeError(f"question {idx} (multiselect) missing 'options' or 'correct_indices'")
        if not isinstance(q["options"], list) or len(q["options"]) < 2:
            raise RuntimeError(f"question {idx} has invalid options")
        if len(q["options"]) > 6:
            raise RuntimeError(f"question {idx} has too many options (max 6)")
        ci_list = q["correct_indices"]
        if not isinstance(ci_list, list) or len(ci_list) < 1:
            raise RuntimeError(f"question {idx} correct_indices must be non-empty list")
        for ci in ci_list:
            if not isinstance(ci, int) or ci < 0 or ci >= len(q["options"]):
                raise RuntimeError(f"question {idx} correct_indices {ci} out of range")

    elif q_type == "ordering":
        if "items" not in q:
            raise RuntimeError(f"question {idx} (ordering) missing 'items'")
        if not isinstance(q["items"], list) or len(q["items"]) < 2:
            raise RuntimeError(f"question {idx} items must have at least 2 elements")
        if len(q["items"]) > 8:
            raise RuntimeError(f"question {idx} has too many items (max 8)")

    elif q_type == "openended":
        if "accepted_answers" not in q:
            raise RuntimeError(f"question {idx} (openended) missing 'accepted_answers'")
        if not isinstance(q["accepted_answers"], list) or len(q["accepted_answers"]) < 1:
            raise RuntimeError(f"question {idx} accepted_answers must be non-empty list")

    elif q_type == "poll":
        if "options" not in q:
            raise RuntimeError(f"question {idx} (poll) missing 'options'")
        if not isinstance(q["options"], list) or len(q["options"]) < 2:
            raise RuntimeError(f"question {idx} poll options must have at least 2 items")
        if len(q["options"]) > 6:
            raise RuntimeError(f"question {idx} has too many options (max 6)")
        for oi, opt in enumerate(q["options"]):
            if not isinstance(opt, str):
                raise RuntimeError(f"question {idx} option {oi} is not a string")

    elif q_type == "wordcloud":
        pass  # Only needs prompt; optional max_length

    elif q_type == "slide":
        # Slide needs a body (title is the prompt field)
        if "body" not in q:
            raise RuntimeError(f"question {idx} (slide) missing 'body'")

    else:
        raise RuntimeError(f"question {idx} unknown type '{q_type}'")

    # Optional image_url field (all question types)
    if "image_url" in q:
        if not isinstance(q["image_url"], str):
            raise RuntimeError(f"question {idx} image_url must be a string")


def load_questions_from_file(filename: str) -> list[dict[str, Any]]:
    safe_name = Path(filename).name
    path = QUESTIONS_DIR / safe_name
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Question bank '{safe_name}' not found")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise RuntimeError("questions file must contain a list")
    for i, q in enumerate(raw):
        _validate_question(q, i)
    return raw


def save_questions_to_file(filename: str, questions: list[dict[str, Any]]) -> None:
    safe_name = Path(filename).name
    if not safe_name.endswith(".json"):
        safe_name += ".json"
    # Validate before saving
    if not isinstance(questions, list):
        raise ValueError("questions must be a list")
    for i, q in enumerate(questions):
        _validate_question(q, i)
    path = QUESTIONS_DIR / safe_name
    path.write_text(json.dumps(questions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def delete_question_bank(filename: str) -> None:
    safe_name = Path(filename).name
    path = QUESTIONS_DIR / safe_name
    if path.exists():
        path.unlink()


def questions_to_csv(questions: list[dict[str, Any]]) -> str:
    """Serialize a question bank to CSV.

    CSV columns: id, type, prompt, options (pipe-separated), correct_answer, explanation
    For multiselect correct_answer is pipe-separated indices.
    For ordering, options holds the ordered items and correct_answer is empty.
    For openended, options is empty and correct_answer holds pipe-separated accepted answers.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["id", "type", "prompt", "options", "correct_answer", "explanation"])
    for q in questions:
        q_type = q.get("type", "choice")
        prompt = q.get("prompt", "")
        explanation = q.get("explanation", "")
        qid = q.get("id", "")
        if q_type in ("choice", "truefalse", "multiselect"):
            options = "|".join(q.get("options", []))
            if q_type == "multiselect":
                correct = "|".join(str(i) for i in q.get("correct_indices", []))
            else:
                correct = str(q.get("correct_index", 0))
        elif q_type == "ordering":
            options = "|".join(q.get("items", []))
            correct = ""
        elif q_type == "openended":
            options = ""
            correct = "|".join(q.get("accepted_answers", []))
        elif q_type == "poll":
            options = "|".join(q.get("options", []))
            correct = ""
        elif q_type == "wordcloud":
            options = ""
            correct = ""
        elif q_type == "slide":
            options = q.get("body", "")
            correct = ""
        else:
            options = ""
            correct = ""
        writer.writerow([qid, q_type, prompt, options, correct, explanation])
    return buf.getvalue()


def questions_from_csv(csv_text: str) -> list[dict[str, Any]]:
    """Parse questions from CSV text. Returns a list of question dicts."""
    reader = csv.DictReader(io.StringIO(csv_text))
    required_cols = {"prompt"}
    questions = []
    for i, row in enumerate(reader):
        if not required_cols.issubset(row.keys()):
            raise ValueError(f"CSV row {i} missing required column 'prompt'")
        q_type = (row.get("type") or "choice").strip()
        prompt = (row.get("prompt") or "").strip()
        if not prompt:
            continue  # skip blank rows
        explanation = (row.get("explanation") or "").strip()
        qid = (row.get("id") or f"csv_{i+1}").strip() or f"csv_{i+1}"
        options_raw = row.get("options", "") or ""
        correct_raw = (row.get("correct_answer") or row.get("correct_index") or "").strip()

        q: dict[str, Any] = {"id": qid, "type": q_type, "prompt": prompt, "explanation": explanation}

        if q_type in ("choice", "truefalse"):
            opts = [o.strip() for o in options_raw.split("|") if o.strip()] if options_raw else []
            if not opts:
                raise ValueError(f"CSV row {i} ({q_type}) has no options")
            try:
                ci = int(correct_raw)
            except (ValueError, TypeError):
                ci = 0
            q["options"] = opts
            q["correct_index"] = ci

        elif q_type == "multiselect":
            opts = [o.strip() for o in options_raw.split("|") if o.strip()] if options_raw else []
            if not opts:
                raise ValueError(f"CSV row {i} (multiselect) has no options")
            try:
                ci_list = [int(x.strip()) for x in correct_raw.split("|") if x.strip()]
            except (ValueError, TypeError):
                ci_list = [0]
            q["options"] = opts
            q["correct_indices"] = ci_list

        elif q_type == "ordering":
            items = [o.strip() for o in options_raw.split("|") if o.strip()] if options_raw else []
            if not items:
                raise ValueError(f"CSV row {i} (ordering) has no items")
            q["items"] = items

        elif q_type == "openended":
            accepted = [a.strip() for a in correct_raw.split("|") if a.strip()]
            if not accepted:
                raise ValueError(f"CSV row {i} (openended) has no accepted answers")
            q["accepted_answers"] = accepted

        elif q_type == "poll":
            opts = [o.strip() for o in options_raw.split("|") if o.strip()] if options_raw else []
            if not opts:
                raise ValueError(f"CSV row {i} (poll) has no options")
            q["options"] = opts

        elif q_type == "wordcloud":
            pass  # no extra fields needed

        elif q_type == "slide":
            q["body"] = options_raw  # body stored in options column

        else:
            raise ValueError(f"CSV row {i} unknown type '{q_type}'")

        questions.append(q)

    return questions


def game_results_to_csv(record: dict[str, Any]) -> str:
    """Serialize a game history record to CSV (rank, name, score)."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["rank", "name", "score"])
    for p in record.get("players", []):
        writer.writerow([p.get("rank", ""), p.get("name", ""), p.get("score", 0)])
    return buf.getvalue()


def load_default_questions() -> list[dict[str, Any]]:
    _migrate_legacy_questions()
    default_path = QUESTIONS_DIR / DEFAULT_QUESTIONS_FILE
    if default_path.exists():
        try:
            return load_questions_from_file(DEFAULT_QUESTIONS_FILE)
        except Exception:
            pass
    banks = list_question_banks()
    if banks:
        try:
            return load_questions_from_file(banks[0]["filename"])
        except Exception:
            pass
    return []


# --- Scoring history ---

def save_game_history(quiz_state: QuizState, class_id: int | None = None) -> dict[str, Any]:
    record = {
        "id": uuid.uuid4().hex[:12],
        "timestamp": datetime.now().isoformat(),
        "total_questions": len(quiz_state.questions),
        "question_duration_sec": quiz_state.question_duration_sec,
        "players": quiz_state._ranked_players_with_ids(),
        "player_count": len(quiz_state.players),
        "bank_name": quiz_state._active_bank or "",
    }
    # Save to JSON file (backward compat)
    filename = f"game_{record['id']}.json"
    path = HISTORY_DIR / filename
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Save to SQLite DB
    try:
        user_map = quiz_state._get_user_map()
        user_db.save_game_to_db(record, class_id=class_id, user_map=user_map)
    except Exception:
        pass  # DB save is best-effort, don't break the game
    return record


def list_game_history() -> list[dict[str, Any]]:
    history = []
    for f in sorted(HISTORY_DIR.iterdir(), reverse=True):
        if f.suffix == ".json" and f.is_file():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                history.append(data)
            except Exception:
                continue
    return history


def delete_game_history(game_id: str) -> bool:
    if not game_id or not GAME_ID_RE.match(game_id):
        return False
    expected_name = f"game_{game_id}.json"
    path = HISTORY_DIR / expected_name
    if path.exists() and path.is_file():
        path.unlink()
        return True
    return False


# --- AI Question Generation via Ollama ---

def generate_questions_ai(
    topic: str,
    count: int = 5,
    base_url: str = "http://localhost:11434",
    model: str = "gpt-oss:20b",
    api_key: str = "",
    language: str = "cs",
) -> list[dict[str, Any]]:
    prompt = f"""Vygeneruj presne {count} kvizovych otazek na tema: "{topic}"

Kazda otazka musi mit:
- "id": unikatni identifikator (napr. "ai_1", "ai_2", ...)
- "prompt": text otazky (v jazyce: {language})
- "options": pole presne 4 moznosti odpovedi
- "correct_index": index spravne odpovedi (0-3)
- "explanation": kratke vysvetleni spravne odpovedi

Odpovez POUZE validnim JSON polem bez zadneho dalsiho textu. Zadny markdown, zadne komentare.
Priklad formatu:
[{{"id":"ai_1","prompt":"Otazka?","options":["A","B","C","D"],"correct_index":0,"explanation":"Vysvetleni"}}]"""

    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a quiz question generator. Respond ONLY with valid JSON."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 4096,
    }).encode("utf-8")

    req = Request(url, data=payload, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except URLError as e:
        raise ConnectionError(f"Nelze se pripojit k AI API na {base_url}: {e}")
    except Exception as e:
        raise RuntimeError(f"Chyba pri komunikaci s AI API: {e}")

    response_text = result.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
    start = response_text.find("[")
    end = response_text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"AI API nevrátila validní JSON pole. Odpoved: {response_text[:500]}")

    json_str = response_text[start:end + 1]
    try:
        questions = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Nevalidní JSON z AI API: {e}. Text: {json_str[:500]}")

    if not isinstance(questions, list):
        raise ValueError("AI API nevrátila pole otázek")

    valid = []
    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            continue
        if "id" not in q:
            q["id"] = f"ai_{i+1}"
        if "prompt" not in q or "options" not in q or "correct_index" not in q:
            continue
        if not isinstance(q["options"], list) or len(q["options"]) < 2:
            continue
        while len(q["options"]) < 4:
            q["options"].append(f"Moznost {len(q['options'])+1}")
        q["options"] = q["options"][:4]
        if not isinstance(q["correct_index"], int) or q["correct_index"] < 0 or q["correct_index"] > 3:
            q["correct_index"] = 0
        if "explanation" not in q:
            q["explanation"] = ""
        valid.append(q)

    if not valid:
        raise ValueError("AI API nevygenerovala zadne validni otazky")

    return valid


def list_ai_models(base_url: str = "http://localhost:11434", api_key: str = "") -> list[dict[str, str]]:
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # Try Ollama native endpoint first
    try:
        req = Request(f"{base_url.rstrip('/')}/api/tags", headers=headers, method="GET")
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        models = result.get("models", [])
        return [{"name": m.get("name", ""), "size": m.get("size", 0)} for m in models]
    except Exception:
        pass

    # Fall back to OpenAI-compatible endpoint
    try:
        req = Request(f"{base_url.rstrip('/')}/v1/models", headers=headers, method="GET")
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        models = result.get("data", [])
        return [{"name": m.get("id", ""), "size": 0} for m in models]
    except Exception:
        return []


# --- Random nickname generator ---

_NICK_ADJECTIVES = [
    "Rychly", "Chytry", "Vesely", "Odvazny", "Tichy", "Silny", "Modry", "Zeleny",
    "Zlaty", "Cerveny", "Smely", "Hbity", "Vtipny", "Statecny", "Bystry", "Divny",
    "Mocny", "Borovy", "Skvely", "Zary", "Ostry", "Temny", "Hrdivy", "Blaznivy",
    "Zarlivy", "Hladovy", "Klidny", "Drzny", "Volny", "Hravy",
]

_NICK_ANIMALS = [
    "Lev", "Orel", "Vlk", "Medved", "Jelen", "Sokol", "Panter", "Delfin",
    "Tygr", "Gepard", "Kolibrik", "Jestrab", "Rys", "Vydra", "Lasicka",
    "Zubr", "Liska", "Krokodyl", "Kondor", "Zralok", "Kocka", "Kun",
    "Antilopa", "Puma", "Nosorozec", "Kakapo", "Tukan", "Piranha", "Kobra", "Jezek",
]


def _generate_random_nickname() -> str:
    """Generate a random Czech adjective+animal nickname, e.g. 'RychlyOrel'.

    Checks against currently registered player names and appends a numeric
    suffix if there is a collision.
    """
    import random as _random
    adj = _random.choice(_NICK_ADJECTIVES)
    animal = _random.choice(_NICK_ANIMALS)
    base = adj + animal
    existing_names = {p.name for p in QUIZ.players.values()}
    if base not in existing_names:
        return base
    for _ in range(20):
        candidate = f"{base}{_random.randint(10, 99)}"
        if candidate not in existing_names:
            return candidate
    return f"{base}{int(time.time()) % 1000}"


# --- Nickname profanity filter ---

_BANNED_WORDS = frozenset([
    # Czech profanity
    "kurva", "pica", "pico", "picus", "kokot", "debil", "vole", "hovado",
    "srac", "hovno", "zasran", "zkurvit", "mrdat", "jebat", "prdel",
    "cajzl", "buzerant", "svinej", "kretén", "kreten", "blbec", "idiot",
    "piča", "píča", "čurák", "curak", "zmrd", "hajzl", "vychcanej",
    # English profanity
    "fuck", "shit", "bitch", "asshole", "dick", "cunt", "bastard",
    "nigger", "faggot", "retard", "whore", "slut", "penis", "vagina",
    "wanker", "twat", "piss", "damn", "crap",
])


def _is_nickname_clean(name: str) -> bool:
    """Return True if the nickname does not contain any banned words."""
    lower = name.lower()
    return not any(word in lower for word in _BANNED_WORDS)


# --- Registration rate limiter ---

class _RegistrationRateLimiter:
    """Simple dict-based rate limiter for player registration."""

    def __init__(self, max_requests: int = 10, window_sec: int = 60) -> None:
        self._lock = threading.Lock()
        self._attempts: dict[str, list[float]] = {}
        self.max_requests = max_requests
        self.window_sec = window_sec

    def is_allowed(self, client_ip: str) -> bool:
        now = time.time()
        with self._lock:
            attempts = self._attempts.get(client_ip, [])
            attempts = [t for t in attempts if now - t < self.window_sec]
            self._attempts[client_ip] = attempts
            if len(attempts) >= self.max_requests:
                return False
            attempts.append(now)
            return True


REGISTER_LIMITER = _RegistrationRateLimiter(max_requests=10, window_sec=60)


# --- Quiz State ---

def _levenshtein(s1: str, s2: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr_row = [i + 1]
        for j, c2 in enumerate(s2):
            cost = 0 if c1 == c2 else 1
            curr_row.append(min(
                curr_row[j] + 1,
                prev_row[j + 1] + 1,
                prev_row[j] + cost,
            ))
        prev_row = curr_row
    return prev_row[-1]


# Bot suspicion thresholds
BOT_SCORE_THRESHOLD = 5  # accumulated suspicion points before flagging
BOT_FAST_ANSWER_SEC = 0.3   # answers faster than this are suspicious
BOT_FAST_ANSWER_POINTS = 2  # points per ultra-fast answer
BOT_FAST_STREAK_POINTS = 1  # additional point per consecutive fast answer


TEAM_NAMES_DEFAULT: list[str] = ["Modri", "Cerveni", "Zeleni", "Zluti", "Fialovi", "Oranzovi"]
TEAM_COLORS: list[str] = ["#1d4ed8", "#dc2626", "#16a34a", "#ca8a04", "#7c3aed", "#ea580c"]


@dataclass
class Player:
    name: str
    secret: str = ""
    score: int = 0
    user_id: int | None = None
    avatar_id: int = 1
    streak: int = 0
    max_streak: int = 0
    last_seen: float = field(default_factory=time.time)
    team: str | None = None
    # Anti-bot fields
    bot_score: int = 0                                    # accumulated suspicion
    answer_times: list = field(default_factory=list)      # per-question elapsed seconds
    fast_answer_streak: int = 0                           # consecutive ultra-fast answers


class QuizState:
    def __init__(self, questions: list[dict[str, Any]]) -> None:
        self._lock = threading.Lock()
        self.questions = questions
        self.players: dict[str, Player] = {}
        self.phase = "lobby"  # lobby | question | reveal | finished
        self.current_index = -1
        self.question_duration_sec = QUESTION_DURATION_SEC
        self.reveal_duration_sec = REVEAL_DURATION_SEC
        self.question_started_at = 0.0
        self.reveal_started_at = 0.0
        self.answers: dict[str, dict[str, Any]] = {}
        self.scored_question_index: int | None = None
        self._active_bank: str | None = None
        self._class_id: int | None = None
        self.team_mode: bool = False
        self.num_teams: int = 2
        self.team_names: list[str] = TEAM_NAMES_DEFAULT[:2]

    def reload_questions(self, questions: list[dict[str, Any]], bank_name: str | None = None) -> None:
        with self._lock:
            self.questions = questions
            self._active_bank = bank_name
            self.phase = "lobby"
            self.current_index = -1
            self.question_started_at = 0.0
            self.reveal_started_at = 0.0
            self.answers = {}
            self.scored_question_index = None
            for player in self.players.values():
                player.score = 0
                player.streak = 0
                player.max_streak = 0
                player.bot_score = 0
                player.answer_times = []
                player.fast_answer_streak = 0
                player.team = None

    def set_timing(self, question_sec: int | None = None, reveal_sec: int | None = None) -> None:
        with self._lock:
            if question_sec is not None:
                self.question_duration_sec = max(5, min(120, question_sec))
            if reveal_sec is not None:
                self.reveal_duration_sec = max(2, min(30, reveal_sec))

    def set_team_mode(self, enabled: bool, num_teams: int = 2, team_names: list[str] | None = None) -> None:
        with self._lock:
            self.team_mode = enabled
            if enabled:
                self.num_teams = max(2, min(6, num_teams))
                if team_names and len(team_names) >= self.num_teams:
                    self.team_names = team_names[:self.num_teams]
                else:
                    self.team_names = TEAM_NAMES_DEFAULT[:self.num_teams]
                # Reassign existing players round-robin
                for i, (pid, player) in enumerate(self.players.items()):
                    player.team = self.team_names[i % self.num_teams]
            else:
                self.team_names = []
                for player in self.players.values():
                    player.team = None

    def register_player(self, name: str, user_id: int | None = None) -> dict[str, Any]:
        clean = " ".join(name.strip().split())[:24]
        if not clean:
            raise ValueError("Jmeno je povinne")
        if not _is_nickname_clean(clean):
            raise ValueError("Prezdivka obsahuje nevhodne slovo")
        # Fetch avatar_id from user profile if logged in
        avatar_id = 0  # 0 means random (client picks)
        if user_id is not None:
            user_data = user_db.get_user(user_id)
            if user_data and user_data.get("avatar_id"):
                avatar_id = user_data["avatar_id"]
        with self._lock:
            player_id = uuid.uuid4().hex[:10]
            player_secret = uuid.uuid4().hex
            assigned_team: str | None = None
            if self.team_mode and self.team_names:
                # Round-robin assignment for balance
                player_count = len(self.players)
                team_idx = player_count % len(self.team_names)
                assigned_team = self.team_names[team_idx]
            self.players[player_id] = Player(name=clean, secret=player_secret, user_id=user_id, avatar_id=avatar_id, team=assigned_team)
        # Notify WS clients about new player
        _ws_notify()
        result: dict[str, Any] = {"player_id": player_id, "player_secret": player_secret, "name": clean, "avatar_id": avatar_id}
        if assigned_team is not None:
            result["team"] = assigned_team
        return result

    def verify_player(self, player_id: str, player_secret: str) -> bool:
        """Verify player identity using secret token."""
        with self._lock:
            player = self.players.get(player_id)
            if not player:
                return False
            return player.secret == player_secret

    def kick_player(self, player_id: str) -> dict[str, Any]:
        """Remove a player from the game (host action)."""
        with self._lock:
            if player_id not in self.players:
                raise ValueError("Neznamy player_id")
            del self.players[player_id]
            self.answers.pop(player_id, None)
        _ws_notify()
        return {"ok": True}

    def host_action(self, action: str) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._sync_timers_locked(now)
            if action == "start":
                if not self.questions:
                    raise ValueError("Quiz nema otazky")
                self.phase = "question"
                self.current_index = 0
                self.question_started_at = now
                self.reveal_started_at = 0.0
                self.answers = {}
                self.scored_question_index = None
            elif action == "reveal":
                if self.phase != "question":
                    raise ValueError("Reveal je mozne jen ve fazi question")
                self._apply_scoring()
                self.phase = "reveal"
                self.reveal_started_at = now
            elif action == "next":
                if self.phase == "question":
                    self._apply_scoring()
                self._advance_to_next_question_locked(now)
            elif action == "reset":
                self.phase = "lobby"
                self.current_index = -1
                self.question_started_at = 0.0
                self.reveal_started_at = 0.0
                self.answers = {}
                self.scored_question_index = None
                for player in self.players.values():
                    player.score = 0
                    player.streak = 0
                    player.max_streak = 0
                    player.bot_score = 0
                    player.answer_times = []
                    player.fast_answer_streak = 0
            elif action == "save_history":
                if self.phase == "finished" or len(self.players) > 0:
                    record = save_game_history(self)
                    return {"ok": True, "record": record}
                raise ValueError("Zadni hraci nebo hra neskoncila")
            else:
                raise ValueError("Neznamy action")
            # Notify WS clients about state change
            _ws_notify()
            return {"ok": True}

    def submit_answer(self, player_id: str, player_secret: str, choice: Any) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._sync_timers_locked(now)
            player = self.players.get(player_id)
            if not player:
                raise ValueError("Neznamy player_id")
            if player.secret != player_secret:
                raise ValueError("Neplatny player_secret")
            player.last_seen = now
            if self.phase != "question":
                raise ValueError("Odpoved lze odeslat jen ve fazi question")
            if player_id in self.answers:
                raise ValueError("Odpoved uz byla odeslana")

            question = self.questions[self.current_index]
            q_type = question.get("type", "choice")

            # Validate answer based on question type
            if q_type in ("choice", "truefalse"):
                options = question.get("options", [])
                if not isinstance(choice, int) or choice < 0 or choice >= len(options):
                    raise ValueError("Neplatna volba")
            elif q_type == "multiselect":
                options = question.get("options", [])
                if not isinstance(choice, list):
                    raise ValueError("Multiselect vyzaduje seznam indexu")
                for c in choice:
                    if not isinstance(c, int) or c < 0 or c >= len(options):
                        raise ValueError("Neplatny index v multiselect")
                choice = sorted(set(choice))  # Deduplicate and sort
            elif q_type == "ordering":
                items = question.get("items", [])
                if not isinstance(choice, list) or len(choice) != len(items):
                    raise ValueError("Ordering vyzaduje permutaci vsech polozek")
                if sorted(choice) != list(range(len(items))):
                    raise ValueError("Neplatna permutace")
            elif q_type == "openended":
                if not isinstance(choice, str) or not choice.strip():
                    raise ValueError("Odpoved nesmi byt prazdna")
                choice = choice.strip()[:200]

            elif q_type == "poll":
                options = question.get("options", [])
                if not isinstance(choice, int) or choice < 0 or choice >= len(options):
                    raise ValueError("Neplatna volba")

            elif q_type == "wordcloud":
                if not isinstance(choice, str) or not choice.strip():
                    raise ValueError("Odpoved nesmi byt prazdna")
                choice = choice.strip()[:100]

            elif q_type == "slide":
                raise ValueError("Slide neprijima odpovedi")

            elapsed = max(0.0, now - self.question_started_at) if self.question_started_at else 0.0
            self.answers[player_id] = {
                "choice": choice,
                "time": now,
            }

            # --- Anti-bot: timing heuristics ---
            player.answer_times.append(elapsed)
            if elapsed < BOT_FAST_ANSWER_SEC:
                player.fast_answer_streak += 1
                player.bot_score += BOT_FAST_ANSWER_POINTS
                if player.fast_answer_streak >= 2:
                    player.bot_score += BOT_FAST_STREAK_POINTS
            else:
                player.fast_answer_streak = 0

        # Notify WS clients about new answer
        _ws_notify()
        return {"ok": True}

    def _apply_scoring(self) -> None:
        if self.scored_question_index == self.current_index:
            return
        if self.current_index < 0:
            return

        question = self.questions[self.current_index]
        q_type = question.get("type", "choice")

        # No scoring for engagement-only types
        if q_type in ("poll", "wordcloud", "slide"):
            self.scored_question_index = self.current_index
            return

        started = self.question_started_at or time.time()

        # Track who answered for streak reset
        answered_pids = set(self.answers.keys())

        for player_id, answer in self.answers.items():
            points = self._calculate_points(question, q_type, answer, started)
            is_correct = points > 0
            player = self.players[player_id]

            if is_correct:
                player.streak += 1
                player.max_streak = max(player.max_streak, player.streak)
                # Streak multiplier
                if player.streak >= 5:
                    multiplier = 1.5
                elif player.streak >= 4:
                    multiplier = 1.4
                elif player.streak >= 3:
                    multiplier = 1.2
                else:
                    multiplier = 1.0
                points = int(points * multiplier)
            else:
                player.streak = 0

            player.score += points

        # Reset streak for players who didn't answer
        for pid, player in self.players.items():
            if pid not in answered_pids:
                player.streak = 0

        self.scored_question_index = self.current_index

    @staticmethod
    def _calculate_points(question: dict, q_type: str, answer: dict, started: float) -> int:
        """Calculate raw points for an answer (before streak multiplier)."""
        elapsed = max(0.0, answer["time"] - started)
        speed_bonus = max(0, 40 - int(elapsed))

        if q_type in ("choice", "truefalse"):
            correct = question["correct_index"]
            if answer["choice"] == correct:
                return 600 + speed_bonus * 10
            return 0

        if q_type == "multiselect":
            selected = set(answer["choice"])
            correct = set(question.get("correct_indices", []))
            if selected == correct:
                return 600 + speed_bonus * 10  # Perfect match
            if selected.issubset(correct) and len(selected) > 0:
                # Partial credit (only correct selections, no wrong ones)
                ratio = len(selected) / len(correct)
                return int(600 * ratio)
            return 0  # Any wrong selection = 0

        if q_type == "ordering":
            items = question.get("items", [])
            player_order = answer["choice"]
            correct_order = list(range(len(items)))
            if player_order == correct_order:
                return 600 + speed_bonus * 10  # Perfect
            # Partial credit via inversion count
            n = len(items)
            inversions = 0
            for i in range(n):
                for j in range(i + 1, n):
                    if player_order[i] > player_order[j]:
                        inversions += 1
            max_inversions = n * (n - 1) // 2
            ratio = max(0.0, 1.0 - inversions / max_inversions) if max_inversions > 0 else 1.0
            if ratio >= 1.0:
                return 600 + speed_bonus * 10
            return int(600 * ratio)

        if q_type == "openended":
            player_answer = str(answer["choice"]).strip().lower()
            accepted = question.get("accepted_answers", [])
            case_sensitive = question.get("case_sensitive", False)
            for acc in accepted:
                target = acc.strip() if case_sensitive else acc.strip().lower()
                compare = answer["choice"].strip() if case_sensitive else player_answer
                if compare == target:
                    return 600 + speed_bonus * 10
                # Fuzzy match: allow small edit distance for longer answers
                max_dist = 0
                if len(target) > 8:
                    max_dist = 2
                elif len(target) > 4:
                    max_dist = 1
                if max_dist > 0 and _levenshtein(compare, target) <= max_dist:
                    return 600 + speed_bonus * 10
            return 0

        return 0

    def _advance_to_next_question_locked(self, now: float) -> None:
        if self.current_index + 1 >= len(self.questions):
            self.phase = "finished"
            self.reveal_started_at = 0.0
            # Auto-save game results
            try:
                save_game_history(self, class_id=self._class_id)
            except Exception:
                pass  # best-effort
            return
        self.current_index += 1
        self.phase = "question"
        self.question_started_at = now
        self.reveal_started_at = 0.0
        self.answers = {}
        self.scored_question_index = None

    def _sync_timers_locked(self, now: float) -> None:
        for _ in range(len(self.questions) + 2):
            if self.phase == "question" and self.question_started_at > 0:
                elapsed = now - self.question_started_at
                q_type = (
                    self.questions[self.current_index].get("type", "choice")
                    if self.current_index >= 0
                    else "choice"
                )
                # Slides have no answers — disable early advance when all answered
                is_slide = q_type == "slide"
                everyone_answered = (
                    not is_slide
                    and len(self.players) > 0
                    and len(self.answers) >= len(self.players)
                )
                if everyone_answered or elapsed >= self.question_duration_sec:
                    self._apply_scoring()
                    self.phase = "reveal"
                    if everyone_answered:
                        self.reveal_started_at = now
                    else:
                        self.reveal_started_at = self.question_started_at + self.question_duration_sec
                    continue
            if self.phase == "reveal" and self.reveal_started_at > 0:
                elapsed = now - self.reveal_started_at
                if elapsed >= self.reveal_duration_sec:
                    self._advance_to_next_question_locked(self.reveal_started_at + self.reveal_duration_sec)
                    continue
            break

    def _phase_time_left_locked(self, now: float) -> int | None:
        if self.phase == "question" and self.question_started_at > 0:
            left = self.question_duration_sec - (now - self.question_started_at)
            return max(0, int(math.ceil(left)))
        if self.phase == "reveal" and self.reveal_started_at > 0:
            left = self.reveal_duration_sec - (now - self.reveal_started_at)
            return max(0, int(math.ceil(left)))
        return None

    def _team_scores_locked(self) -> dict[str, int]:
        """Compute sum of scores per team. Must be called with self._lock held."""
        scores: dict[str, int] = {name: 0 for name in self.team_names}
        for player in self.players.values():
            if player.team and player.team in scores:
                scores[player.team] += player.score
        return scores

    def _ranked_players(self, host_view: bool = False) -> list[dict[str, Any]]:
        ranking = sorted(
            (
                {
                    "player_id": pid,
                    "name": player.name,
                    "score": player.score,
                    "streak": player.streak,
                    "avatar_id": player.avatar_id,
                    **({"bot_score": player.bot_score, "is_suspected_bot": player.bot_score >= BOT_SCORE_THRESHOLD} if host_view else {}),
                    **(({"team": player.team} if player.team else {})),
                }
                for pid, player in self.players.items()
            ),
            key=lambda row: (-row["score"], row["name"].lower()),
        )
        for i, row in enumerate(ranking, start=1):
            row["rank"] = i
        # Strip player_id from non-host views for privacy
        if not host_view:
            for row in ranking:
                row.pop("player_id", None)
        return ranking

    def _ranked_players_with_ids(self) -> list[dict[str, Any]]:
        """Ranked players including player_id for DB mapping. Internal use only."""
        ranking = sorted(
            (
                {
                    "name": player.name,
                    "score": player.score,
                    "player_id": pid,
                }
                for pid, player in self.players.items()
            ),
            key=lambda row: (-row["score"], row["name"].lower()),
        )
        for i, row in enumerate(ranking, start=1):
            row["rank"] = i
        return ranking

    def _get_user_map(self) -> dict[str, int]:
        """Get player_id -> user_id mapping for registered players."""
        return {
            pid: player.user_id
            for pid, player in self.players.items()
            if player.user_id is not None
        }

    def _vote_counts(self) -> list[int]:
        if self.current_index < 0:
            return []
        question = self.questions[self.current_index]
        q_type = question.get("type", "choice")

        if q_type in ("choice", "truefalse", "poll"):
            options_len = len(question.get("options", []))
            counts = [0] * options_len
            for ans in self.answers.values():
                idx = ans["choice"]
                if isinstance(idx, int) and 0 <= idx < options_len:
                    counts[idx] += 1
            return counts

        if q_type == "multiselect":
            options_len = len(question.get("options", []))
            counts = [0] * options_len
            for ans in self.answers.values():
                for idx in ans["choice"]:
                    if isinstance(idx, int) and 0 <= idx < options_len:
                        counts[idx] += 1
            return counts

        # ordering, openended, wordcloud, slide don't have meaningful vote counts
        return []

    def public_state(self, player_id: str | None = None, host_view: bool = False) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            self._sync_timers_locked(now)
            ranked = self._ranked_players(host_view=host_view)
            suspected_bots = [
                {"player_id": p["player_id"], "name": p["name"], "bot_score": p["bot_score"]}
                for p in ranked
                if host_view and p.get("is_suspected_bot")
            ] if host_view else []
            response: dict[str, Any] = {
                "phase": self.phase,
                "current_index": self.current_index,
                "total_questions": len(self.questions),
                "players": ranked,
                "answer_count": len(self.answers),
                "total_players": len(self.players),
                "vote_counts": self._vote_counts(),
                "server_time": now,
                "question_started_at": self.question_started_at,
                "reveal_started_at": self.reveal_started_at,
                "question_duration_sec": self.question_duration_sec,
                "reveal_duration_sec": self.reveal_duration_sec,
                "phase_time_left": self._phase_time_left_locked(now),
                "auto_advance": True,
                "active_bank": self._active_bank,
                "suspected_bots": suspected_bots,
                "suspected_bot_count": len(suspected_bots),
                "team_mode": self.team_mode,
                "team_names": self.team_names,
                "team_colors": TEAM_COLORS[:len(self.team_names)] if self.team_names else [],
            }
            if self.team_mode and self.team_names:
                response["team_scores"] = self._team_scores_locked()

            if player_id and player_id in self.players:
                p = self.players[player_id]
                me_data: dict[str, Any] = {
                    "name": p.name,
                    "score": p.score,
                    "streak": p.streak,
                    "max_streak": p.max_streak,
                    "avatar_id": p.avatar_id,
                }
                if p.team is not None:
                    me_data["team"] = p.team
                    # Include team color
                    try:
                        ti = self.team_names.index(p.team)
                        me_data["team_color"] = TEAM_COLORS[ti]
                    except (ValueError, IndexError):
                        me_data["team_color"] = "#64748b"
                    # Include team score
                    ts = self._team_scores_locked()
                    me_data["team_score"] = ts.get(p.team, 0)
                response["me"] = me_data
                if player_id in self.answers:
                    response["my_choice"] = self.answers[player_id]["choice"]

            if self.current_index >= 0:
                q = self.questions[self.current_index]
                q_type = q.get("type", "choice")
                q_public: dict[str, Any] = {
                    "id": q["id"],
                    "prompt": q["prompt"],
                    "type": q_type,
                }

                # Optional image
                if q.get("image_url"):
                    q_public["image_url"] = q["image_url"]

                # Type-specific fields
                if q_type in ("choice", "truefalse", "multiselect", "poll"):
                    q_public["options"] = q["options"]
                if q_type == "ordering":
                    # Send items in shuffled order during question phase
                    q_public["items"] = q["items"]
                if q_type == "openended":
                    q_public["max_length"] = 200
                if q_type == "slide":
                    q_public["body"] = q.get("body", "")
                if q_type == "wordcloud":
                    q_public["max_length"] = 100

                # Wordcloud: always include aggregated words for host (and on reveal for players)
                if q_type == "wordcloud" and (host_view or self.phase in {"reveal", "finished"}):
                    word_counts: dict[str, int] = {}
                    for ans in self.answers.values():
                        w = str(ans["choice"]).strip().lower()
                        if w:
                            word_counts[w] = word_counts.get(w, 0) + 1
                    q_public["word_counts"] = word_counts

                # Poll: always show vote counts (even during question phase, live)
                if q_type == "poll":
                    q_public["vote_counts"] = self._vote_counts()

                # Reveal correct answers
                if host_view or self.phase in {"reveal", "finished"}:
                    if q_type in ("choice", "truefalse"):
                        q_public["correct_index"] = q["correct_index"]
                    elif q_type == "multiselect":
                        q_public["correct_indices"] = q.get("correct_indices", [])
                    elif q_type == "ordering":
                        q_public["correct_order"] = list(range(len(q.get("items", []))))
                    elif q_type == "openended":
                        q_public["accepted_answers"] = q.get("accepted_answers", [])
                    if q_type not in ("poll", "wordcloud", "slide"):
                        q_public["explanation"] = q.get("explanation", "")

                response["question"] = q_public

            return response


def list_audio_tracks() -> dict[str, list[dict[str, str]]]:
    allowed = {".mp3", ".ogg", ".wav", ".m4a", ".aac"}
    loops: list[dict[str, str]] = []
    stingers: list[dict[str, str]] = []
    all_tracks: list[dict[str, str]] = []

    if not AUDIO_DIR.exists():
        return {"all": [], "loops": [], "stingers": []}

    for file_path in sorted(AUDIO_DIR.iterdir()):
        if not file_path.is_file():
            continue
        if file_path.suffix.lower() not in allowed:
            continue

        track = {
            "name": file_path.name,
            "url": f"/static/audio/{file_path.name}",
        }
        all_tracks.append(track)

        lower = file_path.name.lower()
        if any(key in lower for key in ("stinger", "hit", "reveal", "correct", "win", "lock", "end", "ding")):
            stingers.append(track)
        else:
            loops.append(track)

    if not loops and all_tracks:
        loops = all_tracks.copy()
    if not stingers and all_tracks:
        stingers = all_tracks.copy()

    return {"all": all_tracks, "loops": loops, "stingers": stingers}


QUIZ = QuizState(load_default_questions())


# ---------------------------------------------------------------------------
# Self-Paced / Homework sessions
# ---------------------------------------------------------------------------

def _score_selfpaced_answer(question: dict, choice: Any) -> tuple[bool, int]:
    """Return (is_correct, points) for a self-paced answer. No time bonus."""
    q_type = question.get("type", "choice")

    if q_type in ("choice", "truefalse"):
        correct = question.get("correct_index")
        if choice == correct:
            return True, 600
        return False, 0

    if q_type == "multiselect":
        selected = set(choice) if isinstance(choice, list) else set()
        correct = set(question.get("correct_indices", []))
        if selected == correct:
            return True, 600
        if selected and selected.issubset(correct):
            ratio = len(selected) / len(correct)
            return False, int(600 * ratio)
        return False, 0

    if q_type == "ordering":
        items = question.get("items", [])
        correct_order = list(range(len(items)))
        if isinstance(choice, list) and choice == correct_order:
            return True, 600
        if isinstance(choice, list) and len(choice) == len(items):
            n = len(items)
            inversions = sum(
                1 for i in range(n) for j in range(i + 1, n) if choice[i] > choice[j]
            )
            max_inv = n * (n - 1) // 2
            ratio = max(0.0, 1.0 - inversions / max_inv) if max_inv > 0 else 1.0
            pts = int(600 * ratio)
            return ratio >= 1.0, pts
        return False, 0

    if q_type == "openended":
        player_answer = str(choice).strip().lower()
        accepted = question.get("accepted_answers", [])
        case_sensitive = question.get("case_sensitive", False)
        for acc in accepted:
            target = acc.strip() if case_sensitive else acc.strip().lower()
            compare = str(choice).strip() if case_sensitive else player_answer
            if compare == target:
                return True, 600
            max_dist = 2 if len(target) > 8 else (1 if len(target) > 4 else 0)
            if max_dist > 0 and _levenshtein(compare, target) <= max_dist:
                return True, 600
        return False, 0

    return False, 0


def _public_question_for_selfpaced(question: dict, reveal: bool = False) -> dict:
    """Return the public view of a question for self-paced mode."""
    q_type = question.get("type", "choice")
    q_public: dict[str, Any] = {
        "id": question["id"],
        "prompt": question["prompt"],
        "type": q_type,
    }
    if q_type in ("choice", "truefalse", "multiselect"):
        q_public["options"] = question["options"]
    if q_type == "ordering":
        q_public["items"] = question["items"]
    if q_type == "openended":
        q_public["max_length"] = 200

    if reveal:
        if q_type in ("choice", "truefalse"):
            q_public["correct_index"] = question["correct_index"]
        elif q_type == "multiselect":
            q_public["correct_indices"] = question.get("correct_indices", [])
        elif q_type == "ordering":
            q_public["correct_order"] = list(range(len(question.get("items", []))))
        elif q_type == "openended":
            q_public["accepted_answers"] = question.get("accepted_answers", [])
        q_public["explanation"] = question.get("explanation", "")

    return q_public


def _ws_notify() -> None:
    """Notify all WebSocket clients about state change (fire and forget)."""
    try:
        WS_MANAGER.broadcast_state()
    except Exception:
        pass


def _ws_heartbeat_loop() -> None:
    """Background thread: ping all WS connections every 30s."""
    while True:
        time.sleep(30)
        try:
            WS_MANAGER.ping_all()
        except Exception:
            pass
SERVER_INFO: dict[str, Any] = {
    "bind_host": "0.0.0.0",
    "port": 8765,
    "host_urls": ["http://127.0.0.1:8765/host"],
    "play_urls": ["http://127.0.0.1:8765/play"],
    "loopback_only": False,
}

# Public base URL override (set via --base-url / QUIZ_BASE_URL).
# When non-empty, host_urls and play_urls in SERVER_INFO are replaced with
# URLs derived from this base, and the app reports the public domain instead
# of the detected LAN IP.  Expected format: "https://classrally.example.com"
QUIZ_BASE_URL: str = os.environ.get("QUIZ_BASE_URL", "").rstrip("/")

# Set of trusted reverse-proxy IP networks.  Only requests arriving from one
# of these addresses are allowed to override the client IP via X-Forwarded-For.
# Defaults include localhost and the RFC-1918 / Docker bridge ranges so that a
# local Caddy or nginx proxy works without extra configuration.
# Override via --trusted-proxies / QUIZ_TRUSTED_PROXIES (comma-separated CIDRs
# or plain IPs, e.g. "127.0.0.1,172.18.0.0/16").
_DEFAULT_TRUSTED_PROXY_CIDRS = [
    "127.0.0.0/8",       # loopback
    "::1/128",           # IPv6 loopback
    "10.0.0.0/8",        # RFC-1918
    "172.16.0.0/12",     # RFC-1918 / Docker default bridge range
    "192.168.0.0/16",    # RFC-1918
]
TRUSTED_PROXY_NETS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network(c) for c in _DEFAULT_TRUSTED_PROXY_CIDRS
]

AI_CONFIG: dict[str, Any] = {
    "base_url": os.environ.get("AI_BASE_URL", f"http://{os.environ.get('OLLAMA_HOST', 'localhost')}:{os.environ.get('OLLAMA_PORT', '11434')}"),
    "api_key": os.environ.get("AI_API_KEY", ""),
    "model": os.environ.get("AI_MODEL", os.environ.get("OLLAMA_MODEL", "gpt-oss:20b")),
}

# WebSocket connection manager
WS_MANAGER = WSConnectionManager()
WS_MANAGER.set_state_getter(lambda player_id=None, host_view=False: QUIZ.public_state(player_id=player_id, host_view=host_view))


_DOCKER_BRIDGE_NET = ipaddress.ip_network("172.16.0.0/12")


def _is_loopback_or_linklocal(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return ip.is_loopback or ip.is_link_local


def _is_docker_bridge(ip_str: str) -> bool:
    """Filter out Docker bridge network IPs (172.16.0.0/12)."""
    try:
        return ipaddress.ip_address(ip_str) in _DOCKER_BRIDGE_NET
    except ValueError:
        return False


def detect_lan_ipv4_candidates() -> list[str]:
    ips: list[str] = []

    def add(ip_str: str) -> None:
        if not ip_str:
            return
        if _is_loopback_or_linklocal(ip_str):
            return
        if ip_str not in ips:
            ips.append(ip_str)

    # Method 1: UDP socket trick (gets default-route IP)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("1.1.1.1", 80))
            add(sock.getsockname()[0])
    except OSError:
        pass

    # Method 2: Parse 'ip addr' output (Linux — works on remote servers without internet)
    try:
        result = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                # Format: "2: eth0    inet 192.168.1.10/24 ..."
                parts = line.split()
                for i, part in enumerate(parts):
                    if part == "inet" and i + 1 < len(parts):
                        addr = parts[i + 1].split("/")[0]
                        add(addr)
    except (OSError, subprocess.TimeoutExpired):
        pass

    # Method 3: hostname resolution fallback
    try:
        hostname = socket.gethostname()
        for result in socket.getaddrinfo(hostname, None, family=socket.AF_INET, type=socket.SOCK_STREAM):
            add(result[4][0])
    except OSError:
        pass

    return ips


def build_server_info(
    bind_host: str,
    port: int,
    external_ip: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    host = bind_host.strip() or "0.0.0.0"

    # --base-url takes highest priority: use the public domain directly.
    # This is the right setting when running behind Caddy/nginx with HTTPS.
    if base_url:
        base_url = base_url.rstrip("/")
        return {
            "bind_host": host,
            "port": port,
            "host_urls": [f"{base_url}/host"],
            "play_urls": [f"{base_url}/play"],
            "loopback_only": False,
        }

    # If external IP is provided (e.g. from Docker host), use it directly
    if external_ip:
        ips = [ip.strip() for ip in external_ip.split(",") if ip.strip()]
        ips = [ip for ip in ips if not _is_loopback_or_linklocal(ip)]
        if ips:
            return {
                "bind_host": host,
                "port": port,
                "host_urls": [f"http://{ip}:{port}/host" for ip in ips],
                "play_urls": [f"http://{ip}:{port}/play" for ip in ips],
                "loopback_only": False,
            }

    lan_ips = [ip for ip in detect_lan_ipv4_candidates() if not _is_docker_bridge(ip)]

    if host in {"0.0.0.0", "::"}:
        if lan_ips:
            base_hosts = lan_ips
            loopback_only = False
        else:
            base_hosts = ["127.0.0.1"]
            loopback_only = True
    elif host == "localhost":
        base_hosts = ["127.0.0.1"]
        loopback_only = True
    else:
        try:
            resolved = socket.gethostbyname(host)
            loopback_only = _is_loopback_or_linklocal(resolved)
        except OSError:
            loopback_only = False
        base_hosts = [host]

    host_urls = [f"http://{ip}:{port}/host" for ip in base_hosts]
    play_urls = [f"http://{ip}:{port}/play" for ip in base_hosts]
    return {
        "bind_host": host,
        "port": port,
        "host_urls": host_urls,
        "play_urls": play_urls,
        "loopback_only": loopback_only,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ClassRally/2.1"

    def _json_response(self, payload: dict[str, Any] | list, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text_response(self, payload: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> None:
        body = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self._text_response("Not found", status=404)
            return
        content = path.read_bytes()
        file_size = len(content)
        content_type, _ = mimetypes.guess_type(path.name)
        if not content_type:
            content_type = "application/octet-stream"
        ct_header = f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type

        # Support HTTP Range requests — browsers require this for audio seeking/streaming.
        range_header = self.headers.get("Range", "")
        if range_header.startswith("bytes="):
            try:
                range_spec = range_header[6:]
                start_str, _, end_str = range_spec.partition("-")
                start = int(start_str) if start_str else 0
                end = int(end_str) if end_str else file_size - 1
                end = min(end, file_size - 1)
                if start > end or start >= file_size:
                    self.send_response(416)  # Range Not Satisfiable
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.end_headers()
                    return
                chunk = content[start:end + 1]
                self.send_response(206)
                self.send_header("Content-Type", ct_header)
                self.send_header("Content-Length", str(len(chunk)))
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self.wfile.write(chunk)
                return
            except (ValueError, IndexError):
                pass  # Fall through to full response on malformed Range header

        self.send_response(200)
        self.send_header("Content-Type", ct_header)
        self.send_header("Content-Length", str(file_size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(content)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _get_admin_token(self) -> str | None:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return None

    def _get_client_ip(self) -> str:
        """Return the real client IP address.

        When the direct TCP peer is a trusted reverse proxy (Caddy, nginx, etc.)
        the leftmost entry in X-Forwarded-For is used instead, so that rate
        limiting applies to the actual end-user rather than the proxy.

        Trusting X-Forwarded-For from arbitrary peers would allow clients to
        spoof their IP and bypass rate limiting, so we only honour the header
        when the connecting address is in TRUSTED_PROXY_NETS.
        """
        peer_ip = self.client_address[0] if self.client_address else ""
        try:
            peer_addr = ipaddress.ip_address(peer_ip)
            peer_is_trusted = any(peer_addr in net for net in TRUSTED_PROXY_NETS)
        except ValueError:
            peer_is_trusted = False

        if peer_is_trusted:
            forwarded = self.headers.get("X-Forwarded-For", "").strip()
            if forwarded:
                # X-Forwarded-For may be a comma-separated list; take the leftmost
                # entry which is the original client as set by the first proxy.
                return forwarded.split(",")[0].strip()

        return peer_ip

    def _get_forwarded_proto(self) -> str:
        """Return the protocol seen by the client (http or https).

        Reads X-Forwarded-Proto set by the reverse proxy when the request
        arrives over HTTPS at the proxy but plain HTTP to us.
        """
        peer_ip = self.client_address[0] if self.client_address else ""
        try:
            peer_addr = ipaddress.ip_address(peer_ip)
            peer_is_trusted = any(peer_addr in net for net in TRUSTED_PROXY_NETS)
        except ValueError:
            peer_is_trusted = False

        if peer_is_trusted:
            proto = self.headers.get("X-Forwarded-Proto", "").strip().lower()
            if proto in {"http", "https"}:
                return proto

        return "http"

    def _get_forwarded_host(self) -> str:
        """Return the public hostname seen by the client.

        Prefers X-Forwarded-Host (set by the proxy) over the Host header.
        Only honoured when the request comes from a trusted proxy.
        """
        peer_ip = self.client_address[0] if self.client_address else ""
        try:
            peer_addr = ipaddress.ip_address(peer_ip)
            peer_is_trusted = any(peer_addr in net for net in TRUSTED_PROXY_NETS)
        except ValueError:
            peer_is_trusted = False

        if peer_is_trusted:
            fwd_host = self.headers.get("X-Forwarded-Host", "").strip()
            if fwd_host:
                return fwd_host

        return self.headers.get("Host", "").strip()

    def _get_user_token(self) -> str | None:
        """Extract user auth token from Authorization header or query."""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("UserToken "):
            return auth[10:]
        return None

    def _get_user_id_from_token(self) -> int | None:
        """Validate user token and return user_id or None."""
        token = self._get_user_token()
        if not token:
            return None
        return user_db.validate_session(token)

    def _require_user(self) -> int | None:
        """Require valid user token. Returns user_id or sends 401 and returns None."""
        uid = self._get_user_id_from_token()
        if uid is None:
            self._json_response({"error": "Prihlaseni vyzadovano"}, status=401)
        return uid

    def _require_admin(self) -> bool:
        """Require admin/teacher access. Accepts:
        1. Old AdminAuth session (Bearer token from /api/admin/login)
        2. UserToken from a teacher account
        3. Open access if no admin password set AND no teacher accounts exist
        """
        # Method 1: Legacy admin password session
        if ADMIN_AUTH.enabled:
            admin_tok = self._get_admin_token()
            if ADMIN_AUTH.validate_session(admin_tok):
                return True

        # Method 2: Teacher user account (UserToken header)
        uid = self._get_user_id_from_token()
        if uid is not None:
            user = user_db.get_user(uid)
            if user and user["role"] == "teacher":
                return True

        # Open access: no admin password set AND no teacher account registered yet.
        # Once a teacher registers, admin access requires login.
        if not ADMIN_AUTH.enabled and not user_db.teacher_exists():
            return True

        self._json_response({"error": "Unauthorized", "needs_auth": True}, status=401)
        return False

    def _require_teacher(self) -> int | None:
        """Require teacher user account. Returns user_id or sends 401 and returns None."""
        uid = self._get_user_id_from_token()
        if uid is not None:
            user = user_db.get_user(uid)
            if user and user["role"] == "teacher":
                return uid
        self._json_response({"error": "Pristup odepren — ucitelsky ucet vyzadovan", "needs_auth": True}, status=401)
        return None

    def _handle_ws_upgrade(self) -> None:
        """Handle WebSocket upgrade request."""
        client_key = self.headers.get("Sec-WebSocket-Key", "")
        if not client_key:
            self._text_response("Missing Sec-WebSocket-Key", status=400)
            return

        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        player_id = q.get("player_id", [None])[0]
        is_host = q.get("host", ["0"])[0] == "1"

        # Send 101 response
        response = ws_handshake_response(client_key)
        self.wfile.write(response)
        self.wfile.flush()

        # Hijack the socket
        conn_id = uuid.uuid4().hex[:12]
        raw_sock = self.request
        ws_conn = WSConnection(raw_sock, conn_id, player_id=player_id, is_host=is_host)
        WS_MANAGER.register(ws_conn)

        # Send initial state
        try:
            state = QUIZ.public_state(player_id=player_id, host_view=is_host)
            ws_conn.send_json(state)
        except Exception:
            pass

        # Run read loop in this thread (the HTTP handler thread)
        # This prevents the handler from closing the socket
        WS_MANAGER.read_loop(ws_conn)

    def _require_host_token(self) -> bool:
        """Verify host token from Authorization header."""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:] == HOST_TOKEN:
            return True
        self._json_response({"error": "Invalid host token"}, status=403)
        return False

    def do_GET(self) -> None:  # noqa: N802
        # WebSocket upgrade detection
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self._handle_ws_upgrade()
            return

        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._serve_file(STATIC_DIR / "landing.html")
            return

        if path == "/host":
            self._serve_file(STATIC_DIR / "host.html")
            return

        if path == "/play":
            self._serve_file(STATIC_DIR / "play.html")
            return

        if path == "/admin":
            self._serve_file(STATIC_DIR / "admin.html")
            return

        if path == "/profile":
            self._serve_file(STATIC_DIR / "profile.html")
            return

        if path.startswith("/static/"):
            relative = path.removeprefix("/static/")
            safe = (STATIC_DIR / relative).resolve()
            if not str(safe).startswith(str(STATIC_DIR.resolve())):
                self._text_response("Forbidden", status=403)
                return
            self._serve_file(safe)
            return

        if path == "/api/state":
            q = parse_qs(parsed.query)
            player_id = q.get("player_id", [None])[0]
            host_view = q.get("host", ["0"])[0] == "1"
            self._json_response(QUIZ.public_state(player_id=player_id, host_view=host_view))
            return

        if path == "/api/random-nickname":
            self._json_response({"nickname": _generate_random_nickname()})
            return

        if path == "/api/health":
            self._json_response({"ok": True, "service": "classrally", "version": "2.1"})
            return

        if path == "/api/network":
            self._json_response(SERVER_INFO)
            return

        if path == "/api/audio-tracks":
            self._json_response(list_audio_tracks())
            return

        if path == "/api/qr":
            q = parse_qs(parsed.query)
            url = q.get("url", [None])[0]
            if not url:
                # Default: first play URL
                urls = SERVER_INFO.get("play_urls", [])
                url = urls[0] if urls else f"http://127.0.0.1:{SERVER_INFO.get('port', 8765)}/play"
            try:
                svg = generate_qr_svg(url, module_size=8, margin=4)
            except Exception as e:
                self._text_response(f"QR generation error: {e}", status=500)
                return
            body = svg.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/host/token":
            # Require teacher access (UserToken with teacher role, or legacy admin session).
            # If no teacher account exists yet (first-run), allow open access.
            if not self._require_admin():
                return
            self._json_response({"host_token": HOST_TOKEN})
            return

        if path == "/api/setup-status":
            # Public endpoint: tells the frontend if initial setup is needed
            self._json_response({
                "setup_needed": not user_db.teacher_exists(),
            })
            return

        # --- Admin API (GET) ---
        if path == "/api/admin/auth-status":
            self._json_response({
                "auth_required": ADMIN_AUTH.enabled,
                "authenticated": ADMIN_AUTH.validate_session(self._get_admin_token()),
            })
            return

        if path == "/api/admin/banks":
            if not self._require_admin():
                return
            self._json_response(list_question_banks())
            return

        if path == "/api/admin/bank":
            if not self._require_admin():
                return
            q = parse_qs(parsed.query)
            filename = q.get("filename", [None])[0]
            if not filename:
                self._json_response({"error": "filename required"}, status=400)
                return
            try:
                questions = load_questions_from_file(filename)
                self._json_response({"filename": filename, "questions": questions})
            except Exception as e:
                self._json_response({"error": str(e)}, status=404)
            return

        if path == "/api/admin/history":
            if not self._require_admin():
                return
            self._json_response(list_game_history())
            return

        if path == "/api/admin/ollama/models":
            if not self._require_admin():
                return
            models = list_ai_models(AI_CONFIG["base_url"], AI_CONFIG["api_key"])
            self._json_response({"models": models, "config": AI_CONFIG})
            return

        if path == "/api/admin/ollama/config":
            if not self._require_admin():
                return
            self._json_response(AI_CONFIG)
            return

        # --- User Auth API (GET) ---
        if path == "/api/auth/profile":
            uid = self._require_user()
            if uid is None:
                return
            profile = user_db.get_user_profile(uid)
            if not profile:
                self._json_response({"error": "Uzivatel nenalezen"}, status=404)
                return
            self._json_response(profile)
            return

        # --- User management API (GET, teacher only) ---
        if path == "/api/users":
            tid = self._require_teacher()
            if tid is None:
                return
            users = user_db.list_all_users()
            self._json_response({"users": users})
            return

        # --- Classes API (GET) ---
        if path == "/api/classes":
            uid = self._require_user()
            if uid is None:
                return
            self._json_response(user_db.list_user_classes(uid))
            return

        if path.startswith("/api/classes/") and path.endswith("/members"):
            uid = self._require_user()
            if uid is None:
                return
            try:
                class_id = int(path.split("/")[3])
            except (IndexError, ValueError):
                self._json_response({"error": "Neplatne class_id"}, status=400)
                return
            self._json_response(user_db.get_class_members(class_id))
            return

        if path.startswith("/api/classes/") and path.endswith("/history"):
            uid = self._require_user()
            if uid is None:
                return
            try:
                class_id = int(path.split("/")[3])
            except (IndexError, ValueError):
                self._json_response({"error": "Neplatne class_id"}, status=400)
                return
            self._json_response(user_db.get_class_history(class_id))
            return

        if path.startswith("/api/classes/") and path.endswith("/progress"):
            uid = self._require_user()
            if uid is None:
                return
            try:
                class_id = int(path.split("/")[3])
            except (IndexError, ValueError):
                self._json_response({"error": "Neplatne class_id"}, status=400)
                return
            self._json_response(user_db.get_class_progress(class_id))
            return

        # --- Export endpoints ---

        if path == "/api/admin/history/export":
            if not self._require_admin():
                return
            q = parse_qs(parsed.query)
            game_id = q.get("game_id", [None])[0]
            if not game_id or not GAME_ID_RE.match(game_id):
                self._json_response({"error": "game_id required (12 hex chars)"}, status=400)
                return
            record_path = HISTORY_DIR / f"game_{game_id}.json"
            if not record_path.exists():
                self._json_response({"error": "Game not found"}, status=404)
                return
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
                csv_text = game_results_to_csv(record)
            except Exception as e:
                self._json_response({"error": str(e)}, status=500)
                return
            body = csv_text.encode("utf-8")
            out_filename = f"game_{game_id}_results.csv"
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{out_filename}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/admin/bank/export/csv":
            if not self._require_admin():
                return
            q = parse_qs(parsed.query)
            filename = q.get("filename", [None])[0]
            if not filename:
                self._json_response({"error": "filename required"}, status=400)
                return
            try:
                questions = load_questions_from_file(filename)
                csv_text = questions_to_csv(questions)
            except FileNotFoundError as e:
                self._json_response({"error": str(e)}, status=404)
                return
            except Exception as e:
                self._json_response({"error": str(e)}, status=500)
                return
            body = csv_text.encode("utf-8")
            stem = Path(filename).stem
            out_filename = f"{stem}.csv"
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{out_filename}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/admin/bank/export/json":
            if not self._require_admin():
                return
            q = parse_qs(parsed.query)
            filename = q.get("filename", [None])[0]
            if not filename:
                self._json_response({"error": "filename required"}, status=400)
                return
            try:
                questions = load_questions_from_file(filename)
            except FileNotFoundError as e:
                self._json_response({"error": str(e)}, status=404)
                return
            except Exception as e:
                self._json_response({"error": str(e)}, status=500)
                return
            body = json.dumps(questions, ensure_ascii=False, indent=2).encode("utf-8")
            stem = Path(filename).stem
            out_filename = f"{stem}.json"
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{out_filename}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # --- Self-Paced GET endpoints ---

        if path == "/api/selfpaced/state":
            q = parse_qs(parsed.query)
            session_id = q.get("session_id", [None])[0]
            player_id = q.get("player_id", [None])[0]
            if not session_id or not player_id:
                self._json_response({"error": "session_id and player_id required"}, status=400)
                return
            session = user_db.get_selfpaced_session(session_id)
            if not session:
                self._json_response({"error": "Session not found or expired"}, status=404)
                return
            progress = user_db.get_selfpaced_progress(session_id, player_id)
            if not progress:
                self._json_response({"error": "Player not registered in this session"}, status=404)
                return
            questions = session["questions"]
            answered_count = len(progress["answers"])
            finished = progress["finished_at"] is not None

            if finished or answered_count >= len(questions):
                summary = []
                for i, ans_rec in enumerate(progress["answers"]):
                    q_data = questions[i] if i < len(questions) else {}
                    summary.append({
                        "question": _public_question_for_selfpaced(q_data, reveal=True),
                        "choice": ans_rec.get("choice"),
                        "correct": ans_rec.get("correct", False),
                        "points": ans_rec.get("points", 0),
                    })
                self._json_response({
                    "status": "finished",
                    "player_name": progress["player_name"],
                    "score": progress["score"],
                    "total_questions": len(questions),
                    "summary": summary,
                    "bank_name": session["bank_name"],
                })
                return

            current_q = questions[answered_count]
            self._json_response({
                "status": "question",
                "question_index": answered_count,
                "total_questions": len(questions),
                "question": _public_question_for_selfpaced(current_q, reveal=False),
                "score": progress["score"],
                "player_name": progress["player_name"],
                "bank_name": session["bank_name"],
            })
            return

        if path == "/api/selfpaced/results":
            q = parse_qs(parsed.query)
            session_id = q.get("session_id", [None])[0]
            if not session_id:
                self._json_response({"error": "session_id required"}, status=400)
                return
            if not self._require_admin():
                return
            session = user_db.get_selfpaced_session(session_id)
            if not session:
                self._json_response({"error": "Session not found or expired"}, status=404)
                return
            results = user_db.list_selfpaced_results(session_id)
            self._json_response({
                "session_id": session_id,
                "bank_name": session["bank_name"],
                "total_questions": len(session["questions"]),
                "results": results,
            })
            return

        self._text_response("Not found", status=404)

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            data = self._read_json()

            if path == "/api/register":
                client_ip = self._get_client_ip()
                if not REGISTER_LIMITER.is_allowed(client_ip):
                    self._json_response(
                        {"error": "Prilis mnoho registraci, zkuste to pozdeji"}, status=429,
                    )
                    return
                name = str(data.get("name", ""))
                # Optional: link to user account if user_token provided
                user_id = None
                user_token = data.get("user_token") or self._get_user_token()
                if user_token:
                    user_id = user_db.validate_session(user_token)
                self._json_response(QUIZ.register_player(name, user_id=user_id))
                return

            if path == "/api/submit":
                player_id = str(data.get("player_id", ""))
                player_secret = str(data.get("player_secret", ""))
                choice = data.get("choice", -1)
                # For backward compat: if choice is a number string, convert to int
                if isinstance(choice, (int, float)) and not isinstance(choice, bool):
                    choice = int(choice)
                self._json_response(QUIZ.submit_answer(player_id, player_secret, choice))
                return

            if path == "/api/host/kick":
                if not self._require_host_token():
                    return
                player_id = str(data.get("player_id", ""))
                self._json_response(QUIZ.kick_player(player_id))
                return

            if path == "/api/host/action":
                if not self._require_host_token():
                    return
                action = str(data.get("action", ""))
                self._json_response(QUIZ.host_action(action))
                return

            # --- Admin API (POST) ---
            if path == "/api/admin/login":
                password = str(data.get("password", ""))
                client_ip = self._get_client_ip()
                token = ADMIN_AUTH.check_password(password, client_ip)
                if token:
                    self._json_response({"ok": True, "token": token})
                else:
                    self._json_response({"error": "Spatne heslo nebo prilis mnoho pokusu"}, status=401)
                return

            if path == "/api/admin/bank/save":
                if not self._require_admin():
                    return
                filename = str(data.get("filename", ""))
                questions = data.get("questions", [])
                if not filename:
                    self._json_response({"error": "filename required"}, status=400)
                    return
                try:
                    save_questions_to_file(filename, questions)
                except (ValueError, RuntimeError) as e:
                    self._json_response({"error": str(e)}, status=400)
                    return
                self._json_response({"ok": True, "filename": filename})
                return

            if path == "/api/admin/upload-image":
                if not self._require_admin():
                    return
                image_data_b64 = str(data.get("image", ""))
                filename_hint = str(data.get("filename", "image.png"))
                if not image_data_b64:
                    self._json_response({"error": "image data required"}, status=400)
                    return
                # Strip data-URI prefix if present
                if "," in image_data_b64 and image_data_b64.startswith("data:"):
                    image_data_b64 = image_data_b64.split(",", 1)[1]
                try:
                    image_bytes = base64.b64decode(image_data_b64)
                except Exception:
                    self._json_response({"error": "invalid base64 data"}, status=400)
                    return
                # Max 2MB
                if len(image_bytes) > 2 * 1024 * 1024:
                    self._json_response({"error": "Image too large (max 2MB)"}, status=400)
                    return
                # Validate extension
                ext = Path(filename_hint).suffix.lower()
                allowed_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
                if ext not in allowed_exts:
                    self._json_response({"error": f"Unsupported image format. Allowed: {', '.join(sorted(allowed_exts))}"}, status=400)
                    return
                # Generate unique filename
                UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
                unique_name = f"{uuid.uuid4().hex[:12]}{ext}"
                dest = UPLOADS_DIR / unique_name
                dest.write_bytes(image_bytes)
                url_path = f"/static/uploads/{unique_name}"
                self._json_response({"ok": True, "url": url_path})
                return

            if path == "/api/admin/bank/import/csv":
                if not self._require_admin():
                    return
                filename = str(data.get("filename", ""))
                csv_text = str(data.get("csv", ""))
                if not filename:
                    self._json_response({"error": "filename required"}, status=400)
                    return
                if not csv_text.strip():
                    self._json_response({"error": "csv content required"}, status=400)
                    return
                try:
                    questions = questions_from_csv(csv_text)
                    if not questions:
                        self._json_response({"error": "No valid questions found in CSV"}, status=400)
                        return
                    save_questions_to_file(filename, questions)
                except (ValueError, RuntimeError) as e:
                    self._json_response({"error": str(e)}, status=400)
                    return
                self._json_response({"ok": True, "filename": filename, "count": len(questions)})
                return

            if path == "/api/admin/bank/delete":
                if not self._require_admin():
                    return
                filename = str(data.get("filename", ""))
                if not filename:
                    self._json_response({"error": "filename required"}, status=400)
                    return
                delete_question_bank(filename)
                self._json_response({"ok": True})
                return

            if path == "/api/admin/bank/activate":
                if not self._require_admin():
                    return
                filename = str(data.get("filename", ""))
                if not filename:
                    self._json_response({"error": "filename required"}, status=400)
                    return
                try:
                    questions = load_questions_from_file(filename)
                    QUIZ.reload_questions(questions, bank_name=filename)
                    self._json_response({"ok": True, "count": len(questions)})
                except Exception as e:
                    self._json_response({"error": str(e)}, status=400)
                return

            if path == "/api/admin/timing":
                if not self._require_admin():
                    return
                q_sec = data.get("question_duration_sec")
                r_sec = data.get("reveal_duration_sec")
                QUIZ.set_timing(
                    question_sec=int(q_sec) if q_sec is not None else None,
                    reveal_sec=int(r_sec) if r_sec is not None else None,
                )
                self._json_response({"ok": True, "question_duration_sec": QUIZ.question_duration_sec, "reveal_duration_sec": QUIZ.reveal_duration_sec})
                return

            if path == "/api/admin/team-mode":
                if not self._require_admin():
                    return
                enabled = bool(data.get("enabled", False))
                num_teams = int(data.get("num_teams", 2))
                team_names_raw = data.get("team_names")
                team_names = [str(n) for n in team_names_raw] if isinstance(team_names_raw, list) else None
                QUIZ.set_team_mode(enabled, num_teams=num_teams, team_names=team_names)
                self._json_response({
                    "ok": True,
                    "team_mode": QUIZ.team_mode,
                    "num_teams": QUIZ.num_teams,
                    "team_names": QUIZ.team_names,
                    "team_colors": TEAM_COLORS[:len(QUIZ.team_names)],
                })
                return

            if path == "/api/admin/history/delete":
                if not self._require_admin():
                    return
                game_id = str(data.get("game_id", ""))
                if delete_game_history(game_id):
                    self._json_response({"ok": True})
                else:
                    self._json_response({"error": "Not found"}, status=404)
                return

            if path == "/api/admin/ollama/config":
                if not self._require_admin():
                    return
                if "base_url" in data:
                    AI_CONFIG["base_url"] = str(data["base_url"])
                elif "host" in data or "port" in data:
                    host = str(data.get("host", "localhost"))
                    port = int(data.get("port", 11434))
                    AI_CONFIG["base_url"] = f"http://{host}:{port}"
                if "api_key" in data:
                    AI_CONFIG["api_key"] = str(data["api_key"])
                if "model" in data:
                    AI_CONFIG["model"] = str(data["model"])
                self._json_response({"ok": True, "config": AI_CONFIG})
                return

            if path == "/api/admin/ai/generate":
                if not self._require_admin():
                    return
                topic = str(data.get("topic", ""))
                count = int(data.get("count", 5))
                model = str(data.get("model", AI_CONFIG["model"]))
                api_key = str(data.get("api_key", AI_CONFIG["api_key"]))
                language = str(data.get("language", "cs"))
                if "base_url" in data:
                    base_url = str(data["base_url"])
                elif "host" in data or "port" in data:
                    host = str(data.get("host", "localhost"))
                    port = int(data.get("port", 11434))
                    base_url = f"http://{host}:{port}"
                else:
                    base_url = AI_CONFIG["base_url"]
                if not topic:
                    self._json_response({"error": "topic required"}, status=400)
                    return
                try:
                    questions = generate_questions_ai(
                        topic=topic, count=count,
                        base_url=base_url, model=model,
                        api_key=api_key, language=language,
                    )
                    self._json_response({"ok": True, "questions": questions})
                except Exception as e:
                    self._json_response({"error": str(e)}, status=500)
                return

            # --- User Auth API (POST) ---
            if path == "/api/auth/register":
                nickname = str(data.get("nickname", ""))
                password = str(data.get("password", ""))
                role = str(data.get("role", "student"))
                avatar_id = data.get("avatar_id")
                if avatar_id is not None:
                    try:
                        avatar_id = int(avatar_id)
                    except (ValueError, TypeError):
                        avatar_id = None

                # Block teacher registration if a teacher already exists
                if role == "teacher" and user_db.teacher_exists():
                    self._json_response(
                        {"error": "Ucitelsky ucet jiz existuje. Registrujte se jako zak."},
                        status=403,
                    )
                    return

                try:
                    user = user_db.create_user(nickname, password, role, avatar_id=avatar_id)
                    token = user_db.create_session(user["id"])
                    self._json_response({"ok": True, "token": token, "user": user})
                except ValueError as e:
                    self._json_response({"error": str(e)}, status=400)
                return

            if path == "/api/auth/login":
                nickname = str(data.get("nickname", ""))
                password = str(data.get("password", ""))
                user = user_db.authenticate_user(nickname, password)
                if user:
                    token = user_db.create_session(user["id"])
                    self._json_response({"ok": True, "token": token, "user": user})
                else:
                    self._json_response({"error": "Neplatne prihlasovaci udaje"}, status=401)
                return

            if path == "/api/auth/logout":
                token = self._get_user_token()
                if token:
                    user_db.delete_session(token)
                self._json_response({"ok": True})
                return

            if path == "/api/auth/update-avatar":
                uid = self._require_user()
                if uid is None:
                    return
                avatar_id = data.get("avatar_id")
                if avatar_id is None:
                    self._json_response({"error": "avatar_id required"}, status=400)
                    return
                try:
                    avatar_id = int(avatar_id)
                except (ValueError, TypeError):
                    self._json_response({"error": "avatar_id must be integer"}, status=400)
                    return
                if avatar_id < 1 or avatar_id > 20:
                    self._json_response({"error": "avatar_id must be 1-20"}, status=400)
                    return
                user_db.update_avatar(uid, avatar_id)
                self._json_response({"ok": True, "avatar_id": avatar_id})
                return

            if path == "/api/auth/change-password":
                uid = self._require_user()
                if uid is None:
                    return
                old_password = str(data.get("old_password", ""))
                new_password = str(data.get("new_password", ""))
                try:
                    user_db.change_password(uid, old_password, new_password)
                    self._json_response({"ok": True})
                except ValueError as e:
                    self._json_response({"error": str(e)}, status=400)
                return

            # --- User management (teacher only) ---

            if path == "/api/users":
                tid = self._require_teacher()
                if tid is None:
                    return
                users = user_db.list_all_users()
                self._json_response({"users": users})
                return

            m = re.match(r"^/api/users/(\d+)/set-role$", path)
            if m:
                tid = self._require_teacher()
                if tid is None:
                    return
                target_id = int(m.group(1))
                new_role = str(data.get("role", ""))
                try:
                    user_db.set_user_role(target_id, new_role, tid)
                    self._json_response({"ok": True})
                except ValueError as e:
                    self._json_response({"error": str(e)}, status=400)
                return

            m = re.match(r"^/api/users/(\d+)/reset-password$", path)
            if m:
                tid = self._require_teacher()
                if tid is None:
                    return
                target_id = int(m.group(1))
                new_password = str(data.get("new_password", ""))
                try:
                    user_db.teacher_reset_password(target_id, new_password, tid)
                    self._json_response({"ok": True})
                except ValueError as e:
                    self._json_response({"error": str(e)}, status=400)
                return

            m = re.match(r"^/api/users/(\d+)/delete$", path)
            if m:
                tid = self._require_teacher()
                if tid is None:
                    return
                target_id = int(m.group(1))
                try:
                    user_db.delete_user_by_teacher(target_id, tid)
                    self._json_response({"ok": True})
                except ValueError as e:
                    self._json_response({"error": str(e)}, status=400)
                return

            # --- Classes API (POST) ---
            if path == "/api/classes/create":
                uid = self._require_user()
                if uid is None:
                    return
                name = str(data.get("name", ""))
                try:
                    cls = user_db.create_class(name, uid)
                    self._json_response({"ok": True, "class": cls})
                except ValueError as e:
                    self._json_response({"error": str(e)}, status=400)
                return

            if path == "/api/classes/join":
                uid = self._require_user()
                if uid is None:
                    return
                join_code = str(data.get("join_code", ""))
                try:
                    cls = user_db.join_class(join_code, uid)
                    self._json_response({"ok": True, "class": cls})
                except ValueError as e:
                    self._json_response({"error": str(e)}, status=400)
                return

            if path.startswith("/api/classes/") and path.endswith("/delete"):
                uid = self._require_user()
                if uid is None:
                    return
                try:
                    class_id = int(path.split("/")[3])
                except (IndexError, ValueError):
                    self._json_response({"error": "Neplatne class_id"}, status=400)
                    return
                if user_db.delete_class(class_id, uid):
                    self._json_response({"ok": True})
                else:
                    self._json_response({"error": "Trida nenalezena nebo nemaxte opravneni"}, status=404)
                return

            # --- Teacher management endpoints ---

            # POST /api/classes/{id}/members/{uid}/reset-password
            if "/members/" in path and path.endswith("/reset-password"):
                teacher_id = self._require_teacher()
                if teacher_id is None:
                    return
                try:
                    parts = path.split("/")
                    class_id = int(parts[3])
                    target_uid = int(parts[5])
                except (IndexError, ValueError):
                    self._json_response({"error": "Neplatna URL"}, status=400)
                    return
                new_password = str(data.get("new_password", ""))
                try:
                    user_db.reset_user_password(target_uid, new_password, teacher_id)
                    self._json_response({"ok": True})
                except ValueError as e:
                    self._json_response({"error": str(e)}, status=400)
                return

            # POST /api/classes/{id}/members/{uid}/kick
            if "/members/" in path and path.endswith("/kick"):
                teacher_id = self._require_teacher()
                if teacher_id is None:
                    return
                try:
                    parts = path.split("/")
                    class_id = int(parts[3])
                    target_uid = int(parts[5])
                except (IndexError, ValueError):
                    self._json_response({"error": "Neplatna URL"}, status=400)
                    return
                try:
                    user_db.kick_class_member(class_id, target_uid, teacher_id)
                    self._json_response({"ok": True})
                except ValueError as e:
                    self._json_response({"error": str(e)}, status=400)
                return

            # POST /api/classes/{id}/members/{uid}/delete
            if "/members/" in path and path.endswith("/delete-account"):
                teacher_id = self._require_teacher()
                if teacher_id is None:
                    return
                try:
                    parts = path.split("/")
                    target_uid = int(parts[5])
                except (IndexError, ValueError):
                    self._json_response({"error": "Neplatna URL"}, status=400)
                    return
                try:
                    user_db.delete_user(target_uid, teacher_id)
                    self._json_response({"ok": True})
                except ValueError as e:
                    self._json_response({"error": str(e)}, status=400)
                return

            # --- Self-Paced POST endpoints ---

            if path == "/api/selfpaced/start":
                if not self._require_admin():
                    return
                filename = str(data.get("filename", ""))
                if not filename:
                    self._json_response({"error": "filename required"}, status=400)
                    return
                try:
                    questions = load_questions_from_file(filename)
                except FileNotFoundError:
                    self._json_response({"error": f"Question bank not found: {filename}"}, status=404)
                    return
                except Exception as e:
                    self._json_response({"error": str(e)}, status=400)
                    return
                session_id = user_db.create_selfpaced_session(filename, questions)
                self._json_response({
                    "ok": True,
                    "session_id": session_id,
                    "total_questions": len(questions),
                    "bank_name": filename,
                })
                return

            if path == "/api/selfpaced/join":
                session_id = str(data.get("session_id", ""))
                name = str(data.get("name", "")).strip()[:24]
                if not session_id:
                    self._json_response({"error": "session_id required"}, status=400)
                    return
                if not name:
                    self._json_response({"error": "name required"}, status=400)
                    return
                if not _is_nickname_clean(name):
                    self._json_response({"error": "Prezdivka obsahuje nevhodne slovo"}, status=400)
                    return
                session = user_db.get_selfpaced_session(session_id)
                if not session:
                    self._json_response({"error": "Session not found or expired"}, status=404)
                    return
                # Get optional user_id from token
                user_id = self._get_user_id_from_token()
                # Generate a stable player_id for this student in this session
                player_id = uuid.uuid4().hex[:10]
                player_secret = uuid.uuid4().hex
                user_db.upsert_selfpaced_progress(
                    session_id=session_id,
                    player_id=player_id,
                    player_name=name,
                    user_id=user_id,
                    score=0,
                    total=len(session["questions"]),
                    answers=[],
                    finished=False,
                )
                self._json_response({
                    "ok": True,
                    "player_id": player_id,
                    "player_secret": player_secret,
                    "name": name,
                    "total_questions": len(session["questions"]),
                    "bank_name": session["bank_name"],
                })
                return

            if path == "/api/selfpaced/answer":
                session_id = str(data.get("session_id", ""))
                player_id = str(data.get("player_id", ""))
                choice = data.get("choice")
                if not session_id or not player_id:
                    self._json_response({"error": "session_id and player_id required"}, status=400)
                    return
                if choice is None:
                    self._json_response({"error": "choice required"}, status=400)
                    return

                session = user_db.get_selfpaced_session(session_id)
                if not session:
                    self._json_response({"error": "Session not found or expired"}, status=404)
                    return

                progress = user_db.get_selfpaced_progress(session_id, player_id)
                if not progress:
                    self._json_response({"error": "Player not found"}, status=404)
                    return

                if progress["finished_at"] is not None:
                    self._json_response({"error": "Session already finished"}, status=400)
                    return

                questions = session["questions"]
                answered_count = len(progress["answers"])
                if answered_count >= len(questions):
                    self._json_response({"error": "All questions already answered"}, status=400)
                    return

                question = questions[answered_count]
                q_type = question.get("type", "choice")

                # Validate and normalize choice
                if q_type in ("choice", "truefalse"):
                    if not isinstance(choice, int):
                        self._json_response({"error": "choice must be integer"}, status=400)
                        return
                    opts = question.get("options", [])
                    if choice < 0 or choice >= len(opts):
                        self._json_response({"error": "choice out of range"}, status=400)
                        return
                elif q_type == "multiselect":
                    if not isinstance(choice, list):
                        self._json_response({"error": "choice must be list"}, status=400)
                        return
                    choice = sorted(set(int(c) for c in choice))
                elif q_type == "ordering":
                    items = question.get("items", [])
                    if not isinstance(choice, list) or len(choice) != len(items):
                        self._json_response({"error": "choice must be permutation of all items"}, status=400)
                        return
                    if sorted(choice) != list(range(len(items))):
                        self._json_response({"error": "Invalid permutation"}, status=400)
                        return
                elif q_type == "openended":
                    if not isinstance(choice, str) or not choice.strip():
                        self._json_response({"error": "choice must be non-empty string"}, status=400)
                        return
                    choice = choice.strip()[:200]

                is_correct, points = _score_selfpaced_answer(question, choice)

                # Build new answers list
                new_answers = list(progress["answers"])
                new_answers.append({
                    "choice": choice,
                    "correct": is_correct,
                    "points": points,
                })
                new_score = progress["score"] + points
                finished = len(new_answers) >= len(questions)

                user_db.upsert_selfpaced_progress(
                    session_id=session_id,
                    player_id=player_id,
                    player_name=progress["player_name"],
                    user_id=progress["user_id"],
                    score=new_score,
                    total=len(questions),
                    answers=new_answers,
                    finished=finished,
                )

                # Build reveal info for this question
                reveal_q = _public_question_for_selfpaced(question, reveal=True)
                self._json_response({
                    "ok": True,
                    "correct": is_correct,
                    "points": points,
                    "score": new_score,
                    "question_index": answered_count,
                    "total_questions": len(questions),
                    "finished": finished,
                    "question": reveal_q,
                    "choice": choice,
                })
                return

            self._json_response({"error": "Not found"}, status=404)
        except ValueError as exc:
            self._json_response({"error": str(exc)}, status=400)
        except Exception as exc:
            self._json_response({"error": f"Server error: {exc}"}, status=500)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Local classroom quiz web")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--admin-password", default=os.environ.get("QUIZ_ADMIN_PASSWORD", ""),
                        help="Password for admin portal (empty = no auth)")
    parser.add_argument("--ollama-host", default=os.environ.get("OLLAMA_HOST", "localhost"),
                        help="[DEPRECATED] Use --ai-base-url instead")
    parser.add_argument("--ollama-port", type=int, default=int(os.environ.get("OLLAMA_PORT", "11434")),
                        help="[DEPRECATED] Use --ai-base-url instead")
    parser.add_argument("--ollama-model", default=os.environ.get("OLLAMA_MODEL", "gpt-oss:20b"),
                        help="[DEPRECATED] Use --ai-model instead")
    parser.add_argument("--ai-base-url", default=os.environ.get("AI_BASE_URL", ""),
                        help="Base URL for OpenAI-compatible AI API (env: AI_BASE_URL)")
    parser.add_argument("--ai-api-key", default=os.environ.get("AI_API_KEY", ""),
                        help="API key for AI API (env: AI_API_KEY)")
    parser.add_argument("--ai-model", default=os.environ.get("AI_MODEL", ""),
                        help="Model name for AI API (env: AI_MODEL)")
    parser.add_argument("--external-ip", default=os.environ.get("QUIZ_EXTERNAL_IP", ""),
                        help="External IP(s) for player URLs (comma-separated, e.g. for Docker)")
    parser.add_argument("--question-time", type=int, default=QUESTION_DURATION_SEC,
                        help="Seconds per question (default 20)")
    parser.add_argument("--reveal-time", type=int, default=REVEAL_DURATION_SEC,
                        help="Seconds for reveal phase (default 5)")
    parser.add_argument("--teacher-code", default=os.environ.get("QUIZ_TEACHER_CODE", ""),
                        help="[DEPRECATED] No longer required — anyone can register as teacher")
    parser.add_argument("--base-url", default=os.environ.get("QUIZ_BASE_URL", ""),
                        help="Public base URL when running behind a reverse proxy, e.g. "
                             "https://classrally.example.com  — overrides auto-detected LAN IP "
                             "for QR codes and player URLs  (env: QUIZ_BASE_URL)")
    parser.add_argument("--trusted-proxies",
                        default=os.environ.get("QUIZ_TRUSTED_PROXIES", ""),
                        help="Comma-separated list of trusted reverse-proxy CIDRs/IPs whose "
                             "X-Forwarded-For header is honoured for rate limiting "
                             "(default: loopback + RFC-1918 ranges)  (env: QUIZ_TRUSTED_PROXIES)")
    args = parser.parse_args()

    # Initialize SQLite database
    user_db.init_db()
    user_db.migrate_json_history(HISTORY_DIR)

    # --teacher-code is deprecated and ignored; anyone can register as teacher
    if args.teacher_code:
        print("WARNING: --teacher-code is deprecated and no longer has any effect. Teacher registration is open to all.")
    print("Teacher registration: open (no code required)")

    if args.admin_password:
        ADMIN_AUTH.set_password(args.admin_password)
        print("Admin portal: password protected")
    else:
        print("Admin portal: open (no password)")

    # New --ai-* args take precedence over legacy --ollama-* args
    if args.ai_base_url:
        AI_CONFIG["base_url"] = args.ai_base_url
    else:
        AI_CONFIG["base_url"] = f"http://{args.ollama_host}:{args.ollama_port}"
    if args.ai_api_key:
        AI_CONFIG["api_key"] = args.ai_api_key
    if args.ai_model:
        AI_CONFIG["model"] = args.ai_model
    elif args.ollama_model != "gpt-oss:20b":
        AI_CONFIG["model"] = args.ollama_model

    QUIZ.set_timing(question_sec=args.question_time, reveal_sec=args.reveal_time)

    # Configure trusted proxy networks for X-Forwarded-For handling
    global TRUSTED_PROXY_NETS
    if args.trusted_proxies:
        try:
            TRUSTED_PROXY_NETS = [
                ipaddress.ip_network(cidr.strip(), strict=False)
                for cidr in args.trusted_proxies.split(",")
                if cidr.strip()
            ]
            print(f"Trusted proxies: {', '.join(str(n) for n in TRUSTED_PROXY_NETS)}")
        except ValueError as e:
            print(f"WARNING: Invalid --trusted-proxies value: {e}. Using defaults.")
    else:
        print(f"Trusted proxies: {', '.join(str(n) for n in TRUSTED_PROXY_NETS)} (default RFC-1918)")

    # Configure public base URL (used for QR codes / printed URLs)
    global QUIZ_BASE_URL
    if args.base_url:
        QUIZ_BASE_URL = args.base_url.rstrip("/")
        print(f"Public base URL: {QUIZ_BASE_URL}")

    global SERVER_INFO
    SERVER_INFO = build_server_info(
        args.host, args.port,
        external_ip=args.external_ip or None,
        base_url=QUIZ_BASE_URL or None,
    )

    # Start WebSocket heartbeat thread
    ws_heartbeat = threading.Thread(target=_ws_heartbeat_loop, daemon=True)
    ws_heartbeat.start()

    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Quiz server bind: {SERVER_INFO['bind_host']}:{SERVER_INFO['port']}")
    for url in SERVER_INFO["host_urls"]:
        print(f"  Host screen: {url}")
    for url in SERVER_INFO["play_urls"]:
        print(f"  Player screen: {url}")
    for url in SERVER_INFO["host_urls"]:
        base = url.rsplit("/host", 1)[0]
        print(f"  Admin portal: {base}/admin")
    if SERVER_INFO["loopback_only"]:
        print("WARNING: Server bezi jen na localhostu, mobily se nepripoji.")
    print(f"  AI API: {AI_CONFIG['base_url']} model={AI_CONFIG['model']}")
    print()
    print(f"  *** HOST TOKEN: {HOST_TOKEN} ***")
    print(f"  (zadej na /host pro ovladani hry, nebo najdi v /admin > Nastaveni)")
    print()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
