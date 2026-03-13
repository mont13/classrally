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
import hashlib
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

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
AUDIO_DIR = STATIC_DIR / "audio"
QUESTIONS_DIR = BASE_DIR / "questions"
HISTORY_DIR = BASE_DIR / "history"
DEFAULT_QUESTIONS_FILE = "default.json"
QUESTION_DURATION_SEC = 20
REVEAL_DURATION_SEC = 5

# Ensure directories exist
QUESTIONS_DIR.mkdir(exist_ok=True)
HISTORY_DIR.mkdir(exist_ok=True)

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
    """Validate a single question dict."""
    if not isinstance(q, dict):
        raise RuntimeError(f"question {idx} is not object")
    required = ["id", "prompt", "options", "correct_index"]
    for key in required:
        if key not in q:
            raise RuntimeError(f"question {idx} missing '{key}'")
    if not isinstance(q["prompt"], str) or not q["prompt"].strip():
        raise RuntimeError(f"question {idx} has empty prompt")
    if not isinstance(q["options"], list) or len(q["options"]) < 2:
        raise RuntimeError(f"question {idx} has invalid options")
    if len(q["options"]) > 6:
        raise RuntimeError(f"question {idx} has too many options (max 6)")
    for oi, opt in enumerate(q["options"]):
        if not isinstance(opt, str):
            raise RuntimeError(f"question {idx} option {oi} is not a string")
    ci = q["correct_index"]
    if not isinstance(ci, int) or ci < 0 or ci >= len(q["options"]):
        raise RuntimeError(f"question {idx} correct_index {ci} out of range 0..{len(q['options'])-1}")


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

def save_game_history(quiz_state: QuizState) -> dict[str, Any]:
    record = {
        "id": uuid.uuid4().hex[:12],
        "timestamp": datetime.now().isoformat(),
        "total_questions": len(quiz_state.questions),
        "question_duration_sec": quiz_state.question_duration_sec,
        "players": quiz_state._ranked_players(),
        "player_count": len(quiz_state.players),
    }
    filename = f"game_{record['id']}.json"
    path = HISTORY_DIR / filename
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
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
    ollama_host: str = "localhost",
    ollama_port: int = 11434,
    model: str = "gpt-oss:20b",
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

    url = f"http://{ollama_host}:{ollama_port}/api/generate"
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.7, "num_predict": 4096},
    }).encode("utf-8")

    req = Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except URLError as e:
        raise ConnectionError(f"Nelze se pripojit k Ollama na {ollama_host}:{ollama_port}: {e}")
    except Exception as e:
        raise RuntimeError(f"Chyba pri komunikaci s Ollama: {e}")

    response_text = result.get("response", "").strip()
    start = response_text.find("[")
    end = response_text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"Ollama nevrátila validní JSON pole. Odpoved: {response_text[:500]}")

    json_str = response_text[start:end + 1]
    try:
        questions = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Nevalidní JSON z Ollama: {e}. Text: {json_str[:500]}")

    if not isinstance(questions, list):
        raise ValueError("Ollama nevrátila pole otázek")

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
        raise ValueError("Ollama nevygenerovala zadne validni otazky")

    return valid


def list_ollama_models(ollama_host: str = "localhost", ollama_port: int = 11434) -> list[dict[str, str]]:
    url = f"http://{ollama_host}:{ollama_port}/api/tags"
    req = Request(url, method="GET")
    try:
        with urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    models = result.get("models", [])
    return [{"name": m.get("name", ""), "size": m.get("size", 0)} for m in models]


# --- Quiz State ---

@dataclass
class Player:
    name: str
    secret: str = ""
    score: int = 0
    last_seen: float = field(default_factory=time.time)


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

    def set_timing(self, question_sec: int | None = None, reveal_sec: int | None = None) -> None:
        with self._lock:
            if question_sec is not None:
                self.question_duration_sec = max(5, min(120, question_sec))
            if reveal_sec is not None:
                self.reveal_duration_sec = max(2, min(30, reveal_sec))

    def register_player(self, name: str) -> dict[str, Any]:
        clean = " ".join(name.strip().split())[:24]
        if not clean:
            raise ValueError("Jmeno je povinne")
        with self._lock:
            player_id = uuid.uuid4().hex[:10]
            player_secret = uuid.uuid4().hex
            self.players[player_id] = Player(name=clean, secret=player_secret)
            return {"player_id": player_id, "player_secret": player_secret, "name": clean}

    def verify_player(self, player_id: str, player_secret: str) -> bool:
        """Verify player identity using secret token."""
        with self._lock:
            player = self.players.get(player_id)
            if not player:
                return False
            return player.secret == player_secret

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
            elif action == "save_history":
                if self.phase == "finished" or len(self.players) > 0:
                    record = save_game_history(self)
                    return {"ok": True, "record": record}
                raise ValueError("Zadni hraci nebo hra neskoncila")
            else:
                raise ValueError("Neznamy action")
            return {"ok": True}

    def submit_answer(self, player_id: str, player_secret: str, choice: int) -> dict[str, Any]:
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
            options = question.get("options", [])
            if not isinstance(choice, int) or choice < 0 or choice >= len(options):
                raise ValueError("Neplatna volba")

            self.answers[player_id] = {
                "choice": choice,
                "time": now,
            }
            return {"ok": True}

    def _apply_scoring(self) -> None:
        if self.scored_question_index == self.current_index:
            return
        if self.current_index < 0:
            return

        question = self.questions[self.current_index]
        correct = question["correct_index"]
        started = self.question_started_at or time.time()

        for player_id, answer in self.answers.items():
            if answer["choice"] != correct:
                continue
            elapsed = max(0.0, answer["time"] - started)
            speed_bonus = max(0, 40 - int(elapsed))
            points = 600 + speed_bonus * 10
            self.players[player_id].score += points

        self.scored_question_index = self.current_index

    def _advance_to_next_question_locked(self, now: float) -> None:
        if self.current_index + 1 >= len(self.questions):
            self.phase = "finished"
            self.reveal_started_at = 0.0
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
                everyone_answered = len(self.players) > 0 and len(self.answers) >= len(self.players)
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

    def _ranked_players(self) -> list[dict[str, Any]]:
        ranking = sorted(
            (
                {
                    "name": player.name,
                    "score": player.score,
                }
                for player in self.players.values()
            ),
            key=lambda row: (-row["score"], row["name"].lower()),
        )
        for i, row in enumerate(ranking, start=1):
            row["rank"] = i
        return ranking

    def _vote_counts(self) -> list[int]:
        if self.current_index < 0:
            return []
        options_len = len(self.questions[self.current_index].get("options", []))
        counts = [0] * options_len
        for ans in self.answers.values():
            idx = ans["choice"]
            if 0 <= idx < options_len:
                counts[idx] += 1
        return counts

    def public_state(self, player_id: str | None = None, host_view: bool = False) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            self._sync_timers_locked(now)
            response: dict[str, Any] = {
                "phase": self.phase,
                "current_index": self.current_index,
                "total_questions": len(self.questions),
                "players": self._ranked_players(),
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
            }

            if player_id and player_id in self.players:
                response["me"] = {
                    "name": self.players[player_id].name,
                    "score": self.players[player_id].score,
                }
                if player_id in self.answers:
                    response["my_choice"] = self.answers[player_id]["choice"]

            if self.current_index >= 0:
                q = self.questions[self.current_index]
                q_public = {
                    "id": q["id"],
                    "prompt": q["prompt"],
                    "options": q["options"],
                }
                if host_view or self.phase in {"reveal", "finished"}:
                    q_public["correct_index"] = q["correct_index"]
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
SERVER_INFO: dict[str, Any] = {
    "bind_host": "0.0.0.0",
    "port": 8765,
    "host_urls": ["http://127.0.0.1:8765/host"],
    "play_urls": ["http://127.0.0.1:8765/play"],
    "loopback_only": False,
}

OLLAMA_CONFIG: dict[str, Any] = {
    "host": os.environ.get("OLLAMA_HOST", "localhost"),
    "port": int(os.environ.get("OLLAMA_PORT", "11434")),
    "model": os.environ.get("OLLAMA_MODEL", "gpt-oss:20b"),
}


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


def build_server_info(bind_host: str, port: int, external_ip: str | None = None) -> dict[str, Any]:
    host = bind_host.strip() or "0.0.0.0"

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
        content_type, _ = mimetypes.guess_type(path.name)
        if not content_type:
            content_type = "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
        self.send_header("Content-Length", str(len(content)))
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
        return self.client_address[0] if self.client_address else ""

    def _require_admin(self) -> bool:
        if not ADMIN_AUTH.enabled:
            return True
        token = self._get_admin_token()
        if ADMIN_AUTH.validate_session(token):
            return True
        self._json_response({"error": "Unauthorized", "needs_auth": True}, status=401)
        return False

    def _require_host_token(self) -> bool:
        """Verify host token from Authorization header."""
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:] == HOST_TOKEN:
            return True
        self._json_response({"error": "Invalid host token"}, status=403)
        return False

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/play")
            self.end_headers()
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
            # Only accessible from admin
            if not self._require_admin():
                return
            self._json_response({"host_token": HOST_TOKEN})
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
            models = list_ollama_models(OLLAMA_CONFIG["host"], OLLAMA_CONFIG["port"])
            self._json_response({"models": models, "config": OLLAMA_CONFIG})
            return

        if path == "/api/admin/ollama/config":
            if not self._require_admin():
                return
            self._json_response(OLLAMA_CONFIG)
            return

        self._text_response("Not found", status=404)

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            data = self._read_json()

            if path == "/api/register":
                name = str(data.get("name", ""))
                self._json_response(QUIZ.register_player(name))
                return

            if path == "/api/submit":
                player_id = str(data.get("player_id", ""))
                player_secret = str(data.get("player_secret", ""))
                choice = int(data.get("choice", -1))
                self._json_response(QUIZ.submit_answer(player_id, player_secret, choice))
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
                if "host" in data:
                    OLLAMA_CONFIG["host"] = str(data["host"])
                if "port" in data:
                    OLLAMA_CONFIG["port"] = int(data["port"])
                if "model" in data:
                    OLLAMA_CONFIG["model"] = str(data["model"])
                self._json_response({"ok": True, "config": OLLAMA_CONFIG})
                return

            if path == "/api/admin/ai/generate":
                if not self._require_admin():
                    return
                topic = str(data.get("topic", ""))
                count = int(data.get("count", 5))
                model = str(data.get("model", OLLAMA_CONFIG["model"]))
                host = str(data.get("host", OLLAMA_CONFIG["host"]))
                port = int(data.get("port", OLLAMA_CONFIG["port"]))
                language = str(data.get("language", "cs"))
                if not topic:
                    self._json_response({"error": "topic required"}, status=400)
                    return
                try:
                    questions = generate_questions_ai(
                        topic=topic, count=count,
                        ollama_host=host, ollama_port=port,
                        model=model, language=language,
                    )
                    self._json_response({"ok": True, "questions": questions})
                except Exception as e:
                    self._json_response({"error": str(e)}, status=500)
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
    parser.add_argument("--ollama-host", default=os.environ.get("OLLAMA_HOST", "localhost"))
    parser.add_argument("--ollama-port", type=int, default=int(os.environ.get("OLLAMA_PORT", "11434")))
    parser.add_argument("--ollama-model", default=os.environ.get("OLLAMA_MODEL", "gpt-oss:20b"))
    parser.add_argument("--external-ip", default=os.environ.get("QUIZ_EXTERNAL_IP", ""),
                        help="External IP(s) for player URLs (comma-separated, e.g. for Docker)")
    parser.add_argument("--question-time", type=int, default=QUESTION_DURATION_SEC,
                        help="Seconds per question (default 20)")
    parser.add_argument("--reveal-time", type=int, default=REVEAL_DURATION_SEC,
                        help="Seconds for reveal phase (default 5)")
    args = parser.parse_args()

    if args.admin_password:
        ADMIN_AUTH.set_password(args.admin_password)
        print("Admin portal: password protected")
    else:
        print("Admin portal: open (no password)")

    OLLAMA_CONFIG["host"] = args.ollama_host
    OLLAMA_CONFIG["port"] = args.ollama_port
    OLLAMA_CONFIG["model"] = args.ollama_model

    QUIZ.set_timing(question_sec=args.question_time, reveal_sec=args.reveal_time)

    global SERVER_INFO
    SERVER_INFO = build_server_info(args.host, args.port, external_ip=args.external_ip or None)

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
    print(f"  Ollama: {OLLAMA_CONFIG['host']}:{OLLAMA_CONFIG['port']} model={OLLAMA_CONFIG['model']}")
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
