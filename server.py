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
import hmac
import io
import ipaddress
import json
import math
import mimetypes
import os
import random
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
import bugreport
import db as user_db
import mailer
from ws import WSConnection, WSConnectionManager, ws_handshake_response

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
AUDIO_DIR = STATIC_DIR / "audio"
UPLOADS_DIR = STATIC_DIR / "uploads"
QUESTIONS_DIR = BASE_DIR / "questions"
HISTORY_DIR = Path(os.environ["QUIZ_HISTORY_DIR"]) if os.environ.get("QUIZ_HISTORY_DIR") else BASE_DIR / "history"
DEFAULT_QUESTIONS_FILE = "default.json"
APP_VERSION = "3.0"  # /api/health and the Server header
QUESTION_DURATION_SEC = 20
REVEAL_DURATION_SEC = 5
GET_READY_SEC = 3  # "Get ready" countdown before answers show (0 = off)
# Reading time: a long question (prompt + answers) gets extra seconds on top of
# the teacher's time per question, so slower readers are not out of time before
# they have read it. +1 s per READING_CHARS_PER_SEC characters above
# READING_BASE_CHARS, at most READING_BONUS_MAX_SEC. On by default, the teacher
# can switch it off in the settings.
READING_BASE_CHARS = 80
READING_CHARS_PER_SEC = 15
READING_BONUS_MAX_SEC = 30


def reading_bonus_sec(question: dict | None) -> int:
    """Extra seconds a question gets for reading its text (0 for short ones)."""
    if not isinstance(question, dict):
        return 0
    parts = [question.get("prompt", ""), question.get("body", "")]
    for key in ("options", "items"):
        values = question.get(key)
        if isinstance(values, list):
            parts.extend(values)
    chars = sum(len(str(p).strip()) for p in parts if p)
    extra = chars - READING_BASE_CHARS
    if extra <= 0:
        return 0
    return min(READING_BONUS_MAX_SEC, math.ceil(extra / READING_CHARS_PER_SEC))

# Atypical high port (avoids clashes on shared machines); override via QUIZ_PORT.
DEFAULT_PORT = int(os.environ.get("QUIZ_PORT", "48217"))

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
EXAM_ID_RE = GAME_ID_RE  # saved exams use the same 12-hex ids
ACTIVITY_RECENT_SEC = 15 * 60  # a room used this recently counts as busy (deploy check)
MIN_STUDENTS_FOR_DISCRIMINATION = 10  # below this the item-total correlation is noise


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


# Question banks: questions/*.json is the shared library. On multi-teacher
# servers each teacher also has private banks in questions/users/<id>/, which the
# API names "moje/<file>.json"; a teacher's edit of a shared bank is saved as a
# private copy, so nobody overwrites or deletes someone else's work.
PRIVATE_BANK_PREFIX = "moje/"


def _bank_path(filename: str, owner_id: int | None = None) -> tuple[Path, str]:
    """(path, canonical name) of a bank reference; private refs need the owner."""
    private = filename.startswith(PRIVATE_BANK_PREFIX)
    name = Path(filename[len(PRIVATE_BANK_PREFIX):] if private else filename).name
    if not name.endswith(".json"):
        name += ".json"
    if name.startswith("."):  # "", "/", ".json": no bank name at all
        raise ValueError("Neplatny nazev sady otazek")
    if len(name) > 120:
        raise ValueError("Nazev sady otazek je prilis dlouhy")
    if private:
        if owner_id is None:
            raise FileNotFoundError(f"Question bank '{filename}' not found")
        return QUESTIONS_DIR / "users" / str(int(owner_id)) / name, PRIVATE_BANK_PREFIX + name
    return QUESTIONS_DIR / name, name


def _bank_display_name(stem: str) -> str:
    """Name shown in the portal: "linux_sprava" -> "Linux Sprava" as before; a name
    someone wrote with capitals or diacritics ("OS13-2_DNS_zóny") stays as written."""
    text = stem.replace("_", " ")
    return text.title() if stem.isascii() and stem == stem.lower() else text


def _bank_entries(folder: Path, prefix: str, scope: str, editable: bool) -> list[dict[str, Any]]:
    banks = []
    if not folder.is_dir():
        return banks
    for f in sorted(folder.iterdir()):
        if f.suffix == ".json" and f.is_file():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                count = len(data) if isinstance(data, list) else 0
            except Exception:
                count = 0
            banks.append({
                "filename": prefix + f.name,
                "name": _bank_display_name(f.stem),
                "question_count": count,
                "scope": scope,
                "editable": editable,
            })
    return banks


def list_question_banks(owner_id: int | None = None) -> list[dict[str, Any]]:
    """Shared banks, plus the teacher's private ones when owner_id is given.

    With an owner (multi-teacher server) shared banks are read-only for them.
    """
    _migrate_legacy_questions()
    banks = []
    if owner_id is not None:
        banks += _bank_entries(QUESTIONS_DIR / "users" / str(int(owner_id)), PRIVATE_BANK_PREFIX, "mine", True)
    banks += _bank_entries(QUESTIONS_DIR, "", "shared", owner_id is None)
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

    # Optional double-points flag (all question types)
    if "double" in q:
        if not isinstance(q["double"], bool):
            raise RuntimeError(f"question {idx} double must be a boolean")

    # Open answers: optional exact match (no typo tolerance)
    if "exact" in q and not isinstance(q["exact"], bool):
        raise RuntimeError(f"question {idx} exact must be a boolean")


def load_questions_from_file(filename: str, owner_id: int | None = None) -> list[dict[str, Any]]:
    path, safe_name = _bank_path(filename, owner_id)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Question bank '{safe_name}' not found")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise RuntimeError("questions file must contain a list")
    for i, q in enumerate(raw):
        _validate_question(q, i)
    return raw


def save_questions_to_file(filename: str, questions: list[dict[str, Any]],
                           owner_id: int | None = None) -> str:
    """Save a bank; returns its canonical name.

    With an owner (teacher on a multi-teacher server) the bank always lands in
    the teacher's private folder — editing a shared bank creates a private copy.
    """
    if owner_id is not None and not filename.startswith(PRIVATE_BANK_PREFIX):
        filename = _free_private_name(filename, owner_id)
    path, safe_name = _bank_path(filename, owner_id)
    _check_new_bank_name(path.name)
    # Validate before saving
    if not isinstance(questions, list):
        raise ValueError("questions must be a list")
    for i, q in enumerate(questions):
        _validate_question(q, i)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(questions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return safe_name


def _check_new_bank_name(name: str) -> None:
    """A saved bank's file name must be plain text: a backslash, control
    characters or bytes that are not UTF-8 break backups and other systems."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Neplatny nazev sady otazek") from None
    if "\\" in name or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError("Nazev sady nesmi obsahovat zpetne lomitko ani ridici znaky")


def _free_private_name(filename: str, owner_id: int) -> str:
    """Private name for a copy of a shared bank (or a new bank) that does not
    overwrite one of the teacher's own banks: x.json, x-2.json, x-3.json, ..."""
    stem = Path(filename).name
    stem = stem[:-5] if stem.endswith(".json") else stem
    candidate, n = f"{PRIVATE_BANK_PREFIX}{stem}.json", 1
    while _bank_path(candidate, owner_id)[0].exists():
        n += 1
        candidate = f"{PRIVATE_BANK_PREFIX}{stem}-{n}.json"
    return candidate


def delete_question_bank(filename: str, owner_id: int | None = None) -> None:
    """Delete a bank. A teacher (owner_id) may delete only private banks."""
    if owner_id is not None and not filename.startswith(PRIVATE_BANK_PREFIX):
        raise PermissionError("Sdilenou sadu muze smazat jen spravce serveru.")
    path, _ = _bank_path(filename, owner_id)
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


def _csv_cell(value: Any) -> Any:
    if isinstance(value, float):
        return str(value).replace(".", ",")  # decimal comma for Czech Excel
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value  # a student's name like =HYPERLINK(...) must not run as a formula
    return value


def _csv_text(rows: list[list[Any]]) -> str:
    """CSV for Czech Excel: semicolons, BOM, decimal comma, no formula injection."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\n")
    for row in rows:
        writer.writerow([_csv_cell(v) for v in row])
    return "\ufeff" + buf.getvalue()


def _letter(index: Any) -> str:
    return chr(ord("A") + index) if isinstance(index, int) and 0 <= index < 26 else ""


def _scored_questions(record: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """(position in record["questions"], question) of the questions that give points."""
    return [(i, q) for i, q in enumerate(record.get("questions") or [])
            if q.get("type") not in UNSCORED_QUESTION_TYPES]


def game_results_to_csv(record: dict[str, Any]) -> str:
    """A game for Excel: rank, name, points and, per question, 1 (right) / 0 / empty (no answer)."""
    scored = _scored_questions(record)
    header = ["Poradi", "Jmeno", "Body"]
    if scored:
        header += ["Spravne", "Odpovedi"] + [f"O{q.get('index', i) + 1}" for i, q in scored]
    rows = [header]
    for p in record.get("players", []):
        row = [p.get("rank", ""), p.get("name", ""), p.get("score", 0)]
        if scored:
            answers = p.get("answers") or []
            marks = []
            for i, _ in scored:
                a = answers[i] if i < len(answers) else None
                marks.append("" if not a else (1 if a.get("ok") else 0))
            row += [sum(1 for m in marks if m == 1), sum(1 for m in marks if m != "")] + marks
        rows.append(row)
    return _csv_text(rows)


def _top_wrong_answer(q: dict[str, Any]) -> str:
    """The most common wrong answer of a logged game question ("" if none)."""
    counts, correct = q.get("counts") or [], q.get("correct")
    options = q.get("options") or []
    if q.get("type") in ("choice", "truefalse", "multiselect") and counts:
        right = set(correct) if isinstance(correct, list) else {correct}
        wrong = [(c, -i) for i, c in enumerate(counts) if i not in right and c > 0]
        if wrong:
            i = -max(wrong)[1]
            return f"{_letter(i)}) {options[i] if i < len(options) else ''}".strip()
    if q.get("type") == "openended":
        for item in q.get("top_answers") or []:
            if len(item) >= 3 and item[2] is False:  # the grading's own verdict (it accepts small typos)
                return item[0]
    return ""


def game_items(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-question analysis of a game: success rate, most common wrong answer."""
    items = []
    for i, q in enumerate(record.get("questions") or []):
        if q.get("type") == "slide":
            continue
        scored = q.get("type") not in UNSCORED_QUESTION_TYPES
        players = q.get("players") or 0
        correct = q.get("correct_count") if scored else None
        items.append({
            "number": q.get("index", i) + 1, "type": q.get("type"), "prompt": q.get("prompt", ""),
            "players": players, "answered": q.get("answered", 0), "correct": correct,
            "percent": round(correct / players * 100) if scored and players else None,
            "top_wrong": _top_wrong_answer(q) if scored else "",
            "avg_sec_correct": q.get("avg_sec_correct"),
        })
    return items


def game_items_csv(record: dict[str, Any]) -> str:
    rows = [["Otazka", "Typ", "Zneni", "Hracu", "Odpovedelo", "Spravne", "Uspesnost %",
             "Nejcastejsi chybna odpoved", "Prumerny cas spravnych (s)"]]
    for it in game_items(record):
        rows.append([it["number"], it["type"], it["prompt"], it["players"], it["answered"],
                     "" if it["correct"] is None else it["correct"],
                     "" if it["percent"] is None else it["percent"], it["top_wrong"],
                     "" if it["avg_sec_correct"] is None else float(it["avg_sec_correct"])])
    return _csv_text(rows)


def history_overview(record: dict[str, Any]) -> dict[str, Any]:
    """A game record for the history list: without per-question detail and internal ids."""
    out = {k: v for k, v in record.items() if k != "questions"}
    out["players"] = [{k: v for k, v in p.items() if k not in ("answers", "player_id")}
                      for p in record.get("players", [])]
    out["has_details"] = bool(record.get("questions"))
    return out


def history_detail(record: dict[str, Any]) -> dict[str, Any]:
    """A full game record for the teacher's analysis (no internal player ids)."""
    out = dict(record)
    out["players"] = [{k: v for k, v in p.items() if k != "player_id"} for p in record.get("players", [])]
    out["items"] = game_items(record)
    return out


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
    # called with quiz_state._lock held
    log = quiz_state.question_log
    players = quiz_state._ranked_players_with_ids()
    if log:
        for row in players:  # per question: None (no answer) or {ok, points, choice, sec}
            row["answers"] = [entry["results"].get(row["player_id"]) for entry in log]
    record = {
        "id": uuid.uuid4().hex[:12],
        "timestamp": datetime.now().isoformat(),
        "total_questions": len(quiz_state.questions),
        "question_duration_sec": quiz_state.question_duration_sec,
        "speed_bonus": quiz_state.speed_bonus,
        "players": players,
        "player_count": len(quiz_state.players),
        "bank_name": quiz_state._active_bank or "",
        "owner_id": quiz_state.owner_id,  # teacher whose room played it (None = default room)
        "questions": quiz_state._question_summary_locked(),
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


def list_game_history(owner_id: int | None = None) -> list[dict[str, Any]]:
    """All game records, or only one teacher's (owner_id) on multi-teacher servers."""
    history = []
    for f in sorted(HISTORY_DIR.iterdir(), reverse=True):
        if f.suffix == ".json" and f.is_file():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if owner_id is None or data.get("owner_id") == owner_id:
                history.append(data)
    return history


def load_game_record(game_id: str) -> dict[str, Any] | None:
    if not game_id or not GAME_ID_RE.match(game_id):
        return None
    path = HISTORY_DIR / f"game_{game_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


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

AI_LANGUAGES = {
    "cs": "Czech (čeština)",
    "en": "English",
    "sk": "Slovak (slovenčina)",
    "de": "German (Deutsch)",
    "uk": "Ukrainian (українська)",
}
AI_SOURCE_TEXT_MAX = 12000


def generate_questions_ai(
    topic: str,
    count: int = 5,
    base_url: str = "http://localhost:11434",
    model: str = "gpt-oss:20b",
    api_key: str = "",
    language: str = "cs",
    source_text: str = "",
) -> list[dict[str, Any]]:
    lang_name = AI_LANGUAGES.get(language, language)
    source_part = ""
    if source_text.strip():
        source_part = (
            "\nBase every question ONLY on the following source text (do not use facts that "
            "are not in it):\n<<<\n" + source_text.strip()[:AI_SOURCE_TEXT_MAX] + "\n>>>\n"
        )
    prompt = f"""Create exactly {count} multiple-choice quiz questions for school students on the topic: "{topic}".
{source_part}
Write every question, option and explanation in {lang_name}, with correct spelling and diacritics.
Each question is a JSON object with:
- "id": unique identifier ("ai_1", "ai_2", ...)
- "prompt": the question text
- "options": exactly 4 answer options, only one of them correct, wrong options plausible
- "correct_index": index of the correct option (0-3)
- "explanation": one or two sentences explaining the correct answer

Reply ONLY with a valid JSON array, no markdown, no comments.
Example: [{{"id":"ai_1","prompt":"...?","options":["...","...","...","..."],"correct_index":0,"explanation":"..."}}]"""

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
        "max_tokens": 8192,
    }).encode("utf-8")

    req = Request(url, data=payload, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except URLError as e:
        raise ConnectionError(f"Nelze se pripojit k AI API na {base_url}: {e}")
    except Exception as e:
        raise RuntimeError(f"Chyba pri komunikaci s AI API: {e}")

    response_text = result.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
    # Reasoning models may prepend a <think>...</think> block that can contain brackets.
    response_text = re.sub(r"<think>.*?</think>", "", response_text, flags=re.S).strip()
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
        # Models tend to put the right answer first; shuffle so it is not guessable.
        perm = list(range(len(q["options"])))
        random.shuffle(perm)
        q["options"] = [q["options"][i] for i in perm]
        q["correct_index"] = perm.index(q["correct_index"])
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


def _generate_random_nickname(players: dict[str, Any] | None = None) -> str:
    """Generate a random Czech adjective+animal nickname, e.g. 'RychlyOrel'.

    Checks against currently registered player names and appends a numeric
    suffix if there is a collision.
    """
    import random as _random
    adj = _random.choice(_NICK_ADJECTIVES)
    animal = _random.choice(_NICK_ANIMALS)
    base = adj + animal
    existing_names = {p.name for p in (QUIZ.players if players is None else players).values()}
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


# --- Rate limiters ---

_SWEEP_EVERY = 1000


def _sweep(table: dict[str, list[float]], now: float, window_sec: float) -> None:
    """Drop keys without recent activity once the table grows (bounded memory)."""
    if len(table) < _SWEEP_EVERY:
        return
    for key in [k for k, times in table.items() if not times or now - times[-1] >= window_sec]:
        del table[key]


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
            _sweep(self._attempts, now, self.window_sec)
            attempts = self._attempts.get(client_ip, [])
            attempts = [t for t in attempts if now - t < self.window_sec]
            self._attempts[client_ip] = attempts
            if len(attempts) >= self.max_requests:
                return False
            attempts.append(now)
            return True


# Largest accepted request body: a 2 MB question image as base64 inside JSON.
MAX_BODY_BYTES = 4 * 1024 * 1024


class _BodyTooLarge(Exception):
    pass


class _FailureLimiter:
    """Blocks a key after too many recent failures (successes are not counted)."""

    def __init__(self, max_failures: int, window_sec: int) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}
        self.max_failures = max_failures
        self.window_sec = window_sec

    def _recent(self, key: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(key, []) if now - t < self.window_sec]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key, time.time())) >= self.max_failures

    def fail(self, key: str) -> None:
        now = time.time()
        with self._lock:
            _sweep(self._failures, now, self.window_sec)
            self._failures[key] = self._recent(key, now) + [now]


# Players joining a game: a whole school can sit behind one NAT address.
REGISTER_LIMITER = _RegistrationRateLimiter(max_requests=120, window_sec=60)
ACCOUNT_REGISTER_LIMITER = _RegistrationRateLimiter(max_requests=60, window_sec=600)
# Login: a tight limit per (nickname, address) and a looser one per nickname, so
# one attacker cannot lock a teacher out for everybody else.
LOGIN_FAIL_IP = _FailureLimiter(max_failures=50, window_sec=300)
LOGIN_FAIL_NICK_IP = _FailureLimiter(max_failures=8, window_sec=300)
LOGIN_FAIL_NICK = _FailureLimiter(max_failures=40, window_sec=300)
# Wrong current password when changing password or e-mail (stolen session).
PASSWORD_FAIL_USER = _FailureLimiter(max_failures=8, window_sec=300)
FORGOT_LIMITER_IP = _RegistrationRateLimiter(max_requests=10, window_sec=900)
MAIL_LIMITER_USER = _RegistrationRateLimiter(max_requests=3, window_sec=3600)
SET_EMAIL_LIMITER = _RegistrationRateLimiter(max_requests=10, window_sec=3600)
VERIFY_MAIL_IP = _RegistrationRateLimiter(max_requests=20, window_sec=3600)
INVITE_LIMITER = _RegistrationRateLimiter(max_requests=20, window_sec=3600)
# Bug reports: a whole class behind one school NAT may report at once, but one
# address must not flood the central collector.
BUG_REPORT_LIMITER_IP = _RegistrationRateLimiter(max_requests=60, window_sec=600)
BUG_REPORT_LIMITER_ALL = _RegistrationRateLimiter(max_requests=300, window_sec=3600)
# Hard cap on all outgoing account e-mail of the server.
GLOBAL_MAIL_LIMITER = _RegistrationRateLimiter(max_requests=200, window_sec=3600)
class _GuessLimiter:
    """Blocks a key after too many *different* wrong guesses in a window.

    Repeating one wrong value counts once: after a restart or a new PIN, every
    device of a school (one NAT address) still holds the same stale PIN.
    """

    def __init__(self, max_guesses: int, window_sec: int) -> None:
        self._lock = threading.Lock()
        self._guesses: dict[str, dict[str, float]] = {}
        self.max_guesses = max_guesses
        self.window_sec = window_sec

    def _recent_locked(self, key: str, now: float) -> dict[str, float]:
        recent = {g: t for g, t in self._guesses.get(key, {}).items() if now - t < self.window_sec}
        if recent:
            self._guesses[key] = recent
        else:
            self._guesses.pop(key, None)
        return recent

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent_locked(key, time.time())) >= self.max_guesses

    def fail(self, key: str, guess: str) -> None:
        """Record a wrong guess; callers pass it normalized (one value per real guess)."""
        now = time.time()
        with self._lock:
            if len(self._guesses) >= _SWEEP_EVERY:  # bounded memory
                for k in [k for k, g in self._guesses.items()
                          if not g or now - max(g.values()) >= self.window_sec]:
                    del self._guesses[k]
            recent = self._recent_locked(key, now)
            recent.setdefault(guess, now)
            self._guesses[key] = recent


# Unknown room PINs per address: 100 different ones in 10 minutes. Guessing all
# 900 000 PINs from one address would take months.
PIN_FAIL_IP = _GuessLimiter(max_guesses=100, window_sec=600)


class _PinGuessBlocked(Exception):
    """Too many unknown PINs from one address (answered with HTTP 429)."""


# --- Quiz State ---

def _open_answer_ok(question: dict, answer: Any) -> bool:
    """Is an open answer one of the accepted ones?

    Ends are trimmed and case is ignored unless the question is case_sensitive.
    Longer answers tolerate small typos (5-8 characters one, more two) unless the
    question asks for an exact match: commands such as Get-TimeZone / Set-TimeZone
    or "port 389" / "port 3389" differ by just one or two characters.
    """
    case_sensitive = bool(question.get("case_sensitive", False))
    compare = str(answer).strip()
    compare = compare if case_sensitive else compare.lower()
    for acc in question.get("accepted_answers", []):
        target = str(acc).strip()
        target = target if case_sensitive else target.lower()
        if compare == target:
            return True
        if question.get("exact"):
            continue
        max_dist = 2 if len(target) > 8 else (1 if len(target) > 4 else 0)
        if max_dist > 0 and _levenshtein(compare, target) <= max_dist:
            return True
    return False


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

# End-of-game award thresholds
AWARD_FASTEST_MIN_CORRECT = 3    # fastest_finger needs at least this many correct answers
AWARD_STREAK_MIN = 3             # streak_master needs at least this max_streak
AWARD_COMEBACK_MIN_RANKS = 2     # comeback needs to climb at least this many ranks


TEAM_NAMES_DEFAULT: list[str] = ["Modri", "Cerveni", "Zeleni", "Zluti", "Fialovi", "Oranzovi"]
TEAM_COLORS: list[str] = ["#1d4ed8", "#dc2626", "#16a34a", "#ca8a04", "#7c3aed", "#ea580c"]


# Points: 600 for a correct answer + up to 400 for speed (10 per second left of 40).
MAX_POINTS = 600 + 40 * 10
UNSCORED_QUESTION_TYPES = ("poll", "wordcloud", "slide")


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
    # Award tracking fields
    answered_count: int = 0                               # scored questions this player answered
    correct_count: int = 0                                # scored questions answered correctly
    correct_times: list = field(default_factory=list)     # effective elapsed sec of correct answers
    midgame_rank: int | None = None                       # rank recorded at mid-game reveal


class QuizState:
    def __init__(self, questions: list[dict[str, Any]]) -> None:
        self._lock = threading.Lock()
        self.questions = questions
        self.players: dict[str, Player] = {}
        self.phase = "lobby"  # lobby | question | reveal | finished
        self.current_index = -1
        self.question_duration_sec = QUESTION_DURATION_SEC
        self.reveal_duration_sec = REVEAL_DURATION_SEC
        self.get_ready_sec = GET_READY_SEC
        self.final_double: bool = False
        self.question_started_at = 0.0
        self.reveal_started_at = 0.0
        self.answers: dict[str, dict[str, Any]] = {}
        self.scored_question_index: int | None = None
        self.scored_question_count = 0
        self._awards_cache: list[dict[str, Any]] | None = None
        self._ws_marker: tuple[str, int] = ("lobby", -1)  # last (phase, index) broadcast over WS
        self._active_bank: str | None = None
        self._class_id: int | None = None
        # Set when the game belongs to a room (see RoomRegistry): WS updates go
        # only to that room's clients and history records carry the owner.
        self.room_key: str | None = None
        self.owner_id: int | None = None
        self.team_mode: bool = False
        self.num_teams: int = 2
        self.team_names: list[str] = TEAM_NAMES_DEFAULT[:2]
        # Didactic options (docs: "jen stupne vitezu" instead of the full ranking,
        # no speed pressure for slower students)
        self.speed_bonus: bool = True
        self.show_leaderboard: bool = True
        # Longer questions get reading time on top of question_duration_sec
        self.adaptive_time: bool = True
        # One entry per evaluated question: what the class answered (formative
        # analysis after the game, saved with the game history)
        self.question_log: list[dict[str, Any]] = []
        # Ordering questions are shown shuffled (the bank lists the items in the
        # right order): question index -> display position -> original item index
        self._item_orders: dict[int, list[int]] = {}

    def _reading_bonus_locked(self, index: int | None = None) -> int:
        """Reading time added to the current (or given) question; 0 when off."""
        index = self.current_index if index is None else index
        if not self.adaptive_time or not 0 <= index < len(self.questions):
            return 0
        return reading_bonus_sec(self.questions[index])

    def _question_time_locked(self, index: int | None = None) -> int:
        """Time per question as played: the teacher's setting plus reading time."""
        return self.question_duration_sec + self._reading_bonus_locked(index)

    def _item_order_locked(self, index: int) -> list[int] | None:
        """Display order of an ordering question, never the right order itself."""
        question = self.questions[index] if 0 <= index < len(self.questions) else {}
        n = len(question.get("items", [])) if question.get("type") == "ordering" else 0
        if n < 2:
            return None
        if index not in self._item_orders:
            self._item_orders[index] = _shuffled_order(n, random.Random())
        return self._item_orders[index]

    def _notify(self) -> None:
        """Broadcast this game's state over WS (call only after releasing the lock)."""
        if self.room_key is None:
            _ws_notify()
        else:
            _ws_notify(self.room_key)

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
            self.scored_question_count = 0
            self._awards_cache = None
            self.question_log = []
            self._item_orders = {}
            for player in self.players.values():
                player.score = 0
                player.streak = 0
                player.max_streak = 0
                player.bot_score = 0
                player.answer_times = []
                player.fast_answer_streak = 0
                player.answered_count = 0
                player.correct_count = 0
                player.correct_times = []
                player.midgame_rank = None
                player.team = None

    def set_timing(self, question_sec: int | None = None, reveal_sec: int | None = None,
                   get_ready_sec: int | None = None, final_double: bool | None = None,
                   speed_bonus: bool | None = None, show_leaderboard: bool | None = None,
                   adaptive_time: bool | None = None) -> None:
        with self._lock:
            if adaptive_time is not None:
                self.adaptive_time = bool(adaptive_time)
            if speed_bonus is not None:
                self.speed_bonus = bool(speed_bonus)
            if show_leaderboard is not None:
                self.show_leaderboard = bool(show_leaderboard)
            if question_sec is not None:
                self.question_duration_sec = max(5, min(120, question_sec))
            if reveal_sec is not None:
                self.reveal_duration_sec = max(2, min(30, reveal_sec))
            if get_ready_sec is not None:
                self.get_ready_sec = max(0, min(10, get_ready_sec))
            if final_double is not None:
                self.final_double = bool(final_double)

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

    def register_player(self, name: str, user_id: int | None = None, avatar_id: Any = 0) -> dict[str, Any]:
        clean = " ".join(name.strip().split())[:24]
        if not clean:
            raise ValueError("Jmeno je povinne")
        if not _is_nickname_clean(clean):
            raise ValueError("Prezdivka obsahuje nevhodne slovo")
        # Requested avatar (accept int or numeric string, range 1-20 like
        # /api/auth/update-avatar). Registration is a critical path, so any
        # invalid value silently falls back to 0 (= random, client picks).
        try:
            avatar_id = int(avatar_id)
        except (TypeError, ValueError):
            avatar_id = 0
        if avatar_id < 1 or avatar_id > 20:
            avatar_id = 0
        # Profile avatar of a logged-in user wins over the requested one
        # (unless the profile has no avatar set).
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
            self._mark_ws_state_locked()
        # Notify WS clients about new player
        self._notify()
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
            return hmac.compare_digest(player.secret.encode(), str(player_secret).encode())

    def kick_player(self, player_id: str) -> dict[str, Any]:
        """Remove a player from the game (host action)."""
        with self._lock:
            if player_id not in self.players:
                raise ValueError("Neznamy player_id")
            del self.players[player_id]
            self.answers.pop(player_id, None)
            self._mark_ws_state_locked()
        self._notify()
        return {"ok": True}

    def clear_players(self) -> list[str]:
        """New lesson: back to the lobby with no players; returns the removed ids."""
        with self._lock:
            gone = list(self.players)
            self.players.clear()
            self.phase = "lobby"
            self.current_index = -1
            self.question_started_at = 0.0
            self.reveal_started_at = 0.0
            self.answers = {}
            self.scored_question_index = None
            self.scored_question_count = 0
            self._awards_cache = None
            self.question_log = []
            self._item_orders = {}
            self._mark_ws_state_locked()
        self._notify()
        return gone

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
                self._awards_cache = None
                self.question_log = []
                self._item_orders = {}
            elif action == "reveal":
                if self.phase != "question":
                    raise ValueError("Reveal je mozne jen ve fazi question")
                self._apply_scoring()
                self.phase = "reveal"
                self.reveal_started_at = now
            elif action == "next":
                if self.phase == "finished":
                    return {"ok": True}  # nothing left; advancing again would save the game twice
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
                self.scored_question_count = 0
                self._awards_cache = None
                self.question_log = []
                self._item_orders = {}
                for player in self.players.values():
                    player.score = 0
                    player.streak = 0
                    player.max_streak = 0
                    player.bot_score = 0
                    player.answer_times = []
                    player.fast_answer_streak = 0
                    player.answered_count = 0
                    player.correct_count = 0
                    player.correct_times = []
                    player.midgame_rank = None
            elif action == "save_history":
                if self.phase == "finished" or len(self.players) > 0:
                    record = save_game_history(self)
                    return {"ok": True, "record": record}
                raise ValueError("Zadni hraci nebo hra neskoncila")
            else:
                raise ValueError("Neznamy action")
            self._mark_ws_state_locked()
        # Notify WS clients about the state change AFTER releasing the lock:
        # the broadcast state getter calls public_state(), which re-acquires
        # it — notifying under the lock would deadlock the server.
        self._notify()
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
                if any(type(c) is not int for c in choice) or sorted(choice) != list(range(len(items))):
                    raise ValueError("Neplatna permutace")
                order = self._item_order_locked(self.current_index)
                if order:
                    choice = [order[d] for d in choice]  # display positions -> items of the bank
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
            self._mark_ws_state_locked()

        # Notify WS clients about new answer
        self._notify()
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
            self._safe_log_locked(question, q_type, {})
            self._record_midgame_rank_locked()
            return

        started = self.question_started_at or time.time()
        double_active = self._question_is_double_locked(self.current_index)

        # Track who answered for streak reset
        answered_pids = set(self.answers.keys())
        awarded: dict[str, int] = {}

        for player_id, answer in self.answers.items():
            points = self._calculate_points(question, q_type, answer, started, self.get_ready_sec,
                                            speed_bonus_on=self.speed_bonus)
            is_correct = points > 0
            player = self.players[player_id]
            player.answered_count += 1

            if is_correct:
                elapsed = max(0.0, answer["time"] - started)
                player.correct_times.append(max(0.0, elapsed - self.get_ready_sec))
                player.correct_count += 1
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
                # Double points apply after the streak multiplier
                if double_active:
                    points *= 2
            else:
                player.streak = 0

            player.score += points
            awarded[player_id] = points

        # Reset streak for players who didn't answer
        for pid, player in self.players.items():
            if pid not in answered_pids:
                player.streak = 0

        self.scored_question_index = self.current_index
        self.scored_question_count += 1
        self._safe_log_locked(question, q_type, awarded)
        self._record_midgame_rank_locked()

    def _safe_log_locked(self, question: dict, q_type: str, awarded: dict[str, int]) -> None:
        """The analysis log is extra: it must never break or repeat the scoring."""
        try:
            self._log_question_locked(question, q_type, awarded)
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: question analysis not recorded: {type(e).__name__}: {e}", flush=True)

    def _log_question_locked(self, question: dict, q_type: str, awarded: dict[str, int]) -> None:
        """Record what the class answered to the current question. Must hold self._lock."""
        scored = q_type not in UNSCORED_QUESTION_TYPES
        started = self.question_started_at or time.time()
        results: dict[str, dict[str, Any]] = {}
        for pid, answer in self.answers.items():
            row: dict[str, Any] = {
                "choice": answer["choice"],
                "sec": round(max(0.0, answer["time"] - started - self.get_ready_sec), 1),
            }
            if scored:
                row["ok"] = self._full_credit(question, q_type, answer["choice"])
                row["points"] = awarded.get(pid, 0)
            results[pid] = row
        entry: dict[str, Any] = {
            "index": self.current_index,
            "id": question.get("id"),
            "type": q_type,
            "prompt": str(question.get("prompt", ""))[:500],
            "players": len(self.players),
            "answered": len(results),
            "double": self._question_is_double_locked(self.current_index),
            "results": results,
        }
        if q_type in ("choice", "truefalse", "multiselect", "poll"):
            entry["options"] = list(question.get("options", []))
            entry["counts"] = self._vote_counts()
        if q_type in ("choice", "truefalse"):
            entry["correct"] = question.get("correct_index")
        elif q_type == "multiselect":
            entry["correct"] = sorted(question.get("correct_indices", []))
        elif q_type == "ordering":
            entry["items"] = list(question.get("items", []))  # listed in the correct order
        elif q_type == "openended":
            entry["correct"] = list(question.get("accepted_answers", []))
        if q_type in ("openended", "wordcloud"):
            # [text, count, graded right (None for a word cloud)], grouped the way the
            # grading compares: case only matters for a case-sensitive question
            keep_case = q_type == "openended" and bool(question.get("case_sensitive"))
            texts: dict[str, list] = {}
            for row in results.values():
                text = str(row["choice"]).strip()
                text = text if keep_case else text.lower()
                if text:
                    bucket = texts.setdefault(text, [0, False])
                    bucket[0] += 1
                    bucket[1] = bucket[1] or bool(row.get("ok"))
            ranked = sorted(texts.items(), key=lambda kv: (-kv[1][0], kv[0]))[:8]
            entry["top_answers"] = [[t, c, ok if scored else None] for t, (c, ok) in ranked]
        if scored:
            entry["correct_count"] = sum(1 for row in results.values() if row["ok"])
            entry["explanation"] = str(question.get("explanation", ""))[:1000]
            times = [row["sec"] for row in results.values() if row["ok"]]
            entry["avg_sec_correct"] = round(sum(times) / len(times), 1) if times else None
        self.question_log = [e for e in self.question_log if e["index"] != self.current_index]
        self.question_log.append(entry)

    @classmethod
    def _full_credit(cls, question: dict, q_type: str, choice: Any) -> bool:
        """True when an answer earns full points (partial credit and wrong = False)."""
        at_once = {"choice": choice, "time": 0.0}
        return cls._calculate_points(question, q_type, at_once, 0.0) == MAX_POINTS

    def _question_summary_locked(self) -> list[dict[str, Any]]:
        """The question log without who answered what (safe for the projector)."""
        return [{k: v for k, v in entry.items() if k != "results"} for entry in self.question_log]

    def _question_is_double_locked(self, index: int) -> bool:
        """True when the question at index scores double points. Must hold self._lock."""
        if index < 0 or index >= len(self.questions):
            return False
        if bool(self.questions[index].get("double", False)):
            return True
        return self.final_double and index == len(self.questions) - 1

    def _record_midgame_rank_locked(self) -> None:
        """Record each player's rank at the mid-game reveal. Must hold self._lock."""
        if self.current_index != len(self.questions) // 2:
            return
        ordered = sorted(self.players.values(), key=lambda p: (-p.score, p.name.lower()))
        for rank, player in enumerate(ordered, start=1):
            player.midgame_rank = rank

    @staticmethod
    def _calculate_points(question: dict, q_type: str, answer: dict, started: float,
                          get_ready_sec: int = 0, speed_bonus_on: bool = True) -> int:
        """Calculate raw points for an answer (before streak multiplier).

        The "get ready" countdown is subtracted from elapsed time so the
        speed bonus only starts ticking once answers are visible. Without the
        speed bonus a fully correct answer is worth the maximum at any time.
        """
        elapsed = max(0.0, answer["time"] - started)
        effective_elapsed = max(0.0, elapsed - get_ready_sec)
        speed_bonus = max(0, 40 - int(effective_elapsed)) if speed_bonus_on else 40

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
            return 600 + speed_bonus * 10 if _open_answer_ok(question, answer["choice"]) else 0

        return 0

    def _advance_to_next_question_locked(self, now: float) -> None:
        if self.current_index + 1 >= len(self.questions):
            self.phase = "finished"
            self.reveal_started_at = 0.0
            self._awards_cache = self._compute_awards_locked()
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
                question_time = self._question_time_locked()
                if everyone_answered or elapsed >= question_time:
                    self._apply_scoring()
                    self.phase = "reveal"
                    if everyone_answered:
                        self.reveal_started_at = now
                    else:
                        self.reveal_started_at = self.question_started_at + question_time
                    continue
            if self.phase == "reveal" and self.reveal_started_at > 0:
                elapsed = now - self.reveal_started_at
                if elapsed >= self.reveal_duration_sec:
                    self._advance_to_next_question_locked(self.reveal_started_at + self.reveal_duration_sec)
                    continue
            break

    def _mark_ws_state_locked(self) -> None:
        """Remember the (phase, index) that WS clients are being told about.

        Must be called with self._lock held by every mutator that broadcasts
        after releasing the lock. Keeps tick_and_notify() from re-sending a
        state an explicit action has already broadcast.
        """
        self._ws_marker = (self.phase, self.current_index)

    def tick_and_notify(self) -> bool:
        """Advance timer-driven transitions and broadcast them over WS.

        Called periodically by the ticker thread (_quiz_ticker_loop). Timer
        transitions (question timeout -> reveal -> next question -> finished)
        otherwise reach clients only through polling, and WS clients stop
        polling once connected. Notifies only when (phase, current_index)
        actually changed, and only AFTER releasing the lock — the broadcast
        state getter re-acquires it.
        """
        now = time.time()
        with self._lock:
            self._sync_timers_locked(now)
            marker = (self.phase, self.current_index)
            changed = marker != self._ws_marker
            if changed:
                self._ws_marker = marker
        if changed:
            self._notify()
        return changed

    def _phase_time_left_locked(self, now: float) -> int | None:
        if self.phase == "question" and self.question_started_at > 0:
            left = self._question_time_locked() - (now - self.question_started_at)
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

    def _rank_and_rival_locked(self, player_id: str) -> tuple[int, dict[str, Any] | None]:
        """Return (1-based rank, player directly above me with point gap).

        Uses the same stable ordering as the leaderboard. The second value is
        None for the leader. Must be called with self._lock held.
        """
        ordered = sorted(self.players.items(), key=lambda kv: (-kv[1].score, kv[1].name.lower()))
        rank = 1
        for i, (pid, _) in enumerate(ordered, start=1):
            if pid == player_id:
                rank = i
                break
        if rank <= 1:
            return rank, None
        above = ordered[rank - 2][1]
        gap = above.score - self.players[player_id].score
        return rank, {"name": above.name, "gap": gap}

    def _compute_awards_locked(self) -> list[dict[str, Any]]:
        """Compute end-of-game awards. Must be called with self._lock held.

        Awards are assigned in priority order and each player receives at
        most one. Ties prefer players outside the top 3 so the podium does
        not sweep everything. Entries keep "player_id" for the personal
        view; public_state strips it.
        """
        ordered = sorted(self.players.items(), key=lambda kv: (-kv[1].score, kv[1].name.lower()))
        final_ranks = {pid: rank for rank, (pid, _) in enumerate(ordered, start=1)}
        awards: list[dict[str, Any]] = []
        awarded: set[str] = set()

        def _grant(key: str, pid: str, player: Player, value: str) -> None:
            awards.append({
                "key": key,
                "player": player.name,
                "avatar_id": player.avatar_id,
                "value": value,
                "player_id": pid,
            })
            awarded.add(pid)

        def _best(candidates: list[tuple[str, Player]], metric) -> tuple[str, Player] | None:
            """Pick the not-yet-awarded candidate with the lowest metric(pid, player).

            Ties prefer players outside the top 3, then sort by name for
            deterministic results.
            """
            pool = [
                (metric(pid, p), final_ranks[pid] <= 3, p.name.lower(), pid, p)
                for pid, p in candidates
                if pid not in awarded
            ]
            if not pool:
                return None
            pool.sort(key=lambda row: row[:3])
            _, _, _, pid, player = pool[0]
            return pid, player

        # fastest_finger: lowest average time of correct answers (min 3 correct);
        # not awarded when the teacher switched the speed bonus off
        best = _best(
            [(pid, p) for pid, p in self.players.items()
             if self.speed_bonus and len(p.correct_times) >= AWARD_FASTEST_MIN_CORRECT],
            lambda pid, p: sum(p.correct_times) / len(p.correct_times),
        )
        if best:
            pid, player = best
            avg = sum(player.correct_times) / len(player.correct_times)
            _grant("fastest_finger", pid, player, f"{avg:.1f} s")

        # streak_master: highest max_streak (min 3)
        best = _best(
            [(pid, p) for pid, p in self.players.items() if p.max_streak >= AWARD_STREAK_MIN],
            lambda pid, p: -p.max_streak,
        )
        if best:
            pid, player = best
            _grant("streak_master", pid, player, f"série {player.max_streak}")

        # sharpshooter: answered every scored question, all of them correctly
        total = self.scored_question_count
        best = _best(
            [(pid, p) for pid, p in self.players.items()
             if total > 0 and p.answered_count == total and p.correct_count == total],
            lambda pid, p: 0,
        )
        if best:
            pid, player = best
            _grant("sharpshooter", pid, player, f"{player.correct_count}/{total}")

        # comeback: biggest rank improvement between mid-game and finish (min 2)
        best = _best(
            [(pid, p) for pid, p in self.players.items()
             if p.midgame_rank is not None
             and p.midgame_rank - final_ranks[pid] >= AWARD_COMEBACK_MIN_RANKS],
            lambda pid, p: -(p.midgame_rank - final_ranks[pid]),
        )
        if best:
            pid, player = best
            improvement = player.midgame_rank - final_ranks[pid]
            _grant("comeback", pid, player, f"+{improvement} míst")

        return awards

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
                "question_duration_sec": self.question_duration_sec,  # the teacher's setting
                "adaptive_time": self.adaptive_time,
                "reading_bonus_sec": self._reading_bonus_locked(),
                "question_time_sec": self._question_time_locked(),  # this question as played
                "reveal_duration_sec": self.reveal_duration_sec,
                "get_ready_sec": self.get_ready_sec,
                "final_double": self.final_double,
                "speed_bonus": self.speed_bonus,
                "show_leaderboard": self.show_leaderboard,
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

            if self.phase == "finished":
                if self._awards_cache is None:
                    self._awards_cache = self._compute_awards_locked()
                response["awards"] = [
                    {"key": a["key"], "player": a["player"], "avatar_id": a["avatar_id"], "value": a["value"]}
                    for a in self._awards_cache
                ]
                if host_view:
                    response["question_summary"] = self._question_summary_locked()

            # "Podium only": students see nobody else's rank, only the top 3 at the end
            podium_only = not host_view and not self.show_leaderboard
            if podium_only:
                response["players"] = [p for p in ranked if p["rank"] <= 3] if self.phase == "finished" else []

            if player_id and player_id in self.players:
                p = self.players[player_id]
                my_rank, behind = self._rank_and_rival_locked(player_id)
                if podium_only:
                    my_rank = my_rank if self.phase == "finished" and my_rank <= 3 else None
                    behind = None
                me_data: dict[str, Any] = {
                    "name": p.name,
                    "score": p.score,
                    "streak": p.streak,
                    "max_streak": p.max_streak,
                    "avatar_id": p.avatar_id,
                    "rank": my_rank,
                    "behind": behind,
                }
                if self.phase == "finished":
                    me_data["awards"] = [
                        a["key"] for a in (self._awards_cache or []) if a["player_id"] == player_id
                    ]
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
                    my_choice = self.answers[player_id]["choice"]
                    order = self._item_order_locked(self.current_index)
                    if order and isinstance(my_choice, list):
                        my_choice = [order.index(i) for i in my_choice]
                    response["my_choice"] = my_choice

            if self.current_index >= 0:
                q = self.questions[self.current_index]
                q_type = q.get("type", "choice")
                q_public: dict[str, Any] = {
                    "id": q["id"],
                    "prompt": q["prompt"],
                    "type": q_type,
                    "double": self._question_is_double_locked(self.current_index),
                }

                # Optional image
                if q.get("image_url"):
                    q_public["image_url"] = q["image_url"]

                # Type-specific fields
                if q_type in ("choice", "truefalse", "multiselect", "poll"):
                    q_public["options"] = q["options"]
                item_order = self._item_order_locked(self.current_index) if q_type == "ordering" else None
                if q_type == "ordering":
                    # shuffled for everybody alike; the client answers with display positions
                    q_public["items"] = [q["items"][i] for i in item_order] if item_order else q["items"]
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
                        n_items = len(q.get("items", []))
                        q_public["correct_order"] = ([item_order.index(k) for k in range(n_items)]
                                                     if item_order else list(range(n_items)))
                    elif q_type == "openended":
                        q_public["accepted_answers"] = q.get("accepted_answers", [])
                    if q_type not in ("poll", "wordcloud", "slide"):
                        q_public["explanation"] = q.get("explanation", "")

                response["question"] = q_public

            return response


# --- Exam (písemka) mode ---
#
# A serious written-test mode that runs alongside the live game. Unlike the
# lockstep game, each student works through the whole test at their own pace
# within a shared time window, answers are graded (Czech 1-5), question/option
# order can be shuffled per student, and lightweight proctoring records when a
# student leaves the test window (Page Visibility blur events).

DEFAULT_GRADE_THRESHOLDS = [[90, 1], [75, 2], [60, 3], [45, 4], [0, 5]]
EXAM_TIME_LIMIT_SEC = 1800  # 30 minutes
MAX_PROCTOR_EVENTS = 300


@dataclass
class ExamConfig:
    time_limit_sec: int = EXAM_TIME_LIMIT_SEC
    shuffle_questions: bool = True
    shuffle_options: bool = True
    allow_back: bool = True               # student may revisit and change answers
    disable_copy: bool = True             # client disables copy/paste/context menu
    show_results_to_student: bool = True  # show grade to student after submit
    auto_submit_after_blurs: int = 0      # 0 = off; otherwise auto-submit after N window-leaves
    grade_thresholds: list = field(default_factory=lambda: [r[:] for r in DEFAULT_GRADE_THRESHOLDS])

    def to_dict(self) -> dict[str, Any]:
        return {
            "time_limit_sec": self.time_limit_sec,
            "shuffle_questions": self.shuffle_questions,
            "shuffle_options": self.shuffle_options,
            "allow_back": self.allow_back,
            "disable_copy": self.disable_copy,
            "show_results_to_student": self.show_results_to_student,
            "auto_submit_after_blurs": self.auto_submit_after_blurs,
            "grade_thresholds": [row[:] for row in self.grade_thresholds],
        }

    def update(self, data: dict[str, Any]) -> None:
        if "time_limit_sec" in data:
            self.time_limit_sec = max(30, min(4 * 3600, int(data["time_limit_sec"])))
        for flag in ("shuffle_questions", "shuffle_options", "allow_back",
                     "disable_copy", "show_results_to_student"):
            if flag in data:
                self[flag] = bool(data[flag])
        if "auto_submit_after_blurs" in data:
            self.auto_submit_after_blurs = max(0, min(100, int(data["auto_submit_after_blurs"])))
        if "grade_thresholds" in data:
            self._set_thresholds(data["grade_thresholds"])

    def __setitem__(self, key: str, value: Any) -> None:
        setattr(self, key, value)

    def _set_thresholds(self, rows: Any) -> None:
        if not isinstance(rows, list) or not rows:
            return
        clean: list[list[int]] = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                continue
            pct, grade = int(row[0]), int(row[1])
            pct = max(0, min(100, pct))
            clean.append([pct, grade])
        if not clean:
            return
        # Sort by percentage descending so grade lookup walks high -> low.
        clean.sort(key=lambda r: -r[0])
        self.grade_thresholds = clean

    def grade_for_percent(self, percent: float) -> int:
        for min_pct, grade in self.grade_thresholds:
            if percent >= min_pct:
                return grade
        # Below every threshold -> worst grade defined.
        return self.grade_thresholds[-1][1] if self.grade_thresholds else 5


@dataclass
class ExamStudent:
    name: str
    secret: str
    order: list[int] = field(default_factory=list)            # permutation of question indices
    option_orders: dict[int, list[int]] = field(default_factory=dict)  # qindex -> option permutation
    answers: dict[str, int] = field(default_factory=dict)     # qid -> chosen ORIGINAL option index
    started_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    current_pos: int = 0                                      # position within their order (for live view)
    submitted_at: float | None = None
    auto_submitted: bool = False
    score: int | None = None                                 # number correct
    grade: int | None = None
    # proctoring
    blur_count: int = 0
    away_ms: float = 0.0
    last_blur_at: float | None = None
    events: list[dict[str, Any]] = field(default_factory=list)


EXAM_QUESTION_TYPES = ("choice", "truefalse")


class ExamState:
    def __init__(self, questions: list[dict[str, Any]] | None = None) -> None:
        self._lock = threading.Lock()
        self.questions = questions or []
        self.config = ExamConfig()
        self.students: dict[str, ExamStudent] = {}
        self.phase = "closed"  # closed (lobby) | open (running) | ended
        self.opened_at = 0.0
        self.ended_at = 0.0
        self._active_bank: str | None = None
        # Saved exam (exam_sessions): every answer, proctoring event and grade is
        # written as it happens, so a server restart loses no results.
        self.owner_id: int | None = None
        self.session_id: str | None = None

    # -- saving (best effort: a database problem must never stop a running exam) --

    @staticmethod
    def _save(action: Any, *args: Any) -> None:
        try:
            action(*args)
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: exam results not saved: {type(e).__name__}: {e}", flush=True)

    def _save_student_locked(self, player_id: str, student: "ExamStudent") -> None:
        if self.session_id is None:
            return
        self._save(user_db.save_exam_result, self.session_id, player_id, {
            "name": student.name, "answers": dict(student.answers), "score": student.score,
            "grade": student.grade, "submitted_at": student.submitted_at,
            "auto_submitted": student.auto_submitted, "blur_count": student.blur_count,
            "away_ms": student.away_ms,
        })

    def _start_session_locked(self) -> None:
        if self.session_id is not None:
            # opened again after the end (latecomers): the same saved exam runs on,
            # unless the teacher deleted it in the meantime -> a new one below
            reopened = False
            try:
                reopened = user_db.reopen_exam_session(self.session_id, self.config.to_dict())
            except Exception as e:  # noqa: BLE001
                print(f"WARNING: exam results not saved: {type(e).__name__}: {e}", flush=True)
            if reopened:
                for pid, student in self.students.items():
                    self._save_student_locked(pid, student)
                return
        self.session_id = uuid.uuid4().hex[:12]
        self._save(user_db.create_exam_session, self.session_id, self.owner_id, self._active_bank or "",
                   self.questions, self.config.to_dict(), self.opened_at)
        for pid, student in self.students.items():
            self._save_student_locked(pid, student)

    def _close_session_locked(self) -> None:
        """Before the exam is cleared or replaced: grade, save and close it."""
        if self.phase == "open":
            self._end_locked(time.time())
        self.session_id = None

    def _rows_locked(self) -> list[dict[str, Any]]:
        return [{"name": s.name, "answers": dict(s.answers), "score": s.score, "grade": s.grade,
                 "submitted_at": s.submitted_at, "auto_submitted": s.auto_submitted,
                 "blur_count": s.blur_count, "away_ms": s.away_ms} for s in self.students.values()]

    # -- setup --

    def reload_questions(self, questions: list[dict[str, Any]], bank_name: str | None = None) -> None:
        # The exam screen renders and grades only option questions with one
        # correct answer; other types would be unanswerable (and used to be
        # graded as correct when left blank).
        unsupported = sorted({
            str(q.get("type", "choice")) for q in questions
            if q.get("type", "choice") not in EXAM_QUESTION_TYPES
        })
        if unsupported:
            raise ValueError(
                "Pisemka podporuje jen otazky typu vyber odpovedi a ano/ne; "
                f"sada obsahuje: {', '.join(unsupported)}"
            )
        with self._lock:
            self._close_session_locked()
            self.questions = questions
            self._active_bank = bank_name
            self.phase = "closed"
            self.opened_at = 0.0
            self.ended_at = 0.0
            self.students = {}

    def update_config(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._sync_locked(time.time())  # the time may have just run out
            if self.phase == "open":
                # the form re-sends its time limit; during the exam only "extend" changes it
                data = {k: v for k, v in data.items() if k != "time_limit_sec"}
            self.config.update(data)
            self._save_config_locked()
            return self.config.to_dict()

    def _save_config_locked(self) -> None:
        """A running exam keeps its saved settings current (time limit, grading)."""
        if self.session_id is not None and self.phase == "open":
            self._save(user_db.update_exam_session_config, self.session_id, self.config.to_dict())

    def register_student(self, name: str) -> dict[str, Any]:
        clean = " ".join(name.strip().split())[:24]
        if not clean:
            raise ValueError("Jmeno je povinne")
        with self._lock:
            player_id = uuid.uuid4().hex[:10]
            secret = uuid.uuid4().hex
            student = ExamStudent(name=clean, secret=secret)
            if self.phase == "open":
                self._build_order_locked(player_id, student)
            self.students[player_id] = student
            if self.phase == "open":
                self._save_student_locked(player_id, student)
            return {"player_id": player_id, "player_secret": secret, "name": clean}

    def verify_student(self, player_id: str, secret: str) -> bool:
        with self._lock:
            s = self.students.get(player_id)
            return bool(s and s.secret == secret)

    def _build_order_locked(self, player_id: str, student: ExamStudent) -> None:
        n = len(self.questions)
        order = list(range(n))
        rng = random.Random(player_id)  # deterministic per student
        if self.config.shuffle_questions:
            rng.shuffle(order)
        student.order = order
        student.option_orders = {}
        if self.config.shuffle_options:
            for qi in order:
                opt_n = len(self.questions[qi].get("options", []))
                perm = list(range(opt_n))
                rng.shuffle(perm)
                student.option_orders[qi] = perm

    # -- host controls --

    def open_exam(self) -> None:
        with self._lock:
            if not self.questions:
                raise ValueError("Pisemka nema otazky")
            now = time.time()
            self._sync_locked(now)
            if self.phase == "open":
                return  # already running: a second click must not restart everyone's clock
            self.phase = "open"
            self.opened_at = now
            self.ended_at = 0.0
            for pid, student in self.students.items():
                if not student.order:
                    self._build_order_locked(pid, student)
                student.started_at = now
            self._start_session_locked()

    def end_exam(self) -> None:
        with self._lock:
            self._end_locked(time.time())

    def extend(self, seconds: int) -> None:
        with self._lock:
            seconds = max(-3600, min(3600, int(seconds)))
            self.config.time_limit_sec = max(30, self.config.time_limit_sec + seconds)
            self._save_config_locked()

    def reset(self) -> None:
        with self._lock:
            self._close_session_locked()
            self.phase = "closed"
            self.opened_at = 0.0
            self.ended_at = 0.0
            self.students = {}

    def _end_locked(self, now: float) -> None:
        if self.phase == "ended":
            return
        self.phase = "ended"
        self.ended_at = now
        for student in self.students.values():
            if student.submitted_at is None:
                self._grade_locked(student, now, auto=True)
        if self.session_id is not None:
            self._save(user_db.end_exam_session, self.session_id, now)

    def _sync_locked(self, now: float) -> None:
        if self.phase == "open" and self.opened_at > 0:
            if now - self.opened_at >= self.config.time_limit_sec:
                self._end_locked(self.opened_at + self.config.time_limit_sec)

    def _time_left_locked(self, now: float) -> int:
        if self.phase == "open" and self.opened_at > 0:
            return max(0, int(math.ceil(self.config.time_limit_sec - (now - self.opened_at))))
        return 0

    # -- student actions --

    def save_answer(self, player_id: str, secret: str, qid: str, choice: int) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._sync_locked(now)
            student = self._auth_locked(player_id, secret)
            if self.phase != "open":
                raise ValueError("Pisemka neni spustena")
            if student.submitted_at is not None:
                raise ValueError("Pisemka uz byla odevzdana")
            qindex = self._qindex_by_id(qid)
            if qindex is None:
                raise ValueError("Neznama otazka")
            options = self.questions[qindex].get("options", [])
            if not isinstance(choice, int) or choice < 0 or choice >= len(options):
                raise ValueError("Neplatna volba")
            if not self.config.allow_back and qid in student.answers:
                raise ValueError("Odpoved nelze zmenit")
            student.answers[qid] = choice
            student.last_seen = now
            self._save_student_locked(player_id, student)
            return {"ok": True, "answered": len(student.answers)}

    def set_position(self, player_id: str, secret: str, pos: int) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            student = self._auth_locked(player_id, secret)
            if isinstance(pos, int) and 0 <= pos < len(student.order):
                student.current_pos = pos
            student.last_seen = now
            return {"ok": True}

    def record_event(self, player_id: str, secret: str, event_type: str) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._sync_locked(now)
            student = self._auth_locked(player_id, secret)
            student.last_seen = now
            if event_type == "blur":
                student.blur_count += 1
                student.last_blur_at = now
                if len(student.events) < MAX_PROCTOR_EVENTS:
                    student.events.append({"type": "blur", "t": now})
                limit = self.config.auto_submit_after_blurs
                if (limit > 0 and student.blur_count >= limit
                        and student.submitted_at is None and self.phase == "open"):
                    self._grade_locked(student, now, auto=True)
                    return {"ok": True, "auto_submitted": True, "blur_count": student.blur_count}
            elif event_type == "focus":
                if student.last_blur_at is not None:
                    student.away_ms += (now - student.last_blur_at) * 1000.0
                    student.last_blur_at = None
            if event_type in ("blur", "focus"):
                self._save_student_locked(player_id, student)
            return {"ok": True, "blur_count": student.blur_count}

    def submit(self, player_id: str, secret: str) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._sync_locked(now)
            student = self._auth_locked(player_id, secret)
            if student.submitted_at is not None:
                return self._result_payload_locked(student)
            self._grade_locked(student, now, auto=False)
            return self._result_payload_locked(student)

    # -- grading --

    def _grade_locked(self, student: ExamStudent, now: float, auto: bool) -> None:
        correct = 0
        for q in self.questions:
            ci = q.get("correct_index")
            if isinstance(ci, int) and student.answers.get(q["id"]) == ci:
                correct += 1
        total = len(self.questions)
        percent = (correct / total * 100.0) if total else 0.0
        student.score = correct
        student.grade = self.config.grade_for_percent(percent)
        student.submitted_at = now
        student.auto_submitted = auto
        if student.last_blur_at is not None:
            student.away_ms += (now - student.last_blur_at) * 1000.0
            student.last_blur_at = None
        for pid, st in self.students.items():
            if st is student:
                self._save_student_locked(pid, student)
                break

    def _result_payload_locked(self, student: ExamStudent) -> dict[str, Any]:
        total = len(self.questions)
        payload: dict[str, Any] = {
            "ok": True,
            "submitted": True,
            "auto_submitted": student.auto_submitted,
        }
        if self.config.show_results_to_student:
            payload.update({
                "score": student.score,
                "total": total,
                "percent": round((student.score or 0) / total * 100) if total else 0,
                "grade": student.grade,
            })
        return payload

    # -- helpers --

    def _auth_locked(self, player_id: str, secret: str) -> ExamStudent:
        student = self.students.get(player_id)
        if not student:
            raise ValueError("Neznamy player_id")
        if student.secret != secret:
            raise ValueError("Neplatny player_secret")
        return student

    def _qindex_by_id(self, qid: str) -> int | None:
        for i, q in enumerate(self.questions):
            if q["id"] == qid:
                return i
        return None

    # -- views --

    def student_view(self, player_id: str, secret: str | None = None) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._sync_locked(now)
            student = self.students.get(player_id)
            if not student:
                return {"phase": self.phase, "known": False}
            if not secret or not hmac.compare_digest(student.secret.encode(), str(secret).encode()):
                return {"phase": self.phase, "known": False}
            student.last_seen = now
            view: dict[str, Any] = {
                "known": True,
                "phase": self.phase,
                "name": student.name,
                "time_left": self._time_left_locked(now),
                "time_limit_sec": self.config.time_limit_sec,
                "answered": len(student.answers),
                "total": len(self.questions),
                "current_pos": student.current_pos,
                "config": {
                    "allow_back": self.config.allow_back,
                    "disable_copy": self.config.disable_copy,
                    "auto_submit_after_blurs": self.config.auto_submit_after_blurs,
                },
                "blur_count": student.blur_count,
            }
            if student.submitted_at is not None:
                view["result"] = self._result_payload_locked(student)
                return view
            if self.phase == "open" and student.order:
                questions = []
                for qi in student.order:
                    q = self.questions[qi]
                    opts = q.get("options", [])
                    perm = student.option_orders.get(qi) or list(range(len(opts)))
                    questions.append({
                        "id": q["id"],
                        "prompt": q["prompt"],
                        # each option carries its ORIGINAL index (oid) so the
                        # client returns oid on answer regardless of display order
                        "options": [{"text": opts[oi], "oid": oi} for oi in perm],
                    })
                view["questions"] = questions
                view["answers"] = dict(student.answers)
            return view

    def overview(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            self._sync_locked(now)
            total = len(self.questions)
            rows = []
            for student in self.students.values():
                online = (now - student.last_seen) < 8
                rows.append({
                    "name": student.name,
                    "answered": len(student.answers),
                    "total": total,
                    "current_q": (student.current_pos + 1) if student.order else 0,
                    "submitted": student.submitted_at is not None,
                    "auto_submitted": student.auto_submitted,
                    "score": student.score,
                    "grade": student.grade,
                    "blur_count": student.blur_count,
                    "away_sec": round(student.away_ms / 1000),
                    "online": online,
                })
            rows.sort(key=lambda r: r["name"].lower())
            submitted = sum(1 for r in rows if r["submitted"])
            return {
                "phase": self.phase,
                "active_bank": self._active_bank,
                "total_questions": total,
                "time_left": self._time_left_locked(now),
                "time_limit_sec": self.config.time_limit_sec,
                "config": self.config.to_dict(),
                "student_count": len(rows),
                "submitted_count": submitted,
                "students": rows,
            }

    def results_csv(self) -> str:
        with self._lock:
            return exam_results_csv(self.questions, self._rows_locked())

    def answers_csv(self) -> str:
        with self._lock:
            return exam_answers_csv(self.questions, self._rows_locked())

    def items_csv(self) -> str:
        with self._lock:
            return exam_items_csv(self.questions, self._rows_locked())

    def item_stats(self) -> dict[str, Any]:
        """Anonymous per-question analysis of the graded students (for the projector)."""
        with self._lock:
            self._sync_locked(time.time())
            rows = self._rows_locked()
            return {"phase": self.phase, "students": len(rows),
                    "graded": sum(1 for r in rows if r["score"] is not None),
                    "items": exam_item_stats(self.questions, rows)}


def _item_rest_correlation(marks: list[int], totals: list[int]) -> float | None:
    """Corrected item-total correlation: item right/wrong vs. the score on the other items.

    Around 0.3 and more = the question separates students who know the topic
    from those who do not; near 0 or negative = check the question or its key.
    """
    n = len(marks)
    if n < MIN_STUDENTS_FOR_DISCRIMINATION:
        return None
    rest = [t - m for m, t in zip(marks, totals)]
    mean_m, mean_r = sum(marks) / n, sum(rest) / n
    sxx = sum((m - mean_m) ** 2 for m in marks)
    syy = sum((r - mean_r) ** 2 for r in rest)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((m - mean_m) * (r - mean_r) for m, r in zip(marks, rest))
    return round(sxy / math.sqrt(sxx * syy), 2)


def exam_item_stats(questions: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-question analysis of an exam over the graded students (no names)."""
    graded = [r for r in rows if r.get("score") is not None]
    totals = [int(r["score"]) for r in graded]
    items = []
    for number, q in enumerate(questions, start=1):
        options = list(q.get("options", []))
        key = q.get("correct_index")
        counts = [0] * len(options)
        marks: list[int] = []
        for r in graded:
            a = (r.get("answers") or {}).get(q.get("id"))
            if isinstance(a, int) and 0 <= a < len(options):
                counts[a] += 1
            marks.append(1 if a == key else 0)
        answered = sum(counts)
        correct = sum(marks)
        wrong = [(c, -i) for i, c in enumerate(counts) if i != key and c > 0]
        items.append({
            "number": number, "id": q.get("id"), "prompt": q.get("prompt", ""), "options": options,
            "correct_index": key, "counts": counts, "students": len(graded),
            "correct": correct, "wrong": answered - (counts[key] if isinstance(key, int) and 0 <= key < len(counts) else 0),
            "unanswered": len(graded) - answered,
            "percent": round(correct / len(graded) * 100) if graded else None,
            "top_wrong": -max(wrong)[1] if wrong else None,
            "discrimination": _item_rest_correlation(marks, totals),
            "explanation": q.get("explanation", ""),
        })
    return items


def _submitted_text(ts: Any) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""


def exam_results_csv(questions: list[dict[str, Any]], rows: list[dict[str, Any]]) -> str:
    """Per student: the summary, proctoring and, per question, 1 (right) / 0 / empty."""
    total = len(questions)
    table = [["Jmeno", "Spravne", "Celkem", "Procenta", "Znamka", "Odevzdano", "Auto",
              "Opusteni okna", "Cas mimo (s)"] + [f"O{n}" for n in range(1, total + 1)]]
    for r in sorted(rows, key=lambda r: r["name"].lower()):
        score = r.get("score")
        answers = r.get("answers") or {}
        marks = []
        for q in questions:
            a = answers.get(q.get("id"))
            marks.append("" if a is None else (1 if a == q.get("correct_index") else 0))
        table.append([
            r["name"], "" if score is None else score, total,
            round(score / total * 100) if total and score is not None else "",
            "" if r.get("grade") is None else r["grade"], _submitted_text(r.get("submitted_at")),
            "ano" if r.get("auto_submitted") else "ne", r.get("blur_count", 0),
            round((r.get("away_ms") or 0) / 1000),
        ] + marks)
    return _csv_text(table)


def exam_answers_csv(questions: list[dict[str, Any]], rows: list[dict[str, Any]]) -> str:
    """Every student's answer to every question, marked right or wrong (long format)."""
    def option(q: dict[str, Any], index: Any) -> str:
        opts = q.get("options", [])
        return f"{_letter(index)}) {opts[index]}" if isinstance(index, int) and 0 <= index < len(opts) else ""

    table = [["Jmeno", "Otazka", "Zneni", "Odpoved", "Spravna odpoved", "Vysledek"]]
    for r in sorted(rows, key=lambda r: r["name"].lower()):
        answers = r.get("answers") or {}
        for n, q in enumerate(questions, start=1):
            a = answers.get(q.get("id"))
            result = "bez odpovedi" if a is None else ("spravne" if a == q.get("correct_index") else "chybne")
            table.append([r["name"], n, q.get("prompt", ""), option(q, a), option(q, q.get("correct_index")), result])
    return _csv_text(table)


def exam_items_csv(questions: list[dict[str, Any]], rows: list[dict[str, Any]]) -> str:
    table = [["Otazka", "Zneni", "Spravna odpoved", "Spravne", "Chybne", "Bez odpovedi", "Uspesnost %",
              "Nejcastejsi chybna odpoved", "Korelace s celkem"]]
    for it in exam_item_stats(questions, rows):
        key, wrong = it["correct_index"], it["top_wrong"]
        opts = it["options"]
        table.append([
            it["number"], it["prompt"],
            f"{_letter(key)}) {opts[key]}" if isinstance(key, int) and 0 <= key < len(opts) else "",
            it["correct"], it["wrong"], it["unanswered"], "" if it["percent"] is None else it["percent"],
            f"{_letter(wrong)}) {opts[wrong]}" if wrong is not None else "",
            "" if it["discrimination"] is None else float(it["discrimination"]),
        ])
    return _csv_text(table)


def close_interrupted_exams() -> int:
    """Exams still open in the database were running when the server stopped:
    grade what the students had answered and close them, so no result is lost."""
    closed = 0
    for session in user_db.list_open_exam_sessions():
        config = ExamConfig()
        config.update(session["config"])
        questions = session["questions"]
        total = len(questions)
        last = session["opened_at"]
        for row in session["results"]:
            last = max(last, row["updated_at"])
            if row["score"] is None:
                correct = sum(1 for q in questions if row["answers"].get(q.get("id")) == q.get("correct_index"))
                row.update(score=correct, auto_submitted=True,
                           grade=config.grade_for_percent(correct / total * 100 if total else 0.0))
                user_db.save_exam_result(session["id"], row["player_id"], row)
        user_db.end_exam_session(session["id"], last, interrupted=True)
        closed += 1
    return closed


def server_activity() -> dict[str, Any]:
    """Is anybody using the server right now? Counts only (before a restart)."""
    now = time.time()
    rooms = ROOMS.all()
    games = players = exams = writing = recent = 0
    for room in rooms:
        with room.quiz._lock:
            if room.quiz.phase in ("question", "reveal"):
                games += 1
            players += len(room.quiz.players)
            occupied = bool(room.quiz.players)
        with room.exam._lock:  # one lock at a time
            if room.exam.phase == "open":
                exams += 1
                writing += sum(1 for st in room.exam.students.values() if st.submitted_at is None)
            occupied = occupied or bool(room.exam.students)
        # an empty room (nobody joined) is not "in use" just because it was created recently
        if occupied and now - room.last_active < ACTIVITY_RECENT_SEC:
            recent += 1
    ws = WS_MANAGER.connection_count
    return {
        "busy": bool(games or exams or ws or recent),
        "rooms": len(rooms), "rooms_recent": recent, "games_running": games, "players": players,
        "exams_open": exams, "exam_students_writing": writing, "websocket_clients": ws,
    }


# Sound roles from the file name: "loop" anywhere in the name = background
# music; a name starting with an effect role (correct_ding.ogg, win_jingle.ogg,
# reveal-stinger.mp3 …) = that effect. Anything else counts as music.
AUDIO_EFFECT_ROLES = ("correct", "wrong", "lock", "reveal", "end", "win")

# Files fetched by download_free_quiz_music.sh whose licence requires credit.
AUDIO_CREDITS = {
    "Kevin MacLeod (incompetech.com), CC BY 4.0": {
        "loop_thinking_music.mp3", "loop_sneaky_snitch.mp3", "loop_quirky_dog.mp3",
        "loop_monkeys_spinning_monkeys.mp3", "loop_local_forecast_elevator.mp3",
        "tension-loop.mp3", "reveal-stinger.mp3",
    },
}


def _audio_role(name: str) -> str:
    tokens = [t for t in re.split(r"[\s_.\-]+", name.lower()) if t]
    if "loop" in tokens:
        return "loop"
    if tokens and tokens[0] in AUDIO_EFFECT_ROLES:
        return tokens[0]
    return "loop"


def list_audio_tracks() -> dict[str, Any]:
    allowed = {".mp3", ".ogg", ".wav", ".m4a", ".aac"}
    loops: list[dict[str, str]] = []
    stingers: list[dict[str, str]] = []
    all_tracks: list[dict[str, str]] = []
    effects: dict[str, list[str]] = {}
    credits: list[str] = []

    if not AUDIO_DIR.exists():
        return {"all": [], "loops": [], "stingers": [], "effects": {}, "credits": []}

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

        role = _audio_role(file_path.name)
        if role == "loop":
            loops.append(track)
        else:
            stingers.append(track)
            effects.setdefault(role, []).append(track["url"])
        for credit, names in AUDIO_CREDITS.items():
            if file_path.name in names and credit not in credits:
                credits.append(credit)

    if not loops and all_tracks:
        loops = all_tracks.copy()
    if not stingers and all_tracks:
        stingers = all_tracks.copy()

    return {"all": all_tracks, "loops": loops, "stingers": stingers,
            "effects": effects, "credits": credits}


# --- Rooms -------------------------------------------------------------------
# A room is one classroom session: its own live game, its own exam, its own host
# token and a 6-digit PIN that students use to join.
#
# QUIZ_ROOMS=single (default): one room for the whole server — the classic local
#   setup (one teacher, one class, no PIN needed).
# QUIZ_ROOMS=multi: every teacher account gets its own room; students join with
#   the room PIN (it is in the QR code), so teachers never disturb each other.
ROOM_MODE = "multi" if os.environ.get("QUIZ_ROOMS", "single").strip().lower() == "multi" else "single"
ROOM_IDLE_SEC = 12 * 3600  # rooms of teachers without activity are dropped
ROOM_PIN_RE = re.compile(r"[0-9]{6}")  # room PINs: randrange(100000, 1000000)
ROOM_OWNER_RECHECK_SEC = 30  # how often a host token re-checks its owner's role


@dataclass
class Room:
    pin: str
    owner_id: int | None
    quiz: "QuizState"
    exam: "ExamState"
    host_token: str
    mode: str = "game"  # "game" (live Kahoot-style) | "exam" (písemka)
    last_active: float = field(default_factory=time.time)
    # Stable identity for WebSocket routing: the PIN changes with "new PIN".
    key: str = field(default_factory=lambda: uuid.uuid4().hex)
    owner_checked_at: float = 0.0  # last check that the owner is still a teacher

    def touch(self) -> None:
        self.last_active = time.time()


class RoomRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_pin: dict[str, Room] = {}
        self._by_key: dict[str, Room] = {}
        self._by_owner: dict[int, Room] = {}
        self._by_token: dict[str, Room] = {}
        self._by_player: dict[str, Room] = {}  # player/student id -> room

    def _new_pin_locked(self) -> str:
        while True:
            pin = f"{random.SystemRandom().randrange(100000, 1000000)}"
            if pin not in self._by_pin:
                return pin

    def _add_locked(self, room: Room) -> Room:
        room.quiz.room_key = room.key
        room.quiz.owner_id = room.owner_id
        room.exam.owner_id = room.owner_id
        self._by_pin[room.pin] = room
        self._by_key[room.key] = room
        self._by_token[room.host_token] = room
        if room.owner_id is not None:
            self._by_owner[room.owner_id] = room
        return room

    def _forget_locked(self, room: Room) -> None:
        if self._by_pin.get(room.pin) is room:
            del self._by_pin[room.pin]
        self._by_key.pop(room.key, None)
        self._by_token.pop(room.host_token, None)
        if room.owner_id is not None and self._by_owner.get(room.owner_id) is room:
            del self._by_owner[room.owner_id]
        for pid in [pid for pid, r in self._by_player.items() if r is room]:
            del self._by_player[pid]

    @staticmethod
    def _blank(owner_id: int | None, quiz: "QuizState | None" = None,
               exam: "ExamState | None" = None, host_token: str | None = None) -> Room:
        # built outside the lock (loads the default bank); the PIN is assigned under it
        return Room(pin="", owner_id=owner_id, quiz=quiz or QuizState(load_default_questions()),
                    exam=exam or ExamState(), host_token=host_token or uuid.uuid4().hex)

    def create(self, owner_id: int | None, quiz: "QuizState | None" = None,
               exam: "ExamState | None" = None, host_token: str | None = None) -> Room:
        room = self._blank(owner_id, quiz, exam, host_token)
        with self._lock:
            room.pin = self._new_pin_locked()
            return self._add_locked(room)

    def for_owner(self, owner_id: int) -> Room:
        """The teacher's room (created on first use)."""
        with self._lock:
            room = self._by_owner.get(owner_id)
        if room is None:
            fresh = self._blank(owner_id)
            with self._lock:
                # two first requests of one teacher must not get two rooms
                room = self._by_owner.get(owner_id)
                if room is None:
                    fresh.pin = self._new_pin_locked()
                    room = self._add_locked(fresh)
        room.touch()
        return room

    def by_pin(self, pin: str | None) -> Room | None:
        if not pin:
            return None
        with self._lock:
            return self._by_pin.get(str(pin).strip())

    def by_key(self, key: str | None) -> Room | None:
        if not key:
            return None
        with self._lock:
            return self._by_key.get(key)

    def by_token(self, token: str | None) -> Room | None:
        if not token:
            return None
        with self._lock:
            room = self._by_token.get(token)
        # dict lookup by a random 128-bit token; compare once more in constant time
        if room is not None and hmac.compare_digest(room.host_token.encode(), token.encode()):
            return room
        return None

    def by_player(self, player_id: str | None) -> Room | None:
        if not player_id:
            return None
        with self._lock:
            return self._by_player.get(player_id)

    def bind_player(self, player_id: str, room: Room) -> None:
        with self._lock:
            self._by_player[player_id] = room

    def unbind_player(self, player_id: str) -> None:
        with self._lock:
            self._by_player.pop(player_id, None)

    def renew(self, room: Room) -> str:
        """Next lesson in the same room: a new PIN and an empty lobby.

        The game players are forgotten (their devices see "unknown player" and
        join again); students of a running exam keep their place.
        """
        gone = room.quiz.clear_players()
        with self._lock:
            if self._by_pin.get(room.pin) is room:
                del self._by_pin[room.pin]
            room.pin = self._new_pin_locked()
            self._by_pin[room.pin] = room
            for pid in gone:
                if self._by_player.get(pid) is room:
                    del self._by_player[pid]
        room.touch()
        return room.pin

    def drop(self, room: Room) -> None:
        with self._lock:
            self._forget_locked(room)
        _ws_notify(room.key)  # the state lookup fails -> its WebSocket clients are disconnected

    def all(self) -> list[Room]:
        with self._lock:
            return list(self._by_key.values())

    def drop_idle(self, keep: Room, now: float | None = None) -> int:
        """Forget teacher rooms nobody has used for ROOM_IDLE_SEC.

        Never the default room, and never a room whose exam still holds student
        results (they live only in memory until the teacher exports them).
        Players of an old game are dropped with their room: next lesson gets a
        new PIN and nobody from yesterday's class sits in its lobby.
        """
        now = now or time.time()
        with self._lock:
            idle = [r for r in self._by_key.values()
                    if r is not keep and now - r.last_active > ROOM_IDLE_SEC and not r.exam.students]
            for room in idle:
                self._forget_locked(room)
        for room in idle:
            _ws_notify(room.key)
        return len(idle)


ROOMS = RoomRegistry()
# The default room: the only one in single mode; in multi mode it serves the
# server administrator (admin password / open first-run access).
DEFAULT_ROOM = ROOMS.create(None, quiz=QuizState(load_default_questions()), exam=ExamState(),
                            host_token=HOST_TOKEN)
QUIZ = DEFAULT_ROOM.quiz
EXAM = DEFAULT_ROOM.exam


class _KickedPlayers:
    """Player ids the teacher removed with "Vyhodit", so their device can say
    so (code player_kicked) instead of silently dropping back to the join
    screen. Not a ban: the student may join again under a new player id.
    Bounded and in memory only (a restart forgets it, like the players)."""

    def __init__(self, limit: int = 2000) -> None:
        self._ids: dict[str, float] = {}
        self._limit = limit
        self._lock = threading.Lock()

    def add(self, player_id: str) -> None:
        with self._lock:
            self._ids[player_id] = time.time()
            while len(self._ids) > self._limit:
                self._ids.pop(next(iter(self._ids)))

    def __contains__(self, player_id: object) -> bool:
        with self._lock:
            return player_id in self._ids


KICKED_PLAYERS = _KickedPlayers()


def get_app_mode(room: Room | None = None) -> str:
    return (room or DEFAULT_ROOM).mode


def set_app_mode(mode: str, room: Room | None = None) -> str:
    room = room or DEFAULT_ROOM
    if mode in ("game", "exam"):
        room.mode = mode
    return room.mode




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
        return (True, 600) if _open_answer_ok(question, choice) else (False, 0)

    return False, 0


def _shuffled_order(n: int, rng: random.Random) -> list[int]:
    """A random order of n items. From three items on it is never the original
    one (handing out the answer); two items are swapped or not at random, since
    "always swapped" would give the answer away just the same."""
    order = list(range(n))
    rng.shuffle(order)
    while n >= 3 and order == sorted(order):
        rng.shuffle(order)
    return order


def _selfpaced_item_order(question: dict, session_id: str, player_id: str) -> list[int] | None:
    """Homework: each student gets the ordering items shuffled, the same way on
    every reload, from a server secret the student cannot compute."""
    n = len(question.get("items", [])) if question.get("type") == "ordering" else 0
    if n < 2:
        return None
    seed = hmac.new(user_db.server_secret("item-order"),
                    f"{session_id}|{player_id}|{question.get('id')}".encode("utf-8"), hashlib.sha256).digest()
    return _shuffled_order(n, random.Random(seed))


def _public_question_for_selfpaced(question: dict, reveal: bool = False,
                                   item_order: list[int] | None = None) -> dict:
    """Return the public view of a question for self-paced mode.

    item_order: how the ordering items were shown to this student (display
    position -> item of the bank); the right order is then given in display
    positions as well.
    """
    q_type = question.get("type", "choice")
    q_public: dict[str, Any] = {
        "id": question["id"],
        "prompt": question["prompt"],
        "type": q_type,
    }
    if q_type in ("choice", "truefalse", "multiselect"):
        q_public["options"] = question["options"]
    if q_type == "ordering":
        q_public["items"] = [question["items"][i] for i in item_order] if item_order else question["items"]
    if q_type == "openended":
        q_public["max_length"] = 200

    if reveal:
        if q_type in ("choice", "truefalse"):
            q_public["correct_index"] = question["correct_index"]
        elif q_type == "multiselect":
            q_public["correct_indices"] = question.get("correct_indices", [])
        elif q_type == "ordering":
            n_items = len(question.get("items", []))
            q_public["correct_order"] = ([item_order.index(k) for k in range(n_items)]
                                         if item_order else list(range(n_items)))
        elif q_type == "openended":
            q_public["accepted_answers"] = question.get("accepted_answers", [])
        q_public["explanation"] = question.get("explanation", "")

    return q_public


def _ws_notify(room: str | None = None) -> None:
    """Notify the WebSocket clients of a room (or all clients) about a state change."""
    try:
        WS_MANAGER.broadcast_state(room)
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


# Game ticker: drives timer-based phase transitions for WS clients.
TICK_INTERVAL_ACTIVE_SEC = 0.3  # question/reveal: a timeout may fire any moment
TICK_INTERVAL_IDLE_SEC = 1.0    # lobby/finished: nothing is time-driven

_ticker_thread: threading.Thread | None = None
_ticker_thread_lock = threading.Lock()


def _quiz_ticker_loop() -> None:
    """Background thread: broadcast timer-driven phase changes over WS.

    Without it, auto transitions would only happen when some client polls
    /api/state — and WS clients stop polling, so they would hang on a stale
    phase (host tabs in background get throttled by the browser too).
    """
    last_sweep = time.time()
    while True:
        busy = False
        for room in ROOMS.all():
            try:
                room.quiz.tick_and_notify()
                busy = busy or room.quiz.phase in ("question", "reveal")
            except Exception:
                pass
        if time.time() - last_sweep > 600:
            last_sweep = time.time()
            try:
                ROOMS.drop_idle(keep=DEFAULT_ROOM)
            except Exception:
                pass
        time.sleep(TICK_INTERVAL_ACTIVE_SEC if busy else TICK_INTERVAL_IDLE_SEC)


def start_quiz_ticker() -> None:
    """Start the game ticker thread (idempotent).

    Started lazily from main() — never at import time — so importing this
    module (e.g. in tests) creates no background activity by itself.
    """
    global _ticker_thread
    with _ticker_thread_lock:
        if _ticker_thread is None or not _ticker_thread.is_alive():
            _ticker_thread = threading.Thread(target=_quiz_ticker_loop, daemon=True, name="quiz-ticker")
            _ticker_thread.start()


SERVER_INFO: dict[str, Any] = {
    "bind_host": "0.0.0.0",
    "port": DEFAULT_PORT,
    "host_urls": [f"http://127.0.0.1:{DEFAULT_PORT}/host"],
    "play_urls": [f"http://127.0.0.1:{DEFAULT_PORT}/play"],
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
# True when the AI endpoint comes from the server configuration (--ai-base-url /
# AI_BASE_URL): teachers may then only pick a model, while the endpoint and the
# API key stay on the server (no key in the browser, no arbitrary URLs = SSRF).
AI_CONFIG_LOCKED = False


def ai_public_config(shared_teacher: bool = False) -> dict[str, Any]:
    """AI settings that may be sent to the browser — never the API key itself.

    shared_teacher: a teacher on a multi-teacher server. The settings serve all
    teachers, so only the server administrator changes them; the teacher picks
    a model for each generation.
    """
    return {
        "base_url": AI_CONFIG["base_url"],
        "model": AI_CONFIG["model"],
        "api_key_set": bool(AI_CONFIG["api_key"]),
        "locked": AI_CONFIG_LOCKED or shared_teacher,
        "model_locked": shared_teacher,
    }


# Outgoing e-mail (password reset, e-mail verification, teacher invitations).
# MAIL_MODE=log (default) only writes messages to data/outbox.
MAILER = mailer.Mailer.from_env(outbox_dir=user_db._DB_DIR / "outbox")


def public_base_url() -> str:
    """Base URL for links in e-mails.

    QUIZ_BASE_URL when set, otherwise the address shown on /host. Never taken
    from the request Host header, which a client controls (link poisoning).
    """
    if QUIZ_BASE_URL:
        return QUIZ_BASE_URL
    host_urls = SERVER_INFO.get("host_urls") or [f"http://127.0.0.1:{DEFAULT_PORT}/host"]
    return host_urls[0].rsplit("/host", 1)[0]


def _mail_lang(data: dict[str, Any]) -> str:
    lang = str(data.get("lang", "cs"))
    return lang if lang in mailer.LANGS else "cs"


def _send_reset_link(login: str, lang: str) -> None:
    """Background part of /api/auth/forgot-password."""
    try:
        user = user_db.find_user_for_reset(login)
        if user and MAIL_LIMITER_USER.is_allowed(f"reset:{user['id']}"):
            raw = user_db.create_user_token("reset", user_id=user["id"])
            send_account_mail("reset", user["email"], lang, nickname=user["nickname"],
                              link=f"{public_base_url()}/reset?token={raw}")
    except Exception as e:  # noqa: BLE001 — never break the request thread pool
        print(f"reset link not sent: {type(e).__name__}: {e}")


def send_account_mail(kind: str, to: str, lang: str, **values: str) -> str:
    """Render and queue an account e-mail. Returns the mail mode, 'rate_limited' or 'error'."""
    if MAILER.mode != "off" and not GLOBAL_MAIL_LIMITER.is_allowed("all"):
        print(f"mail not queued ({kind}): server-wide hourly limit reached")
        return "rate_limited"
    values.setdefault("base", public_base_url())
    subject, body = mailer.render(kind, lang, **values)
    try:
        return MAILER.send(to, subject, body)
    except Exception as e:  # noqa: BLE001 — a broken mail must not break the request
        print(f"mail not queued ({kind}): {type(e).__name__}: {e}")
        return "error"

# WebSocket connection manager
WS_MANAGER = WSConnectionManager()
def _ws_state(player_id: str | None = None, host_view: bool = False, room: str | None = None) -> dict[str, Any]:
    target = ROOMS.by_key(room) if room else DEFAULT_ROOM
    if target is None:
        # the room was closed (idle or its teacher lost the role): the manager
        # drops the connection and the client falls back to polling
        raise LookupError("room closed")
    return target.quiz.public_state(player_id=player_id, host_view=host_view)


WS_MANAGER.set_state_getter(_ws_state)


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

    # Method 3: Windows 'ipconfig' parse (Linux 'ip addr' is absent there)
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["ipconfig"], capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    line = line.strip()
                    # Localized Windows: match "IPv4 ... : 192.168.1.10"
                    if "IPv4" in line or "IPv4 Address" in line:
                        m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", line)
                        if m:
                            add(m.group(1))
        except (OSError, subprocess.TimeoutExpired):
            pass

    # Method 4: hostname resolution fallback
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
            "public": True,  # reachable from the internet, not just the classroom Wi-Fi
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
    server_version = f"ClassRally/{APP_VERSION}"

    def _csv_response(self, text: str, filename: str) -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        if length > MAX_BODY_BYTES:
            raise _BodyTooLarge()
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

    def _send_verification(self, user: dict[str, Any] | None, lang: str) -> str:
        """Send the e-mail confirmation link (rate limited per account and address)."""
        if not user or not user.get("email"):
            return "error"
        if not (MAIL_LIMITER_USER.is_allowed(f"verify:{user['id']}")
                and VERIFY_MAIL_IP.is_allowed(self._get_client_ip())):
            return "rate_limited"
        raw = user_db.create_user_token("verify", user_id=user["id"], email=user["email"])
        return send_account_mail("verify", user["email"], lang, nickname=user["nickname"],
                                 link=f"{public_base_url()}/verify-email?token={raw}")

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
        # host=1 over WebSocket is privileged too (broadcast_state sends the
        # host view to is_host connections). Require the host token from the
        # handshake headers (X-Host-Token / Authorization: Bearer); without it
        # silently degrade to a public (non-host) connection. Shipped pages
        # never open a host WebSocket (host.html polls /api/state), so this
        # only affects non-browser clients, which can send headers.
        host_room = self._host_room() if q.get("host", ["0"])[0] == "1" else None
        room = host_room or self._viewer_room(player_id, q.get("pin", [None])[0])
        if room is None:
            self._json_response({"error": "PIN required", "code": "pin_required"}, status=400)
            return
        is_host = host_room is not None

        # Send 101 response
        response = ws_handshake_response(client_key)
        self.wfile.write(response)
        self.wfile.flush()

        # Hijack the socket
        conn_id = uuid.uuid4().hex[:12]
        raw_sock = self.request
        ws_conn = WSConnection(raw_sock, conn_id, player_id=player_id, is_host=is_host, room=room.key)
        WS_MANAGER.register(ws_conn)

        # Send initial state
        try:
            state = room.quiz.public_state(player_id=player_id, host_view=is_host)
            ws_conn.send_json(state)
        except Exception:
            pass

        # Run read loop in this thread (the HTTP handler thread)
        # This prevents the handler from closing the socket
        WS_MANAGER.read_loop(ws_conn)

    def _host_room(self) -> Room | None:
        """The room whose host token the request carries, silently (no response).

        Accepts the ``X-Host-Token`` header (preferred — never put the token
        into the query string, it would leak into access logs) or the legacy
        ``Authorization: Bearer <token>`` form.
        """
        candidates = []
        x_token = self.headers.get("X-Host-Token", "")
        if x_token:
            candidates.append(x_token)
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            candidates.append(auth[7:])
        for tok in candidates:
            room = ROOMS.by_token(tok)
            if room is not None:
                return room if self._owner_still_teacher(room) else None
        return None

    @staticmethod
    def _owner_still_teacher(room: Room) -> bool:
        """A teacher who lost the role or the account (console) loses the room too."""
        if room.owner_id is None:
            return True
        now = time.time()
        if now - room.owner_checked_at < ROOM_OWNER_RECHECK_SEC:
            return True
        user = user_db.get_user(room.owner_id)
        if user and user["role"] == "teacher":
            room.owner_checked_at = now
            return True
        ROOMS.drop(room)
        return False

    def _pin_room(self, pin: Any) -> Room | None:
        """Room of a PIN sent by a client; unknown PINs count as guesses (429 when too many).

        The PIN is normalized once and the same value is looked up and counted:
        padding a guess (spaces, "+") must not make different guesses look alike.
        """
        if not pin:
            return None
        ip = self._get_client_ip()
        if PIN_FAIL_IP.blocked(ip):
            raise _PinGuessBlocked()
        pin = str(pin).strip()
        if not ROOM_PIN_RE.fullmatch(pin):
            PIN_FAIL_IP.fail(ip, "malformed")  # never a room, counted once
            return None
        room = ROOMS.by_pin(pin)
        if room is None:
            PIN_FAIL_IP.fail(ip, pin)
        return room

    def _has_host_token(self) -> bool:
        return self._host_room() is not None

    def _require_host_room(self) -> Room | None:
        """Room of the host token; rejects the request with 403 when invalid."""
        room = self._host_room()
        if room is None:
            self._json_response({"error": "Invalid host token"}, status=403)
        return room

    def _require_host_token(self) -> bool:
        return self._require_host_room() is not None

    def _join_room(self, pin: Any) -> Room | None:
        """Room a new player joins: the PIN's room; in single mode the default room."""
        room = self._pin_room(pin)
        if room is None and ROOM_MODE == "single":
            room = DEFAULT_ROOM
        return room

    def _teacher_room(self) -> Room | None:
        """The logged-in teacher's own room (multi mode), else None."""
        if ROOM_MODE != "multi":
            return None
        uid = self._get_user_id_from_token()
        if uid is None:
            return None
        user = user_db.get_user(uid)
        return ROOMS.for_owner(uid) if user and user["role"] == "teacher" else None

    def _viewer_room(self, player_id: str | None, pin: Any = None) -> Room | None:
        """Room for a read request: the player's room, the PIN's room, the
        teacher's own room, or (single mode) the default room."""
        return (ROOMS.by_player(player_id) or self._pin_room(pin)
                or self._teacher_room() or (DEFAULT_ROOM if ROOM_MODE == "single" else None))

    def _owner_scope(self) -> int | None:
        """Teacher whose own data a request may see on a multi-teacher server.

        None means everything: single mode, the server administrator (admin
        password) or first-run open access.
        """
        if ROOM_MODE != "multi":
            return None
        uid = self._get_user_id_from_token()
        if uid is None:
            return None
        user = user_db.get_user(uid)
        return uid if user and user["role"] == "teacher" else None

    @staticmethod
    def _visible_users(teacher_id: int) -> list[dict[str, Any]]:
        """All accounts (single mode) or teachers + the teacher's own students (multi)."""
        if ROOM_MODE == "multi":
            return user_db.list_users_for_teacher(teacher_id)
        return user_db.list_all_users()

    def _may_manage_account(self, target_id: int, teacher_id: int) -> bool:
        """Multi-teacher server: a teacher manages only accounts of students who are
        in their classes and nobody else's (sends 403 otherwise)."""
        if ROOM_MODE != "multi" or user_db.is_only_student_of(target_id, teacher_id):
            return True
        msg = ("Zak je i ve tride jineho ucitele — muzete ho jen vyradit ze sve tridy."
               if user_db.is_student_of(target_id, teacher_id) else "Spravovat muzete jen zaky svych trid.")
        self._json_response({"error": msg}, status=403)
        return False

    def _owns(self, owner_id: int | None) -> bool:
        scope = self._owner_scope()
        return scope is None or owner_id == scope

    def _player_exam(self, player_id: str) -> "ExamState":
        """Exam of the room a student registered in (default room if unknown)."""
        return (ROOMS.by_player(player_id) or DEFAULT_ROOM).exam

    def _admin_room(self) -> Room:
        """Room managed by an authorized teacher/admin request (after _require_admin)."""
        return self._teacher_room() or DEFAULT_ROOM

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._do_get()
        except _PinGuessBlocked:
            self._pin_guess_response()

    def _pin_guess_response(self) -> None:
        self._json_response({"error": "Prilis mnoho pokusu o PIN, zkuste to za par minut",
                             "code": "pin_guesses"}, status=429)

    def _do_get(self) -> None:
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

        if path == "/exam":
            self._serve_file(STATIC_DIR / "exam.html")
            return

        if path == "/profile":
            self._serve_file(STATIC_DIR / "profile.html")
            return

        if path in ("/reset", "/verify-email", "/invite"):
            # One page for forgotten password, e-mail confirmation and teacher
            # invitations; the token is redeemed by a POST from the page, so
            # link scanners that prefetch e-mail links cannot use it up.
            self._serve_file(STATIC_DIR / "account.html")
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
            if player_id and player_id in KICKED_PLAYERS:
                # removed by the teacher: the device tells the student and offers to join again
                self._json_response({"error": "Removed by the teacher", "code": "player_kicked"}, status=404)
                return
            # Host view is privileged: it exposes player_id of every player,
            # the correct answer already during the question phase and bot
            # scores. Grant it only with a valid host token; otherwise degrade
            # silently to the public view (no error) so the host page keeps
            # working before the token has been entered (lobby/QR screen).
            host_room = self._host_room() if q.get("host", ["0"])[0] == "1" else None
            room = host_room or self._viewer_room(player_id, q.get("pin", [None])[0])
            if room is None:
                if player_id:
                    # restart, a closed room or a new PIN: the device has to join again
                    self._json_response({"error": "Unknown player", "code": "player_unknown"}, status=404)
                else:
                    self._json_response({"error": "PIN required", "code": "pin_required"}, status=400)
                return
            self._json_response(room.quiz.public_state(player_id=player_id, host_view=host_room is not None))
            return

        if path == "/api/random-nickname":
            room = self._join_room(parse_qs(parsed.query).get("pin", [None])[0]) or DEFAULT_ROOM
            self._json_response({"nickname": _generate_random_nickname(room.quiz.players)})
            return

        if path == "/api/health":
            self._json_response({"ok": True, "service": "classrally", "version": APP_VERSION})
            return

        if path == "/api/bug-report/config":
            self._json_response(bugreport.public_config())
            return

        if path.startswith("/buggy/"):
            asset = bugreport.widget_file(path.removeprefix("/buggy/"))
            if asset is None:
                self._text_response("Not found", status=404)
                return
            body, content_type = asset
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/mode":
            pin = parse_qs(parsed.query).get("pin", [None])[0]
            room = self._join_room(pin)
            payload: dict[str, Any] = {"mode": room.mode if room else "game", "rooms": ROOM_MODE,
                                       "pin_required": room is None}
            if pin and room is None:
                payload["pin_unknown"] = True
            self._json_response(payload)
            return

        if path == "/api/exam/state":
            q = parse_qs(parsed.query)
            player_id = q.get("player_id", [None])[0]
            secret = q.get("secret", [None])[0]
            pin = q.get("pin", [None])[0]
            own = ROOMS.by_player(player_id)
            if own is not None and pin:
                linked = self._pin_room(pin)
                if linked is not None and linked is not own:
                    # a link of another room: the student starts over there. A stale
                    # PIN of the own room (after "new PIN") keeps the test going.
                    self._json_response({"phase": linked.exam.phase, "known": False, "other_room": True})
                    return
            room = own or self._viewer_room(None, pin) or DEFAULT_ROOM
            if not player_id:
                self._json_response({"phase": room.exam.phase, "known": False})
                return
            self._json_response(room.exam.student_view(player_id, secret))
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
                url = urls[0] if urls else f"http://127.0.0.1:{SERVER_INFO.get('port', DEFAULT_PORT)}/play"
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
            room = self._admin_room()
            self._json_response({"host_token": room.host_token, "pin": room.pin, "rooms": ROOM_MODE})
            return

        if path == "/api/host/exam/overview":
            room = self._require_host_room()
            if room is None:
                return
            self._json_response(room.exam.overview())
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
            self._json_response(list_question_banks(self._owner_scope()))
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
                questions = load_questions_from_file(filename, self._owner_scope())
                self._json_response({"filename": filename, "questions": questions})
            except Exception as e:
                self._json_response({"error": str(e)}, status=404)
            return

        if path == "/api/admin/history":
            if not self._require_admin():
                return
            self._json_response([history_overview(r) for r in list_game_history(self._owner_scope())])
            return

        if path in ("/api/admin/history/detail", "/api/admin/history/items.csv"):
            if not self._require_admin():
                return
            game_id = parse_qs(parsed.query).get("game_id", [""])[0]
            record = load_game_record(game_id)
            if record is None or not self._owns(record.get("owner_id")):
                self._json_response({"error": "Game not found"}, status=404)
                return
            if path.endswith(".csv"):
                self._csv_response(game_items_csv(record), f"hra-{game_id}-otazky.csv")
            else:
                self._json_response(history_detail(record))
            return

        if path == "/api/admin/activity":
            # server administrator only (admin password): counts, no names
            if not self._require_admin():
                return
            if self._owner_scope() is not None:
                self._json_response({"error": "Jen pro spravce serveru"}, status=403)
                return
            self._json_response(server_activity())
            return

        if path == "/api/admin/exam/items":
            if not self._require_admin():
                return
            self._json_response(self._admin_room().exam.item_stats())
            return

        if path in ("/api/admin/exam/answers.csv", "/api/admin/exam/items.csv"):
            if not self._require_admin():
                return
            exam = self._admin_room().exam
            if path.endswith("answers.csv"):
                self._csv_response(exam.answers_csv(), "pisemka-odpovedi.csv")
            else:
                self._csv_response(exam.items_csv(), "pisemka-rozbor-otazek.csv")
            return

        if path == "/api/admin/exams":
            if not self._require_admin():
                return
            self._json_response(user_db.list_exam_sessions(self._owner_scope()))
            return

        if path in ("/api/admin/exams/detail", "/api/admin/exams/results.csv",
                    "/api/admin/exams/answers.csv", "/api/admin/exams/items.csv"):
            if not self._require_admin():
                return
            exam_id = parse_qs(parsed.query).get("id", [""])[0]
            session = user_db.get_exam_session(exam_id) if EXAM_ID_RE.match(exam_id) else None
            if session is None or not self._owns(session["owner_id"]):
                self._json_response({"error": "Exam not found"}, status=404)
                return
            questions, rows = session["questions"], session["results"]
            if path.endswith("results.csv"):
                self._csv_response(exam_results_csv(questions, rows), f"pisemka-{exam_id}-vysledky.csv")
            elif path.endswith("answers.csv"):
                self._csv_response(exam_answers_csv(questions, rows), f"pisemka-{exam_id}-odpovedi.csv")
            elif path.endswith("items.csv"):
                self._csv_response(exam_items_csv(questions, rows), f"pisemka-{exam_id}-rozbor-otazek.csv")
            else:
                self._json_response({
                    **{k: v for k, v in session.items() if k not in ("results", "questions")},
                    "total_questions": len(questions),
                    "students": [{k: v for k, v in r.items() if k not in ("player_id", "answers")} for r in rows],
                    "items": exam_item_stats(questions, rows),
                })
            return

        if path == "/api/admin/exam/overview":
            if not self._require_admin():
                return
            self._json_response(self._admin_room().exam.overview())
            return

        if path == "/api/admin/exam/config":
            if not self._require_admin():
                return
            self._json_response(self._admin_room().exam.config.to_dict())
            return

        if path == "/api/admin/exam/results.csv":
            if not self._require_admin():
                return
            self._csv_response(self._admin_room().exam.results_csv(), "vysledky-pisemky.csv")
            return

        if path == "/api/admin/ollama/models":
            if not self._require_admin():
                return
            models = list_ai_models(AI_CONFIG["base_url"], AI_CONFIG["api_key"])
            self._json_response({"models": models, "config": ai_public_config(self._owner_scope() is not None)})
            return

        if path == "/api/admin/ollama/config":
            if not self._require_admin():
                return
            self._json_response(ai_public_config(self._owner_scope() is not None))
            return

        if path == "/api/mail/status":
            if not self._require_admin():
                return
            self._json_response(MAILER.status())
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
            self._json_response({"users": self._visible_users(tid)})
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
            if not user_db.can_view_class(class_id, uid):
                self._json_response({"error": "Trida nenalezena"}, status=404)
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
            if not user_db.can_view_class(class_id, uid):
                self._json_response({"error": "Trida nenalezena"}, status=404)
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
            # scores of every student: for the class teacher, not for classmates
            if not user_db.can_view_class(class_id, uid, teacher_only=True):
                self._json_response({"error": "Trida nenalezena"}, status=404)
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
            record = load_game_record(game_id)
            if record is None or not self._owns(record.get("owner_id")):
                self._json_response({"error": "Game not found"}, status=404)
                return
            self._csv_response(game_results_to_csv(record), f"game_{game_id}_results.csv")
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
                questions = load_questions_from_file(filename, self._owner_scope())
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
                questions = load_questions_from_file(filename, self._owner_scope())
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
            if not progress or not user_db.selfpaced_secret_ok(session_id, player_id, q.get("secret", [None])[0]):
                self._json_response({"error": "Player not registered in this session"}, status=404)
                return
            questions = session["questions"]
            answered_count = len(progress["answers"])
            finished = progress["finished_at"] is not None

            if finished or answered_count >= len(questions):
                summary = []
                for i, ans_rec in enumerate(progress["answers"]):
                    q_data = questions[i] if i < len(questions) else {}
                    shown = ans_rec.get("item_order")  # answers from before the shuffle have none
                    choice = ans_rec.get("choice")
                    if shown and isinstance(choice, list):
                        choice = [shown.index(c) for c in choice]
                    summary.append({
                        "question": _public_question_for_selfpaced(q_data, reveal=True, item_order=shown),
                        "choice": choice,
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
                "question": _public_question_for_selfpaced(
                    current_q, reveal=False, item_order=_selfpaced_item_order(current_q, session_id, player_id)),
                "score": progress["score"],
                "player_name": progress["player_name"],
                "bank_name": session["bank_name"],
            })
            return

        if path == "/api/selfpaced/mine":
            if not self._require_admin():
                return
            self._json_response({"sessions": user_db.list_selfpaced_sessions(self._owner_scope())})
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
            if not session or not self._owns(session.get("owner_id")):
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
            if path == "/api/bug-report":
                self._handle_bug_report()
                return
            data = self._read_json()

            if path == "/api/register":
                client_ip = self._get_client_ip()
                if not REGISTER_LIMITER.is_allowed(client_ip):
                    self._json_response(
                        {"error": "Prilis mnoho registraci, zkuste to pozdeji"}, status=429,
                    )
                    return
                room = self._join_room(data.get("pin"))
                if room is None:
                    unknown = bool(data.get("pin"))
                    self._json_response(
                        {"error": "Neznamy PIN hry" if unknown else "Zadejte PIN hry",
                         "code": "pin_unknown" if unknown else "pin_required"}, status=400)
                    return
                name = str(data.get("name", ""))
                if room.mode == "exam":
                    result = room.exam.register_student(name)
                else:
                    # Optional: link to user account if user_token provided
                    user_id = None
                    user_token = data.get("user_token") or self._get_user_token()
                    if user_token:
                        user_id = user_db.validate_session(user_token)
                    result = room.quiz.register_player(name, user_id=user_id, avatar_id=data.get("avatar_id", 0))
                ROOMS.bind_player(result["player_id"], room)
                room.touch()
                result["mode"] = room.mode
                if ROOM_MODE == "multi":
                    result["pin"] = room.pin
                self._json_response(result)
                return

            # --- Exam (písemka) student API ---
            if path == "/api/exam/answer":
                player_id = str(data.get("player_id", ""))
                secret = str(data.get("player_secret", ""))
                qid = str(data.get("question_id", ""))
                choice = int(data.get("choice", -1))
                self._json_response(self._player_exam(player_id).save_answer(player_id, secret, qid, choice))
                return

            if path == "/api/exam/position":
                player_id = str(data.get("player_id", ""))
                secret = str(data.get("player_secret", ""))
                pos = int(data.get("position", 0))
                self._json_response(self._player_exam(player_id).set_position(player_id, secret, pos))
                return

            if path == "/api/exam/event":
                player_id = str(data.get("player_id", ""))
                secret = str(data.get("player_secret", ""))
                event_type = str(data.get("type", ""))
                self._json_response(self._player_exam(player_id).record_event(player_id, secret, event_type))
                return

            if path == "/api/exam/submit":
                player_id = str(data.get("player_id", ""))
                secret = str(data.get("player_secret", ""))
                self._json_response(self._player_exam(player_id).submit(player_id, secret))
                return

            if path == "/api/submit":
                player_id = str(data.get("player_id", ""))
                player_secret = str(data.get("player_secret", ""))
                choice = data.get("choice", -1)
                # For backward compat: if choice is a number string, convert to int
                if isinstance(choice, (int, float)) and not isinstance(choice, bool):
                    choice = int(choice)
                room = ROOMS.by_player(player_id) or DEFAULT_ROOM
                self._json_response(room.quiz.submit_answer(player_id, player_secret, choice))
                return

            if path == "/api/host/kick":
                room = self._require_host_room()
                if room is None:
                    return
                player_id = str(data.get("player_id", ""))
                result = room.quiz.kick_player(player_id)
                KICKED_PLAYERS.add(player_id)
                if ROOMS.by_player(player_id) is room:
                    ROOMS.unbind_player(player_id)
                self._json_response(result)
                return

            if path == "/api/host/new-pin":
                # next lesson: empty lobby and a PIN the previous class does not know
                room = self._require_host_room()
                if room is None:
                    return
                if ROOM_MODE != "multi":
                    self._json_response({"error": "PIN mistnosti se pouziva jen pri QUIZ_ROOMS=multi"},
                                        status=400)
                    return
                self._json_response({"ok": True, "pin": ROOMS.renew(room), "rooms": ROOM_MODE})
                return

            if path == "/api/host/action":
                room = self._require_host_room()
                if room is None:
                    return
                room.touch()
                action = str(data.get("action", ""))
                self._json_response(room.quiz.host_action(action))
                return

            if path == "/api/host/exam-action":
                room = self._require_host_room()
                if room is None:
                    return
                room.touch()
                exam = room.exam
                action = str(data.get("action", ""))
                if action == "open":
                    exam.open_exam()
                elif action == "end":
                    exam.end_exam()
                elif action == "extend":
                    exam.extend(int(data.get("seconds", 300)))
                elif action == "reset":
                    exam.reset()
                else:
                    raise ValueError("Neznamy action")
                self._json_response({"ok": True, "phase": exam.phase})
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
                    filename = save_questions_to_file(filename, questions, self._owner_scope())
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
                    filename = save_questions_to_file(filename, questions, self._owner_scope())
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
                delete_question_bank(filename, self._owner_scope())
                self._json_response({"ok": True})
                return

            if path == "/api/admin/bank/activate":
                if not self._require_admin():
                    return
                filename = str(data.get("filename", ""))
                mode = str(data.get("mode", "game"))
                if not filename:
                    self._json_response({"error": "filename required"}, status=400)
                    return
                try:
                    questions = load_questions_from_file(filename, self._owner_scope())
                    room = self._admin_room()
                    if mode == "exam":
                        room.exam.reload_questions(questions, bank_name=filename)
                        set_app_mode("exam", room)
                    else:
                        room.quiz.reload_questions(questions, bank_name=filename)
                        set_app_mode("game", room)
                    room.touch()
                    self._json_response({"ok": True, "count": len(questions), "mode": room.mode})
                except FileNotFoundError as e:
                    self._json_response({"error": str(e)}, status=404)
                except Exception as e:
                    self._json_response({"error": str(e)}, status=400)
                return

            if path == "/api/admin/mode":
                if not self._require_admin():
                    return
                mode = str(data.get("mode", ""))
                if mode not in ("game", "exam"):
                    self._json_response({"error": "mode must be game or exam"}, status=400)
                    return
                self._json_response({"ok": True, "mode": set_app_mode(mode, self._admin_room())})
                return

            if path == "/api/admin/exam/config":
                if not self._require_admin():
                    return
                self._json_response({"ok": True, "config": self._admin_room().exam.update_config(data)})
                return

            if path == "/api/admin/timing":
                if not self._require_admin():
                    return
                q_sec = data.get("question_duration_sec")
                r_sec = data.get("reveal_duration_sec")
                g_sec = data.get("get_ready_sec")
                f_double = data.get("final_double")
                speed = data.get("speed_bonus")
                board = data.get("show_leaderboard")
                adaptive = data.get("adaptive_time")
                quiz = self._admin_room().quiz
                quiz.set_timing(
                    question_sec=int(q_sec) if q_sec is not None else None,
                    reveal_sec=int(r_sec) if r_sec is not None else None,
                    get_ready_sec=int(g_sec) if g_sec is not None else None,
                    final_double=bool(f_double) if f_double is not None else None,
                    speed_bonus=bool(speed) if speed is not None else None,
                    show_leaderboard=bool(board) if board is not None else None,
                    adaptive_time=bool(adaptive) if adaptive is not None else None,
                )
                self._json_response({
                    "ok": True,
                    "question_duration_sec": quiz.question_duration_sec,
                    "reveal_duration_sec": quiz.reveal_duration_sec,
                    "get_ready_sec": quiz.get_ready_sec,
                    "final_double": quiz.final_double,
                    "speed_bonus": quiz.speed_bonus,
                    "show_leaderboard": quiz.show_leaderboard,
                    "adaptive_time": quiz.adaptive_time,
                })
                return

            if path == "/api/admin/team-mode":
                if not self._require_admin():
                    return
                enabled = bool(data.get("enabled", False))
                num_teams = int(data.get("num_teams", 2))
                team_names_raw = data.get("team_names")
                team_names = [str(n) for n in team_names_raw] if isinstance(team_names_raw, list) else None
                quiz = self._admin_room().quiz
                quiz.set_team_mode(enabled, num_teams=num_teams, team_names=team_names)
                self._json_response({
                    "ok": True,
                    "team_mode": quiz.team_mode,
                    "num_teams": quiz.num_teams,
                    "team_names": quiz.team_names,
                    "team_colors": TEAM_COLORS[:len(quiz.team_names)],
                })
                return

            if path == "/api/admin/exams/delete":
                if not self._require_admin():
                    return
                exam_id = str(data.get("id", ""))
                session = user_db.get_exam_session(exam_id) if EXAM_ID_RE.match(exam_id) else None
                if session is None or not self._owns(session["owner_id"]):
                    self._json_response({"error": "Not found"}, status=404)
                elif session["ended_at"] is None:
                    self._json_response({"error": "Pisemka prave probiha, nejdriv ji ukoncete."}, status=409)
                elif user_db.delete_exam_session(exam_id):
                    self._json_response({"ok": True})
                else:
                    self._json_response({"error": "Not found"}, status=404)
                return

            if path == "/api/admin/history/delete":
                if not self._require_admin():
                    return
                game_id = str(data.get("game_id", ""))
                record = load_game_record(game_id)
                if record is not None and self._owns(record.get("owner_id")) and delete_game_history(game_id):
                    self._json_response({"ok": True})
                else:
                    self._json_response({"error": "Not found"}, status=404)
                return

            if path == "/api/admin/ollama/config":
                if not self._require_admin():
                    return
                if self._owner_scope() is not None:
                    self._json_response({"error": "Nastaveni AI je spolecne pro vsechny ucitele, meni ho "
                                                  "spravce serveru. Model zvolite primo u generovani."},
                                        status=403)
                    return
                if not AI_CONFIG_LOCKED:
                    if "base_url" in data:
                        AI_CONFIG["base_url"] = str(data["base_url"])
                    elif "host" in data or "port" in data:
                        host = str(data.get("host", "localhost"))
                        port = int(data.get("port", 11434))
                        AI_CONFIG["base_url"] = f"http://{host}:{port}"
                    # The browser never receives the key, so an empty field means "keep".
                    if data.get("api_key"):
                        AI_CONFIG["api_key"] = str(data["api_key"])
                if data.get("model"):
                    AI_CONFIG["model"] = str(data["model"])
                self._json_response({"ok": True, "config": ai_public_config()})
                return

            if path == "/api/admin/ai/generate":
                if not self._require_admin():
                    return
                topic = str(data.get("topic", "")).strip()
                try:
                    count = max(1, min(20, int(data.get("count", 5))))
                except (ValueError, TypeError):
                    count = 5
                model = str(data.get("model") or AI_CONFIG["model"])
                language = str(data.get("language", "cs"))
                source_text = str(data.get("source_text", ""))[:AI_SOURCE_TEXT_MAX]
                base_url, api_key = AI_CONFIG["base_url"], AI_CONFIG["api_key"]
                if not AI_CONFIG_LOCKED and self._owner_scope() is None:
                    # Local single-teacher setups may still point a request elsewhere.
                    requested = ""
                    if data.get("base_url"):
                        requested = str(data["base_url"])
                    elif "host" in data or "port" in data:
                        host = str(data.get("host", "localhost"))
                        port = int(data.get("port", 11434))
                        requested = f"http://{host}:{port}"
                    if requested and requested.rstrip("/") != base_url.rstrip("/"):
                        # the stored key belongs to the configured server, never to another address
                        base_url, api_key = requested, ""
                    api_key = str(data.get("api_key") or api_key)
                if not topic:
                    self._json_response({"error": "topic required"}, status=400)
                    return
                try:
                    questions = generate_questions_ai(
                        topic=topic, count=count,
                        base_url=base_url, model=model,
                        api_key=api_key, language=language,
                        source_text=source_text,
                    )
                    self._json_response({"ok": True, "questions": questions})
                except Exception as e:
                    self._json_response({"error": str(e)}, status=500)
                return

            # --- User Auth API (POST) ---
            if path == "/api/auth/register":
                if not ACCOUNT_REGISTER_LIMITER.is_allowed(self._get_client_ip()):
                    self._json_response({"error": "Prilis mnoho registraci, zkuste to pozdeji"}, status=429)
                    return
                nickname = str(data.get("nickname", ""))
                password = str(data.get("password", ""))
                role = str(data.get("role", "student"))
                email = str(data.get("email", "") or "").strip() or None
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
                    user = user_db.create_user(nickname, password, role, avatar_id=avatar_id, email=email)
                    token = user_db.create_session(user["id"])
                    payload = {"ok": True, "token": token, "user": user}
                    if user.get("email"):
                        payload["mail"] = self._send_verification(user, _mail_lang(data))
                    self._json_response(payload)
                except ValueError as e:
                    self._json_response({"error": str(e)}, status=400)
                return

            if path == "/api/auth/login":
                nickname = str(data.get("nickname", ""))
                password = str(data.get("password", ""))
                if not nickname.strip() or len(nickname.strip()) > 24 or len(password) > 1024:
                    # cannot be a real account (nicknames are 1-24 chars): answer like a
                    # wrong password, without creating limiter keys or hashing megabytes
                    self._json_response({"error": "Neplatne prihlasovaci udaje"}, status=401)
                    return
                ip_key = self._get_client_ip()
                nick_key = nickname.strip().lower()
                pair_key = f"{nick_key}|{ip_key}"
                if (LOGIN_FAIL_IP.blocked(ip_key) or LOGIN_FAIL_NICK_IP.blocked(pair_key)
                        or LOGIN_FAIL_NICK.blocked(nick_key)):
                    self._json_response(
                        {"error": "Prilis mnoho neuspesnych pokusu, zkuste to za par minut", "code": "rate_limited"},
                        status=429,
                    )
                    return
                user = user_db.authenticate_user(nickname, password)
                if user:
                    token = user_db.create_session(user["id"])
                    self._json_response({"ok": True, "token": token, "user": user})
                else:
                    LOGIN_FAIL_IP.fail(ip_key)
                    LOGIN_FAIL_NICK_IP.fail(pair_key)
                    LOGIN_FAIL_NICK.fail(nick_key)
                    self._json_response({"error": "Neplatne prihlasovaci udaje"}, status=401)
                return

            # --- Account recovery, e-mail confirmation, teacher invitations ---

            if path == "/api/auth/forgot-password":
                # Same answer whether or not the account exists (no enumeration).
                if not FORGOT_LIMITER_IP.is_allowed(self._get_client_ip()):
                    self._json_response({"error": "Prilis mnoho pozadavku, zkuste to pozdeji"}, status=429)
                    return
                # Looked up and sent in the background: the answer and its timing are
                # the same whether or not the account exists.
                login = str(data.get("login", ""))[:254]
                threading.Thread(target=_send_reset_link, daemon=True,
                                 args=(login, _mail_lang(data))).start()
                self._json_response({"ok": True})
                return

            if path == "/api/auth/token-check":
                purpose = str(data.get("purpose", ""))
                tok = user_db.peek_user_token(str(data.get("token", "")), purpose) \
                    if purpose in user_db.TOKEN_TTL else None
                payload: dict[str, Any] = {"valid": tok is not None}
                if tok and purpose == "invite":
                    payload["email"] = tok["email"]
                self._json_response(payload)
                return

            if path == "/api/auth/reset-password":
                try:
                    user = user_db.reset_password_with_token(
                        str(data.get("token", "")), str(data.get("new_password", "")))
                except ValueError as e:
                    self._json_response({"error": str(e), "code": "weak_password"}, status=400)
                    return
                if not user:
                    self._json_response({"error": "Odkaz je neplatny nebo vyprsel", "code": "invalid_token"}, status=400)
                    return
                if user.get("email"):
                    send_account_mail("password_changed", user["email"], _mail_lang(data),
                                      nickname=user["nickname"])
                self._json_response({"ok": True, "nickname": user["nickname"]})
                return

            if path == "/api/auth/verify-email":
                try:
                    user = user_db.verify_email_with_token(str(data.get("token", "")))
                except ValueError as e:
                    self._json_response({"error": str(e), "code": "email_taken"}, status=400)
                    return
                if not user:
                    self._json_response({"error": "Odkaz je neplatny nebo vyprsel", "code": "invalid_token"}, status=400)
                    return
                self._json_response({"ok": True, "nickname": user["nickname"], "email": user["email"]})
                return

            if path == "/api/auth/set-email":
                uid = self._require_user()
                if uid is None:
                    return
                if PASSWORD_FAIL_USER.blocked(f"pw:{uid}") or not SET_EMAIL_LIMITER.is_allowed(f"email:{uid}"):
                    self._json_response({"error": "Prilis mnoho pokusu, zkuste to pozdeji", "code": "rate_limited"}, status=429)
                    return
                try:
                    result = user_db.set_email(uid, str(data.get("email", "")), str(data.get("password", "")))
                except user_db.WrongPassword as e:
                    PASSWORD_FAIL_USER.fail(f"pw:{uid}")
                    self._json_response({"error": str(e)}, status=400)
                    return
                except ValueError as e:
                    self._json_response({"error": str(e)}, status=400)
                    return
                if result["changed"]:
                    result["mail"] = self._send_verification(user_db.get_user(uid), _mail_lang(data))
                self._json_response({"ok": True, **result})
                return

            if path == "/api/auth/resend-verification":
                uid = self._require_user()
                if uid is None:
                    return
                user = user_db.get_user(uid)
                if not user or not user.get("email"):
                    self._json_response({"error": "Ucet nema e-mail"}, status=400)
                    return
                if user.get("email_verified"):
                    self._json_response({"ok": True, "already_verified": True})
                    return
                self._json_response({"ok": True, "mail": self._send_verification(user, _mail_lang(data))})
                return

            if path == "/api/auth/invite-teacher":
                tid = self._require_teacher()
                if tid is None:
                    return
                try:
                    email = user_db.normalize_email(str(data.get("email", "")))
                except ValueError as e:
                    self._json_response({"error": str(e)}, status=400)
                    return
                if not INVITE_LIMITER.is_allowed(f"invite:{tid}"):
                    self._json_response({"error": "Prilis mnoho pozvanek, zkuste to pozdeji"}, status=429)
                    return
                inviter = user_db.get_user(tid)
                raw = user_db.create_user_token("invite", email=email, created_by=tid)
                mode = send_account_mail("invite", email, _mail_lang(data),
                                         inviter=inviter["nickname"] if inviter else "ClassRally",
                                         link=f"{public_base_url()}/invite?token={raw}")
                self._json_response({"ok": True, "mail": mode})
                return

            if path == "/api/auth/accept-invite":
                avatar_id = data.get("avatar_id")
                try:
                    avatar_id = int(avatar_id) if avatar_id is not None else None
                except (ValueError, TypeError):
                    avatar_id = None
                try:
                    user = user_db.accept_invite(str(data.get("token", "")), str(data.get("nickname", "")),
                                                 str(data.get("password", "")), avatar_id=avatar_id)
                except ValueError as e:
                    self._json_response({"error": str(e)}, status=400)
                    return
                if not user:
                    self._json_response({"error": "Pozvanka je neplatna nebo vyprsela", "code": "invalid_token"}, status=400)
                    return
                token = user_db.create_session(user["id"])
                self._json_response({"ok": True, "token": token, "user": user})
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
                if PASSWORD_FAIL_USER.blocked(f"pw:{uid}"):
                    self._json_response({"error": "Prilis mnoho pokusu, zkuste to pozdeji", "code": "rate_limited"}, status=429)
                    return
                old_password = str(data.get("old_password", ""))
                new_password = str(data.get("new_password", ""))
                try:
                    user = user_db.change_password(uid, old_password, new_password,
                                                   keep_session=self._get_user_token())
                except user_db.WrongPassword as e:
                    PASSWORD_FAIL_USER.fail(f"pw:{uid}")
                    self._json_response({"error": str(e)}, status=400)
                    return
                except ValueError as e:
                    self._json_response({"error": str(e)}, status=400)
                    return
                if user and user.get("email") and user.get("email_verified"):
                    send_account_mail("password_changed", user["email"], _mail_lang(data),
                                      nickname=user["nickname"])
                self._json_response({"ok": True})
                return

            # --- User management (teacher only) ---

            if path == "/api/users":
                tid = self._require_teacher()
                if tid is None:
                    return
                self._json_response({"users": self._visible_users(tid)})
                return

            m = re.match(r"^/api/users/(\d+)/set-role$", path)
            if m:
                tid = self._require_teacher()
                if tid is None:
                    return
                target_id = int(m.group(1))
                if not self._may_manage_account(target_id, tid):
                    return
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
                if not self._may_manage_account(target_id, tid):
                    return
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
                if not self._may_manage_account(target_id, tid):
                    return
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
                if not self._may_manage_account(target_uid, teacher_id):
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
                if not self._may_manage_account(target_uid, teacher_id):
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
                    questions = load_questions_from_file(filename, self._owner_scope())
                except FileNotFoundError:
                    self._json_response({"error": f"Question bank not found: {filename}"}, status=404)
                    return
                except Exception as e:
                    self._json_response({"error": str(e)}, status=400)
                    return
                session_id = user_db.create_selfpaced_session(filename, questions, owner_id=self._owner_scope())
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
                user_db.set_selfpaced_secret(session_id, player_id, player_secret)
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
                if not progress or not user_db.selfpaced_secret_ok(session_id, player_id, data.get("secret")):
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
                    if any(type(c) is not int for c in choice) or sorted(choice) != list(range(len(items))):
                        self._json_response({"error": "Invalid permutation"}, status=400)
                        return
                elif q_type == "openended":
                    if not isinstance(choice, str) or not choice.strip():
                        self._json_response({"error": "choice must be non-empty string"}, status=400)
                        return
                    choice = choice.strip()[:200]

                item_order = _selfpaced_item_order(question, session_id, player_id)
                shown_choice = choice
                if item_order:
                    choice = [item_order[d] for d in choice]  # display positions -> items of the bank
                is_correct, points = _score_selfpaced_answer(question, choice)

                # Build new answers list
                new_answers = list(progress["answers"])
                answer_record = {
                    "choice": choice,
                    "correct": is_correct,
                    "points": points,
                }
                if item_order:
                    answer_record["item_order"] = item_order  # how the items were shown (summary)
                new_answers.append(answer_record)
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
                reveal_q = _public_question_for_selfpaced(question, reveal=True, item_order=item_order)
                self._json_response({
                    "ok": True,
                    "correct": is_correct,
                    "points": points,
                    "score": new_score,
                    "question_index": answered_count,
                    "total_questions": len(questions),
                    "finished": finished,
                    "question": reveal_q,
                    "choice": shown_choice,
                })
                return

            self._json_response({"error": "Not found"}, status=404)
        except _BodyTooLarge:
            self.close_connection = True  # the unread body stays in the socket
            self._json_response({"error": "Request too large"}, status=413)
        except _PinGuessBlocked:
            self._pin_guess_response()
        except PermissionError as exc:
            self._json_response({"error": str(exc)}, status=403)
        except FileNotFoundError as exc:
            self._json_response({"error": str(exc)}, status=404)
        except ValueError as exc:
            self._json_response({"error": str(exc)}, status=400)
        except Exception as exc:
            self._json_response({"error": f"Server error: {exc}"}, status=500)

    def _bug_reporter(self) -> tuple[str, str | None]:
        """Role of the reporter and, for teachers only, their nickname.

        Pupils are reported without any name: the report needs the page, not
        the child. A teacher's nickname lets the maintainer ask back.
        """
        uid = self._get_user_id_from_token()
        if uid is not None:
            user = user_db.get_user(uid)
            if user and user["role"] == "teacher":
                return "teacher", str(user["nickname"])
            if user:
                return "student", None
        if ADMIN_AUTH.enabled and ADMIN_AUTH.validate_session(self._get_admin_token()):
            return "teacher", None
        return "anonymous", None

    def _handle_bug_report(self) -> None:
        """POST /api/bug-report: validate, redact and forward to the collector."""
        if not bugreport.enabled():
            self._json_response({"error": "Hlaseni chyb neni zapnute", "code": "disabled"}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length <= 0 or length > bugreport.MAX_BODY_BYTES:
            self.close_connection = True
            self._json_response({"error": "Request too large", "code": "too_large"}, status=413)
            return
        client_ip = self._get_client_ip()
        if not (BUG_REPORT_LIMITER_IP.is_allowed(client_ip) and BUG_REPORT_LIMITER_ALL.is_allowed("all")):
            self.rfile.read(length)
            self._json_response({"error": "Prilis mnoho hlaseni, zkuste to za chvili",
                                 "code": "rate_limited"}, status=429)
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._json_response({"error": "Invalid JSON", "code": "invalid"}, status=400)
            return
        if not isinstance(data, dict):
            self._json_response({"error": "Invalid JSON", "code": "invalid"}, status=400)
            return
        role, reporter = self._bug_reporter()
        try:
            payload = bugreport.build_report(data, role=role, reporter=reporter,
                                             user_agent=self.headers.get("User-Agent", ""))
        except bugreport.BadReport as exc:
            self._json_response({"error": str(exc), "code": str(exc)}, status=400)
            return
        try:
            result = bugreport.forward(payload, data.get("clientRequestId"))
        except bugreport.DeliveryError as exc:
            self._json_response({"error": "Hlaseni se nepodarilo predat", "code": exc.code}, status=502)
            return
        self._json_response(result, status=201)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


def main() -> None:
    global QUESTION_DURATION_SEC, REVEAL_DURATION_SEC
    parser = argparse.ArgumentParser(description="Local classroom quiz web")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
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
    parser.add_argument("--set-password", metavar="NICKNAME", default="",
                        help="Emergency access: set a new password for the account (read from "
                             "the terminal or stdin) and exit without starting the server")
    parser.add_argument("--set-role", nargs=2, metavar=("NICKNAME", "ROLE"),
                        help="Server administration: set an account's role (student/teacher), "
                             "e.g. to remove a teacher, and exit")
    parser.add_argument("--trusted-proxies",
                        default=os.environ.get("QUIZ_TRUSTED_PROXIES", ""),
                        help="Comma-separated list of trusted reverse-proxy CIDRs/IPs whose "
                             "X-Forwarded-For header is honoured for rate limiting "
                             "(default: loopback + RFC-1918 ranges)  (env: QUIZ_TRUSTED_PROXIES)")
    args = parser.parse_args()

    # Initialize SQLite database
    user_db.init_db()

    if args.set_password:
        import getpass
        import sys
        if sys.stdin.isatty():
            new_password = getpass.getpass(f"Nove heslo pro '{args.set_password}': ")
        else:
            new_password = sys.stdin.readline().rstrip("\r\n")
        try:
            user = user_db.admin_set_password(args.set_password, new_password)
        except ValueError as e:
            print(f"CHYBA: {e}")
            sys.exit(1)
        print(f"Heslo uctu '{user['nickname']}' ({user['role']}) zmeneno, prihlaseni ukoncena.")
        sys.exit(0)

    if args.set_role:
        import sys
        try:
            user = user_db.admin_set_role(*args.set_role)
        except ValueError as e:
            print(f"CHYBA: {e}")
            sys.exit(1)
        print(f"Ucet '{user['nickname']}' ma roli {user['role']}, prihlaseni ukoncena.")
        sys.exit(0)

    user_db.migrate_json_history(HISTORY_DIR)

    if args.teacher_code:
        print("WARNING: --teacher-code is deprecated and no longer has any effect.")
    if user_db.teacher_exists():
        print("Teacher registration: closed (teacher accounts exist; invite more teachers from /admin)")
    else:
        print("Teacher registration: OPEN until the first teacher account is created")
    print(f"Outgoing e-mail: MAIL_MODE={MAILER.mode}"
          + (f" via {MAILER.host}:{MAILER.port}" if MAILER.mode == "live" else ""))

    if args.admin_password:
        ADMIN_AUTH.set_password(args.admin_password)
        print("Admin portal: password protected")
    else:
        print("Admin portal: open (no password)")

    # New --ai-* args take precedence over legacy --ollama-* args
    global AI_CONFIG_LOCKED
    if args.ai_base_url:
        AI_CONFIG["base_url"] = args.ai_base_url
        AI_CONFIG_LOCKED = True
    else:
        AI_CONFIG["base_url"] = f"http://{args.ollama_host}:{args.ollama_port}"
    if args.ai_api_key:
        AI_CONFIG["api_key"] = args.ai_api_key
    if args.ai_model:
        AI_CONFIG["model"] = args.ai_model
    elif args.ollama_model != "gpt-oss:20b":
        AI_CONFIG["model"] = args.ollama_model

    # CLI timing is the default for every room (QuizState reads these on creation)
    QUESTION_DURATION_SEC, REVEAL_DURATION_SEC = args.question_time, args.reveal_time
    QUIZ.set_timing(question_sec=args.question_time, reveal_sec=args.reveal_time)
    print(f"Rooms: {ROOM_MODE}" + (" (each teacher gets a room with a PIN)" if ROOM_MODE == "multi" else ""))

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
    # Start the game ticker (pushes timer-driven phase changes to WS clients)
    start_quiz_ticker()

    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    # only now (the port is ours, so no other instance runs on this data): exams
    # still open in the database were cut off by the last stop
    try:
        interrupted = close_interrupted_exams()
        if interrupted:
            print(f"Exams interrupted by the last restart: {interrupted} (graded from saved answers, see /admin)")
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: could not close interrupted exams: {type(e).__name__}: {e}")
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
