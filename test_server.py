#!/usr/bin/env python3
"""Unit tests for ClassRally server."""

import contextlib
import gzip
import hashlib
import io
import json
import math
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.request import Request, urlopen
from urllib.error import HTTPError

# Add parent dir to path so we can import server module
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Isolate all runtime data (accounts DB, game history, uploads, question banks)
# from the developer's real data/, history/ and questions/: the DB location is
# read from QUIZ_DATA_DIR when db.py is imported, the rest is patched below.
_TEST_DATA_ROOT = Path(tempfile.mkdtemp(prefix="classrally-tests-"))
os.environ["QUIZ_DATA_DIR"] = str(_TEST_DATA_ROOT / "data")
os.environ["MAIL_MODE"] = "off"  # tests that need mail use their own log-mode Mailer
os.environ["QUIZ_GAME_LOG"] = "0"  # no game-flow lines in the test output (TestGameFlow checks them)
for _k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM"):
    os.environ.pop(_k, None)

import server

server.HISTORY_DIR = _TEST_DATA_ROOT / "history"
server.HISTORY_DIR.mkdir()
server.UPLOADS_DIR = _TEST_DATA_ROOT / "uploads"
shutil.copytree(server.QUESTIONS_DIR, _TEST_DATA_ROOT / "questions")
server.QUESTIONS_DIR = _TEST_DATA_ROOT / "questions"
import atexit
atexit.register(shutil.rmtree, _TEST_DATA_ROOT, True)

import db as _db
_db.PBKDF2_ITERATIONS = 1000  # keep the suite fast; production uses 600k

import backup


class TestQuizState(unittest.TestCase):
    """Tests for QuizState game logic."""

    def _sample_questions(self):
        return [
            {"id": "t1", "prompt": "Q1?", "options": ["A", "B", "C", "D"], "correct_index": 1, "explanation": "B is correct"},
            {"id": "t2", "prompt": "Q2?", "options": ["X", "Y", "Z", "W"], "correct_index": 0, "explanation": "X is correct"},
            {"id": "t3", "prompt": "Q3?", "options": ["1", "2", "3", "4"], "correct_index": 2, "explanation": "3 is correct"},
        ]

    def test_initial_state(self):
        qs = server.QuizState(self._sample_questions())
        self.assertEqual(qs.phase, "lobby")
        self.assertEqual(qs.current_index, -1)
        self.assertEqual(len(qs.players), 0)

    def test_register_player(self):
        qs = server.QuizState(self._sample_questions())
        result = qs.register_player("Alice")
        self.assertIn("player_id", result)
        self.assertEqual(result["name"], "Alice")
        self.assertEqual(len(qs.players), 1)

    def test_register_empty_name_fails(self):
        qs = server.QuizState(self._sample_questions())
        with self.assertRaises(ValueError):
            qs.register_player("")

    def test_register_whitespace_name_fails(self):
        qs = server.QuizState(self._sample_questions())
        with self.assertRaises(ValueError):
            qs.register_player("   ")

    def test_name_truncated_to_24(self):
        qs = server.QuizState(self._sample_questions())
        result = qs.register_player("A" * 50)
        self.assertEqual(len(result["name"]), 24)

    def test_start_quiz(self):
        qs = server.QuizState(self._sample_questions())
        qs.register_player("Alice")
        qs.host_action("start")
        self.assertEqual(qs.phase, "question")
        self.assertEqual(qs.current_index, 0)

    def test_start_empty_quiz_fails(self):
        qs = server.QuizState([])
        with self.assertRaises(ValueError):
            qs.host_action("start")

    def test_register_returns_secret(self):
        qs = server.QuizState(self._sample_questions())
        result = qs.register_player("Alice")
        self.assertIn("player_secret", result)
        self.assertTrue(len(result["player_secret"]) > 0)

    def test_submit_answer(self):
        qs = server.QuizState(self._sample_questions())
        result = qs.register_player("Alice")
        pid, secret = result["player_id"], result["player_secret"]
        qs.host_action("start")
        qs.submit_answer(pid, secret, 1)
        self.assertIn(pid, qs.answers)
        self.assertEqual(qs.answers[pid]["choice"], 1)

    def test_submit_answer_twice_fails(self):
        qs = server.QuizState(self._sample_questions())
        result = qs.register_player("Alice")
        pid, secret = result["player_id"], result["player_secret"]
        qs.host_action("start")
        qs.submit_answer(pid, secret, 1)
        with self.assertRaises(ValueError):
            qs.submit_answer(pid, secret, 2)

    def test_submit_wrong_secret_fails(self):
        qs = server.QuizState(self._sample_questions())
        result = qs.register_player("Alice")
        pid = result["player_id"]
        qs.host_action("start")
        with self.assertRaises(ValueError):
            qs.submit_answer(pid, "wrong_secret", 1)

    def test_submit_in_lobby_fails(self):
        qs = server.QuizState(self._sample_questions())
        result = qs.register_player("Alice")
        pid, secret = result["player_id"], result["player_secret"]
        with self.assertRaises(ValueError):
            qs.submit_answer(pid, secret, 0)

    def test_submit_invalid_choice_fails(self):
        qs = server.QuizState(self._sample_questions())
        result = qs.register_player("Alice")
        pid, secret = result["player_id"], result["player_secret"]
        qs.host_action("start")
        with self.assertRaises(ValueError):
            qs.submit_answer(pid, secret, 10)

    def test_submit_unknown_player_fails(self):
        qs = server.QuizState(self._sample_questions())
        qs.register_player("Alice")
        qs.host_action("start")
        with self.assertRaises(ValueError):
            qs.submit_answer("unknown_id", "any_secret", 0)

    def test_reveal_action(self):
        qs = server.QuizState(self._sample_questions())
        qs.register_player("Alice")
        qs.host_action("start")
        qs.host_action("reveal")
        self.assertEqual(qs.phase, "reveal")

    def test_reveal_in_lobby_fails(self):
        qs = server.QuizState(self._sample_questions())
        with self.assertRaises(ValueError):
            qs.host_action("reveal")

    def test_scoring_correct_answer(self):
        qs = server.QuizState(self._sample_questions())
        r1 = qs.register_player("Alice")
        r2 = qs.register_player("Dummy")  # prevent auto-advance
        pid, secret = r1["player_id"], r1["player_secret"]
        qs.host_action("start")
        qs.submit_answer(pid, secret, 1)  # correct for q1
        qs.host_action("reveal")
        self.assertGreater(qs.players[pid].score, 0)

    def test_scoring_wrong_answer(self):
        qs = server.QuizState(self._sample_questions())
        r1 = qs.register_player("Bob")
        r2 = qs.register_player("Dummy")  # prevent auto-advance
        pid, secret = r1["player_id"], r1["player_secret"]
        qs.host_action("start")
        qs.submit_answer(pid, secret, 0)  # wrong for q1 (correct is 1)
        qs.host_action("reveal")
        self.assertEqual(qs.players[pid].score, 0)

    def test_next_question(self):
        qs = server.QuizState(self._sample_questions())
        qs.register_player("Alice")
        qs.host_action("start")
        qs.host_action("reveal")
        qs.host_action("next")
        self.assertEqual(qs.phase, "question")
        self.assertEqual(qs.current_index, 1)

    def test_finish_after_last_question(self):
        qs = server.QuizState(self._sample_questions())
        qs.register_player("Alice")
        qs.host_action("start")
        for i in range(len(qs.questions)):
            qs.host_action("reveal")
            if i < len(qs.questions) - 1:
                qs.host_action("next")
        qs.host_action("next")
        self.assertEqual(qs.phase, "finished")

    def test_reset(self):
        qs = server.QuizState(self._sample_questions())
        r = qs.register_player("Alice")
        qs.register_player("Dummy")  # prevent auto-advance
        qs.host_action("start")
        qs.submit_answer(r["player_id"], r["player_secret"], 1)
        qs.host_action("reveal")
        qs.host_action("reset")
        self.assertEqual(qs.phase, "lobby")
        self.assertEqual(qs.current_index, -1)
        self.assertEqual(qs.players[r["player_id"]].score, 0)

    def test_unknown_action_fails(self):
        qs = server.QuizState(self._sample_questions())
        with self.assertRaises(ValueError):
            qs.host_action("dance")

    def test_public_state_lobby(self):
        qs = server.QuizState(self._sample_questions())
        state = qs.public_state()
        self.assertEqual(state["phase"], "lobby")
        self.assertEqual(state["total_questions"], 3)
        self.assertNotIn("question", state)

    def test_public_state_question(self):
        qs = server.QuizState(self._sample_questions())
        qs.register_player("Alice")
        qs.host_action("start")
        state = qs.public_state()
        self.assertEqual(state["phase"], "question")
        self.assertIn("question", state)
        self.assertNotIn("correct_index", state["question"])

    def test_public_state_host_view(self):
        qs = server.QuizState(self._sample_questions())
        qs.register_player("Alice")
        qs.host_action("start")
        state = qs.public_state(host_view=True)
        self.assertIn("correct_index", state["question"])

    def test_public_state_player_view(self):
        qs = server.QuizState(self._sample_questions())
        r = qs.register_player("Alice")
        state = qs.public_state(player_id=r["player_id"])
        self.assertIn("me", state)
        self.assertEqual(state["me"]["name"], "Alice")

    def test_set_timing(self):
        qs = server.QuizState(self._sample_questions())
        qs.set_timing(question_sec=30, reveal_sec=10)
        self.assertEqual(qs.question_duration_sec, 30)
        self.assertEqual(qs.reveal_duration_sec, 10)

    def test_set_timing_clamped(self):
        qs = server.QuizState(self._sample_questions())
        qs.set_timing(question_sec=1, reveal_sec=1)
        self.assertEqual(qs.question_duration_sec, 5)  # min 5
        self.assertEqual(qs.reveal_duration_sec, 2)  # min 2

    def test_reload_questions(self):
        qs = server.QuizState(self._sample_questions())
        r = qs.register_player("Alice")
        qs.host_action("start")
        new_q = [{"id": "n1", "prompt": "New?", "options": ["A", "B"], "correct_index": 0}]
        qs.reload_questions(new_q, bank_name="test.json")
        self.assertEqual(qs.phase, "lobby")
        self.assertEqual(len(qs.questions), 1)
        self.assertEqual(qs._active_bank, "test.json")

    def test_ranked_players(self):
        qs = server.QuizState(self._sample_questions())
        r1 = qs.register_player("Alice")
        r2 = qs.register_player("Bob")
        qs.register_player("Dummy")  # prevent auto-advance
        qs.host_action("start")
        qs.submit_answer(r1["player_id"], r1["player_secret"], 1)  # correct
        qs.submit_answer(r2["player_id"], r2["player_secret"], 0)  # wrong
        qs.host_action("reveal")
        state = qs.public_state()
        self.assertEqual(state["players"][0]["name"], "Alice")
        self.assertEqual(state["players"][1]["name"], "Bob")
        # player_id should NOT be exposed in rankings
        self.assertNotIn("player_id", state["players"][0])

    def test_vote_counts(self):
        qs = server.QuizState(self._sample_questions())
        r1 = qs.register_player("Alice")
        r2 = qs.register_player("Bob")
        r3 = qs.register_player("Carol")
        qs.host_action("start")
        qs.submit_answer(r1["player_id"], r1["player_secret"], 0)
        qs.submit_answer(r2["player_id"], r2["player_secret"], 0)
        qs.submit_answer(r3["player_id"], r3["player_secret"], 2)
        state = qs.public_state(host_view=True)
        self.assertEqual(state["vote_counts"], [2, 0, 1, 0])

    def test_auto_advance_on_timeout(self):
        qs = server.QuizState(self._sample_questions())
        qs.set_timing(question_sec=5, reveal_sec=2)
        qs.register_player("Alice")
        qs.host_action("start")
        # Simulate time passage: 3 s get-ready + 5 s to answer are over
        qs.question_started_at = time.time() - 9
        state = qs.public_state()
        self.assertEqual(state["phase"], "reveal")

    def test_ws_notify_called_after_lock_released(self):
        """State mutations must broadcast only AFTER releasing the game lock.

        The WS broadcast state getter re-acquires the lock via public_state(),
        so notifying while still holding it deadlocks the whole server
        (regression: host_action used to call _ws_notify inside the lock).
        """
        qs = server.QuizState(self._sample_questions())
        lock_free: list[bool] = []
        orig_notify = server._ws_notify

        def probe():
            got = qs._lock.acquire(timeout=1)
            if got:
                qs._lock.release()
            lock_free.append(got)

        server._ws_notify = probe
        try:
            ra = qs.register_player("Alice")
            qs.register_player("Bob")  # silent second player blocks auto-advance
            qs.host_action("start")
            qs.submit_answer(ra["player_id"], ra["player_secret"], 1)
            qs.host_action("reveal")
            qs.host_action("next")
            qs.host_action("reset")
            qs.kick_player(ra["player_id"])
        finally:
            server._ws_notify = orig_notify

        # register x2, start, submit, reveal, next, reset, kick = 8 notifies,
        # every one of them with the game lock already released
        self.assertEqual(lock_free, [True] * 8)

    def test_tick_and_notify_broadcasts_timer_transitions(self):
        """tick_and_notify() must pick up timeout-driven phase changes,
        notify exactly once per change (no spam) and never under the lock."""
        qs = server.QuizState(self._sample_questions())
        qs.set_timing(question_sec=5, reveal_sec=2)
        qs.register_player("Alice")
        notifications: list[bool] = []
        orig_notify = server._ws_notify

        def probe():
            got = qs._lock.acquire(timeout=1)
            if got:
                qs._lock.release()
            notifications.append(got)

        server._ws_notify = probe
        try:
            qs.host_action("start")  # explicit action broadcasts itself
            notifications.clear()

            # Nothing changed yet -> tick stays silent
            self.assertFalse(qs.tick_and_notify())
            self.assertEqual(notifications, [])

            # Question times out (3 s get-ready + 5 s) -> tick must advance to reveal and notify
            qs.question_started_at = time.time() - 9
            self.assertTrue(qs.tick_and_notify())
            self.assertEqual(qs.phase, "reveal")
            self.assertEqual(notifications, [True])  # lock already released

            # Same state again -> no repeated broadcast
            self.assertFalse(qs.tick_and_notify())
            self.assertEqual(notifications, [True])
        finally:
            server._ws_notify = orig_notify


class TestAdminAuth(unittest.TestCase):
    """Tests for admin authentication."""

    def test_no_password_always_valid(self):
        auth = server.AdminAuth()
        self.assertFalse(auth.enabled)
        self.assertTrue(auth.validate_session(None))

    def test_password_set(self):
        auth = server.AdminAuth()
        auth.set_password("test123")
        self.assertTrue(auth.enabled)
        self.assertFalse(auth.validate_session(None))

    def test_correct_password(self):
        auth = server.AdminAuth()
        auth.set_password("secret")
        token = auth.check_password("secret")
        self.assertIsNotNone(token)
        self.assertTrue(auth.validate_session(token))

    def test_wrong_password(self):
        auth = server.AdminAuth()
        auth.set_password("secret")
        token = auth.check_password("wrong")
        self.assertIsNone(token)

    def test_disable_password(self):
        auth = server.AdminAuth()
        auth.set_password("secret")
        self.assertTrue(auth.enabled)
        auth.set_password(None)
        self.assertFalse(auth.enabled)

    def test_session_expiry(self):
        auth = server.AdminAuth()
        auth.set_password("test")
        auth.SESSION_TTL = 0  # immediate expiry
        token = auth.check_password("test")
        time.sleep(0.01)
        self.assertFalse(auth.validate_session(token))


class TestQuestionValidation(unittest.TestCase):
    """Tests for question validation logic."""

    def test_valid_question(self):
        q = {"id": "v1", "prompt": "Q?", "options": ["A", "B", "C", "D"], "correct_index": 1}
        server._validate_question(q, 0)  # should not raise

    def test_missing_prompt(self):
        q = {"id": "v1", "options": ["A", "B"], "correct_index": 0}
        with self.assertRaises(RuntimeError):
            server._validate_question(q, 0)

    def test_empty_prompt(self):
        q = {"id": "v1", "prompt": "  ", "options": ["A", "B"], "correct_index": 0}
        with self.assertRaises(RuntimeError):
            server._validate_question(q, 0)

    def test_correct_index_out_of_range(self):
        q = {"id": "v1", "prompt": "Q?", "options": ["A", "B"], "correct_index": 5}
        with self.assertRaises(RuntimeError):
            server._validate_question(q, 0)

    def test_too_few_options(self):
        q = {"id": "v1", "prompt": "Q?", "options": ["A"], "correct_index": 0}
        with self.assertRaises(RuntimeError):
            server._validate_question(q, 0)

    def test_too_many_options(self):
        q = {"id": "v1", "prompt": "Q?", "options": ["A", "B", "C", "D", "E", "F", "G"], "correct_index": 0}
        with self.assertRaises(RuntimeError):
            server._validate_question(q, 0)

    def test_save_validates(self):
        """save_questions_to_file rejects invalid questions."""
        tmpdir = tempfile.mkdtemp()
        orig = server.QUESTIONS_DIR
        server.QUESTIONS_DIR = Path(tmpdir)
        try:
            bad_q = [{"id": "b1", "prompt": "", "options": ["A"], "correct_index": 0}]
            with self.assertRaises((RuntimeError, ValueError)):
                server.save_questions_to_file("bad.json", bad_q)
        finally:
            server.QUESTIONS_DIR = orig
            shutil.rmtree(tmpdir)


class TestHistoryDeletion(unittest.TestCase):
    """Tests for exact-match history deletion."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orig_history_dir = server.HISTORY_DIR
        server.HISTORY_DIR = Path(self.tmpdir)

    def tearDown(self):
        server.HISTORY_DIR = self.orig_history_dir
        shutil.rmtree(self.tmpdir)

    def test_delete_exact_match_only(self):
        """Deletion should use exact match, not substring."""
        # Create two history files with similar IDs
        for gid in ["aabbccddeeff", "aabbccddeef0"]:
            record = {"id": gid, "timestamp": "2026-01-01", "players": [], "player_count": 0}
            path = server.HISTORY_DIR / f"game_{gid}.json"
            path.write_text(json.dumps(record))
        self.assertTrue(server.delete_game_history("aabbccddeeff"))
        self.assertEqual(len(server.list_game_history()), 1)

    def test_invalid_game_id_rejected(self):
        """Game IDs not matching the expected format should be rejected."""
        self.assertFalse(server.delete_game_history(""))
        self.assertFalse(server.delete_game_history("../evil"))
        self.assertFalse(server.delete_game_history("short"))


class TestQuestionBanks(unittest.TestCase):
    """Tests for question bank file management."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orig_questions_dir = server.QUESTIONS_DIR
        self.orig_base_dir = server.BASE_DIR
        server.QUESTIONS_DIR = Path(self.tmpdir)
        # Point BASE_DIR to tmpdir too so legacy migration won't find old files
        server.BASE_DIR = Path(self.tmpdir)

    def tearDown(self):
        server.QUESTIONS_DIR = self.orig_questions_dir
        server.BASE_DIR = self.orig_base_dir
        shutil.rmtree(self.tmpdir)

    def test_save_and_load(self):
        questions = [{"id": "t1", "prompt": "Q?", "options": ["A", "B", "C", "D"], "correct_index": 0}]
        server.save_questions_to_file("test.json", questions)
        loaded = server.load_questions_from_file("test.json")
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["id"], "t1")

    def test_odd_bank_names_are_refused_but_old_files_stay_usable(self):
        q = [{"id": "a1", "prompt": "?", "options": ["A", "B"], "correct_index": 0}]
        for name in ("fyzika\\opakovani.json", "tab\there.json", "\udcffzlomene.json"):
            with self.assertRaises(ValueError):
                server.save_questions_to_file(name, q)
        self.assertEqual(list(Path(self.tmpdir).iterdir()), [])
        # a file like that from before the check can still be opened and deleted
        old = Path(self.tmpdir) / "stara\\sada.json"
        old.write_text(json.dumps(q), encoding="utf-8")
        self.assertEqual(len(server.load_questions_from_file("stara\\sada.json")), 1)
        server.delete_question_bank("stara\\sada.json")
        self.assertFalse(old.exists())

    def test_list_banks(self):
        q1 = [{"id": "a1", "prompt": "?", "options": ["A", "B"], "correct_index": 0}]
        server.save_questions_to_file("bank1.json", q1)
        server.save_questions_to_file("bank2.json", q1)
        banks = server.list_question_banks()
        self.assertEqual(len(banks), 2)

    def test_bank_names_written_by_people_are_shown_as_written(self):
        q = [{"id": "a1", "prompt": "?", "options": ["A", "B"], "correct_index": 0}]
        for name in ("linux_sprava.json", "OS13-2_DNS_zóny_a_záznamy.json", "fyzika_č1.json"):
            server.save_questions_to_file(name, q)
        names = {b["filename"]: b["name"] for b in server.list_question_banks()}
        self.assertEqual(names["linux_sprava.json"], "Linux Sprava")  # old snake_case names as before
        self.assertEqual(names["OS13-2_DNS_zóny_a_záznamy.json"], "OS13-2 DNS zóny a záznamy")
        self.assertEqual(names["fyzika_č1.json"], "fyzika č1")

    def test_delete_bank(self):
        q = [{"id": "d1", "prompt": "?", "options": ["A", "B"], "correct_index": 0}]
        server.save_questions_to_file("del.json", q)
        server.delete_question_bank("del.json")
        banks = server.list_question_banks()
        self.assertEqual(len(banks), 0)

    def test_load_nonexistent_fails(self):
        with self.assertRaises(FileNotFoundError):
            server.load_questions_from_file("nope.json")

    def test_path_traversal_blocked(self):
        # Saving with path traversal should stay in questions dir
        server.save_questions_to_file("../../evil.json", [])
        self.assertFalse((Path(self.tmpdir).parent.parent / "evil.json").exists())
        self.assertTrue((Path(self.tmpdir) / "evil.json").exists())

    def test_auto_add_json_extension(self):
        server.save_questions_to_file("noext", [])
        self.assertTrue((Path(self.tmpdir) / "noext.json").exists())


class TestScoringHistory(unittest.TestCase):
    """Tests for game history persistence."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orig_history_dir = server.HISTORY_DIR
        server.HISTORY_DIR = Path(self.tmpdir)

    def tearDown(self):
        server.HISTORY_DIR = self.orig_history_dir
        shutil.rmtree(self.tmpdir)

    def test_save_and_list(self):
        qs = server.QuizState([
            {"id": "h1", "prompt": "Q?", "options": ["A", "B", "C", "D"], "correct_index": 0}
        ])
        qs.register_player("Alice")
        record = server.save_game_history(qs)
        self.assertIn("id", record)
        self.assertIn("timestamp", record)

        history = server.list_game_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["player_count"], 1)

    def test_delete_history(self):
        qs = server.QuizState([
            {"id": "h2", "prompt": "Q?", "options": ["A", "B"], "correct_index": 0}
        ])
        qs.register_player("Bob")
        record = server.save_game_history(qs)
        self.assertTrue(server.delete_game_history(record["id"]))
        self.assertEqual(len(server.list_game_history()), 0)

    def test_delete_nonexistent(self):
        self.assertFalse(server.delete_game_history("nope"))


class TestHTTPIntegration(unittest.TestCase):
    """Integration tests using actual HTTP server."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        cls.orig_questions_dir = server.QUESTIONS_DIR
        cls.orig_history_dir = server.HISTORY_DIR
        server.QUESTIONS_DIR = Path(cls.tmpdir) / "questions"
        server.HISTORY_DIR = Path(cls.tmpdir) / "history"
        server.QUESTIONS_DIR.mkdir()
        server.HISTORY_DIR.mkdir()

        # Isolate user DB so teacher_exists() returns False (no auth needed)
        import db
        cls.orig_db_path = db._DB_PATH
        db.set_db_path(Path(cls.tmpdir) / "test_http.db")
        db.init_db()

        # Save test questions
        test_q = [
            {"id": "ht1", "prompt": "HTTP Q1?", "options": ["A", "B", "C", "D"], "correct_index": 1, "explanation": "B"},
            {"id": "ht2", "prompt": "HTTP Q2?", "options": ["X", "Y", "Z", "W"], "correct_index": 0, "explanation": "X"},
        ]
        server.save_questions_to_file("test_bank.json", test_q)

        # Reset quiz with test questions
        server.QUIZ.reload_questions(test_q, "test_bank.json")
        server.ADMIN_AUTH.set_password(None)  # no auth for tests

        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        server.QUESTIONS_DIR = cls.orig_questions_dir
        server.HISTORY_DIR = cls.orig_history_dir
        import db
        db.set_db_path(cls.orig_db_path)
        shutil.rmtree(cls.tmpdir)

    def setUp(self):
        # Reset quiz state before each test
        test_q = [
            {"id": "ht1", "prompt": "HTTP Q1?", "options": ["A", "B", "C", "D"], "correct_index": 1, "explanation": "B"},
            {"id": "ht2", "prompt": "HTTP Q2?", "options": ["X", "Y", "Z", "W"], "correct_index": 0, "explanation": "X"},
        ]
        server.QUIZ.reload_questions(test_q, "test_bank.json")

    def _get(self, path, headers=None):
        req = Request(f"{self.base_url}{path}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        with urlopen(req) as r:
            return json.loads(r.read())

    def _post(self, path, data, headers=None):
        body = json.dumps(data).encode()
        req = Request(f"{self.base_url}{path}", data=body, headers={"Content-Type": "application/json"}, method="POST")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        with urlopen(req) as r:
            return json.loads(r.read())

    def _host_post(self, path, data):
        """POST with host token auth."""
        return self._post(path, data, headers={"Authorization": f"Bearer {server.HOST_TOKEN}"})

    def _host_get(self, path):
        """GET with host token auth (X-Host-Token header)."""
        return self._get(path, headers={"X-Host-Token": server.HOST_TOKEN})

    def _raw_status_lines(self, path, extra_headers=""):
        import re, socket
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        sock.sendall(f"GET {path} HTTP/1.0\r\nHost: x\r\n{extra_headers}\r\n".encode())
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        sock.close()
        # a second response may follow the body without any line break
        return re.findall(rb"HTTP/1\.[01] \d{3} ", data)

    def test_exam_page_sends_single_response(self):
        self.assertEqual(len(self._raw_status_lines("/exam")), 1)

    def test_exam_overview_sends_single_response(self):
        lines = self._raw_status_lines("/api/host/exam/overview", f"X-Host-Token: {server.HOST_TOKEN}\r\n")
        self.assertEqual(len(lines), 1)

    def test_health(self):
        data = self._get("/api/health")
        self.assertTrue(data["ok"])
        self.assertEqual(data["version"], server.APP_VERSION)
        self.assertEqual(server.APP_VERSION, "3.0")

    def test_register_and_state(self):
        reg = self._post("/api/register", {"name": "TestPlayer"})
        self.assertIn("player_id", reg)
        self.assertIn("player_secret", reg)
        state = self._get(f"/api/state?player_id={reg['player_id']}")
        self.assertEqual(state["me"]["name"], "TestPlayer")

    def test_full_game_flow(self):
        # Register two players
        r1 = self._post("/api/register", {"name": "P1"})
        r2 = self._post("/api/register", {"name": "P2"})

        # Start (requires host token)
        self._host_post("/api/host/action", {"action": "start"})
        state = self._get("/api/state?host=1")
        self.assertEqual(state["phase"], "question")

        # Submit answers (with player_secret)
        self._post("/api/submit", {"player_id": r1["player_id"], "player_secret": r1["player_secret"], "choice": 1})
        self._post("/api/submit", {"player_id": r2["player_id"], "player_secret": r2["player_secret"], "choice": 0})

        # Auto-reveal may happen, check
        state = self._get("/api/state?host=1")
        if state["phase"] == "question":
            self._host_post("/api/host/action", {"action": "reveal"})

        state = self._get("/api/state?host=1")
        self.assertEqual(state["phase"], "reveal")
        self.assertEqual(state["question"]["correct_index"], 1)

        # P1 should have score > 0
        p1_score = next(p for p in state["players"] if p["name"] == "P1")
        self.assertGreater(p1_score["score"], 0)
        # host=1 WITHOUT token silently degrades to public view: no player_id
        self.assertNotIn("player_id", state["players"][0])
        # host=1 WITH the X-Host-Token header → full host view with player_id
        host_state = self._host_get("/api/state?host=1")
        self.assertIn("player_id", host_state["players"][0])
        player_state = self._get("/api/state")
        self.assertNotIn("player_id", player_state["players"][0])  # player view

    def test_state_host_view_requires_token(self):
        """host=1 without a valid token silently degrades to the public view."""
        self._post("/api/register", {"name": "SecPlayer"})
        self._host_post("/api/host/action", {"action": "start"})

        # No token → public view: no answer leak during the question phase
        state = self._get("/api/state?host=1")
        self.assertEqual(state["phase"], "question")
        self.assertNotIn("correct_index", state["question"])
        self.assertNotIn("explanation", state["question"])
        self.assertNotIn("player_id", state["players"][0])
        self.assertNotIn("bot_score", state["players"][0])
        self.assertEqual(state["suspected_bots"], [])

        # Wrong token → still the public view, no error
        bad = self._get("/api/state?host=1", headers={"X-Host-Token": "wrong-token"})
        self.assertEqual(bad["phase"], "question")
        self.assertNotIn("correct_index", bad["question"])
        self.assertNotIn("player_id", bad["players"][0])

        # Correct X-Host-Token header → full host view during the question
        host_state = self._host_get("/api/state?host=1")
        self.assertEqual(host_state["question"]["correct_index"], 1)
        self.assertIn("player_id", host_state["players"][0])
        self.assertIn("bot_score", host_state["players"][0])

        # Legacy Authorization: Bearer form is accepted as well
        bearer = self._get(
            "/api/state?host=1",
            headers={"Authorization": f"Bearer {server.HOST_TOKEN}"},
        )
        self.assertEqual(bearer["question"]["correct_index"], 1)

        # Without host=1 the token alone must not switch to host view
        plain = self._host_get("/api/state")
        self.assertNotIn("player_id", plain["players"][0])

    def test_ws_host_view_requires_token(self):
        """WebSocket host=1 without a token must not receive the host view."""
        import socket as socket_mod

        from ws import OPCODE_TEXT, ws_decode_frame

        self._post("/api/register", {"name": "WsPlayer"})
        self._host_post("/api/host/action", {"action": "start"})

        def ws_initial_state(extra_headers=""):
            sock = socket_mod.create_connection(("127.0.0.1", self.port), timeout=5)
            try:
                request = (
                    "GET /api/ws?host=1 HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{self.port}\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                    "Sec-WebSocket-Version: 13\r\n"
                    f"{extra_headers}"
                    "\r\n"
                )
                sock.sendall(request.encode("ascii"))
                buf = b""
                while b"\r\n\r\n" not in buf:
                    chunk = sock.recv(4096)
                    if not chunk:
                        self.fail("WebSocket handshake failed (connection closed)")
                    buf += chunk
                status_line = buf.split(b"\r\n", 1)[0]
                self.assertIn(b"101", status_line)
                frame_data = buf.split(b"\r\n\r\n", 1)[1]
                for _ in range(50):
                    result = ws_decode_frame(bytes(frame_data))
                    if result is not None:
                        opcode, payload, consumed = result
                        frame_data = frame_data[consumed:]
                        if opcode == OPCODE_TEXT:
                            return json.loads(payload.decode("utf-8"))
                        continue  # skip pings etc.
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    frame_data += chunk
                self.fail("No WebSocket state frame received")
            finally:
                sock.close()

        # Without token: public view even with host=1 in the WS query
        state = ws_initial_state()
        self.assertNotIn("player_id", state["players"][0])
        self.assertNotIn("correct_index", state["question"])

        # With X-Host-Token handshake header: full host view
        host_state = ws_initial_state(f"X-Host-Token: {server.HOST_TOKEN}\r\n")
        self.assertIn("player_id", host_state["players"][0])
        self.assertEqual(host_state["question"]["correct_index"], 1)

    def test_host_websocket_with_subprotocol_token(self):
        """The projector page cannot set headers on a WebSocket: it offers the
        subprotocols "classrally.host, <token>" and gets the host view (#97)."""
        import socket as socket_mod

        from ws import OPCODE_TEXT, ws_decode_frame

        self._post("/api/register", {"name": "WsProto"})
        self._host_post("/api/host/action", {"action": "start"})

        def handshake(protocols):
            sock = socket_mod.create_connection(("127.0.0.1", self.port), timeout=5)
            sock.sendall((
                "GET /api/ws?host=1 HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                f"Sec-WebSocket-Protocol: {protocols}\r\n\r\n").encode("ascii"))
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            return sock, buf

        sock, buf = handshake(f"classrally.host, {server.HOST_TOKEN}")
        try:
            head, frames = buf.split(b"\r\n\r\n", 1)
            self.assertIn(b"101", head.split(b"\r\n", 1)[0])
            self.assertIn(b"Sec-WebSocket-Protocol: classrally.host\r\n", head + b"\r\n")
            self.assertNotIn(server.HOST_TOKEN.encode(), head)  # the token is never echoed back
            state = None
            for _ in range(50):
                result = ws_decode_frame(bytes(frames))
                if result is not None:
                    opcode, payload, consumed = result
                    frames = frames[consumed:]
                    if opcode == OPCODE_TEXT:
                        state = json.loads(payload.decode("utf-8"))
                        break
                    continue
                frames += sock.recv(4096)
            self.assertIsNotNone(state)
            self.assertIn("player_id", state["players"][0])
            self.assertEqual(state["question"]["correct_index"], 1)
        finally:
            sock.close()

        sock, buf = handshake("classrally.host, " + "0" * 32)  # wrong token: refused, not a public view
        sock.close()
        self.assertIn(b"403", buf.split(b"\r\n", 1)[0])

    def test_register_same_device_and_taken_name_over_http(self):
        device = "d" * 32
        first = self._post("/api/register", {"name": "HttpJan", "device_id": device})
        again = self._post("/api/register", {"name": "HttpJan", "device_id": device})
        self.assertEqual(again["player_id"], first["player_id"])
        self.assertTrue(again["rejoined"])
        with self.assertRaises(HTTPError) as ctx:
            self._post("/api/register", {"name": "httpjan", "device_id": "e" * 32})
        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(json.loads(ctx.exception.read())["code"], "name_taken")

    def test_ws_client_does_not_deadlock_host_actions(self):
        """Host actions must not freeze the server while a WS client is open.

        Regression: _ws_notify() used to run inside the game lock; the WS
        broadcast re-acquires that lock via public_state(), so any host
        action with >= 1 connected WebSocket deadlocked the whole server.
        Each POST below uses a hard 5s timeout — without the fix it hangs,
        with it it returns 200 and the change arrives as a WS broadcast.
        """
        import socket as socket_mod

        from ws import OPCODE_TEXT, ws_decode_frame

        self._post("/api/register", {"name": "WsDeadlock"})

        def host_action(action):
            body = json.dumps({"action": action}).encode()
            req = Request(
                f"{self.base_url}/api/host/action",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {server.HOST_TOKEN}",
                },
                method="POST",
            )
            with urlopen(req, timeout=5) as r:  # hangs forever without the fix
                self.assertEqual(r.status, 200)
                return json.loads(r.read())

        sock = socket_mod.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            request = (
                "GET /api/ws HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n"
            )
            sock.sendall(request.encode("ascii"))
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    self.fail("WebSocket handshake failed (connection closed)")
                buf += chunk
            self.assertIn(b"101", buf.split(b"\r\n", 1)[0])
            frame_buf = bytearray(buf.split(b"\r\n\r\n", 1)[1])

            def next_state_where(predicate, what):
                """Read WS frames until a state frame satisfies predicate."""
                deadline = time.time() + 5
                while time.time() < deadline:
                    result = ws_decode_frame(bytes(frame_buf))
                    if result is not None:
                        opcode, payload, consumed = result
                        del frame_buf[:consumed]
                        if opcode == OPCODE_TEXT:
                            state = json.loads(payload.decode("utf-8"))
                            if predicate(state):
                                return state
                        continue  # skip pings etc.
                    try:
                        chunk = sock.recv(4096)
                    except socket_mod.timeout:
                        break
                    if not chunk:
                        self.fail("WebSocket closed while waiting for broadcast")
                    frame_buf.extend(chunk)
                self.fail(f"No WS broadcast received: {what}")

            # Initial state is pushed right after the handshake
            next_state_where(lambda s: s["phase"] == "lobby", "initial lobby state")

            self.assertTrue(host_action("start")["ok"])
            next_state_where(lambda s: s["phase"] == "question", "start broadcast")

            self.assertTrue(host_action("reveal")["ok"])
            next_state_where(lambda s: s["phase"] == "reveal", "reveal broadcast")

            self.assertTrue(host_action("next")["ok"])
            next_state_where(
                lambda s: s["phase"] == "question" and s["current_index"] == 1,
                "next broadcast",
            )

            self.assertTrue(host_action("reset")["ok"])
            next_state_where(lambda s: s["phase"] == "lobby", "reset broadcast")
        finally:
            sock.close()

    def test_ws_ticker_broadcasts_auto_transitions(self):
        """Timer-driven transitions must reach WS clients without any client
        action (regression): WS clients stop polling, so question timeout ->
        reveal -> next question -> finished must be pushed by the server
        ticker. Also checks the ticker stays silent while nothing changes.
        """
        import socket as socket_mod

        from ws import OPCODE_TEXT, ws_decode_frame

        server.start_quiz_ticker()  # lazy-started by main() in production
        time.sleep(0.1)  # let the first tick sync its (phase, index) marker

        orig_q_sec = server.QUIZ.question_duration_sec
        orig_r_sec = server.QUIZ.reveal_duration_sec

        sock = socket_mod.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            request = (
                "GET /api/ws HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                "\r\n"
            )
            sock.sendall(request.encode("ascii"))
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    self.fail("WebSocket handshake failed (connection closed)")
                buf += chunk
            self.assertIn(b"101", buf.split(b"\r\n", 1)[0])
            frame_buf = bytearray(buf.split(b"\r\n\r\n", 1)[1])

            def read_text_frames_for(seconds):
                """Collect all TEXT frames arriving within `seconds`."""
                frames = []
                deadline = time.time() + seconds
                while True:
                    result = ws_decode_frame(bytes(frame_buf))
                    if result is not None:
                        opcode, payload, consumed = result
                        del frame_buf[:consumed]
                        if opcode == OPCODE_TEXT:
                            frames.append(json.loads(payload.decode("utf-8")))
                        continue
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        return frames
                    sock.settimeout(remaining)
                    try:
                        chunk = sock.recv(4096)
                    except socket_mod.timeout:
                        return frames
                    finally:
                        sock.settimeout(5)
                    if not chunk:
                        self.fail("WebSocket closed unexpectedly")
                    frame_buf.extend(chunk)

            def next_state_where(predicate, what, deadline_sec):
                """Read WS frames until a state frame satisfies predicate."""
                deadline = time.time() + deadline_sec
                while time.time() < deadline:
                    result = ws_decode_frame(bytes(frame_buf))
                    if result is not None:
                        opcode, payload, consumed = result
                        del frame_buf[:consumed]
                        if opcode == OPCODE_TEXT:
                            state = json.loads(payload.decode("utf-8"))
                            if predicate(state):
                                return state
                        continue  # skip pings etc.
                    try:
                        chunk = sock.recv(4096)
                    except socket_mod.timeout:
                        continue
                    if not chunk:
                        self.fail("WebSocket closed while waiting for broadcast")
                    frame_buf.extend(chunk)
                self.fail(f"No WS broadcast received: {what}")

            # Initial state is pushed right after the handshake
            next_state_where(lambda s: s["phase"] == "lobby", "initial lobby state", 5)

            # Quiet lobby: the ticker must not spam unchanged state
            self.assertEqual(read_text_frames_for(2.0), [])

            # Short timers so the whole game runs on timeouts alone
            # (set directly: set_timing clamps question_sec to >= 5 s)
            server.QUIZ.question_duration_sec = 1.5
            server.QUIZ.reveal_duration_sec = 1.5

            body = json.dumps({"action": "start"}).encode()
            req = Request(
                f"{self.base_url}/api/host/action",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {server.HOST_TOKEN}",
                },
                method="POST",
            )
            with urlopen(req, timeout=5) as r:
                self.assertEqual(r.status, 200)

            # From here on NOBODY answers or polls — the ticker must push
            # every transition of the 2-question bank on its own.
            next_state_where(
                lambda s: s["phase"] == "question" and s["current_index"] == 0,
                "start broadcast", 5,
            )
            next_state_where(
                lambda s: s["phase"] == "reveal" and s["current_index"] == 0,
                "auto reveal broadcast (question timeout)", 10,
            )
            next_state_where(
                lambda s: s["phase"] == "question" and s["current_index"] == 1,
                "auto next-question broadcast", 10,
            )
            next_state_where(
                lambda s: s["phase"] == "finished",
                "auto finished broadcast", 10,
            )
        finally:
            sock.close()
            server.QUIZ.question_duration_sec = orig_q_sec
            server.QUIZ.reveal_duration_sec = orig_r_sec

    def test_admin_banks_api(self):
        banks = self._get("/api/admin/banks")
        self.assertIsInstance(banks, list)
        self.assertGreater(len(banks), 0)

    def test_admin_bank_load(self):
        data = self._get("/api/admin/bank?filename=test_bank.json")
        self.assertEqual(len(data["questions"]), 2)

    def test_admin_bank_save(self):
        new_q = [{"id": "new1", "prompt": "New?", "options": ["A", "B", "C", "D"], "correct_index": 0}]
        result = self._post("/api/admin/bank/save", {"filename": "new_test.json", "questions": new_q})
        self.assertTrue(result["ok"])

    def test_admin_activate_bank(self):
        result = self._post("/api/admin/bank/activate", {"filename": "test_bank.json"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 2)

    def test_admin_timing(self):
        result = self._post("/api/admin/timing", {"question_duration_sec": 30, "reveal_duration_sec": 8})
        self.assertTrue(result["ok"])
        self.assertEqual(result["question_duration_sec"], 30)
        self.assertEqual(result["reveal_duration_sec"], 8)

    def test_admin_timing_new_keys(self):
        result = self._post("/api/admin/timing", {"get_ready_sec": 5, "final_double": True})
        self.assertTrue(result["ok"])
        self.assertEqual(result["get_ready_sec"], 5)
        self.assertTrue(result["final_double"])
        # Restore defaults so other tests see a clean global QUIZ
        result = self._post("/api/admin/timing", {"get_ready_sec": 3, "final_double": False})
        self.assertEqual(result["get_ready_sec"], 3)
        self.assertFalse(result["final_double"])

    def test_admin_timing_adaptive_time(self):
        result = self._post("/api/admin/timing", {"adaptive_time": False})
        self.assertFalse(result["adaptive_time"])
        self.assertFalse(self._get("/api/state")["adaptive_time"])
        result = self._post("/api/admin/timing", {"adaptive_time": True})
        self.assertTrue(result["adaptive_time"])

    def test_admin_timing_old_payload_compat(self):
        """Old payloads without the new keys still work and leave them untouched."""
        before = self._get("/api/state")
        result = self._post("/api/admin/timing", {"question_duration_sec": 25})
        self.assertTrue(result["ok"])
        self.assertEqual(result["question_duration_sec"], 25)
        self.assertEqual(result["get_ready_sec"], before["get_ready_sec"])
        self.assertEqual(result["final_double"], before["final_double"])

    def test_admin_auth_status(self):
        data = self._get("/api/admin/auth-status")
        self.assertFalse(data["auth_required"])

    def test_admin_history_api(self):
        history = self._get("/api/admin/history")
        self.assertIsInstance(history, list)

    def test_admin_ollama_config(self):
        data = self._get("/api/admin/ollama/config")
        self.assertIn("base_url", data)
        self.assertIn("model", data)

    def test_admin_ollama_config_update(self):
        result = self._post("/api/admin/ollama/config", {"base_url": "http://myhost:12345", "model": "test:7b"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["config"]["base_url"], "http://myhost:12345")
        # Reset
        self._post("/api/admin/ollama/config", {"base_url": "http://localhost:11434", "model": "gpt-oss:20b"})

    def test_host_action_requires_token(self):
        """Host action without token should return 403."""
        r = self._post("/api/register", {"name": "Player"})
        try:
            self._post("/api/host/action", {"action": "start"})
            self.fail("Expected HTTPError 403")
        except Exception:
            pass  # Expected 403

    def test_save_history_action(self):
        r = self._post("/api/register", {"name": "HistoryPlayer"})
        result = self._host_post("/api/host/action", {"action": "save_history"})
        self.assertTrue(result["ok"])
        self.assertIn("record", result)


class TestRegisterAvatarHTTP(unittest.TestCase):
    """Tests for optional avatar_id in POST /api/register."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        cls.orig_questions_dir = server.QUESTIONS_DIR
        cls.orig_history_dir = server.HISTORY_DIR
        server.QUESTIONS_DIR = Path(cls.tmpdir) / "questions"
        server.HISTORY_DIR = Path(cls.tmpdir) / "history"
        server.QUESTIONS_DIR.mkdir()
        server.HISTORY_DIR.mkdir()

        # Isolated user DB (needed for logged-in registration tests)
        import db
        cls.orig_db_path = db._DB_PATH
        db.set_db_path(Path(cls.tmpdir) / "test_avatar.db")
        db._migrated = False
        db.init_db()

        test_q = [
            {"id": "av1", "prompt": "Avatar Q1?", "options": ["A", "B", "C", "D"], "correct_index": 0, "explanation": "A"},
        ]
        server.QUIZ.reload_questions(test_q, "avatar_bank.json")
        server.ADMIN_AUTH.set_password(None)

        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        server.QUESTIONS_DIR = cls.orig_questions_dir
        server.HISTORY_DIR = cls.orig_history_dir
        import db
        db.set_db_path(cls.orig_db_path)
        shutil.rmtree(cls.tmpdir)

    def setUp(self):
        # Avoid 429s from the shared registration rate limiter
        server.REGISTER_LIMITER._attempts.clear()

    def _get(self, path):
        with urlopen(Request(f"{self.base_url}{path}")) as r:
            return json.loads(r.read())

    def _post(self, path, data):
        body = json.dumps(data).encode()
        req = Request(f"{self.base_url}{path}", data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req) as r:
            return json.loads(r.read())

    def test_register_valid_avatar_saved_and_returned(self):
        reg = self._post("/api/register", {"name": "AvatarFan", "avatar_id": 7})
        self.assertEqual(reg["avatar_id"], 7)
        # avatar_id propagates to me + players in state
        state = self._get(f"/api/state?player_id={reg['player_id']}")
        self.assertEqual(state["me"]["avatar_id"], 7)
        entry = next(p for p in state["players"] if p["name"] == "AvatarFan")
        self.assertEqual(entry["avatar_id"], 7)

    def test_register_avatar_numeric_string(self):
        reg = self._post("/api/register", {"name": "StringAv", "avatar_id": "12"})
        self.assertEqual(reg["avatar_id"], 12)

    def test_register_avatar_invalid_falls_back_to_zero(self):
        for i, bad in enumerate(["abc", -1, 999]):
            reg = self._post("/api/register", {"name": f"BadAv{i}", "avatar_id": bad})
            self.assertIn("player_id", reg)  # registration must still succeed
            self.assertEqual(reg["avatar_id"], 0)

    def test_register_avatar_missing_defaults_to_zero(self):
        reg = self._post("/api/register", {"name": "NoAv"})
        self.assertIn("player_id", reg)
        self.assertEqual(reg["avatar_id"], 0)

    def test_register_logged_in_profile_avatar_wins(self):
        import db
        user = db.create_user("AvUser", "pass1234", "student", avatar_id=5)
        token = db.create_session(user["id"])
        reg = self._post("/api/register", {"name": "AvUser", "avatar_id": 9, "user_token": token})
        self.assertEqual(reg["avatar_id"], 5)

    def test_register_logged_in_without_profile_avatar_uses_sent(self):
        import db
        user = db.create_user("NoAvUser", "pass1234", "student")
        # create_user always assigns 1-20, so force "no avatar" directly in DB
        conn = db.get_conn()
        conn.execute("UPDATE users SET avatar_id = 0 WHERE id = ?", (user["id"],))
        conn.commit()
        conn.close()
        token = db.create_session(user["id"])
        reg = self._post("/api/register", {"name": "NoAvUser", "avatar_id": 9, "user_token": token})
        self.assertEqual(reg["avatar_id"], 9)


class TestAdminAuthHTTP(unittest.TestCase):
    """Tests for admin auth over HTTP."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        cls.orig_questions_dir = server.QUESTIONS_DIR
        cls.orig_history_dir = server.HISTORY_DIR
        server.QUESTIONS_DIR = Path(cls.tmpdir) / "questions"
        server.HISTORY_DIR = Path(cls.tmpdir) / "history"
        server.QUESTIONS_DIR.mkdir()
        server.HISTORY_DIR.mkdir()

        server.ADMIN_AUTH.set_password("testpass123")

        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        server.QUESTIONS_DIR = cls.orig_questions_dir
        server.HISTORY_DIR = cls.orig_history_dir
        server.ADMIN_AUTH.set_password(None)
        shutil.rmtree(cls.tmpdir)

    def _post(self, path, data, headers=None):
        body = json.dumps(data).encode()
        req = Request(f"{self.base_url}{path}", data=body, headers={"Content-Type": "application/json"}, method="POST")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urlopen(req) as r:
                return json.loads(r.read()), r.status
        except HTTPError as e:
            return json.loads(e.read()), e.code

    def _get(self, path, headers=None):
        req = Request(f"{self.base_url}{path}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urlopen(req) as r:
                return json.loads(r.read()), r.status
        except HTTPError as e:
            return json.loads(e.read()), e.code

    def test_admin_banks_unauthorized(self):
        data, status = self._get("/api/admin/banks")
        self.assertEqual(status, 401)

    def test_login_wrong_password(self):
        data, status = self._post("/api/admin/login", {"password": "wrong"})
        self.assertEqual(status, 401)

    def test_login_correct_password(self):
        data, status = self._post("/api/admin/login", {"password": "testpass123"})
        self.assertEqual(status, 200)
        self.assertIn("token", data)

    def test_access_with_bearer_token(self):
        login_data, _ = self._post("/api/admin/login", {"password": "testpass123"})
        token = login_data["token"]
        # Token must be sent via Authorization header, not query param
        data, status = self._get("/api/admin/banks", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(status, 200)

    def test_token_in_query_rejected(self):
        """Token in URL query should NOT work — only Authorization header."""
        login_data, _ = self._post("/api/admin/login", {"password": "testpass123"})
        token = login_data["token"]
        data, status = self._get(f"/api/admin/banks?token={token}")
        self.assertEqual(status, 401)

    def test_auth_status_shows_required(self):
        data, status = self._get("/api/admin/auth-status")
        self.assertEqual(status, 200)
        self.assertTrue(data["auth_required"])
        self.assertFalse(data["authenticated"])

    def test_rate_limiting(self):
        """After MAX_ATTEMPTS wrong logins, further attempts are blocked."""
        # Reset rate limiter
        server.ADMIN_AUTH._login_attempts.clear()
        for i in range(server.ADMIN_AUTH.MAX_ATTEMPTS):
            self._post("/api/admin/login", {"password": "wrong"})
        # Next attempt should be rate limited even with correct password
        data, status = self._post("/api/admin/login", {"password": "testpass123"})
        self.assertEqual(status, 401)
        # Clean up
        server.ADMIN_AUTH._login_attempts.clear()


class TestExamState(unittest.TestCase):
    """Tests for ExamState (písemka / written-test mode)."""

    def _sample_questions(self):
        return [
            {"id": "q1", "prompt": "2+2?", "options": ["3", "4", "5", "6"], "correct_index": 1},
            {"id": "q2", "prompt": "Capital of CZ?", "options": ["Brno", "Praha", "Ostrava"], "correct_index": 1},
            {"id": "q3", "prompt": "HTML is?", "options": ["markup", "database"], "correct_index": 0},
        ]

    def _exam(self, **cfg):
        e = server.ExamState()
        e.reload_questions(self._sample_questions(), "test")
        if cfg:
            e.update_config(cfg)
        return e

    def test_initial_phase_closed(self):
        e = self._exam()
        self.assertEqual(e.phase, "closed")

    def test_register_and_open(self):
        e = self._exam()
        r = e.register_student("Alice")
        self.assertIn("player_id", r)
        e.open_exam()
        self.assertEqual(e.phase, "open")
        v = e.student_view(r["player_id"], r["player_secret"])
        self.assertTrue(v["known"])
        self.assertEqual(v["total"], 3)
        self.assertEqual(len(v["questions"]), 3)

    def test_open_without_questions_fails(self):
        e = server.ExamState()
        with self.assertRaises(ValueError):
            e.open_exam()

    def test_exam_rejects_unsupported_question_types(self):
        e = server.ExamState()
        qs = self._sample_questions() + [
            {"id": "q4", "type": "typed", "prompt": "Napis OS?", "accept": ["linux"]},
        ]
        with self.assertRaises(ValueError) as ctx:
            e.reload_questions(qs, "mixed")
        self.assertIn("typed", str(ctx.exception))
        self.assertEqual(e.questions, [])  # nothing was loaded

    def test_exam_accepts_truefalse(self):
        e = server.ExamState()
        e.reload_questions([{"id": "t", "type": "truefalse", "prompt": "Linux je OS?",
                             "options": ["Ano", "Ne"], "correct_index": 0}], "tf")
        self.assertEqual(len(e.questions), 1)

    def test_unanswered_question_without_correct_index_not_counted(self):
        # Defensive: a question without integer correct_index must never be
        # counted as correct for a blank answer (None == None).
        e = self._exam(shuffle_questions=False, shuffle_options=False)
        e.questions.append({"id": "qx", "prompt": "?", "options": ["a", "b"]})
        r = e.register_student("Eve")
        e.open_exam()
        res = e.submit(r["player_id"], r["player_secret"])
        self.assertEqual(res["score"], 0)

    def test_grading_all_correct(self):
        e = self._exam(shuffle_questions=False, shuffle_options=False)
        r = e.register_student("Bob")
        pid, sec = r["player_id"], r["player_secret"]
        e.open_exam()
        e.save_answer(pid, sec, "q1", 1)
        e.save_answer(pid, sec, "q2", 1)
        e.save_answer(pid, sec, "q3", 0)
        res = e.submit(pid, sec)
        self.assertEqual(res["score"], 3)
        self.assertEqual(res["grade"], 1)
        self.assertEqual(res["percent"], 100)

    def test_grading_partial(self):
        e = self._exam()
        r = e.register_student("Cara")
        pid, sec = r["player_id"], r["player_secret"]
        e.open_exam()
        e.save_answer(pid, sec, "q1", 1)  # correct only one of three
        res = e.submit(pid, sec)
        self.assertEqual(res["score"], 1)
        # 33% -> worst grade with default thresholds
        self.assertEqual(res["grade"], 5)

    def test_shuffle_preserves_oid_mapping(self):
        e = self._exam(shuffle_questions=True, shuffle_options=True)
        r = e.register_student("Dana")
        pid, sec = r["player_id"], r["player_secret"]
        e.open_exam()
        v = e.student_view(pid, sec)
        # Answer every question with its correct ORIGINAL oid -> full marks
        for q in v["questions"]:
            orig = next(x for x in self._sample_questions() if x["id"] == q["id"])
            e.save_answer(pid, sec, q["id"], orig["correct_index"])
        res = e.submit(pid, sec)
        self.assertEqual(res["score"], 3)

    def test_cannot_answer_after_submit(self):
        e = self._exam()
        r = e.register_student("Eva")
        pid, sec = r["player_id"], r["player_secret"]
        e.open_exam()
        e.submit(pid, sec)
        with self.assertRaises(ValueError):
            e.save_answer(pid, sec, "q1", 1)

    def test_wrong_secret_rejected(self):
        e = self._exam()
        r = e.register_student("Fred")
        e.open_exam()
        with self.assertRaises(ValueError):
            e.save_answer(r["player_id"], "badsecret", "q1", 1)

    def test_auto_submit_after_blurs(self):
        e = self._exam(auto_submit_after_blurs=3)
        r = e.register_student("Greta")
        pid, sec = r["player_id"], r["player_secret"]
        e.open_exam()
        out = None
        for _ in range(3):
            out = e.record_event(pid, sec, "blur")
        self.assertTrue(out.get("auto_submitted"))
        ov = e.overview()
        student = ov["students"][0]
        self.assertTrue(student["submitted"])
        self.assertTrue(student["auto_submitted"])
        self.assertEqual(student["blur_count"], 3)

    def test_time_limit_auto_ends(self):
        e = self._exam(time_limit_sec=30)
        r = e.register_student("Hugo")
        pid, sec = r["player_id"], r["player_secret"]
        e.open_exam()
        # Force the clock back so the window appears expired
        e.opened_at -= 31
        v = e.student_view(pid, sec)
        self.assertEqual(v["phase"], "ended")
        self.assertIn("result", v)

    def test_invalid_choice_rejected(self):
        e = self._exam()
        r = e.register_student("Iva")
        pid, sec = r["player_id"], r["player_secret"]
        e.open_exam()
        with self.assertRaises(ValueError):
            e.save_answer(pid, sec, "q1", 99)

    def test_grade_thresholds_config(self):
        e = self._exam()
        e.update_config({"grade_thresholds": [[50, 1], [0, 2]]})
        self.assertEqual(e.config.grade_for_percent(100), 1)
        self.assertEqual(e.config.grade_for_percent(49), 2)

    def test_results_csv(self):
        e = self._exam(shuffle_questions=False, shuffle_options=False)
        r = e.register_student("Jana")
        pid, sec = r["player_id"], r["player_secret"]
        e.open_exam()
        e.save_answer(pid, sec, "q1", 1)
        e.submit(pid, sec)
        csv_text = e.results_csv()
        self.assertIn("Jana", csv_text)
        self.assertIn("Znamka", csv_text)


class TestExamMode(unittest.TestCase):
    """Tests for the game/exam mode switch."""

    def setUp(self):
        server.set_app_mode("game")

    def tearDown(self):
        server.set_app_mode("game")

    def test_default_mode_is_game(self):
        self.assertEqual(server.get_app_mode(), "game")

    def test_set_mode(self):
        self.assertEqual(server.set_app_mode("exam"), "exam")
        self.assertEqual(server.get_app_mode(), "exam")

    def test_invalid_mode_ignored(self):
        server.set_app_mode("exam")
        server.set_app_mode("nonsense")
        self.assertEqual(server.get_app_mode(), "exam")
class _DBTestBase(unittest.TestCase):
    """Base class for tests that need an isolated SQLite database."""

    def setUp(self):
        import db
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        db.set_db_path(self.db_path)
        # Reset migration flag so each test gets a fresh schema
        db._migrated = False
        db.init_db()

    def tearDown(self):
        import db
        # Reset to a safe in-memory location to avoid stale connections
        db.set_db_path(os.path.join(self.tmpdir, "test.db"))
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestAuth(_DBTestBase):
    """Tests for the user auth system (db.py functions)."""

    def setUp(self):
        super().setUp()
        import db
        self.db = db

    def test_register_student_success(self):
        user = self.db.create_user("Alice", "pass1234", "student")
        self.assertEqual(user["nickname"], "Alice")
        self.assertEqual(user["role"], "student")
        self.assertIn("id", user)

    def test_register_teacher_no_code_required(self):
        user = self.db.create_user("TeacherBob", "pass1234", "teacher")
        self.assertEqual(user["role"], "teacher")

    def test_register_duplicate_nickname(self):
        self.db.create_user("Alice", "pass1234", "student")
        with self.assertRaises(ValueError):
            self.db.create_user("Alice", "otherpass", "student")

    def test_register_duplicate_nickname_case_insensitive(self):
        self.db.create_user("Alice", "pass1234", "student")
        with self.assertRaises(ValueError):
            self.db.create_user("alice", "otherpass", "student")

    def test_login_correct_credentials(self):
        self.db.create_user("Carol", "mypassword", "student")
        user = self.db.authenticate_user("Carol", "mypassword")
        self.assertIsNotNone(user)
        self.assertEqual(user["nickname"], "Carol")

    def test_login_wrong_password(self):
        self.db.create_user("Dave", "correctpass", "student")
        user = self.db.authenticate_user("Dave", "wrongpass")
        self.assertIsNone(user)

    def test_login_nonexistent_user(self):
        user = self.db.authenticate_user("NoSuchUser", "anypass")
        self.assertIsNone(user)

    def test_session_validation_valid_token(self):
        user = self.db.create_user("Eve", "pass1234", "student")
        token = self.db.create_session(user["id"])
        uid = self.db.validate_session(token)
        self.assertEqual(uid, user["id"])

    def test_session_validation_invalid_token(self):
        uid = self.db.validate_session("notarealtoken")
        self.assertIsNone(uid)

    def test_session_validation_none_token(self):
        uid = self.db.validate_session(None)
        self.assertIsNone(uid)

    def test_session_expiry(self):
        user = self.db.create_user("Frank", "pass1234", "student")
        token = self.db.create_session(user["id"])
        # Manually expire the session by patching TTL
        orig_ttl = self.db.SESSION_TTL
        self.db.SESSION_TTL = 0
        time.sleep(0.01)
        try:
            uid = self.db.validate_session(token)
            self.assertIsNone(uid)
        finally:
            self.db.SESSION_TTL = orig_ttl

    def test_logout_invalidates_session(self):
        user = self.db.create_user("Grace", "pass1234", "student")
        token = self.db.create_session(user["id"])
        # Session is valid before logout
        self.assertIsNotNone(self.db.validate_session(token))
        # Logout
        self.db.delete_session(token)
        # Session should be invalid after logout
        self.assertIsNone(self.db.validate_session(token))

    def test_profile_returns_correct_data(self):
        user = self.db.create_user("Heidi", "pass1234", "student")
        profile = self.db.get_user_profile(user["id"])
        self.assertIsNotNone(profile)
        self.assertEqual(profile["nickname"], "Heidi")
        self.assertEqual(profile["role"], "student")
        self.assertIn("classes", profile)
        self.assertIn("recent_games", profile)

    def test_profile_nonexistent_user(self):
        profile = self.db.get_user_profile(99999)
        self.assertIsNone(profile)

    def test_register_short_password_fails(self):
        with self.assertRaises(ValueError):
            self.db.create_user("Ivan", "abc", "student")

    def test_register_empty_nickname_fails(self):
        with self.assertRaises(ValueError):
            self.db.create_user("", "pass1234", "student")

    def test_register_nickname_too_long_fails(self):
        with self.assertRaises(ValueError):
            self.db.create_user("A" * 25, "pass1234", "student")


class TestClasses(_DBTestBase):
    """Tests for class/group management (db.py functions)."""

    def setUp(self):
        super().setUp()
        import db
        self.db = db
        # Create a teacher and a student for tests
        self.teacher = db.create_user("MrSmith", "pass1234", "teacher")
        self.student = db.create_user("StudentJoe", "pass1234", "student")

    def test_teacher_creates_class_success(self):
        cls = self.db.create_class("Math 101", self.teacher["id"])
        self.assertEqual(cls["name"], "Math 101")
        self.assertIn("join_code", cls)
        self.assertIn("id", cls)
        # join_code should be a 6-char uppercase alphanumeric string
        self.assertEqual(len(cls["join_code"]), 6)

    def test_student_cannot_create_class(self):
        with self.assertRaises(ValueError):
            self.db.create_class("My Class", self.student["id"])

    def test_student_joins_class_by_code(self):
        cls = self.db.create_class("History", self.teacher["id"])
        joined = self.db.join_class(cls["join_code"], self.student["id"])
        self.assertEqual(joined["id"], cls["id"])

    def test_join_invalid_code(self):
        with self.assertRaises(ValueError):
            self.db.join_class("INVALID", self.student["id"])

    def test_join_code_case_insensitive(self):
        cls = self.db.create_class("Science", self.teacher["id"])
        code = cls["join_code"].lower()
        joined = self.db.join_class(code, self.student["id"])
        self.assertEqual(joined["id"], cls["id"])

    def test_join_class_idempotent(self):
        """Joining the same class twice should not raise."""
        cls = self.db.create_class("Art", self.teacher["id"])
        self.db.join_class(cls["join_code"], self.student["id"])
        # Second join should not raise
        self.db.join_class(cls["join_code"], self.student["id"])

    def test_list_classes_for_teacher(self):
        self.db.create_class("Class A", self.teacher["id"])
        self.db.create_class("Class B", self.teacher["id"])
        classes = self.db.list_user_classes(self.teacher["id"])
        self.assertEqual(len(classes), 2)

    def test_list_classes_for_student(self):
        cls1 = self.db.create_class("Class X", self.teacher["id"])
        cls2 = self.db.create_class("Class Y", self.teacher["id"])
        self.db.join_class(cls1["join_code"], self.student["id"])
        classes = self.db.list_user_classes(self.student["id"])
        self.assertEqual(len(classes), 1)
        self.assertEqual(classes[0]["id"], cls1["id"])

    def test_get_class_members(self):
        cls = self.db.create_class("PE Class", self.teacher["id"])
        self.db.join_class(cls["join_code"], self.student["id"])
        members = self.db.get_class_members(cls["id"])
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["nickname"], "StudentJoe")

    def test_get_class_members_empty(self):
        cls = self.db.create_class("Empty Class", self.teacher["id"])
        members = self.db.get_class_members(cls["id"])
        self.assertEqual(len(members), 0)

    def test_delete_class_by_owner(self):
        cls = self.db.create_class("To Delete", self.teacher["id"])
        result = self.db.delete_class(cls["id"], self.teacher["id"])
        self.assertTrue(result)
        classes = self.db.list_user_classes(self.teacher["id"])
        self.assertEqual(len(classes), 0)

    def test_delete_class_wrong_owner(self):
        # Another teacher tries to delete the class
        import db
        other_teacher = db.create_user("MrsJones", "pass1234", "teacher")
        cls = self.db.create_class("Protected", self.teacher["id"])
        result = self.db.delete_class(cls["id"], other_teacher["id"])
        self.assertFalse(result)
        # Class should still exist
        classes = self.db.list_user_classes(self.teacher["id"])
        self.assertEqual(len(classes), 1)

    def test_delete_nonexistent_class(self):
        result = self.db.delete_class(99999, self.teacher["id"])
        self.assertFalse(result)

    def test_create_class_empty_name_fails(self):
        with self.assertRaises(ValueError):
            self.db.create_class("", self.teacher["id"])

    def test_create_class_name_too_long_fails(self):
        with self.assertRaises(ValueError):
            self.db.create_class("A" * 101, self.teacher["id"])


class TestQuestionTypes(unittest.TestCase):
    """Tests for all 5 question type scoring via QuizState._calculate_points."""

    # Use a fixed far-past timestamp so speed_bonus is always 0
    # (elapsed >= 40s means speed_bonus = max(0, 40 - 40) = 0)
    _STARTED = 0.0          # Unix epoch
    _TIME_40S = 40.0        # 40 seconds after start -> speed_bonus = 0

    def _answer(self, choice, t=None):
        """Build an answer dict."""
        return {"choice": choice, "time": t if t is not None else self._TIME_40S}

    # --- Choice ---

    def test_choice_correct_scores(self):
        q = {"type": "choice", "options": ["A", "B", "C", "D"], "correct_index": 2}
        pts = server.QuizState._calculate_points(q, "choice", self._answer(2), self._STARTED)
        self.assertGreater(pts, 0)

    def test_choice_correct_base_score(self):
        q = {"type": "choice", "options": ["A", "B", "C", "D"], "correct_index": 2}
        pts = server.QuizState._calculate_points(q, "choice", self._answer(2), self._STARTED)
        self.assertEqual(pts, 600)  # speed_bonus = 0 at t=40s

    def test_choice_wrong_scores_zero(self):
        q = {"type": "choice", "options": ["A", "B", "C", "D"], "correct_index": 2}
        pts = server.QuizState._calculate_points(q, "choice", self._answer(0), self._STARTED)
        self.assertEqual(pts, 0)

    def test_choice_speed_bonus_fast_answer(self):
        q = {"type": "choice", "options": ["A", "B", "C", "D"], "correct_index": 0}
        # Answer at t=0 (instant) -> elapsed=0, speed_bonus=40, points=600+40*10=1000
        pts = server.QuizState._calculate_points(q, "choice", self._answer(0, t=0.0), self._STARTED)
        self.assertEqual(pts, 1000)

    # --- TrueFalse ---

    def test_truefalse_correct_scores(self):
        q = {"type": "truefalse", "options": ["True", "False"], "correct_index": 0}
        pts = server.QuizState._calculate_points(q, "truefalse", self._answer(0), self._STARTED)
        self.assertGreater(pts, 0)

    def test_truefalse_wrong_scores_zero(self):
        q = {"type": "truefalse", "options": ["True", "False"], "correct_index": 0}
        pts = server.QuizState._calculate_points(q, "truefalse", self._answer(1), self._STARTED)
        self.assertEqual(pts, 0)

    # --- Multiselect (Jaccard-style) ---

    def test_multiselect_all_correct_scores_full(self):
        q = {"type": "multiselect", "options": ["A", "B", "C", "D"],
             "correct_indices": [0, 2]}
        pts = server.QuizState._calculate_points(q, "multiselect", self._answer([0, 2]), self._STARTED)
        self.assertEqual(pts, 600)

    def test_multiselect_partial_correct_subset(self):
        """Select only 1 of 2 correct, no wrong — partial credit."""
        q = {"type": "multiselect", "options": ["A", "B", "C", "D"],
             "correct_indices": [0, 2]}
        pts = server.QuizState._calculate_points(q, "multiselect", self._answer([0]), self._STARTED)
        # partial: ratio = 1/2 = 0.5 -> int(600 * 0.5) = 300
        self.assertGreater(pts, 0)
        self.assertLess(pts, 600)

    def test_multiselect_wrong_selection_scores_zero(self):
        """Including a wrong answer scores 0."""
        q = {"type": "multiselect", "options": ["A", "B", "C", "D"],
             "correct_indices": [0, 2]}
        pts = server.QuizState._calculate_points(q, "multiselect", self._answer([0, 1]), self._STARTED)
        self.assertEqual(pts, 0)

    def test_multiselect_all_wrong_scores_zero(self):
        q = {"type": "multiselect", "options": ["A", "B", "C", "D"],
             "correct_indices": [0, 2]}
        pts = server.QuizState._calculate_points(q, "multiselect", self._answer([1, 3]), self._STARTED)
        self.assertEqual(pts, 0)

    # --- Ordering (Kendall tau / inversion count) ---

    def test_ordering_items_are_shuffled_and_answers_mapped_back(self):
        items = ["Discover", "Offer", "Request", "Acknowledge"]
        q = server.QuizState([{"id": "o", "type": "ordering", "prompt": "DORA?", "items": list(items)}])
        q.get_ready_sec = 0
        q.speed_bonus = False  # full points do not depend on how fast the test machine is
        lazy, smart = q.register_player("Lina"), q.register_player("Bystra")
        q.host_action("start")
        shown = q.public_state(lazy["player_id"])["question"]["items"]
        self.assertEqual(sorted(shown), sorted(items))
        self.assertNotEqual(shown, items)  # never handed out in the right order
        self.assertEqual(q.public_state(smart["player_id"])["question"]["items"], shown)  # same for all
        self.assertNotIn("correct_order", q.public_state(lazy["player_id"])["question"])
        # sending the order as shown is not a free answer any more
        q.submit_answer(lazy["player_id"], lazy["player_secret"], list(range(4)))
        right = [shown.index(x) for x in items]  # display positions in the right order
        q.submit_answer(smart["player_id"], smart["player_secret"], right)
        self.assertEqual(q.answers[smart["player_id"]]["choice"], [0, 1, 2, 3])  # stored as bank items
        self.assertTrue(q.public_state()["all_answered"])  # everybody answered: a short beat, then the result
        with mock.patch("time.time", return_value=time.time() + server.ALL_ANSWERED_GRACE_SEC + 0.1):
            self.assertEqual(q.public_state()["phase"], "reveal")
        state = q.public_state(smart["player_id"])
        self.assertEqual(state["question"]["items"], shown)
        self.assertEqual(state["my_choice"], right)  # the player sees the answer the way it was shown
        self.assertEqual([shown[i] for i in state["question"]["correct_order"]], items)
        scores = {p.name: p.score for p in q.players.values()}
        self.assertEqual(scores["Bystra"], 1000)
        self.assertLess(scores["Lina"], 1000)
        self.assertEqual(q.question_log[0]["items"], items)  # the analysis lists the right order

    def test_ordering_perfect_order_scores_full(self):
        q = {"type": "ordering", "items": ["A", "B", "C", "D"]}
        pts = server.QuizState._calculate_points(q, "ordering", self._answer([0, 1, 2, 3]), self._STARTED)
        self.assertEqual(pts, 600)

    def test_ordering_reversed_scores_zero(self):
        """Fully reversed order has max inversions, ratio=0, score=0."""
        q = {"type": "ordering", "items": ["A", "B", "C", "D"]}
        pts = server.QuizState._calculate_points(q, "ordering", self._answer([3, 2, 1, 0]), self._STARTED)
        self.assertEqual(pts, 0)

    def test_ordering_partially_correct(self):
        """One adjacent swap — partial credit."""
        q = {"type": "ordering", "items": ["A", "B", "C", "D"]}
        # [0,1,3,2] — one inversion out of 6 max
        pts = server.QuizState._calculate_points(q, "ordering", self._answer([0, 1, 3, 2]), self._STARTED)
        self.assertGreater(pts, 0)
        self.assertLess(pts, 600)

    def test_ordering_two_items_wrong_order_zero(self):
        """For 2 items, reversed = 1 inversion, max_inversions=1, ratio=0 -> 0 pts."""
        q = {"type": "ordering", "items": ["A", "B"]}
        pts = server.QuizState._calculate_points(q, "ordering", self._answer([1, 0]), self._STARTED)
        self.assertEqual(pts, 0)

    # --- OpenEnded ---

    def test_openended_exact_match(self):
        q = {"type": "openended", "accepted_answers": ["Python"]}
        pts = server.QuizState._calculate_points(q, "openended", self._answer("Python"), self._STARTED)
        self.assertGreater(pts, 0)

    def test_openended_exact_match_case_insensitive(self):
        q = {"type": "openended", "accepted_answers": ["Python"]}
        pts = server.QuizState._calculate_points(q, "openended", self._answer("python"), self._STARTED)
        self.assertGreater(pts, 0)

    def test_openended_fuzzy_match_levenshtein_1(self):
        """One typo in a >4-char answer should still score (max_dist=1)."""
        q = {"type": "openended", "accepted_answers": ["Linux"]}
        # "Linus" has edit distance 1 from "Linux"
        pts = server.QuizState._calculate_points(q, "openended", self._answer("Linus"), self._STARTED)
        self.assertGreater(pts, 0)

    def test_openended_fuzzy_match_levenshtein_2(self):
        """Two typos in a >8-char answer should still score (max_dist=2)."""
        q = {"type": "openended", "accepted_answers": ["JavaScript"]}
        # "Javascrpyt" has edit distance 2 from "javascript"
        pts = server.QuizState._calculate_points(q, "openended", self._answer("Javascrpyt"), self._STARTED)
        self.assertGreater(pts, 0)

    def test_openended_exact_turns_off_typo_tolerance(self):
        """Commands or ports one or two characters apart must not pass for each other."""
        cmd = {"type": "openended", "accepted_answers": ["Get-TimeZone"]}
        self.assertGreater(server.QuizState._calculate_points(cmd, "openended", self._answer("Set-TimeZone"),
                                                              self._STARTED), 0)  # the trap without "exact"
        cmd["exact"] = True
        for wrong in ("Set-TimeZone", "Get-TimeZon"):
            self.assertEqual(server.QuizState._calculate_points(cmd, "openended", self._answer(wrong),
                                                                self._STARTED), 0, wrong)
        self.assertGreater(server.QuizState._calculate_points(cmd, "openended", self._answer("  get-timezone "),
                                                              self._STARTED), 0)  # case and spaces still free
        self.assertEqual(server._score_selfpaced_answer(cmd, "Set-TimeZone"), (False, 0))
        self.assertEqual(server._score_selfpaced_answer(cmd, "GET-TIMEZONE"), (True, 600))
        with self.assertRaises(RuntimeError):
            server._validate_question({"id": "x", "prompt": "?", "type": "openended",
                                       "accepted_answers": ["a"], "exact": "ano"}, 0)

    def test_two_ordering_items_are_not_always_swapped(self):
        """Always swapping two items would give the answer away just like never swapping."""
        rng = server.random.Random(7)
        orders = {tuple(server._shuffled_order(2, rng)) for _ in range(50)}
        self.assertEqual(orders, {(0, 1), (1, 0)})
        rng = server.random.Random(7)
        self.assertNotIn((0, 1, 2), {tuple(server._shuffled_order(3, rng)) for _ in range(200)})

    def test_ordering_answer_must_be_whole_numbers(self):
        q = server.QuizState([{"id": "o", "type": "ordering", "prompt": "?", "items": ["a", "b", "c"]}])
        q.get_ready_sec = 0
        p = q.register_player("Ema")
        q.host_action("start")
        with self.assertRaises(ValueError):
            q.submit_answer(p["player_id"], p["player_secret"], [0.0, 1.0, 2.0])

    def test_openended_no_match_scores_zero(self):
        q = {"type": "openended", "accepted_answers": ["Python"]}
        pts = server.QuizState._calculate_points(q, "openended", self._answer("Ruby"), self._STARTED)
        self.assertEqual(pts, 0)

    def test_openended_short_answer_no_fuzzy(self):
        """Short answers (<=4 chars) get no fuzzy matching."""
        q = {"type": "openended", "accepted_answers": ["cat"]}
        # "bat" has edit distance 1 but target len=3 <= 4, max_dist=0
        pts = server.QuizState._calculate_points(q, "openended", self._answer("bat"), self._STARTED)
        self.assertEqual(pts, 0)

    def test_openended_multiple_accepted_answers(self):
        q = {"type": "openended", "accepted_answers": ["Python", "python3", "CPython"]}
        pts = server.QuizState._calculate_points(q, "openended", self._answer("python3"), self._STARTED)
        self.assertGreater(pts, 0)


class TestStreakCombo(unittest.TestCase):
    """Tests for streak and combo multiplier system."""

    def _make_qs(self, n_questions=6):
        """Create a QuizState with n simple choice questions."""
        questions = [
            {
                "id": f"s{i}",
                "prompt": f"Q{i}?",
                "options": ["A", "B", "C", "D"],
                "correct_index": 0,
            }
            for i in range(n_questions)
        ]
        return server.QuizState(questions)

    def _play_question(self, qs, pid, secret, correct=True):
        """Reveal current question, submit answer, then advance."""
        choice = 0 if correct else 1  # correct_index is always 0
        qs.submit_answer(pid, secret, choice)
        qs.host_action("reveal")
        qs.host_action("next")

    def test_streak_increments_on_consecutive_correct(self):
        qs = self._make_qs()
        r = qs.register_player("Alice")
        qs.register_player("Dummy")  # prevent auto-advance
        pid, secret = r["player_id"], r["player_secret"]
        qs.host_action("start")

        # Answer first two questions correctly
        for _ in range(2):
            qs.submit_answer(pid, secret, 0)  # correct
            qs.host_action("reveal")
            qs.host_action("next")

        self.assertEqual(qs.players[pid].streak, 2)

    def test_streak_resets_on_wrong_answer(self):
        qs = self._make_qs()
        r = qs.register_player("Bob")
        qs.register_player("Dummy")
        pid, secret = r["player_id"], r["player_secret"]
        qs.host_action("start")

        # Two correct, then one wrong
        for _ in range(2):
            qs.submit_answer(pid, secret, 0)  # correct
            qs.host_action("reveal")
            qs.host_action("next")

        self.assertEqual(qs.players[pid].streak, 2)

        qs.submit_answer(pid, secret, 1)  # wrong
        qs.host_action("reveal")
        qs.host_action("next")

        self.assertEqual(qs.players[pid].streak, 0)

    def test_streak_resets_on_no_answer(self):
        """Player who doesn't answer a question should have streak reset."""
        qs = self._make_qs()
        r = qs.register_player("Carol")
        qs.register_player("Dummy")
        pid, secret = r["player_id"], r["player_secret"]
        qs.host_action("start")

        # Answer first question correctly (streak=1)
        qs.submit_answer(pid, secret, 0)
        qs.host_action("reveal")
        qs.host_action("next")
        self.assertEqual(qs.players[pid].streak, 1)

        # Skip answering question 2 — only Dummy answers
        dr = [p for p_id, p in qs.players.items() if p.name == "Dummy"]
        # Reveal without Carol answering (force reveal)
        qs.host_action("reveal")
        qs.host_action("next")

        # Carol didn't answer, streak should be 0
        self.assertEqual(qs.players[pid].streak, 0)

    def _inject_answer(self, qs, pid, correct=True):
        """Inject an answer directly into qs.answers with a fresh start time.

        Uses time.time() as question_started_at so elapsed ≈ 0, giving
        speed_bonus = 40 and base_points = 1000.  The phase is kept as
        "question" so host_action("reveal") works normally.
        """
        now = time.time()
        qs.question_started_at = now
        qs.answers = {}
        choice = 0 if correct else 1  # correct_index is always 0
        qs.answers[pid] = {"choice": choice, "time": now}
        qs.scored_question_index = None  # ensure scoring runs

    # Base points when speed_bonus = 40 (answered instantly): 600 + 40*10 = 1000
    _BASE_PTS = 1000

    def test_multiplier_streak_1_and_2_is_1x(self):
        """Streaks 1 and 2 use 1.0x multiplier."""
        qs = self._make_qs(n_questions=3)
        r = qs.register_player("Dave")
        qs.register_player("Dummy")
        pid, secret = r["player_id"], r["player_secret"]
        qs.host_action("start")

        # Two correct answers -> streak 1, 2 (both 1.0x)
        for _ in range(2):
            self._inject_answer(qs, pid, correct=True)
            qs.host_action("reveal")
            qs.host_action("next")

        # 1000*1.0 + 1000*1.0 = 2000
        self.assertEqual(qs.players[pid].score, self._BASE_PTS * 2)
        self.assertEqual(qs.players[pid].streak, 2)

    def test_multiplier_streak_3_is_1_2x(self):
        """Streak 3 uses 1.2x multiplier — 3rd question gives int(1000*1.2)=1200."""
        qs = self._make_qs(n_questions=4)
        r = qs.register_player("Eve")
        qs.register_player("Dummy")
        pid, secret = r["player_id"], r["player_secret"]
        qs.host_action("start")

        for _ in range(3):
            self._inject_answer(qs, pid, correct=True)
            qs.host_action("reveal")
            qs.host_action("next")

        # 1000 + 1000 + int(1000*1.2)=1200 = 3200
        expected = self._BASE_PTS * 2 + int(self._BASE_PTS * 1.2)
        self.assertEqual(qs.players[pid].score, expected)
        self.assertEqual(qs.players[pid].streak, 3)

    def test_multiplier_streak_4_is_1_4x(self):
        """Streak 4 uses 1.4x multiplier — 4th question gives int(1000*1.4)=1400."""
        qs = self._make_qs(n_questions=5)
        r = qs.register_player("Frank")
        qs.register_player("Dummy")
        pid, secret = r["player_id"], r["player_secret"]
        qs.host_action("start")

        for _ in range(4):
            self._inject_answer(qs, pid, correct=True)
            qs.host_action("reveal")
            qs.host_action("next")

        # 1000+1000+1200+int(1000*1.4)=1400 = 4600
        expected = (self._BASE_PTS * 2
                    + int(self._BASE_PTS * 1.2)
                    + int(self._BASE_PTS * 1.4))
        self.assertEqual(qs.players[pid].score, expected)
        self.assertEqual(qs.players[pid].streak, 4)

    def test_multiplier_streak_5_plus_is_1_5x(self):
        """Streak 5+ uses 1.5x multiplier — 5th question gives int(1000*1.5)=1500."""
        qs = self._make_qs(n_questions=6)
        r = qs.register_player("Grace")
        qs.register_player("Dummy")
        pid, secret = r["player_id"], r["player_secret"]
        qs.host_action("start")

        for _ in range(5):
            self._inject_answer(qs, pid, correct=True)
            qs.host_action("reveal")
            qs.host_action("next")

        # 1000+1000+1200+1400+int(1000*1.5)=1500 = 6100
        expected = (self._BASE_PTS * 2
                    + int(self._BASE_PTS * 1.2)
                    + int(self._BASE_PTS * 1.4)
                    + int(self._BASE_PTS * 1.5))
        self.assertEqual(qs.players[pid].score, expected)
        self.assertEqual(qs.players[pid].streak, 5)

    def test_max_streak_tracks_highest(self):
        qs = self._make_qs(n_questions=4)
        r = qs.register_player("Heidi")
        qs.register_player("Dummy")
        pid, secret = r["player_id"], r["player_secret"]
        qs.host_action("start")

        # Three correct in a row (streak reaches 3, max_streak=3)
        for _ in range(3):
            self._inject_answer(qs, pid, correct=True)
            qs.host_action("reveal")
            qs.host_action("next")

        self.assertEqual(qs.players[pid].max_streak, 3)

        # Now get one wrong (streak resets to 0, max_streak stays 3)
        self._inject_answer(qs, pid, correct=False)
        qs.host_action("reveal")

        self.assertEqual(qs.players[pid].streak, 0)
        self.assertEqual(qs.players[pid].max_streak, 3)

    def test_max_streak_exposed_in_public_state(self):
        qs = self._make_qs(n_questions=2)
        r = qs.register_player("Ivan")
        qs.register_player("Dummy")
        pid, secret = r["player_id"], r["player_secret"]
        qs.host_action("start")

        self._inject_answer(qs, pid, correct=True)
        qs.host_action("reveal")

        state = qs.public_state(player_id=pid)
        self.assertIn("max_streak", state["me"])
        self.assertEqual(state["me"]["max_streak"], 1)


class TestDoublePoints(unittest.TestCase):
    """Tests for the double-points question flag and final_double timing option."""

    def _make_qs(self, n_questions=3, double_indices=()):
        questions = []
        for i in range(n_questions):
            q = {"id": f"d{i}", "prompt": f"Q{i}?", "options": ["A", "B", "C", "D"], "correct_index": 0}
            if i in double_indices:
                q["double"] = True
            questions.append(q)
        return server.QuizState(questions)

    def _inject_answer(self, qs, pid, correct=True):
        """Inject an instant answer (elapsed 0 -> base points 1000)."""
        now = time.time()
        qs.question_started_at = now
        qs.answers = {pid: {"choice": 0 if correct else 1, "time": now}}
        qs.scored_question_index = None

    def test_double_flag_validation_accepts_missing(self):
        q = {"id": "v1", "prompt": "Q?", "options": ["A", "B"], "correct_index": 0}
        server._validate_question(q, 0)  # should not raise

    def test_double_flag_validation_accepts_bool(self):
        for value in (True, False):
            q = {"id": "v1", "prompt": "Q?", "options": ["A", "B"], "correct_index": 0, "double": value}
            server._validate_question(q, 0)  # should not raise

    def test_double_flag_validation_rejects_non_bool(self):
        for value in (1, "yes", None, [True]):
            q = {"id": "v1", "prompt": "Q?", "options": ["A", "B"], "correct_index": 0, "double": value}
            with self.assertRaises(RuntimeError):
                server._validate_question(q, 0)

    def test_double_question_doubles_points(self):
        qs = self._make_qs(double_indices={0})
        r = qs.register_player("Alice")
        qs.register_player("Dummy")  # prevent auto-advance
        pid = r["player_id"]
        qs.host_action("start")
        self._inject_answer(qs, pid, correct=True)
        qs.host_action("reveal")
        self.assertEqual(qs.players[pid].score, 2000)  # 1000 * 2

    def test_normal_question_not_doubled(self):
        qs = self._make_qs()
        r = qs.register_player("Alice")
        qs.register_player("Dummy")
        pid = r["player_id"]
        qs.host_action("start")
        self._inject_answer(qs, pid, correct=True)
        qs.host_action("reveal")
        self.assertEqual(qs.players[pid].score, 1000)

    def test_double_wrong_answer_still_zero(self):
        qs = self._make_qs(double_indices={0})
        r = qs.register_player("Alice")
        qs.register_player("Dummy")
        pid = r["player_id"]
        qs.host_action("start")
        self._inject_answer(qs, pid, correct=False)
        qs.host_action("reveal")
        self.assertEqual(qs.players[pid].score, 0)

    def test_set_timing_final_double(self):
        qs = self._make_qs()
        self.assertFalse(qs.final_double)  # default off
        qs.set_timing(final_double=True)
        self.assertTrue(qs.final_double)
        qs.set_timing(question_sec=30)  # old-style call leaves it untouched
        self.assertTrue(qs.final_double)
        qs.set_timing(final_double=False)
        self.assertFalse(qs.final_double)

    def test_final_double_scores_last_question_double(self):
        qs = self._make_qs(n_questions=2)
        qs.set_timing(final_double=True)
        r = qs.register_player("Alice")
        qs.register_player("Dummy")
        pid = r["player_id"]
        qs.host_action("start")

        self._inject_answer(qs, pid, correct=True)
        qs.host_action("reveal")
        self.assertEqual(qs.players[pid].score, 1000)  # first question not doubled

        qs.host_action("next")
        self._inject_answer(qs, pid, correct=True)
        qs.host_action("reveal")
        # Last question: streak 2 (1.0x) -> 1000 * 2 = 2000
        self.assertEqual(qs.players[pid].score, 3000)

    def test_final_double_off_by_default(self):
        qs = self._make_qs(n_questions=2)
        r = qs.register_player("Alice")
        qs.register_player("Dummy")
        pid = r["player_id"]
        qs.host_action("start")
        self._inject_answer(qs, pid, correct=True)
        qs.host_action("reveal")
        qs.host_action("next")
        self._inject_answer(qs, pid, correct=True)
        qs.host_action("reveal")
        self.assertEqual(qs.players[pid].score, 2000)  # no doubling anywhere

    def test_double_stacks_with_streak_multiplier(self):
        """Double applies AFTER the streak multiplier: int(1000*1.2)*2 = 2400."""
        qs = self._make_qs(n_questions=3, double_indices={2})
        r = qs.register_player("Alice")
        qs.register_player("Dummy")
        pid = r["player_id"]
        qs.host_action("start")

        for _ in range(2):
            self._inject_answer(qs, pid, correct=True)
            qs.host_action("reveal")
            qs.host_action("next")

        self._inject_answer(qs, pid, correct=True)  # streak 3 -> 1.2x, doubled
        qs.host_action("reveal")
        expected = 1000 + 1000 + int(1000 * 1.2) * 2
        self.assertEqual(qs.players[pid].score, expected)

    def test_double_exposed_in_state(self):
        qs = self._make_qs(n_questions=2, double_indices={0})
        r = qs.register_player("Alice")
        qs.register_player("Dummy")
        qs.host_action("start")

        # Question phase: player and host both see the flag
        state = qs.public_state(player_id=r["player_id"])
        self.assertTrue(state["question"]["double"])
        self.assertTrue(qs.public_state(host_view=True)["question"]["double"])

        # Reveal phase keeps the flag
        qs.host_action("reveal")
        self.assertTrue(qs.public_state()["question"]["double"])

        # Second question has no flag
        qs.host_action("next")
        self.assertFalse(qs.public_state()["question"]["double"])

    def test_final_double_exposed_on_last_question(self):
        qs = self._make_qs(n_questions=2)
        qs.set_timing(final_double=True)
        qs.register_player("Alice")
        qs.register_player("Dummy")
        qs.host_action("start")
        self.assertFalse(qs.public_state()["question"]["double"])
        qs.host_action("reveal")
        qs.host_action("next")
        self.assertTrue(qs.public_state()["question"]["double"])

    def test_final_double_in_top_level_state(self):
        qs = self._make_qs()
        self.assertFalse(qs.public_state()["final_double"])
        qs.set_timing(final_double=True)
        self.assertTrue(qs.public_state()["final_double"])


class TestGetReady(unittest.TestCase):
    """Tests for the get-ready countdown and its scoring adjustment."""

    def _sample_questions(self):
        return [
            {"id": "g1", "prompt": "Q1?", "options": ["A", "B", "C", "D"], "correct_index": 0},
        ]

    def _q(self):
        return {"id": "c1", "prompt": "?", "options": ["A", "B", "C", "D"], "correct_index": 0}

    def test_default_value(self):
        qs = server.QuizState(self._sample_questions())
        self.assertEqual(qs.get_ready_sec, 3)

    def test_exposed_in_state(self):
        qs = server.QuizState(self._sample_questions())
        state = qs.public_state()
        self.assertEqual(state["get_ready_sec"], 3)
        qs.set_timing(get_ready_sec=5)
        self.assertEqual(qs.public_state()["get_ready_sec"], 5)

    def test_set_timing_clamped_0_to_10(self):
        qs = server.QuizState(self._sample_questions())
        qs.set_timing(get_ready_sec=99)
        self.assertEqual(qs.get_ready_sec, 10)
        qs.set_timing(get_ready_sec=-4)
        self.assertEqual(qs.get_ready_sec, 0)
        qs.set_timing(get_ready_sec=7)
        self.assertEqual(qs.get_ready_sec, 7)

    def test_set_timing_old_call_keeps_get_ready(self):
        qs = server.QuizState(self._sample_questions())
        qs.set_timing(question_sec=30, reveal_sec=8)
        self.assertEqual(qs.get_ready_sec, 3)

    def test_speed_bonus_uses_effective_elapsed(self):
        """elapsed 5s with get_ready 3 counts as 2s: 600 + 38*10 = 980."""
        started = 1000.0
        answer = {"choice": 0, "time": started + 5.0}
        pts = server.QuizState._calculate_points(self._q(), "choice", answer, started, 3)
        self.assertEqual(pts, 980)

    def test_speed_bonus_without_get_ready(self):
        """Default get_ready_sec=0 keeps the original formula: 600 + 35*10 = 950."""
        started = 1000.0
        answer = {"choice": 0, "time": started + 5.0}
        pts = server.QuizState._calculate_points(self._q(), "choice", answer, started)
        self.assertEqual(pts, 950)

    def test_full_bonus_within_get_ready_window(self):
        """Answer during the countdown (elapsed 2 < 3) gets the full bonus."""
        started = 1000.0
        answer = {"choice": 0, "time": started + 2.0}
        pts = server.QuizState._calculate_points(self._q(), "choice", answer, started, 3)
        self.assertEqual(pts, 1000)

    def test_get_ready_zero_disables_adjustment(self):
        started = 1000.0
        answer = {"choice": 0, "time": started + 5.0}
        pts = server.QuizState._calculate_points(self._q(), "choice", answer, started, 0)
        self.assertEqual(pts, 950)

    def test_scoring_integration_uses_quiz_get_ready(self):
        """_apply_scoring passes the configured get_ready_sec through."""
        qs = server.QuizState(self._sample_questions())
        qs.set_timing(get_ready_sec=5)
        r = qs.register_player("Alice")
        qs.register_player("Dummy")
        pid = r["player_id"]
        qs.host_action("start")
        started = time.time()
        qs.question_started_at = started
        qs.answers = {pid: {"choice": 0, "time": started + 4.0}}  # inside the window
        qs.scored_question_index = None
        qs.host_action("reveal")
        self.assertEqual(qs.players[pid].score, 1000)  # full speed bonus

    def test_answers_accepted_during_get_ready_window(self):
        """Server accepts answers immediately after start (during the countdown)."""
        qs = server.QuizState(self._sample_questions())
        r = qs.register_player("Alice")
        qs.register_player("Dummy")
        qs.host_action("start")
        qs.submit_answer(r["player_id"], r["player_secret"], 0)  # elapsed ~0 < 3
        self.assertIn(r["player_id"], qs.answers)


class TestAwards(unittest.TestCase):
    """Tests for end-of-game awards, using simulated multi-player games."""

    def setUp(self):
        # Finished games auto-save history; keep it out of the repo dir
        self.tmpdir = tempfile.mkdtemp()
        self.orig_history_dir = server.HISTORY_DIR
        server.HISTORY_DIR = Path(self.tmpdir)

    def tearDown(self):
        server.HISTORY_DIR = self.orig_history_dir
        shutil.rmtree(self.tmpdir)

    def _make_qs(self, n_questions):
        questions = [
            {"id": f"a{i}", "prompt": f"Q{i}?", "options": ["A", "B", "C", "D"], "correct_index": 0}
            for i in range(n_questions)
        ]
        qs = server.QuizState(questions)
        qs.set_timing(get_ready_sec=0)  # deterministic elapsed -> points math
        return qs

    def _register(self, qs, names):
        """Register players plus a silent dummy that prevents auto-advance."""
        pids = {name: qs.register_player(name)["player_id"] for name in names}
        qs.register_player("zzz")  # never answers
        return pids

    def _play_question(self, qs, answers):
        """Score the current question with controlled answers and advance.

        answers: {player_id: (correct, elapsed_sec)}
        """
        started = time.time()
        qs.question_started_at = started
        qs.answers = {
            pid: {"choice": 0 if correct else 1, "time": started + elapsed}
            for pid, (correct, elapsed) in answers.items()
        }
        qs.scored_question_index = None
        qs.host_action("reveal")
        qs.host_action("next")

    def _award_map(self, state):
        return {a["key"]: a for a in state.get("awards", [])}

    def test_fastest_finger_needs_3_correct_and_lowest_avg(self):
        qs = self._make_qs(4)
        p = self._register(qs, ["Rychla", "Pomaly", "Kratka"])
        qs.host_action("start")
        # Rychla: 3 correct at 2s (avg 2.0); Pomaly: 4 correct at 10s (streak 4);
        # Kratka: only 2 correct at 0s -> below the 3-correct minimum
        self._play_question(qs, {p["Rychla"]: (True, 2), p["Pomaly"]: (True, 10), p["Kratka"]: (True, 0)})
        self._play_question(qs, {p["Rychla"]: (True, 2), p["Pomaly"]: (True, 10), p["Kratka"]: (True, 0)})
        self._play_question(qs, {p["Rychla"]: (True, 2), p["Pomaly"]: (True, 10), p["Kratka"]: (False, 0)})
        self._play_question(qs, {p["Rychla"]: (False, 2), p["Pomaly"]: (True, 10), p["Kratka"]: (False, 0)})

        self.assertEqual(qs.phase, "finished")
        awards = self._award_map(qs.public_state())
        self.assertIn("fastest_finger", awards)
        self.assertEqual(awards["fastest_finger"]["player"], "Rychla")
        self.assertEqual(awards["fastest_finger"]["value"], "2.0 s")
        # Pomaly has 4-in-a-row -> streak_master
        self.assertEqual(awards["streak_master"]["player"], "Pomaly")
        self.assertEqual(awards["streak_master"]["value"], "série 4")
        # Nobody else qualifies for the remaining awards
        self.assertNotIn("sharpshooter", awards)
        self.assertNotIn("comeback", awards)

    def test_priority_order_and_max_one_award_per_player(self):
        """Adam qualifies for everything but gets only the highest-priority award."""
        qs = self._make_qs(4)
        p = self._register(qs, ["Adam", "Bara", "Dana"])
        qs.host_action("start")
        # All three answer everything correctly, at 0s / 10s / 20s
        for _ in range(4):
            self._play_question(qs, {p["Adam"]: (True, 0), p["Bara"]: (True, 10), p["Dana"]: (True, 20)})

        state = qs.public_state()
        awards = self._award_map(state)
        self.assertEqual(awards["fastest_finger"]["player"], "Adam")
        self.assertEqual(awards["streak_master"]["player"], "Bara")  # tie 4 vs 4, name order
        self.assertEqual(awards["sharpshooter"]["player"], "Dana")
        self.assertEqual(awards["sharpshooter"]["value"], "4/4")
        # Max one award per player
        winners = [a["player"] for a in state["awards"]]
        self.assertEqual(len(winners), len(set(winners)))
        # Priority order preserved in the list itself
        keys = [a["key"] for a in state["awards"]]
        self.assertEqual(keys, ["fastest_finger", "streak_master", "sharpshooter"])

    def test_comeback_rank_improvement(self):
        qs = self._make_qs(6)  # midgame rank recorded at question index 3
        p = self._register(qs, ["Alena", "Beata", "Cyril", "David"])
        qs.host_action("start")
        self._play_question(qs, {p["Alena"]: (True, 10), p["Beata"]: (True, 20), p["Cyril"]: (True, 30)})
        self._play_question(qs, {p["Alena"]: (True, 10), p["Beata"]: (True, 20)})
        self._play_question(qs, {})
        self._play_question(qs, {})  # midgame: Alena 1., Beata 2., Cyril 3., David 4.
        self._play_question(qs, {p["David"]: (True, 0)})
        self._play_question(qs, {p["David"]: (True, 0)})  # David finishes 1st (2000 pts)

        self.assertEqual(qs.players[p["David"]].midgame_rank, 4)
        awards = self._award_map(qs.public_state())
        self.assertEqual(list(awards), ["comeback"])  # nobody qualifies for the rest
        self.assertEqual(awards["comeback"]["player"], "David")
        self.assertEqual(awards["comeback"]["value"], "+3 míst")

    def test_tie_prefers_player_outside_top3(self):
        """streak_master tie (6 vs 6 vs 6) goes to the player outside the top 3."""
        qs = self._make_qs(6)
        p = self._register(qs, ["Alik", "Bara", "Cyril", "Zofie"])
        qs.host_action("start")
        # Everyone 100% correct; speed decides final ranks:
        # Alik 7600 (1.), Bara 6840 (2.), Cyril 6080 (3.), Zofie 5320 (4.)
        for _ in range(6):
            self._play_question(qs, {
                p["Alik"]: (True, 0), p["Bara"]: (True, 10),
                p["Cyril"]: (True, 20), p["Zofie"]: (True, 30),
            })

        awards = self._award_map(qs.public_state())
        self.assertEqual(awards["fastest_finger"]["player"], "Alik")
        # Bara, Cyril and Zofie all have max_streak 6; plain name order would
        # pick Bara, but Zofie is outside the top 3 and takes precedence
        self.assertEqual(awards["streak_master"]["player"], "Zofie")
        self.assertEqual(awards["streak_master"]["value"], "série 6")
        self.assertEqual(awards["sharpshooter"]["player"], "Bara")

    def test_awards_cached_and_reset(self):
        qs = self._make_qs(2)
        p = self._register(qs, ["Hrac"])
        qs.host_action("start")
        self._play_question(qs, {p["Hrac"]: (True, 0)})
        self._play_question(qs, {p["Hrac"]: (True, 0)})

        self.assertEqual(qs.phase, "finished")
        self.assertIsNotNone(qs._awards_cache)
        cache_before = qs._awards_cache
        qs.public_state()
        qs.public_state()
        self.assertIs(qs._awards_cache, cache_before)  # computed once, reused

        qs.host_action("reset")
        self.assertIsNone(qs._awards_cache)
        self.assertNotIn("awards", qs.public_state())
        player = qs.players[p["Hrac"]]
        self.assertEqual(player.answered_count, 0)
        self.assertEqual(player.correct_count, 0)
        self.assertEqual(player.correct_times, [])
        self.assertIsNone(player.midgame_rank)

    def test_awards_shape_and_me_keys(self):
        qs = self._make_qs(2)
        p = self._register(qs, ["Hrac"])
        qs.host_action("start")
        self.assertNotIn("awards", qs.public_state())  # only in finished phase
        self._play_question(qs, {p["Hrac"]: (True, 0)})
        self._play_question(qs, {p["Hrac"]: (True, 0)})

        state = qs.public_state(player_id=p["Hrac"])
        # Hrac answered 2/2 correctly -> sharpshooter (too few correct for fastest)
        self.assertEqual(len(state["awards"]), 1)
        entry = state["awards"][0]
        self.assertEqual(set(entry), {"key", "player", "avatar_id", "value"})  # no player_id leak
        self.assertEqual(entry["key"], "sharpshooter")
        self.assertEqual(entry["value"], "2/2")
        self.assertEqual(state["me"]["awards"], ["sharpshooter"])

        # The silent dummy earned nothing
        dummy_pid = next(pid for pid, pl in qs.players.items() if pl.name == "zzz")
        dummy_state = qs.public_state(player_id=dummy_pid)
        self.assertEqual(dummy_state["me"]["awards"], [])


class TestRankRival(unittest.TestCase):
    """Tests for personal rank and the rival ("behind") info in me."""

    def _make_game(self):
        questions = [
            {"id": "r1", "prompt": "Q?", "options": ["A", "B", "C", "D"], "correct_index": 0},
        ]
        qs = server.QuizState(questions)
        pids = {}
        for name in ("Ada", "Ben", "Cid"):
            pids[name] = qs.register_player(name)["player_id"]
        qs.host_action("start")
        qs.players[pids["Ada"]].score = 3000
        qs.players[pids["Ben"]].score = 2000
        qs.players[pids["Cid"]].score = 1000
        return qs, pids

    def test_rank_in_me(self):
        qs, pids = self._make_game()
        self.assertEqual(qs.public_state(player_id=pids["Ada"])["me"]["rank"], 1)
        self.assertEqual(qs.public_state(player_id=pids["Ben"])["me"]["rank"], 2)
        self.assertEqual(qs.public_state(player_id=pids["Cid"])["me"]["rank"], 3)

    def test_behind_names_player_directly_above(self):
        qs, pids = self._make_game()
        me = qs.public_state(player_id=pids["Cid"])["me"]
        self.assertEqual(me["behind"], {"name": "Ben", "gap": 1000})
        me = qs.public_state(player_id=pids["Ben"])["me"]
        self.assertEqual(me["behind"], {"name": "Ada", "gap": 1000})

    def test_leader_behind_is_null(self):
        qs, pids = self._make_game()
        me = qs.public_state(player_id=pids["Ada"])["me"]
        self.assertIsNone(me["behind"])

    def test_tie_uses_stable_name_order(self):
        qs, pids = self._make_game()
        qs.players[pids["Ben"]].score = 3000  # tie with Ada
        me_ada = qs.public_state(player_id=pids["Ada"])["me"]
        me_ben = qs.public_state(player_id=pids["Ben"])["me"]
        self.assertEqual(me_ada["rank"], 1)
        self.assertEqual(me_ben["rank"], 2)
        self.assertEqual(me_ben["behind"], {"name": "Ada", "gap": 0})

    def test_present_in_reveal_phase(self):
        qs, pids = self._make_game()
        qs.host_action("reveal")
        me = qs.public_state(player_id=pids["Ben"])["me"]
        self.assertEqual(me["rank"], 2)
        self.assertIn("behind", me)


class TestWebSocket(unittest.TestCase):
    """Tests for ws.py WebSocket implementation."""

    def test_accept_key_known_vector(self):
        """Accept key matches the official RFC 6455 section 1.3 example.

        The expected value is a spec literal on purpose: recomputing it from
        our own GUID constant once masked a wrong GUID (browsers rejected
        every handshake while this suite stayed green).
        """
        from ws import ws_accept_key
        self.assertEqual(
            ws_accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )

    def test_accept_key_different_inputs(self):
        """Different keys produce different accept values."""
        from ws import ws_accept_key
        key1 = ws_accept_key("AAAAAAAAAAAAAAAAAAAAAA==")
        key2 = ws_accept_key("BBBBBBBBBBBBBBBBBBBBBB==")
        self.assertNotEqual(key1, key2)

    def test_handshake_response_contains_101(self):
        from ws import ws_handshake_response, ws_accept_key
        client_key = "AAAAAAAAAAAAAAAAAAAAAA=="
        response = ws_handshake_response(client_key)
        self.assertIn(b"101", response)
        self.assertIn(b"Upgrade: websocket", response)
        expected_accept = ws_accept_key(client_key).encode("ascii")
        self.assertIn(expected_accept, response)

    def test_text_frame_encode_decode_roundtrip_short(self):
        """Short message (<126 bytes) encodes and decodes correctly."""
        from ws import ws_encode_text, ws_decode_frame, OPCODE_TEXT
        message = "Hello, ClassRally!"
        encoded = ws_encode_text(message)

        # Server sends unmasked frames — simulate receiving them as-is
        # ws_decode_frame expects client frames (masked), so we build a masked version
        import struct, os
        payload = message.encode("utf-8")
        mask = os.urandom(4)
        masked_payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        frame = bytes([0x80 | OPCODE_TEXT, 0x80 | len(payload)]) + mask + masked_payload

        result = ws_decode_frame(frame)
        self.assertIsNotNone(result)
        opcode, decoded_payload, consumed = result
        self.assertEqual(opcode, OPCODE_TEXT)
        self.assertEqual(decoded_payload.decode("utf-8"), message)
        self.assertEqual(consumed, len(frame))

    def test_text_frame_encode_medium(self):
        """Message of 126-65535 bytes uses 16-bit length prefix."""
        from ws import ws_encode_text
        message = "x" * 200
        encoded = ws_encode_text(message)
        # Byte 1 should be 126 (16-bit length follows)
        self.assertEqual(encoded[1], 126)
        # Bytes 2-3 should be big-endian length 200
        import struct
        length = struct.unpack("!H", encoded[2:4])[0]
        self.assertEqual(length, 200)

    def test_text_frame_server_to_client_unmasked(self):
        """Server frames must be unmasked (MSB of byte 1 is 0)."""
        from ws import ws_encode_text
        encoded = ws_encode_text("test")
        # Byte 1: mask bit is 0 for server->client
        self.assertEqual(encoded[1] & 0x80, 0)

    def test_decode_frame_insufficient_data_returns_none(self):
        """Incomplete data returns None instead of raising."""
        from ws import ws_decode_frame
        result = ws_decode_frame(b"\x81")  # Only 1 byte
        self.assertIsNone(result)

    def test_decode_frame_empty_returns_none(self):
        from ws import ws_decode_frame
        result = ws_decode_frame(b"")
        self.assertIsNone(result)

    def test_ping_frame_opcode(self):
        """Ping frame has opcode 0x9."""
        from ws import ws_encode_ping, OPCODE_PING
        frame = ws_encode_ping(b"hello")
        opcode = frame[0] & 0x0F
        self.assertEqual(opcode, OPCODE_PING)
        # FIN bit should be set
        self.assertEqual(frame[0] & 0x80, 0x80)

    def test_ping_frame_payload(self):
        """Ping frame carries the provided payload."""
        from ws import ws_encode_ping
        data = b"ping_data"
        frame = ws_encode_ping(data)
        # payload starts at byte 2
        self.assertEqual(frame[2:], data)

    def test_close_frame_opcode(self):
        """Close frame has opcode 0x8."""
        from ws import ws_encode_close, OPCODE_CLOSE
        frame = ws_encode_close(1000, "goodbye")
        opcode = frame[0] & 0x0F
        self.assertEqual(opcode, OPCODE_CLOSE)

    def test_close_frame_status_code(self):
        """Close frame encodes the status code in the first 2 payload bytes."""
        from ws import ws_encode_close
        import struct
        frame = ws_encode_close(1001, "going away")
        # payload starts at byte 2; first 2 bytes are the status code
        status = struct.unpack("!H", frame[2:4])[0]
        self.assertEqual(status, 1001)

    def test_close_frame_handling_in_decode(self):
        """A close frame from the client is correctly decoded as OPCODE_CLOSE."""
        from ws import ws_decode_frame, OPCODE_CLOSE
        import os
        # Build a masked close frame (client -> server, code 1000)
        import struct
        payload = struct.pack("!H", 1000)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        frame = bytes([0x80 | OPCODE_CLOSE, 0x80 | len(payload)]) + mask + masked

        result = ws_decode_frame(frame)
        self.assertIsNotNone(result)
        opcode, decoded_payload, _ = result
        self.assertEqual(opcode, OPCODE_CLOSE)
        status = struct.unpack("!H", decoded_payload)[0]
        self.assertEqual(status, 1000)

    def test_ws_connection_manager_register_unregister(self):
        """WSConnectionManager tracks and removes connections correctly."""
        from ws import WSConnectionManager, WSConnection
        import socket

        manager = WSConnectionManager()
        # Use a socket pair for a realistic mock
        s1, s2 = socket.socketpair()
        try:
            conn = WSConnection(s1, conn_id="test-conn-1")
            manager.register(conn)
            self.assertEqual(manager.connection_count, 1)
            manager.unregister("test-conn-1")
            self.assertEqual(manager.connection_count, 0)
        finally:
            s2.close()

    def test_ws_connection_send_text(self):
        """WSConnection.send_text transmits data through socket."""
        from ws import WSConnection, ws_decode_frame, OPCODE_TEXT
        import socket

        s1, s2 = socket.socketpair()
        try:
            conn = WSConnection(s1, conn_id="test-conn-2")
            result = conn.send_text("hello")
            self.assertTrue(result)
            # Read from the other end and decode
            data = s2.recv(4096)
            frame = ws_decode_frame(data)
            # Server sends unmasked frames, decode them directly
            # The frame is unmasked so decode with masked=False simulation:
            # byte1 mask bit will be 0, so ws_decode_frame may not find mask
            # Just verify the raw encoded payload is present
            self.assertIn(b"hello", data)
        finally:
            s1.close()
            s2.close()

    def test_ws_connection_alive_flag(self):
        """WSConnection.alive becomes False after close."""
        from ws import WSConnection
        import socket

        s1, s2 = socket.socketpair()
        try:
            conn = WSConnection(s1, conn_id="test-conn-3")
            self.assertTrue(conn.alive)
            conn.close()
            self.assertFalse(conn.alive)
        finally:
            s2.close()



class TestPasswordHashing(_DBTestBase):
    """Salted PBKDF2 hashes, legacy SHA-256 upgrade, role-based policy."""

    def setUp(self):
        super().setUp()
        import db
        self.db = db

    def _stored_hash(self, user_id):
        conn = self.db.get_conn()
        try:
            return conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()[0]
        finally:
            conn.close()

    def test_new_hash_is_salted_pbkdf2(self):
        a = self.db.create_user("Ann", "secret12", "student")
        b = self.db.create_user("Ben", "secret12", "student")
        ha, hb = self._stored_hash(a["id"]), self._stored_hash(b["id"])
        self.assertTrue(ha.startswith("pbkdf2_sha256$"))
        self.assertNotEqual(ha, hb)  # same password, different salt
        self.assertIsNotNone(self.db.authenticate_user("ann", "secret12"))
        self.assertIsNone(self.db.authenticate_user("ann", "wrong"))

    def test_legacy_sha256_login_upgrades_hash(self):
        import hashlib
        u = self.db.create_user("Old", "legacy1", "student")
        conn = self.db.get_conn()
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (hashlib.sha256(b"legacy1").hexdigest(), u["id"]))
        conn.commit()
        conn.close()
        self.assertIsNone(self.db.authenticate_user("Old", "nope"))
        user = self.db.authenticate_user("Old", "legacy1")
        self.assertEqual(user["id"], u["id"])
        self.assertNotIn("password_hash", user)
        self.assertTrue(self._stored_hash(u["id"]).startswith("pbkdf2_sha256$"))
        self.assertIsNotNone(self.db.authenticate_user("Old", "legacy1"))

    def test_teacher_password_minimum(self):
        with self.assertRaises(ValueError):
            self.db.create_user("Teach", "short12", "teacher")  # 7 chars
        self.db.create_user("Teach", "longer12", "teacher")
        self.db.create_user("Kid", "1234", "student")  # students keep the 4-char minimum

    def test_change_password_with_new_hash(self):
        u = self.db.create_user("Cara", "start123", "student")
        with self.assertRaises(ValueError):
            self.db.change_password(u["id"], "bad", "newpass1")
        self.db.change_password(u["id"], "start123", "newpass1")
        self.assertIsNotNone(self.db.authenticate_user("Cara", "newpass1"))

    def test_admin_set_password_logs_out(self):
        u = self.db.create_user("Lost", "forgotten1", "teacher")
        tok = self.db.create_session(u["id"])
        with self.assertRaises(ValueError):
            self.db.admin_set_password("Lost", "short")
        self.db.admin_set_password("lost", "brandnew99")
        self.assertIsNone(self.db.validate_session(tok))
        self.assertIsNotNone(self.db.authenticate_user("Lost", "brandnew99"))


class TestEmailAndTokens(_DBTestBase):
    """E-mail on accounts, verification, password reset and invitations."""

    def setUp(self):
        super().setUp()
        import db
        self.db = db
        self.user = db.create_user("Dana", "danapass", "teacher", email=" Dana@Example.COM ")

    def test_email_normalized_and_validated(self):
        self.assertEqual(self.user["email"], "dana@example.com")
        self.assertFalse(self.user["email_verified"])
        for bad in ("not-an-email", "a@b", "a b@example.com", "a@example.com,b@example.com",
                    "<a@example.com>", "a\u2028b@example.com", "ě@example.com"):
            with self.assertRaises(ValueError, msg=bad):
                self.db.create_user("Bad", "badpass1", "student", email=bad)

    def test_unconfirmed_address_cannot_be_squatted(self):
        # Someone else entering Dana's address neither fails (no enumeration)
        # nor blocks Dana: only a confirmed address is unique.
        other = self.db.create_user("Squatter", "squat123", "student", email="DANA@example.com")
        self.assertEqual(other["email"], "dana@example.com")
        raw = self.db.create_user_token("verify", user_id=self.user["id"], email="dana@example.com")
        self.assertIsNotNone(self.db.verify_email_with_token(raw))
        late = self.db.create_user_token("verify", user_id=other["id"], email="dana@example.com")
        with self.assertRaises(ValueError):
            self.db.verify_email_with_token(late)  # already confirmed by Dana
        self.assertEqual(self.db.find_user_for_reset("dana@example.com")["id"], self.user["id"])

    def test_nickname_shaped_like_address_does_not_capture_reset(self):
        raw = self.db.create_user_token("verify", user_id=self.user["id"], email="dana@example.com")
        self.db.verify_email_with_token(raw)
        trap = self.db.create_user("dana@example.com", "trap1234", "student", email="trap@example.com")
        raw2 = self.db.create_user_token("verify", user_id=trap["id"], email="trap@example.com")
        self.db.verify_email_with_token(raw2)
        self.assertEqual(self.db.find_user_for_reset("dana@example.com")["id"], self.user["id"])

    def test_invite_for_confirmed_address_rejected(self):
        raw = self.db.create_user_token("verify", user_id=self.user["id"], email="dana@example.com")
        self.db.verify_email_with_token(raw)
        inv = self.db.create_user_token("invite", email="dana@example.com", created_by=self.user["id"])
        with self.assertRaises(ValueError):
            self.db.accept_invite(inv, "Copy", "copypass1")

    def test_set_email_requires_password_and_resets_verification(self):
        with self.assertRaises(ValueError):
            self.db.set_email(self.user["id"], "new@example.com", "wrong")
        r = self.db.set_email(self.user["id"], "new@example.com", "danapass")
        self.assertTrue(r["changed"])
        self.assertEqual(self.db.get_user(self.user["id"])["email"], "new@example.com")

    def test_verify_token_confirms_current_address_only(self):
        raw = self.db.create_user_token("verify", user_id=self.user["id"], email="dana@example.com")
        self.assertIsNotNone(self.db.verify_email_with_token(raw))
        self.assertTrue(self.db.get_user(self.user["id"])["email_verified"])
        self.assertIsNone(self.db.verify_email_with_token(raw))  # one-time
        # a link for an address that was changed afterwards must not verify the new one
        old = self.db.create_user_token("verify", user_id=self.user["id"], email="dana@example.com")
        self.db.set_email(self.user["id"], "other@example.com", "danapass")
        self.assertIsNone(self.db.verify_email_with_token(old))
        self.assertFalse(self.db.get_user(self.user["id"])["email_verified"])

    def test_reset_requires_verified_email(self):
        self.assertIsNone(self.db.find_user_for_reset("Dana"))
        raw = self.db.create_user_token("verify", user_id=self.user["id"], email="dana@example.com")
        self.db.verify_email_with_token(raw)
        self.assertEqual(self.db.find_user_for_reset("dana")["id"], self.user["id"])
        self.assertEqual(self.db.find_user_for_reset("DANA@example.com")["id"], self.user["id"])
        self.assertIsNone(self.db.find_user_for_reset("nobody"))

    def test_reset_with_token(self):
        session = self.db.create_session(self.user["id"])
        raw = self.db.create_user_token("reset", user_id=self.user["id"])
        with self.assertRaises(ValueError):
            self.db.reset_password_with_token(raw, "short")  # teacher minimum 8
        self.assertIsNotNone(self.db.peek_user_token(raw, "reset"))  # not consumed by the error
        user = self.db.reset_password_with_token(raw, "fresh-pass")
        self.assertEqual(user["id"], self.user["id"])
        self.assertIsNone(self.db.validate_session(session))  # logged out everywhere
        self.assertIsNotNone(self.db.authenticate_user("Dana", "fresh-pass"))
        self.assertIsNone(self.db.reset_password_with_token(raw, "another-pass"))  # one-time

    def test_expired_token_rejected(self):
        raw = self.db.create_user_token("reset", user_id=self.user["id"])
        conn = self.db.get_conn()
        conn.execute("UPDATE user_tokens SET expires_at = ?", (time.time() - 1,))
        conn.commit()
        conn.close()
        self.assertIsNone(self.db.reset_password_with_token(raw, "fresh-pass"))

    def test_token_purpose_is_checked(self):
        raw = self.db.create_user_token("verify", user_id=self.user["id"], email="dana@example.com")
        self.assertIsNone(self.db.reset_password_with_token(raw, "fresh-pass"))

    def test_only_token_hash_is_stored(self):
        raw = self.db.create_user_token("reset", user_id=self.user["id"])
        conn = self.db.get_conn()
        stored = [r[0] for r in conn.execute("SELECT token_hash FROM user_tokens")]
        conn.close()
        self.assertNotIn(raw, stored)

    def test_accept_invite_creates_verified_teacher(self):
        raw = self.db.create_user_token("invite", email="new.teacher@example.com", created_by=self.user["id"])
        with self.assertRaises(ValueError):
            self.db.accept_invite(raw, "NewT", "short")  # stays usable
        t = self.db.accept_invite(raw, "NewT", "teacherpass")
        self.assertEqual(t["role"], "teacher")
        self.assertTrue(self.db.get_user(t["id"])["email_verified"])
        self.assertIsNone(self.db.accept_invite(raw, "NewT2", "teacherpass"))


class TestMailer(unittest.TestCase):
    def setUp(self):
        import mailer
        self.mailer = mailer
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_log_mode_writes_private_outbox_file(self):
        m = self.mailer.Mailer(mode="log", outbox_dir=self.tmp, sender="ClassRally <cr@example.com>")
        self.assertEqual(m.send("a@example.com", "Predmet", "Telo"), "log")
        files = list(self.tmp.glob("*.eml"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        self.assertIn(b"To: a@example.com", files[0].read_bytes())

    def test_off_mode_stores_nothing(self):
        m = self.mailer.Mailer(mode="off", outbox_dir=self.tmp)
        self.assertEqual(m.send("a@example.com", "S", "T"), "off")
        self.assertEqual(list(self.tmp.glob("*")), [])

    def test_header_injection_rejected(self):
        m = self.mailer.Mailer(mode="log", outbox_dir=self.tmp)
        with self.assertRaises(ValueError):
            m.send("a@example.com\r\nBcc: x@evil.example", "S", "T")
        with self.assertRaises(ValueError):
            m.send("a@example.com", "S\r\nBcc: x@evil.example", "T")

    def test_live_without_smtp_falls_back_to_log(self):
        m = self.mailer.Mailer(mode="live", outbox_dir=self.tmp)
        self.assertEqual(m.mode, "log")

    def test_all_templates_render_in_all_languages(self):
        values = {"nickname": "Dana", "link": "https://x/reset?token=T", "base": "https://x", "inviter": "Miro"}
        for kind in ("verify", "reset", "password_changed", "invite"):
            for lang in self.mailer.LANGS:
                subject, body = self.mailer.render(kind, lang, **values)
                self.assertIn("ClassRally", subject)
                self.assertNotIn("{", body)
        self.assertEqual(self.mailer.render("reset", "xx", **values)[0],
                         self.mailer.render("reset", "cs", **values)[0])


class _AccountHTTPBase(unittest.TestCase):
    """Isolated DB + log-mode mailer + running server for account API tests."""

    @classmethod
    def setUpClass(cls):
        import db
        import mailer
        cls.db = db
        cls.tmpdir = Path(tempfile.mkdtemp())
        cls.orig_db_path = db._DB_PATH
        db.set_db_path(cls.tmpdir / "recovery.db")
        db.init_db()
        cls.orig_mailer = server.MAILER
        server.MAILER = mailer.Mailer(mode="log", outbox_dir=cls.tmpdir / "outbox",
                                      sender="ClassRally <classrally@example.com>")
        cls.orig_base = server.QUIZ_BASE_URL
        server.QUIZ_BASE_URL = "https://classrally.example.com"
        server.ADMIN_AUTH.set_password(None)
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base_url = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        server.MAILER = cls.orig_mailer
        server.QUIZ_BASE_URL = cls.orig_base
        cls.db.set_db_path(cls.orig_db_path)
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        for lim in (server.LOGIN_FAIL_IP, server.LOGIN_FAIL_NICK, server.LOGIN_FAIL_NICK_IP,
                    server.PASSWORD_FAIL_USER):
            lim._failures.clear()
        for lim in (server.ACCOUNT_REGISTER_LIMITER, server.FORGOT_LIMITER_IP, server.MAIL_LIMITER_USER,
                    server.INVITE_LIMITER, server.SET_EMAIL_LIMITER, server.VERIFY_MAIL_IP,
                    server.GLOBAL_MAIL_LIMITER):
            lim._attempts.clear()

    def _wait_mails(self, count, timeout=3.0):
        """Forgotten-password mail is sent in the background."""
        deadline = time.time() + timeout
        while len(self._mails()) < count and time.time() < deadline:
            time.sleep(0.05)
        return self._mails()

    def _post(self, path, data, token=None):
        req = Request(f"{self.base_url}{path}", data=json.dumps(data).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
        if token:
            req.add_header("Authorization", f"UserToken {token}")
        try:
            with urlopen(req) as r:
                return r.status, json.loads(r.read())
        except HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _get(self, path, token=None):
        req = Request(f"{self.base_url}{path}")
        if token:
            req.add_header("Authorization", f"UserToken {token}")
        with urlopen(req) as r:
            return r.status, r.read()

    def _mails(self):
        import email
        out = []
        for f in sorted((self.tmpdir / "outbox").glob("*.eml")):
            msg = email.message_from_bytes(f.read_bytes(), policy=__import__("email.policy").policy.default)
            out.append(msg)
        return out

    def _link_token(self, msg, path):
        import re
        body = msg.get_content()
        m = re.search(re.escape(f"https://classrally.example.com{path}?token=") + r"([A-Za-z0-9_\-]+)", body)
        self.assertIsNotNone(m, body)
        return m.group(1)



class TestAccountRecoveryHTTP(_AccountHTTPBase):
    """End-to-end over HTTP: register with e-mail, verify, forgot/reset, invites."""

    def test_full_recovery_flow(self):
        before = len(self._mails())
        code, reg = self._post("/api/auth/register", {"nickname": "Rita", "password": "ritapass1",
                                                     "role": "student", "email": "rita@example.com", "lang": "en"})
        self.assertEqual(code, 200, reg)
        self.assertEqual(reg["mail"], "log")
        verify_mail = self._mails()[before]
        self.assertEqual(verify_mail["To"], "rita@example.com")
        self.assertIn("confirm", verify_mail["Subject"].lower())

        # forgot before verification: generic answer, no mail
        code, r = self._post("/api/auth/forgot-password", {"login": "Rita"})
        self.assertEqual((code, r), (200, {"ok": True}))
        time.sleep(0.3)
        self.assertEqual(len(self._mails()), before + 1)

        code, r = self._post("/api/auth/verify-email", {"token": self._link_token(verify_mail, "/verify-email")})
        self.assertEqual(code, 200, r)

        code, r = self._post("/api/auth/forgot-password", {"login": "rita@example.com", "lang": "cs"})
        self.assertEqual((code, r), (200, {"ok": True}))
        reset_mail = self._wait_mails(before + 2)[-1]
        self.assertEqual(reset_mail["Subject"], "ClassRally: obnova hesla")
        token = self._link_token(reset_mail, "/reset")

        code, r = self._post("/api/auth/token-check", {"token": token, "purpose": "reset"})
        self.assertTrue(r["valid"])
        code, r = self._post("/api/auth/reset-password", {"token": token, "new_password": "newrita99"})
        self.assertEqual(code, 200, r)
        self.assertIn("heslo", self._mails()[-1]["Subject"].lower())  # change notification
        code, r = self._post("/api/auth/reset-password", {"token": token, "new_password": "again1234"})
        self.assertEqual((code, r.get("code")), (400, "invalid_token"))
        code, r = self._post("/api/auth/login", {"nickname": "Rita", "password": "newrita99"})
        self.assertEqual(code, 200)

    def test_forgot_password_does_not_reveal_accounts(self):
        before = len(self._mails())
        code, r = self._post("/api/auth/forgot-password", {"login": "does-not-exist"})
        self.assertEqual((code, r), (200, {"ok": True}))
        time.sleep(0.3)
        self.assertEqual(len(self._mails()), before)

    def test_login_lockout_per_nickname(self):
        self._post("/api/auth/register", {"nickname": "Lock", "password": "lockpass1", "role": "student"})
        for _ in range(server.LOGIN_FAIL_NICK_IP.max_failures):
            code, _ = self._post("/api/auth/login", {"nickname": "Lock", "password": "wrong"})
            self.assertEqual(code, 401)
        code, r = self._post("/api/auth/login", {"nickname": "lock", "password": "lockpass1"})
        self.assertEqual((code, r.get("code")), (429, "rate_limited"))
        code, _ = self._post("/api/auth/login", {"nickname": "Other", "password": "x"})
        self.assertEqual(code, 401)  # other accounts are not locked

    def test_invite_teacher_flow(self):
        code, t = self._post("/api/auth/register", {"nickname": "Boss", "password": "bosspass1", "role": "student"})
        code, r = self._post("/api/auth/invite-teacher", {"email": "kolega@example.com"}, token=t["token"])
        self.assertEqual(code, 401)  # students cannot invite
        conn = self.db.get_conn()
        conn.execute("UPDATE users SET role = 'teacher' WHERE nickname = 'Boss'")
        conn.commit()
        conn.close()
        code, r = self._post("/api/auth/invite-teacher", {"email": "kolega@example.com", "lang": "cs"}, token=t["token"])
        self.assertEqual(code, 200, r)
        invite = self._mails()[-1]
        self.assertEqual(invite["To"], "kolega@example.com")
        self.assertIn("Boss", invite.get_content())
        token = self._link_token(invite, "/invite")
        code, r = self._post("/api/auth/token-check", {"token": token, "purpose": "invite"})
        self.assertEqual(r, {"valid": True, "email": "kolega@example.com"})
        code, r = self._post("/api/auth/accept-invite", {"token": token, "nickname": "Kolega", "password": "kolega123"})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["user"]["role"], "teacher")
        code, r = self._post("/api/auth/accept-invite", {"token": token, "nickname": "Kolega2", "password": "kolega123"})
        self.assertEqual((code, r.get("code")), (400, "invalid_token"))

    def test_account_pages_served(self):
        for path in ("/reset", "/verify-email", "/invite"):
            code, body = self._get(path)
            self.assertEqual(code, 200)
            self.assertIn(b"account.js", body)


class TestAIConfigHTTP(_AccountHTTPBase):
    """The AI API key never reaches the browser; a locked config ignores client URLs."""

    def setUp(self):
        super().setUp()
        self.saved = (dict(server.AI_CONFIG), server.AI_CONFIG_LOCKED)
        server.AI_CONFIG.update({"base_url": "http://ai.internal:9", "api_key": "sk-secret", "model": "m1"})

    def tearDown(self):
        server.AI_CONFIG.clear()
        server.AI_CONFIG.update(self.saved[0])
        server.AI_CONFIG_LOCKED = self.saved[1]

    def test_config_hides_key(self):
        code, body = self._get("/api/admin/ollama/config")
        cfg = json.loads(body)
        self.assertNotIn("sk-secret", body.decode())
        self.assertTrue(cfg["api_key_set"])

    def test_locked_config_keeps_endpoint_and_key(self):
        server.AI_CONFIG_LOCKED = True
        code, r = self._post("/api/admin/ollama/config", {"base_url": "http://169.254.169.254", "api_key": "x", "model": "m2"})
        self.assertEqual(code, 200)
        self.assertEqual(server.AI_CONFIG["base_url"], "http://ai.internal:9")
        self.assertEqual(server.AI_CONFIG["api_key"], "sk-secret")
        self.assertEqual(server.AI_CONFIG["model"], "m2")

    def test_locked_generate_ignores_client_url(self):
        server.AI_CONFIG_LOCKED = True
        seen = {}
        orig = server.generate_questions_ai

        def fake(**kw):
            seen.update(kw)
            return [{"id": "ai_1", "prompt": "Q?", "options": ["a", "b", "c", "d"], "correct_index": 0}]
        server.generate_questions_ai = fake
        try:
            code, r = self._post("/api/admin/ai/generate", {"topic": "Linux", "base_url": "http://evil.example",
                                                             "api_key": "stolen", "source_text": "Text."})
        finally:
            server.generate_questions_ai = orig
        self.assertEqual(code, 200, r)
        self.assertEqual(seen["base_url"], "http://ai.internal:9")
        self.assertEqual(seen["api_key"], "sk-secret")
        self.assertEqual(seen["source_text"], "Text.")

    def test_unlocked_empty_key_keeps_stored_key(self):
        code, r = self._post("/api/admin/ollama/config", {"base_url": "http://other:1", "api_key": "", "model": "m3"})
        self.assertEqual(server.AI_CONFIG["api_key"], "sk-secret")
        self.assertEqual(server.AI_CONFIG["base_url"], "http://other:1")


class TestRateLimiters(unittest.TestCase):
    def test_failure_limiter_counts_only_failures(self):
        lim = server._FailureLimiter(max_failures=2, window_sec=60)
        self.assertFalse(lim.blocked("k"))
        lim.fail("k")
        self.assertFalse(lim.blocked("k"))
        lim.fail("k")
        self.assertTrue(lim.blocked("k"))
        self.assertFalse(lim.blocked("other"))

    def test_class_behind_nat_can_join(self):
        # 35 pupils joining from one school IP within a minute must not be blocked
        lim = server._RegistrationRateLimiter(max_requests=server.REGISTER_LIMITER.max_requests,
                                              window_sec=server.REGISTER_LIMITER.window_sec)
        self.assertTrue(all(lim.is_allowed("10.0.0.1") for _ in range(35)))



class TestI18nCatalogs(unittest.TestCase):
    """All languages carry the same keys and every key used by the UI exists."""

    LANGS = ("cs", "en", "sk", "de", "uk")

    def _flat(self, lang):
        data = json.loads((Path(server.STATIC_DIR) / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))
        out = {}
        for k, v in data.items():
            if isinstance(v, dict):
                out.update({f"{k}.{kk}": vv for kk, vv in v.items()})
            else:
                out[k] = v
        return out

    def test_key_parity(self):
        base = set(self._flat("cs"))
        for lang in self.LANGS[1:]:
            keys = set(self._flat(lang))
            self.assertEqual(sorted(base - keys), [], f"missing in {lang}")
            self.assertEqual(sorted(keys - base), [], f"extra in {lang}")

    def test_placeholders_match(self):
        import re
        cs = self._flat("cs")
        for lang in self.LANGS[1:]:
            other = self._flat(lang)
            for key, text in cs.items():
                if isinstance(text, str):
                    self.assertEqual(sorted(re.findall(r"\{\d+\}", text)),
                                     sorted(re.findall(r"\{\d+\}", other[key])), f"{lang}:{key}")

    def test_used_keys_exist(self):
        import re
        known = set(self._flat("cs"))
        used = set()
        for f in Path(server.STATIC_DIR).glob("*.*"):
            if f.suffix not in (".html", ".js"):
                continue
            text = f.read_text(encoding="utf-8")
            used |= set(re.findall(r'data-i18n(?:-[a-z]+)?="([a-z_]+\.[a-z0-9_]+)"', text))
            used |= set(re.findall(r"i18n\.t\(\s*'([a-z_]+\.[a-z0-9_]+)'", text))
        self.assertEqual(sorted(used - known), [])



class TestAudioTracks(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.orig = server.AUDIO_DIR
        server.AUDIO_DIR = self.tmp

    def tearDown(self):
        server.AUDIO_DIR = self.orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_roles_by_name_prefix(self):
        cases = {"loop_quirky_dog.mp3": "loop", "tension-loop.mp3": "loop", "reveal-stinger.mp3": "reveal",
                 "correct_ding.ogg": "correct", "wrong_hit.ogg": "wrong", "win_jingle.ogg": "win",
                 "end_gong.ogg": "end", "lock_click.ogg": "lock",
                 # substrings must not decide any more ("legend" contains "end", "white" contains "hit")
                 "legend.mp3": "loop", "white-noise.mp3": "loop"}
        for name, role in cases.items():
            self.assertEqual(server._audio_role(name), role, name)

    def test_catalog_effects_and_credits(self):
        for n in ("loop_quirky_dog.mp3", "correct_ding.ogg", "notes.txt"):
            (self.tmp / n).write_bytes(b"x")
        cat = server.list_audio_tracks()
        self.assertEqual([t["name"] for t in cat["loops"]], ["loop_quirky_dog.mp3"])
        self.assertEqual(cat["effects"], {"correct": ["/static/audio/correct_ding.ogg"]})
        self.assertEqual(cat["credits"], ["Kevin MacLeod (incompetech.com), CC BY 4.0"])

    def test_no_credit_for_own_music(self):
        (self.tmp / "my_song.mp3").write_bytes(b"x")
        self.assertEqual(server.list_audio_tracks()["credits"], [])

    def test_gain_evens_out_loudness(self):
        for n in ("loud_loop.mp3", "quiet_loop.mp3", "target.mp3", "unmeasured.mp3", "broken.mp3"):
            (self.tmp / n).write_bytes(b"x")
        (self.tmp / "levels.json").write_text(json.dumps({
            "loud_loop.mp3": {"lufs": -8.0}, "quiet_loop.mp3": {"lufs": -40.0},
            "target.mp3": {"lufs": server.AUDIO_TARGET_LUFS}, "broken.mp3": {"lufs": "loud"}}))
        gains = {t["name"]: t["gain"] for t in server.list_audio_tracks()["all"]}
        self.assertAlmostEqual(gains["loud_loop.mp3"], 0.398, places=3)  # 8 dB down
        self.assertEqual(gains["quiet_loop.mp3"], 2.0)                   # boost capped
        self.assertEqual(gains["target.mp3"], 1.0)
        self.assertEqual(gains["unmeasured.mp3"], 1.0)
        self.assertEqual(gains["broken.mp3"], 1.0)
        self.assertNotIn("levels.json", gains)  # not a track
        (self.tmp / "levels.json").write_text("{not json")
        self.assertEqual(server.list_audio_tracks()["all"][0]["gain"], 1.0)



class TestAIGenerationParsing(unittest.TestCase):
    """Response parsing: reasoning blocks ignored, answers shuffled, source text sent."""

    def _run(self, content, **kw):
        sent = {}

        class FakeResp:
            def __init__(self, body): self.body = body
            def read(self): return self.body
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            sent["payload"] = json.loads(req.data.decode())
            sent["url"] = req.full_url
            body = {"choices": [{"message": {"content": content}}]}
            return FakeResp(json.dumps(body).encode())

        orig = server.urlopen
        server.urlopen = fake_urlopen
        try:
            return server.generate_questions_ai(topic="Linux", base_url="https://ai.example/", **kw), sent
        finally:
            server.urlopen = orig

    def test_think_block_ignored_and_answers_shuffled(self):
        qs = [{"id": f"ai_{i}", "prompt": f"Q{i}?", "options": ["right", "w1", "w2", "w3"],
               "correct_index": 0} for i in range(30)]
        content = "<think>[draft] maybe [x]</think>\n" + json.dumps(qs)
        out, sent = self._run(content, count=30)
        self.assertEqual(sent["url"], "https://ai.example/v1/chat/completions")
        self.assertEqual(len(out), 30)
        for q in out:
            self.assertEqual(q["options"][q["correct_index"]], "right")
            self.assertEqual(sorted(q["options"]), ["right", "w1", "w2", "w3"])
        # with 30 questions the right answer cannot always stay first
        self.assertTrue(any(q["correct_index"] != 0 for q in out))

    def test_source_text_and_language_in_prompt(self):
        content = json.dumps([{"id": "a", "prompt": "Q?", "options": ["a", "b", "c", "d"], "correct_index": 2}])
        _, sent = self._run(content, language="uk", source_text="Proces je běžící program.")
        prompt = sent["payload"]["messages"][1]["content"]
        self.assertIn("Ukrainian", prompt)
        self.assertIn("Proces je běžící program.", prompt)



class TestAccountHardening(_DBTestBase):
    def setUp(self):
        super().setUp()
        import db
        self.db = db
        self.t1 = db.create_user("TeachA", "teacher-a1", "teacher")
        self.t2 = db.create_user("TeachB", "teacher-b1", "teacher")
        self.kid = db.create_user("Kid", "kid1", "student")

    def test_sessions_stored_as_hash(self):
        tok = self.db.create_session(self.kid["id"])
        conn = self.db.get_conn()
        stored = [r[0] for r in conn.execute("SELECT token FROM sessions")]
        conn.close()
        self.assertNotIn(tok, stored)
        self.assertEqual(self.db.validate_session(tok), self.kid["id"])
        self.db.delete_session(tok)
        self.assertIsNone(self.db.validate_session(tok))

    def test_teacher_cannot_take_over_teacher(self):
        for call in (lambda: self.db.teacher_reset_password(self.t2["id"], "newpass99", self.t1["id"]),
                     lambda: self.db.delete_user_by_teacher(self.t2["id"], self.t1["id"]),
                     lambda: self.db.set_user_role(self.t2["id"], "student", self.t1["id"])):
            with self.assertRaises(ValueError):
                call()
        self.db.teacher_reset_password(self.kid["id"], "kid2", self.t1["id"])  # students still fine
        self.db.set_user_role(self.kid["id"], "teacher", self.t1["id"])        # promotion still fine

    def test_change_password_ends_other_sessions_and_reset_links(self):
        keep = self.db.create_session(self.t1["id"])
        other = self.db.create_session(self.t1["id"])
        link = self.db.create_user_token("reset", user_id=self.t1["id"])
        with self.assertRaises(self.db.WrongPassword):
            self.db.change_password(self.t1["id"], "bad", "teacher-a2", keep_session=keep)
        user = self.db.change_password(self.t1["id"], "teacher-a1", "teacher-a2", keep_session=keep)
        self.assertEqual(user["nickname"], "TeachA")
        self.assertEqual(self.db.validate_session(keep), self.t1["id"])
        self.assertIsNone(self.db.validate_session(other))
        self.assertIsNone(self.db.peek_user_token(link, "reset"))

    def test_unknown_login_costs_a_hash_check(self):
        calls = []
        orig = self.db._verify_password
        self.db._verify_password = lambda pw, stored: calls.append(stored) or orig(pw, stored)
        try:
            self.assertIsNone(self.db.authenticate_user("nobody-here", "whatever"))
        finally:
            self.db._verify_password = orig
        self.assertEqual(len(calls), 1)


class TestMailerHardening(unittest.TestCase):
    def test_unknown_security_falls_back_to_starttls(self):
        import mailer
        m = mailer.Mailer(mode="log", security="tls")
        self.assertEqual(m.security, "starttls")
        self.assertEqual(mailer.Mailer(mode="log", security="ssl").security, "ssl")

    def test_old_outbox_files_pruned(self):
        import mailer
        tmp = Path(tempfile.mkdtemp())
        try:
            old = tmp / "20200101T000000000000-aaaa.eml"
            old.write_bytes(b"x")
            os.utime(old, (time.time() - 30 * 86400,) * 2)
            mailer.Mailer(mode="log", outbox_dir=tmp).send("a@example.com", "S", "T")
            self.assertFalse(old.exists())
            self.assertEqual(len(list(tmp.glob("*.eml"))), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestAccountHardeningHTTP(_AccountHTTPBase):
    def test_set_email_wrong_password_is_limited(self):
        code, r = self._post("/api/auth/register", {"nickname": "Guess", "password": "guess123", "role": "student"})
        tok = r["token"]
        for _ in range(server.PASSWORD_FAIL_USER.max_failures):
            code, _ = self._post("/api/auth/set-email", {"email": "g@example.com", "password": "wrong"}, token=tok)
            self.assertEqual(code, 400)
        code, r = self._post("/api/auth/set-email", {"email": "g@example.com", "password": "guess123"}, token=tok)
        self.assertEqual((code, r.get("code")), (429, "rate_limited"))

    def test_change_password_notifies_confirmed_address(self):
        code, r = self._post("/api/auth/register", {"nickname": "Noti", "password": "noti1234", "role": "student",
                                                     "email": "noti@example.com"})
        tok = r["token"]
        verify = self._wait_mails(1)[-1]
        self._post("/api/auth/verify-email", {"token": self._link_token(verify, "/verify-email")})
        before = len(self._mails())
        code, r = self._post("/api/auth/change-password", {"old_password": "noti1234", "new_password": "noti5678"},
                             token=tok)
        self.assertEqual(code, 200, r)
        mails = self._wait_mails(before + 1)
        self.assertEqual(mails[-1]["To"], "noti@example.com")
        code, _ = self._get("/api/auth/profile", token=tok)  # current session stays valid
        self.assertEqual(code, 200)



class TestResourceLimits(_AccountHTTPBase):
    def test_long_nickname_login_rejected_without_limiter_key(self):
        code, _ = self._post("/api/auth/login", {"nickname": "x" * 5000, "password": "p"})
        self.assertEqual(code, 401)
        self.assertFalse(any(len(k) > 100 for k in server.LOGIN_FAIL_NICK._failures))
        self.assertFalse(any(len(k) > 100 for k in server.LOGIN_FAIL_NICK_IP._failures))

    def test_oversized_body_rejected(self):
        import socket
        host, port = "127.0.0.1", self.httpd.server_address[1]
        sock = socket.create_connection((host, port), timeout=5)
        sock.sendall((f"POST /api/auth/login HTTP/1.0\r\nHost: x\r\nContent-Type: application/json\r\n"
                      f"Content-Length: {server.MAX_BODY_BYTES + 1}\r\n\r\n{{}}").encode())
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
        sock.close()
        self.assertIn(b" 413 ", data.split(b"\r\n", 1)[0])

    def test_limiters_forget_stale_keys(self):
        lim = server._FailureLimiter(max_failures=3, window_sec=60)
        old = time.time() - 120
        lim._failures.update({f"k{i}": [old] for i in range(server._SWEEP_EVERY + 5)})
        lim.fail("fresh")
        self.assertEqual(list(lim._failures), ["fresh"])
        reg = server._RegistrationRateLimiter(max_requests=3, window_sec=60)
        reg._attempts.update({f"k{i}": [old] for i in range(server._SWEEP_EVERY + 5)})
        reg.is_allowed("fresh")
        self.assertEqual(list(reg._attempts), ["fresh"])


class TestEmailDotsAndConsole(_DBTestBase):
    def test_dot_rules(self):
        import db
        for bad in (".a@example.com", "a.@example.com", "a..b@example.com", ("a" * 65) + "@example.com"):
            with self.assertRaises(ValueError, msg=bad):
                db.normalize_email(bad)
        self.assertEqual(db.normalize_email("Jan.Novak+kviz@Example.com"), "jan.novak+kviz@example.com")

    def test_admin_set_role(self):
        import db
        t = db.create_user("Former", "former-t1", "teacher")
        tok = db.create_session(t["id"])
        self.assertEqual(db.admin_set_role("former", "student")["role"], "student")
        self.assertEqual(db.get_user(t["id"])["role"], "student")
        self.assertIsNone(db.validate_session(tok))
        with self.assertRaises(ValueError):
            db.admin_set_role("nobody", "student")



class TestRoomsMulti(_AccountHTTPBase):
    """QUIZ_ROOMS=multi: every teacher has an own room (game, exam, host token, PIN)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.orig_mode = server.ROOM_MODE
        server.ROOM_MODE = "multi"
        tmpq = cls.tmpdir / "questions"
        tmpq.mkdir()
        cls.orig_qdir = server.QUESTIONS_DIR
        server.QUESTIONS_DIR = tmpq
        server.save_questions_to_file("bank_a.json", [
            {"id": "a1", "prompt": "A?", "options": ["x", "y"], "correct_index": 0}])
        server.save_questions_to_file("bank_b.json", [
            {"id": "b1", "prompt": "B?", "options": ["x", "y"], "correct_index": 1},
            {"id": "b2", "prompt": "B2?", "options": ["x", "y"], "correct_index": 0}])
        for nick in ("TeachA", "TeachB"):
            cls.db.create_user(nick, f"{nick.lower()}-pass", "teacher")

    @classmethod
    def tearDownClass(cls):
        server.ROOM_MODE = cls.orig_mode
        server.QUESTIONS_DIR = cls.orig_qdir
        super().tearDownClass()

    def _login(self, nick):
        code, r = self._post("/api/auth/login", {"nickname": nick, "password": f"{nick.lower()}-pass"})
        self.assertEqual(code, 200, r)
        return r["token"]

    def _get_json(self, path, headers=None):
        req = Request(f"{self.base_url}{path}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urlopen(req) as r:
                return r.status, json.loads(r.read())
        except HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _host(self, user_token):
        code, r = self._get_json("/api/host/token", {"Authorization": f"UserToken {user_token}"})
        self.assertEqual(code, 200, r)
        return r

    def test_teachers_get_separate_rooms(self):
        ta, tb = self._login("TeachA"), self._login("TeachB")
        ha, hb = self._host(ta), self._host(tb)
        self.assertEqual(ha["rooms"], "multi")
        self.assertNotEqual(ha["pin"], hb["pin"])
        self.assertNotEqual(ha["host_token"], hb["host_token"])
        self.assertEqual(self._host(ta)["pin"], ha["pin"])  # stable per teacher

        # each teacher loads a bank into their own room
        self.assertEqual(self._post("/api/admin/bank/activate", {"filename": "bank_a.json"}, token=ta)[0], 200)
        self.assertEqual(self._post("/api/admin/bank/activate", {"filename": "bank_b.json"}, token=tb)[0], 200)
        sa = self._get_json("/api/state?host=1", {"X-Host-Token": ha["host_token"]})[1]
        sb = self._get_json("/api/state?host=1", {"X-Host-Token": hb["host_token"]})[1]
        self.assertEqual((sa["total_questions"], sb["total_questions"]), (1, 2))

        # students: PIN required, then they land in that room only
        code, r = self._post("/api/register", {"name": "NoPin"})
        self.assertEqual((code, r.get("code")), (400, "pin_required"))
        code, r = self._post("/api/register", {"name": "Bad", "pin": "000001"})
        self.assertEqual((code, r.get("code")), (400, "pin_unknown"))
        code, p1 = self._post("/api/register", {"name": "Eva", "pin": ha["pin"]})
        self.assertEqual(code, 200, p1)
        self.assertEqual(p1["pin"], ha["pin"])
        sa = self._get_json("/api/state?host=1", {"X-Host-Token": ha["host_token"]})[1]
        sb = self._get_json("/api/state?host=1", {"X-Host-Token": hb["host_token"]})[1]
        self.assertEqual((sa["total_players"], sb["total_players"]), (1, 0))
        me = self._get_json(f"/api/state?player_id={p1['player_id']}")[1]
        self.assertEqual(me["total_questions"], 1)  # found via the player's room

        # host actions only touch the own room; a token never controls another room
        req = Request(f"{self.base_url}/api/host/action", data=json.dumps({"action": "start"}).encode(),
                      headers={"Content-Type": "application/json", "X-Host-Token": ha["host_token"]}, method="POST")
        with urlopen(req) as r:
            self.assertEqual(r.status, 200)
        sa = self._get_json("/api/state?host=1", {"X-Host-Token": ha["host_token"]})[1]
        sb = self._get_json("/api/state?host=1", {"X-Host-Token": hb["host_token"]})[1]
        self.assertNotEqual(sa["phase"], "lobby")
        self.assertEqual(sb["phase"], "lobby")
        self.assertFalse("player_id" in str(self._get_json(f"/api/state?pin={hb['pin']}")[1].get("players", [])))

    def test_exam_in_one_room_does_not_switch_the_other(self):
        ta, tb = self._login("TeachA"), self._login("TeachB")
        ha, hb = self._host(ta), self._host(tb)
        self.assertEqual(self._post("/api/admin/bank/activate", {"filename": "bank_a.json", "mode": "exam"},
                                    token=ta)[0], 200)
        self._post("/api/admin/bank/activate", {"filename": "bank_b.json"}, token=tb)
        self.assertEqual(self._get_json(f"/api/mode?pin={ha['pin']}")[1]["mode"], "exam")
        self.assertEqual(self._get_json(f"/api/mode?pin={hb['pin']}")[1]["mode"], "game")
        self.assertTrue(self._get_json("/api/mode")[1]["pin_required"])
        code, st = self._post("/api/register", {"name": "Jan", "pin": ha["pin"]})
        self.assertEqual(st["mode"], "exam")
        # a student's exam view needs the student's secret
        view = self._get_json(f"/api/exam/state?player_id={st['player_id']}")[1]
        self.assertFalse(view["known"])
        view = self._get_json(f"/api/exam/state?player_id={st['player_id']}&secret={st['player_secret']}")[1]
        self.assertTrue(view["known"])

    def test_new_pin_sends_old_players_to_join_again(self):
        self.db.create_user("TeachNew", "teachnew-pass", "teacher")  # own room, game mode
        ta = self._login("TeachNew")
        ha = self._host(ta)
        code, p1 = self._post("/api/register", {"name": "Lojza", "pin": ha["pin"]})
        self.assertEqual(code, 200, p1)
        req = Request(f"{self.base_url}/api/host/new-pin", data=b"{}", method="POST",
                      headers={"Content-Type": "application/json", "X-Host-Token": ha["host_token"]})
        with urlopen(req) as r:
            renewed = json.loads(r.read())
        self.assertNotEqual(renewed["pin"], ha["pin"])
        self.assertEqual(self._host(ta)["pin"], renewed["pin"])
        code, r = self._get_json(f"/api/state?player_id={p1['player_id']}")
        self.assertEqual((code, r.get("code")), (404, "player_unknown"))
        self.assertTrue(self._get_json(f"/api/mode?pin={ha['pin']}")[1].get("pin_unknown"))
        self.assertEqual(self._post("/api/register", {"name": "Lojza", "pin": renewed["pin"]})[0], 200)
        state = self._get_json("/api/state?host=1", {"X-Host-Token": ha["host_token"]})[1]
        self.assertEqual(state["total_players"], 1)

    def test_kicked_player_is_told_and_may_join_again(self):
        self.db.create_user("TeachKick", "teachkick-pass", "teacher")
        ha = self._host(self._login("TeachKick"))
        hdr = {"Content-Type": "application/json", "X-Host-Token": ha["host_token"]}
        _, p1 = self._post("/api/register", {"name": "Rusil", "pin": ha["pin"]})
        _, p2 = self._post("/api/register", {"name": "Hodny", "pin": ha["pin"]})
        # without the host token nobody can kick
        req = Request(f"{self.base_url}/api/host/kick", method="POST",
                      data=json.dumps({"player_id": p1["player_id"]}).encode(),
                      headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as ctx:
            urlopen(req)
        self.assertEqual(ctx.exception.code, 403)
        req = Request(f"{self.base_url}/api/host/kick", method="POST",
                      data=json.dumps({"player_id": p1["player_id"]}).encode(), headers=hdr)
        with urlopen(req) as r:
            self.assertTrue(json.loads(r.read())["ok"])
        code, r = self._get_json(f"/api/state?player_id={p1['player_id']}")
        self.assertEqual((code, r.get("code")), (404, "player_kicked"))
        # the other player plays on; the kicked one can join again as a new player
        self.assertIn("me", self._get_json(f"/api/state?player_id={p2['player_id']}")[1])
        code, again = self._post("/api/register", {"name": "Rusil", "pin": ha["pin"]})
        self.assertEqual(code, 200, again)
        self.assertNotEqual(again["player_id"], p1["player_id"])
        state = self._get_json("/api/state?host=1", {"X-Host-Token": ha["host_token"]})[1]
        self.assertEqual(state["total_players"], 2)

    def test_kicked_players_memory_is_bounded(self):
        kicked = server._KickedPlayers(limit=3)
        for i in range(5):
            kicked.add(f"p{i}")
        self.assertNotIn("p0", kicked)
        self.assertNotIn("p1", kicked)
        self.assertIn("p4", kicked)

    def test_pin_guessing_is_limited_per_address(self):
        orig = server.PIN_FAIL_IP
        server.PIN_FAIL_IP = server._GuessLimiter(max_guesses=3, window_sec=600)
        try:
            ha = self._host(self._login("TeachB"))
            for _ in range(10):  # every device of a school with the same stale PIN counts once
                self.assertTrue(self._get_json("/api/mode?pin=000002")[1].get("pin_unknown"))
            self.assertEqual(self._get_json(f"/api/mode?pin=%20%20{ha['pin']}")[1].get("pin_required"), False)
            # padding does not make different guesses look alike
            for guess in ("%20%20%20%20%20%20%20%20%20%20%20%20%20%20%20%20000003", "++++++++++++++++000004"):
                self.assertTrue(self._get_json(f"/api/mode?pin={guess}")[1].get("pin_unknown"))
            code, r = self._get_json(f"/api/mode?pin={ha['pin']}")
            self.assertEqual((code, r.get("code")), (429, "pin_guesses"))  # even the right PIN now
            code, r = self._post("/api/register", {"name": "Hadac", "pin": ha["pin"]})
            self.assertEqual((code, r.get("code")), (429, "pin_guesses"))
            self.assertEqual(self._get_json(f"/api/state?pin={ha['pin']}")[0], 429)
        finally:
            server.PIN_FAIL_IP = orig

    def test_teacher_without_the_role_loses_the_room(self):
        self.db.create_user("TeachGone", "teachgone-pass", "teacher")
        h = self._host(self._login("TeachGone"))
        self.db.admin_set_role("TeachGone", "student")  # server console
        orig = server.ROOM_OWNER_RECHECK_SEC
        server.ROOM_OWNER_RECHECK_SEC = 0
        try:
            req = Request(f"{self.base_url}/api/host/action", data=json.dumps({"action": "start"}).encode(),
                          headers={"Content-Type": "application/json", "X-Host-Token": h["host_token"]},
                          method="POST")
            with self.assertRaises(HTTPError) as ctx:
                urlopen(req)
            self.assertEqual(ctx.exception.code, 403)
            self.assertIsNone(server.ROOMS.by_pin(h["pin"]))
        finally:
            server.ROOM_OWNER_RECHECK_SEC = orig

    def test_exam_link_of_another_room_starts_over_but_new_pin_does_not(self):
        for nick in ("TeachX", "TeachY"):
            self.db.create_user(nick, f"{nick.lower()}-pass", "teacher")
        tx, ty = self._login("TeachX"), self._login("TeachY")
        hx, hy = self._host(tx), self._host(ty)
        for tok in (tx, ty):
            self.assertEqual(self._post("/api/admin/bank/activate", {"filename": "bank_a.json", "mode": "exam"},
                                        token=tok)[0], 200)
        code, st = self._post("/api/register", {"name": "Pisar", "pin": hx["pin"]})
        self.assertEqual((code, st["mode"]), (200, "exam"))
        view = f"/api/exam/state?player_id={st['player_id']}&secret={st['player_secret']}"
        self.assertTrue(self._get_json(view + f"&pin={hx['pin']}")[1]["known"])
        other = self._get_json(view + f"&pin={hy['pin']}")[1]
        self.assertEqual((other["known"], other.get("other_room")), (False, True))
        # the teacher gives the room a new PIN: a reload with the old link keeps the test
        req = Request(f"{self.base_url}/api/host/new-pin", data=b"{}", method="POST",
                      headers={"Content-Type": "application/json", "X-Host-Token": hx["host_token"]})
        with urlopen(req) as r:
            new_pin = json.loads(r.read())["pin"]
        self.assertTrue(self._get_json(view + f"&pin={hx['pin']}")[1]["known"])
        self.assertTrue(self._get_json(view + f"&pin={new_pin}")[1]["known"])

    def test_closed_room_disconnects_its_websocket_clients(self):
        import socket as socketmod
        from ws import WSConnection
        server_side, client_side = socketmod.socketpair()
        reader = server_side.makefile("rb")  # the HTTP handler keeps one like this
        try:
            room = server.ROOMS.create(owner_id=None)
            server.WS_MANAGER.register(WSConnection(server_side, "gone-room-client", room=room.key))
            server.ROOMS.drop(room)
            client_side.settimeout(5)
            self.assertEqual(client_side.recv(1), b"")  # EOF: the TCP connection really ended
            self.assertNotIn("gone-room-client", server.WS_MANAGER._connections)
        finally:
            reader.close()
            client_side.close()

    def test_admin_password_session_uses_default_room(self):
        server.ADMIN_AUTH.set_password("server-admin-1")
        try:
            code, r = self._post("/api/admin/login", {"password": "server-admin-1"})
            req = Request(f"{self.base_url}/api/host/token", headers={"Authorization": f"Bearer {r['token']}"})
            with urlopen(req) as resp:
                data = json.loads(resp.read())
            self.assertEqual(data["host_token"], server.DEFAULT_ROOM.host_token)
        finally:
            server.ADMIN_AUTH.set_password(None)


class TestRoomRegistry(unittest.TestCase):
    def test_idle_rooms_dropped_but_default_and_exams_kept(self):
        reg = server.RoomRegistry()
        keep = reg.create(None)
        old = reg.for_owner(42)
        played = reg.for_owner(43)
        exam = reg.for_owner(44)
        pid = played.quiz.register_player("Yesterday")["player_id"]
        reg.bind_player(pid, played)
        exam.exam.register_student("Writer")
        for room in (keep, old, played, exam):
            room.last_active = time.time() - server.ROOM_IDLE_SEC - 1
        self.assertEqual(reg.drop_idle(keep=keep), 2)
        self.assertIsNone(reg.by_pin(old.pin))
        self.assertIsNone(reg.by_pin(played.pin))   # yesterday's players do not keep the PIN alive
        self.assertIsNone(reg.by_player(pid))       # and their binding is gone
        self.assertIsNotNone(reg.by_pin(exam.pin))  # exam results live only in memory
        self.assertIsNotNone(reg.by_pin(keep.pin))
        self.assertIsNot(reg.for_owner(42), old)  # a fresh room on next use

    def test_renew_gives_new_pin_and_empty_lobby(self):
        reg = server.RoomRegistry()
        room = reg.for_owner(50)
        old_pin, key = room.pin, room.key
        pid = room.quiz.register_player("Old class")["player_id"]
        reg.bind_player(pid, room)
        sid = room.exam.register_student("Exam student")["player_id"]
        reg.bind_player(sid, room)
        room.quiz.host_action("start")
        new_pin = reg.renew(room)
        self.assertNotEqual(new_pin, old_pin)
        self.assertIsNone(reg.by_pin(old_pin))
        self.assertIs(reg.by_pin(new_pin), room)
        self.assertIs(reg.by_key(key), room)      # WebSocket routing survives the new PIN
        self.assertEqual((room.quiz.players, room.quiz.phase), ({}, "lobby"))
        self.assertIsNone(reg.by_player(pid))
        self.assertIs(reg.by_player(sid), room)   # a running exam keeps its students

    def test_ws_state_of_closed_room_ends_connection(self):
        with self.assertRaises(LookupError):
            server._ws_state(room="no-such-room")

    def test_secret_compare_accepts_any_characters(self):
        exam = server.ExamState()
        sid = exam.register_student("Eva")["player_id"]
        self.assertFalse(exam.student_view(sid, "žluťoučký kůň")["known"])
        quiz = server.QuizState([])
        pid = quiz.register_player("Jan")["player_id"]
        self.assertFalse(quiz.verify_player(pid, "příliš"))

    def test_ws_broadcast_only_to_room(self):
        from ws import WSConnection, WSConnectionManager
        sent = []

        class FakeConn(WSConnection):
            def __init__(self, cid, room):
                super().__init__(None, cid, room=room)
            def send_json(self, data):
                sent.append((self.conn_id, data["room"]))
                return True

        m = WSConnectionManager()
        m.set_state_getter(lambda player_id=None, host_view=False, room=None: {"room": room})
        m.register(FakeConn("a1", "111111"))
        m.register(FakeConn("b1", "222222"))
        m.broadcast_state("111111")
        self.assertEqual(sent, [("a1", "111111")])

    def test_for_owner_concurrent_first_use_gives_one_room(self):
        reg = server.RoomRegistry()
        got, start = [], threading.Barrier(8)

        def first_request():
            start.wait()
            got.append(reg.for_owner(77))

        threads = [threading.Thread(target=first_request) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len({id(r) for r in got}), 1)
        self.assertEqual(len(reg.all()), 1)


class TestMultiTeacherData(_AccountHTTPBase):
    """QUIZ_ROOMS=multi: a teacher sees and changes only their own data."""

    Q = [{"id": "q1", "prompt": "Q?", "options": ["x", "y"], "correct_index": 0}]

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.orig = (server.ROOM_MODE, server.QUESTIONS_DIR, server.HISTORY_DIR)
        server.ROOM_MODE = "multi"
        server.QUESTIONS_DIR = cls.tmpdir / "questions"
        server.HISTORY_DIR = cls.tmpdir / "history"
        server.QUESTIONS_DIR.mkdir()
        server.HISTORY_DIR.mkdir()
        server.save_questions_to_file("shared.json", cls.Q)
        cls.uid = {nick: cls.db.create_user(nick, f"{nick.lower()}-pass", role)["id"]
                   for nick, role in (("TeachC", "teacher"), ("TeachD", "teacher"),
                                      ("PupilC", "student"), ("PupilD", "student"))}

    @classmethod
    def tearDownClass(cls):
        server.ROOM_MODE, server.QUESTIONS_DIR, server.HISTORY_DIR = cls.orig
        super().tearDownClass()

    def _login(self, nick):
        code, r = self._post("/api/auth/login", {"nickname": nick, "password": f"{nick.lower()}-pass"})
        self.assertEqual(code, 200, r)
        return r["token"]

    def _get_json(self, path, token=None, bearer=None):
        req = Request(f"{self.base_url}{path}")
        if token:
            req.add_header("Authorization", f"UserToken {token}")
        if bearer:
            req.add_header("Authorization", f"Bearer {bearer}")
        try:
            with urlopen(req) as r:
                body = r.read()
                return r.status, (json.loads(body) if r.headers.get_content_type() == "application/json" else body)
        except HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_question_banks_private_and_shared(self):
        tc, td = self._login("TeachC"), self._login("TeachD")
        banks = {b["filename"]: b for b in self._get_json("/api/admin/banks", tc)[1]}
        self.assertEqual((banks["shared.json"]["scope"], banks["shared.json"]["editable"]), ("shared", False))

        # editing a shared bank stores the teacher's own copy; the original stays
        code, r = self._post("/api/admin/bank/save", {"filename": "shared.json", "questions": self.Q * 2}, token=tc)
        self.assertEqual((code, r.get("filename")), (200, "moje/shared.json"), r)
        self.assertEqual(len(server.load_questions_from_file("shared.json")), 1)
        self.assertTrue((server.QUESTIONS_DIR / "users" / str(self.uid["TeachC"]) / "shared.json").is_file())

        # only the owner lists and opens it
        def mine(tok):
            return [b["filename"] for b in self._get_json("/api/admin/banks", tok)[1] if b["scope"] == "mine"]
        self.assertIn("moje/shared.json", mine(tc))
        self.assertEqual(mine(td), [])
        code, r = self._get_json("/api/admin/bank?filename=moje/shared.json", tc)
        self.assertEqual((code, len(r["questions"])), (200, 2))
        self.assertEqual(self._get_json("/api/admin/bank?filename=moje/shared.json", td)[0], 404)
        self.assertEqual(self._post("/api/admin/bank/activate", {"filename": "moje/shared.json"}, token=td)[0], 404)
        self.assertEqual(self._post("/api/admin/bank/activate", {"filename": "moje/shared.json"}, token=tc)[0], 200)

        # names cannot leave the teacher's folder and must not be empty
        code, r = self._post("/api/admin/bank/save", {"filename": "moje/../../x.json", "questions": self.Q}, token=tc)
        self.assertEqual((code, r.get("filename")), (200, "moje/x.json"))
        self.assertFalse((server.QUESTIONS_DIR / "x.json").exists())
        self.assertEqual(self._post("/api/admin/bank/save", {"filename": "/", "questions": self.Q}, token=tc)[0], 400)

        # a teacher deletes only own banks; the shared library belongs to the server admin
        self.assertEqual(self._post("/api/admin/bank/delete", {"filename": "shared.json"}, token=td)[0], 403)
        self.assertTrue((server.QUESTIONS_DIR / "shared.json").is_file())
        self.assertEqual(self._post("/api/admin/bank/delete", {"filename": "moje/x.json"}, token=tc)[0], 200)
        self.assertNotIn("moje/x.json", mine(tc))

    def test_game_history_is_per_teacher(self):
        tc, td = self._login("TeachC"), self._login("TeachD")
        rec_c = server.save_game_history(server.ROOMS.for_owner(self.uid["TeachC"]).quiz)
        rec_d = server.save_game_history(server.ROOMS.for_owner(self.uid["TeachD"]).quiz)
        self.assertEqual(rec_c["owner_id"], self.uid["TeachC"])
        ids_c = [g["id"] for g in self._get_json("/api/admin/history", tc)[1]]
        self.assertIn(rec_c["id"], ids_c)
        self.assertNotIn(rec_d["id"], ids_c)
        self.assertEqual(self._get_json(f"/api/admin/history/export?game_id={rec_d['id']}", tc)[0], 404)
        self.assertEqual(self._get_json(f"/api/admin/history/export?game_id={rec_c['id']}", tc)[0], 200)
        self.assertEqual(self._post("/api/admin/history/delete", {"game_id": rec_d["id"]}, token=tc)[0], 404)
        self.assertIsNotNone(server.load_game_record(rec_d["id"]))

        # the server admin (portal password) sees every teacher's games
        server.ADMIN_AUTH.set_password("server-admin-2")
        try:
            admin = self._post("/api/admin/login", {"password": "server-admin-2"})[1]["token"]
            ids_all = [g["id"] for g in self._get_json("/api/admin/history", bearer=admin)[1]]
            self.assertTrue({rec_c["id"], rec_d["id"]} <= set(ids_all))
        finally:
            server.ADMIN_AUTH.set_password(None)
        self.assertEqual(self._post("/api/admin/history/delete", {"game_id": rec_d["id"]}, token=td)[0], 200)

    def test_homework_is_per_teacher_and_needs_secret(self):
        tc, td = self._login("TeachC"), self._login("TeachD")
        code, s = self._post("/api/selfpaced/start", {"filename": "shared.json"}, token=tc)
        self.assertEqual(code, 200, s)
        sid = s["session_id"]
        def mine(tok):
            return [x["id"] for x in self._get_json("/api/selfpaced/mine", tok)[1]["sessions"]]
        self.assertIn(sid, mine(tc))
        self.assertNotIn(sid, mine(td))
        self.assertEqual(self._get_json(f"/api/selfpaced/results?session_id={sid}", td)[0], 404)
        self.assertEqual(self._get_json(f"/api/selfpaced/results?session_id={sid}", tc)[0], 200)

        # a student's homework is read and answered only with the student's secret
        code, p = self._post("/api/selfpaced/join", {"session_id": sid, "name": "Ema"})
        self.assertEqual(code, 200, p)
        state = f"/api/selfpaced/state?session_id={sid}&player_id={p['player_id']}"
        self.assertEqual(self._get_json(state)[0], 404)
        self.assertEqual(self._get_json(state + "&secret=wrong")[0], 404)
        code, st = self._get_json(state + f"&secret={p['player_secret']}")
        self.assertEqual((code, st.get("status")), (200, "question"))
        answer = {"session_id": sid, "player_id": p["player_id"], "choice": 0}
        self.assertEqual(self._post("/api/selfpaced/answer", answer)[0], 404)
        code, r = self._post("/api/selfpaced/answer", dict(answer, secret=p["player_secret"]))
        self.assertEqual(code, 200, r)

    def test_homework_ordering_items_are_shuffled_per_student(self):
        tc = self._login("TeachC")
        items = ["Ověřit", "Pojmenovat", "Čas", "Aktualizovat", "Síť"]
        bank = [{"id": "o1", "type": "ordering", "prompt": "Postup po instalaci?", "items": list(items),
                 "explanation": "Nejdřív ověřit, pak měnit."}]
        self.assertEqual(self._post("/api/admin/bank/save", {"filename": "moje/poradi.json", "questions": bank},
                                    token=tc)[0], 200)
        sid = self._post("/api/selfpaced/start", {"filename": "moje/poradi.json"}, token=tc)[1]["session_id"]

        def join(name):
            p = self._post("/api/selfpaced/join", {"session_id": sid, "name": name})[1]
            state = (f"/api/selfpaced/state?session_id={sid}&player_id={p['player_id']}"
                     f"&secret={p['player_secret']}")
            return p, state

        ema, state_ema = join("Ema")
        shown = self._get_json(state_ema)[1]["question"]["items"]
        self.assertEqual(sorted(shown), sorted(items))
        self.assertNotEqual(shown, items)
        self.assertEqual(self._get_json(state_ema)[1]["question"]["items"], shown)  # same on reload
        right = [shown.index(x) for x in items]
        code, r = self._post("/api/selfpaced/answer", {"session_id": sid, "player_id": ema["player_id"],
                                                       "secret": ema["player_secret"], "choice": right})
        self.assertEqual(code, 200, r)
        self.assertTrue(r["correct"])
        self.assertEqual(r["choice"], right)
        self.assertEqual([r["question"]["items"][i] for i in r["question"]["correct_order"]], items)
        summary = self._get_json(state_ema)[1]["summary"][0]
        self.assertEqual(summary["choice"], right)
        self.assertEqual([summary["question"]["items"][i] for i in summary["choice"]], items)

        # submitting the order as shown is wrong (it is never the right one)
        ota, state_ota = join("Ota")
        code, r = self._post("/api/selfpaced/answer", {"session_id": sid, "player_id": ota["player_id"],
                                                       "secret": ota["player_secret"], "choice": [0, 1, 2, 3, 4]})
        self.assertEqual(code, 200, r)
        self.assertFalse(r["correct"])
        self.assertTrue((server.user_db._DB_DIR / "item-order.key").is_file())

    def test_classes_and_students_are_per_teacher(self):
        tc, td = self._login("TeachC"), self._login("TeachD")
        pc = self._login("PupilC")
        cls_c = self._post("/api/classes/create", {"name": "6.A"}, token=tc)[1]["class"]
        self.assertEqual(self._post("/api/classes/join", {"join_code": cls_c["join_code"]}, token=pc)[0], 200)

        base = f"/api/classes/{cls_c['id']}"
        for part in ("members", "history", "progress"):
            self.assertEqual(self._get_json(f"{base}/{part}", tc)[0], 200, part)
            self.assertEqual(self._get_json(f"{base}/{part}", td)[0], 404, part)
        self.assertEqual(self._get_json(f"{base}/members", pc)[0], 200)
        self.assertEqual(self._get_json(f"{base}/progress", pc)[0], 404)  # classmates' scores

        # user list: the teachers plus the teacher's own students
        def names(tok):
            return {u["nickname"] for u in self._get_json("/api/users", tok)[1]["users"]}
        self.assertTrue({"TeachC", "PupilC"} <= names(tc))
        self.assertFalse({"TeachD", "PupilD"} & names(tc))
        self.assertNotIn("PupilC", names(td))

        # accounts are managed only by the teacher of the student's class
        pupil, other_teacher = self.uid["PupilC"], self.uid["TeachC"]
        for path, body in ((f"/api/users/{pupil}/reset-password", {"new_password": "nove-heslo-1"}),
                           (f"/api/users/{pupil}/set-role", {"role": "teacher"}),
                           (f"/api/users/{pupil}/delete", {}),
                           (f"/api/users/{other_teacher}/reset-password", {"new_password": "nove-heslo-1"})):
            self.assertEqual(self._post(path, body, token=td)[0], 403, path)
        self.assertEqual(self.db.get_user(pupil)["role"], "student")
        self.assertEqual(self._post(f"/api/users/{pupil}/reset-password",
                                    {"new_password": "nove-heslo-1"}, token=tc)[0], 200)

    def test_shared_student_is_managed_by_neither_teacher_alone(self):
        tc, td = self._login("TeachC"), self._login("TeachD")
        shared = self.db.create_user("PupilS", "pupils-pass", "student")["id"]
        ts = self._login("PupilS")
        cls_c = self._post("/api/classes/create", {"name": "Matematika"}, token=tc)[1]["class"]
        cls_d = self._post("/api/classes/create", {"name": "Fyzika"}, token=td)[1]["class"]
        for c in (cls_c, cls_d):
            self.assertEqual(self._post("/api/classes/join", {"join_code": c["join_code"]}, token=ts)[0], 200)

        for tok, cls in ((td, cls_d), (tc, cls_c)):
            for path, body in ((f"/api/users/{shared}/reset-password", {"new_password": "cizi-heslo-1"}),
                               (f"/api/users/{shared}/set-role", {"role": "teacher"}),
                               (f"/api/users/{shared}/delete", {}),
                               (f"/api/classes/{cls['id']}/members/{shared}/reset-password",
                                {"new_password": "cizi-heslo-1"}),
                               (f"/api/classes/{cls['id']}/members/{shared}/delete-account", {})):
                code, r = self._post(path, body, token=tok)
                self.assertEqual(code, 403, path)
                self.assertIn("jineho ucitele", r["error"])
        self.assertEqual(self._login("PupilS") and self.db.get_user(shared)["role"], "student")

        # a teacher may still remove the student from their own class; then the other one owns it
        self.assertEqual(self._post(f"/api/classes/{cls_d['id']}/members/{shared}/kick", {}, token=td)[0], 200)
        self.assertEqual(self._post(f"/api/users/{shared}/reset-password",
                                    {"new_password": "nove-heslo-2"}, token=tc)[0], 200)
        self.assertEqual(self._post(f"/api/users/{shared}/reset-password",
                                    {"new_password": "nove-heslo-3"}, token=td)[0], 403)

    def test_copy_of_shared_bank_never_overwrites_own_bank(self):
        tc = self._login("TeachC")
        own = self.Q * 3
        code, r = self._post("/api/admin/bank/save", {"filename": "moje/twin.json", "questions": own}, token=tc)
        self.assertEqual((code, r.get("filename")), (200, "moje/twin.json"))
        server.save_questions_to_file("twin.json", self.Q)  # shared bank of the same name
        code, r = self._post("/api/admin/bank/save", {"filename": "twin.json", "questions": self.Q}, token=tc)
        self.assertEqual((code, r.get("filename")), (200, "moje/twin-2.json"))
        self.assertEqual(len(server.load_questions_from_file("moje/twin.json", self.uid["TeachC"])), 3)
        code, r = self._post("/api/admin/bank/save", {"filename": "x" * 300, "questions": self.Q}, token=tc)
        self.assertEqual(code, 400)
        self.assertNotIn(str(server.QUESTIONS_DIR), r.get("error", ""))

    def test_ai_settings_are_shared_and_the_key_stays_home(self):
        calls = []
        orig = (server.generate_questions_ai, dict(server.AI_CONFIG), server.AI_CONFIG_LOCKED)

        def fake_generate(**kwargs):
            calls.append(kwargs)
            return [{"id": "g1", "prompt": "G?", "options": ["a", "b"], "correct_index": 0}]

        server.generate_questions_ai = fake_generate
        server.AI_CONFIG.update(base_url="http://ai.internal:11434", api_key="server-secret-key", model="m1")
        server.AI_CONFIG_LOCKED = False
        try:
            tc = self._login("TeachC")
            code, cfg = self._get_json("/api/admin/ollama/config", tc)
            self.assertEqual((code, cfg["locked"], cfg["model_locked"]), (200, True, True))
            self.assertNotIn("server-secret-key", json.dumps(cfg))
            self.assertEqual(self._post("/api/admin/ollama/config", {"model": "m2"}, token=tc)[0], 403)
            self.assertEqual(server.AI_CONFIG["model"], "m1")
            # a teacher cannot point generation (and the key) elsewhere, only pick the model
            code, r = self._post("/api/admin/ai/generate", {"topic": "T", "model": "m3",
                                                            "base_url": "http://evil.example"}, token=tc)
            self.assertEqual(code, 200, r)
            self.assertEqual((calls[-1]["base_url"], calls[-1]["model"]), ("http://ai.internal:11434", "m3"))

            # the server admin of an unlocked server may try another address — without the stored key
            server.ADMIN_AUTH.set_password("server-admin-3")
            try:
                admin = self._post("/api/admin/login", {"password": "server-admin-3"})[1]["token"]
                req = Request(f"{self.base_url}/api/admin/ai/generate", method="POST",
                              data=json.dumps({"topic": "T", "base_url": "http://evil.example"}).encode(),
                              headers={"Content-Type": "application/json", "Authorization": f"Bearer {admin}"})
                with urlopen(req) as resp:
                    self.assertEqual(resp.status, 200)
                self.assertEqual((calls[-1]["base_url"], calls[-1]["api_key"]), ("http://evil.example", ""))
            finally:
                server.ADMIN_AUTH.set_password(None)
        finally:
            server.generate_questions_ai = orig[0]
            server.AI_CONFIG.clear()
            server.AI_CONFIG.update(orig[1])
            server.AI_CONFIG_LOCKED = orig[2]


class TestBackup(unittest.TestCase):
    """backup.py: consistent copy, 7 daily + 12 monthly, checks, push, restore."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="classrally-backup-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "src"
        self._make_tree(self.root, uploads="uploads", audio="audio")
        # live database in WAL mode whose rows so far exist only in the WAL
        self.conn = sqlite3.connect(str(self.root / "data" / "classrally.db"))
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA wal_autocheckpoint=0")
        self.conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, nickname TEXT)")
        self.conn.executemany("INSERT INTO users (nickname) VALUES (?)", [("ucitel",), ("zak1",), ("zak2",)])
        self.conn.commit()
        self.dest = self.tmp / "zalohy"

    @staticmethod
    def _make_tree(root, uploads, audio):
        for d in ("data/outbox", "history", "questions/users/7", uploads, audio):
            (root / d).mkdir(parents=True)
        (root / "history" / "game_1.json").write_text('{"id": 1}', encoding="utf-8")
        (root / "questions" / "sada.json").write_text("[]", encoding="utf-8")
        (root / "questions" / "users" / "7" / "moje.json").write_text('[{"q": 1}]', encoding="utf-8")
        (root / uploads / "obr.png").write_bytes(b"\x89PNG fake")
        (root / audio / "hudba.mp3").write_bytes(b"ID3 fake")
        (root / "data" / "outbox" / "reset.eml").write_text("one-time link", encoding="utf-8")

    def _quiet_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = backup.main(list(argv))
        return rc, out.getvalue()

    def _craft(self, members, manifest_files, tables=None):
        path = self.tmp / "crafted.tar.gz"
        manifest = {"files": manifest_files, "database": {"tables": tables}}
        with tarfile.open(path, "w:gz") as tar:
            for name, data in list(members.items()) + [("MANIFEST.json", json.dumps(manifest).encode())]:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return path

    def test_backup_is_consistent_and_leaves_out_the_outbox(self):
        naive = self.tmp / "naive.db"  # what a plain copy of the live file would give
        shutil.copy(self.root / "data" / "classrally.db", naive)
        c = sqlite3.connect(str(naive))
        with self.assertRaises(sqlite3.OperationalError):
            c.execute("SELECT COUNT(*) FROM users")
        c.close()

        result = backup.create(self.root, self.dest)
        archive = self.dest / result["archive"]
        with tarfile.open(archive) as tar:
            names = set(tar.getnames())
            db_bytes = tar.extractfile("data/classrally.db").read()
        self.assertEqual(names, {"MANIFEST.json", "data/classrally.db", "history/game_1.json",
                                 "questions/sada.json", "questions/users/7/moje.json", "uploads/obr.png"})
        copy = self.tmp / "copy.db"
        copy.write_bytes(db_bytes)
        c = sqlite3.connect(str(copy))
        self.addCleanup(c.close)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM users").fetchone()[0], 3)
        self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertEqual(result["tables"], {"users": 3})
        self.assertEqual(result["files"], 5)
        sidecar = (archive.parent / (archive.name + ".sha256")).read_text().split()
        self.assertEqual(sidecar, [hashlib.sha256(archive.read_bytes()).hexdigest(), archive.name])
        self.assertTrue((self.dest / "monthly" / archive.name).is_file())
        self.assertTrue(json.loads((self.dest / "last-run.json").read_text())["ok"])
        if os.name == "posix":
            self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
            self.assertEqual((self.dest / "daily").stat().st_mode & 0o777, 0o700)

    def test_app_dir_layout_and_optional_audio(self):
        app = self.tmp / "app"
        self._make_tree(app, uploads="static/uploads", audio="static/audio")
        db = sqlite3.connect(str(app / "data" / "classrally.db"))
        db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
        db.commit()
        db.close()
        result = backup.create(app, self.dest, layout="app-dir", with_audio=True)
        with tarfile.open(self.dest / result["archive"]) as tar:
            names = set(tar.getnames())
        self.assertTrue({"uploads/obr.png", "audio/hudba.mp3"} <= names)
        back = self.tmp / "zpet"
        backup.restore(self.dest / result["archive"], back, layout="app-dir")
        self.assertEqual((back / "static" / "uploads" / "obr.png").read_bytes(), b"\x89PNG fake")
        self.assertEqual((back / "static" / "audio" / "hudba.mp3").read_bytes(), b"ID3 fake")

    def test_rotation_keeps_newest_of_7_days_and_first_of_12_months(self):
        start = time.mktime((2025, 1, 1, 2, 40, 0, 0, 0, -1))
        runs = [start + day * 86400 for day in range(0, 430, 3)]
        last = runs[-1]
        runs += [last + 3600, last + 7200]  # two more backups on the last day
        (self.dest / "daily").mkdir(parents=True)
        (self.dest / "daily" / "poznamka.txt").write_text("not a backup")
        for t in runs:
            backup.create(self.root, self.dest, now=t)

        def name(t):  # archive names and day/month groups are UTC
            return time.strftime("classrally-%Y%m%d-%H%M%SZ.tar.gz", time.gmtime(t))

        by_day, by_month = {}, {}
        for t in runs:
            by_day[time.strftime("%Y%m%d", time.gmtime(t))] = t           # newest of the day
            by_month.setdefault(time.strftime("%Y%m", time.gmtime(t)), t)  # first of the month
        expect_daily = sorted(name(t) for t in sorted(by_day.values())[-7:])
        expect_monthly = sorted(name(t) for t in sorted(by_month.values())[-12:])
        self.assertEqual(backup._archives(self.dest / "daily"), expect_daily)
        self.assertEqual(backup._archives(self.dest / "monthly"), expect_monthly)
        self.assertEqual(sorted(p.name for p in (self.dest / "daily").glob("*.sha256")),
                         [n + ".sha256" for n in expect_daily])
        self.assertTrue((self.dest / "daily" / "poznamka.txt").exists())
        ok, message = backup.check(self.dest, now=last + 7300, verify_all=True)
        self.assertTrue(ok, message)
        self.assertIn("dennich 7, mesicnich 12", message)

    def test_check_notices_old_damaged_and_failed_backups(self):
        archive = self.dest / backup.create(self.root, self.dest)["archive"]
        ok, message = backup.check(self.dest)
        self.assertTrue(ok, message)
        self.assertIn("uctu 3", message)

        old = time.time() - 30 * 3600
        os.utime(archive, (old, old))
        ok, message = backup.check(self.dest)
        self.assertFalse(ok)
        self.assertIn("stara 30", message)

        good = archive.read_bytes()
        archive.write_bytes(good[:len(good) // 2])
        ok, message = backup.check(self.dest)
        self.assertFalse(ok)
        self.assertIn("kontrolni soucet", message)
        backup._write_sidecar(archive, hashlib.sha256(archive.read_bytes()).hexdigest())
        ok, message = backup.check(self.dest)
        self.assertFalse(ok)
        self.assertIn("nejde precist", message)

        archive.write_bytes(good)
        backup._write_sidecar(archive, hashlib.sha256(good).hexdigest())
        self.assertTrue(backup.check(self.dest)[0])
        rc, _ = self._quiet_main("create", "--data-root", str(self.tmp / "nic"), "--dest", str(self.dest))
        self.assertEqual(rc, 1)
        ok, message = backup.check(self.dest)
        self.assertFalse(ok)
        self.assertIn("selhal", message)

    def test_check_reports_to_push_monitor_without_printing_the_url(self):
        hits = []

        class Monitor(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                known = "SPATNY" not in self.path
                body = b'{"ok":true}' if known else b'{"ok":false,"msg":"Monitor not found or not active."}'
                self.send_response(200 if known else 404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Monitor)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        url_file = self.tmp / "push.url"
        url_file.write_text(f"# Uptime Kuma\nhttp://127.0.0.1:{srv.server_port}/api/push/TAJNYTOKEN123\n")
        archive = self.dest / backup.create(self.root, self.dest)["archive"]

        def last_query():
            return urllib.parse.parse_qs(urllib.parse.urlsplit(hits[-1]).query)

        rc, out = self._quiet_main("check", "--dest", str(self.dest), "--push-url-file", str(url_file))
        self.assertEqual(rc, 0, out)
        self.assertEqual(last_query()["status"], ["up"])
        self.assertTrue(last_query()["msg"][0].startswith("OK"))
        self.assertNotIn("TAJNYTOKEN123", out)

        old = time.time() - 40 * 3600
        os.utime(archive, (old, old))
        rc, out = self._quiet_main("check", "--dest", str(self.dest), "--push-url-file", str(url_file))
        self.assertEqual(rc, 1)
        self.assertEqual(last_query()["status"], ["down"])

        # pasted from Kuma's copy button, with its own ?status=up&msg=OK&ping=
        url_file.write_text(f"http://127.0.0.1:{srv.server_port}/api/push/TAJNYTOKEN123?status=up&msg=OK&ping=\n")
        rc, out = self._quiet_main("check", "--dest", str(self.dest), "--push-url-file", str(url_file))
        self.assertEqual(rc, 1)
        self.assertEqual(last_query()["status"], ["down"])
        self.assertEqual(len(last_query()["msg"]), 1)

        os.utime(archive, None)
        url_file.write_text(f"http://127.0.0.1:{srv.server_port}/api/push/SPATNY\n")
        rc, out = self._quiet_main("check", "--dest", str(self.dest), "--push-url-file", str(url_file))
        self.assertEqual(rc, 2, out)
        self.assertIn("404", out)
        self.assertNotIn("SPATNY", out)

    def test_restore_round_trip_and_never_over_existing_data(self):
        archive = self.dest / backup.create(self.root, self.dest)["archive"]
        live = self.tmp / "provoz"
        (live / "data").mkdir(parents=True)
        (live / "data" / "classrally.db").write_bytes(b"live")
        rc, out = self._quiet_main("restore", str(archive), "--into", str(live))
        self.assertEqual(rc, 1)
        self.assertIn("neni prazdna", out)
        self.assertEqual((live / "data" / "classrally.db").read_bytes(), b"live")

        target = self.tmp / "obnova"
        result = backup.restore(archive, target)
        self.assertEqual(result["tables"], {"users": 3})
        for rel in ("history/game_1.json", "questions/users/7/moje.json", "uploads/obr.png"):
            self.assertEqual((target / rel).read_bytes(), (self.root / rel).read_bytes())
        self.assertFalse((target / "data" / "outbox").exists())
        c = sqlite3.connect(str(target / "data" / "classrally.db"))
        self.addCleanup(c.close)
        self.assertEqual([r[0] for r in c.execute("SELECT nickname FROM users ORDER BY id")],
                         ["ucitel", "zak1", "zak2"])

    def test_restore_refuses_unsafe_or_tampered_archives(self):
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db_bytes = (self.root / "data" / "classrally.db").read_bytes()
        good = {"data/classrally.db": db_bytes}
        files = {n: {"sha256": hashlib.sha256(d).hexdigest()} for n, d in good.items()}
        for members, listed in (
                ({**good, "../utek.txt": b"x"}, {**files, "../utek.txt": {"sha256": hashlib.sha256(b"x").hexdigest()}}),
                ({**good, "./history/a.json": b"x"}, {**files, "./history/a.json": {"sha256": hashlib.sha256(b"x").hexdigest()}}),
                ({**good, "history/a.json": b"changed"}, {**files, "history/a.json": {"sha256": hashlib.sha256(b"x").hexdigest()}}),
                ({"history/a.json": b"x"}, {"history/a.json": {"sha256": hashlib.sha256(b"x").hexdigest()}})):
            archive = self._craft(members, listed)
            target = self.tmp / "cil"
            with self.assertRaises(backup.BackupError):
                backup.restore(archive, target)
            self.assertFalse(target.exists())
            self.assertFalse((self.tmp / "utek.txt").exists())

        archive = self._craft(good, files, tables={"users": 3})
        backup.restore(archive, self.tmp / "spravne")  # the crafted archive itself is fine
        garbage = {"data/classrally.db": b"not a database " * 300}
        archive = self._craft(garbage, {n: {"sha256": hashlib.sha256(d).hexdigest()} for n, d in garbage.items()})
        with self.assertRaisesRegex(backup.BackupError, "databaze v archivu je poskozena"):
            backup.restore(archive, self.tmp / "cil")
        archive = self._craft(good, files, tables={"users": 99})  # rows lost since the manifest
        with self.assertRaisesRegex(backup.BackupError, "pocty radku"):
            backup.restore(archive, self.tmp / "cil")
        real = self.dest / backup.create(self.root, self.dest)["archive"]
        sidecar = real.parent / (real.name + ".sha256")
        sidecar.write_text("0" * 64 + "  " + real.name + "\n")
        with self.assertRaisesRegex(backup.BackupError, "kontrolni soucet"):
            backup.restore(real, self.tmp / "cil")
        self.assertFalse((self.tmp / "cil").exists())

        path = self.tmp / "odkaz.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            info = tarfile.TarInfo("history/odkaz")
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tar.addfile(info)
        with self.assertRaises(backup.BackupError):
            backup.restore(path, self.tmp / "cil")

    def test_damaged_archive_is_reported_never_crashes(self):
        archive = self.dest / backup.create(self.root, self.dest)["archive"]
        good = archive.read_bytes()
        plain = gzip.decompress(good)
        start = good.index(b"\0", 10) + 1 if good[3] & 0x08 else 10  # past the gzip header (file name)
        seen = set()
        for offset in range(start, len(good) - 8, max(1, len(good) // 60)):
            damaged = bytearray(good)
            damaged[offset] ^= 0x5A
            try:
                if gzip.decompress(bytes(damaged)) == plain:
                    continue  # the flip hit bits that do not change the content
            except Exception:
                pass
            archive.write_bytes(bytes(damaged))
            # matching checksum, so the reader itself has to cope with the damage
            backup._write_sidecar(archive, hashlib.sha256(bytes(damaged)).hexdigest())
            ok, message = backup.check(self.dest)  # must never raise
            if ok:
                # the flip hit tar padding no file uses: everything must still restore intact
                out = self.tmp / f"cela-{offset}"
                backup.restore(archive, out)
                self.assertEqual((out / "questions" / "users" / "7" / "moje.json").read_bytes(),
                                 (self.root / "questions" / "users" / "7" / "moje.json").read_bytes())
                continue
            seen.add(message.split("(")[1].split(":")[0] if "nejde precist (" in message else message[:40])
            with self.assertRaises(backup.BackupError):
                backup.restore(archive, self.tmp / "cil")
            self.assertFalse((self.tmp / "cil").exists())
        self.assertGreater(len(seen), 1)  # several kinds of damage were exercised
        archive.write_bytes(good)
        backup._write_sidecar(archive, hashlib.sha256(good).hexdigest())
        self.assertTrue(backup.check(self.dest)[0])

    def test_bad_push_url_never_shows_its_secret(self):
        backup.create(self.root, self.dest)
        url_file = self.tmp / "push.url"
        for url in ("http://127.0.0.1:1/api/push/TAJNY TOKEN",          # whitespace: InvalidURL
                    "http://uzivatel:TAJNYTOKEN@127.0.0.1:abc/api/push/x",  # bad port
                    "http://uzivatel:TAJNYTOKEN@127.0.0.1:1/api/push/x",    # nobody listening
                    "https://[TAJNYTOKEN/api/push/x",                        # malformed IPv6 host
                    "file:///etc/TAJNYTOKEN"):
            url_file.write_text(url + "\n")
            rc, out = self._quiet_main("check", "--dest", str(self.dest), "--push-url-file", str(url_file))
            self.assertEqual(rc, 2, out)
            self.assertNotIn("TAJNY", out)
            self.assertNotIn("uzivatel", out)

    def test_future_dated_newest_backup_is_an_error(self):
        archive = self.dest / backup.create(self.root, self.dest)["archive"]
        future = time.time() + 5 * 3600
        os.utime(archive, (future, future))
        ok, message = backup.check(self.dest)
        self.assertFalse(ok)
        self.assertIn("v budoucnosti", message)

    def test_clock_going_back_never_deletes_the_new_backup(self):
        now = time.time()
        first = backup.create(self.root, self.dest, now=now)["archive"]
        second = backup.create(self.root, self.dest, now=now - 3600)["archive"]  # sorts before `first`
        self.assertTrue((self.dest / second).exists())
        self.assertTrue((self.dest / first).exists())

    def test_odd_file_names_are_left_out_and_reported_not_fatal(self):
        bad = self.root / "questions" / "users" / "7"
        (bad / "fyzika\\opakovani.json").write_text("[]", encoding="utf-8")
        (bad / os.fsdecode(b"\xffzlomene.json")).write_text("[]", encoding="utf-8")
        result = backup.create(self.root, self.dest)
        with tarfile.open(self.dest / result["archive"]) as tar:
            names = tar.getnames()
        self.assertIn("questions/users/7/moje.json", names)
        self.assertFalse(any("opakovani" in n or "zlomene" in n for n in names))
        self.assertEqual(result["skipped_count"], 2)
        self.assertTrue(any("\\xff" in item for item in result["skipped"]))
        json.dumps(result)  # printable and storable despite the broken name
        ok, message = backup.check(self.dest)
        self.assertFalse(ok)
        self.assertIn("vynechala 2", message)
        rc, out = self._quiet_main("create", "--data-root", str(self.root), "--dest", str(self.dest))
        self.assertEqual(rc, 0)  # every other file is still backed up every night
        self.assertIn("vynechano 2", out)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads everything")
    def test_unreadable_folder_is_reported(self):
        locked = self.root / "history" / "zamcena"
        locked.mkdir()
        (locked / "hra.json").write_text("{}", encoding="utf-8")
        self.addCleanup(locked.chmod, 0o700)
        # unreadable; listable but not enterable (chmod -R 644); enterable but not listable
        for mode in (0, 0o444, 0o111):
            locked.chmod(mode)
            result = backup.create(self.root, self.dest)
            self.assertEqual(result["skipped_count"], 1, oct(mode))
            self.assertIn("zamcena", result["skipped"][0])
            self.assertFalse(backup.check(self.dest)[0])
            locked.chmod(0o700)
            shutil.rmtree(self.dest)
        # the outbox is left out anyway: an unreadable one is no problem
        outbox = self.root / "data" / "outbox"
        outbox.chmod(0)
        self.addCleanup(outbox.chmod, 0o700)
        self.assertEqual(backup.create(self.root, self.dest)["skipped_count"], 0)

    def test_failed_monthly_copy_leaves_nothing_behind(self):
        real_copy = shutil.copy2

        def half_copy(src, dst, **kwargs):
            Path(dst).write_bytes(b"half")
            raise OSError(28, "No space left on device")

        with mock.patch.object(backup.os, "link", side_effect=OSError(18, "Invalid cross-device link")), \
                mock.patch.object(backup.shutil, "copy2", half_copy):
            rc, out = self._quiet_main("create", "--data-root", str(self.root), "--dest", str(self.dest))
        self.assertEqual(rc, 1)
        self.assertEqual([p.name for p in (self.dest / "monthly").iterdir() if not p.name.endswith(".sha256")], [])
        with mock.patch.object(backup.os, "link", side_effect=OSError(18, "Invalid cross-device link")):
            result = backup.create(self.root, self.dest)  # next run makes the monthly copy
        self.assertTrue(result["monthly_created"])
        self.assertEqual(backup._archives(self.dest / "monthly"), [result["archive"].split("/")[1]])
        self.assertTrue(backup.check(self.dest, verify_all=True)[0])
        self.assertIs(shutil.copy2, real_copy)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads everything")
    def test_folder_that_cannot_be_entered_is_reported(self):
        app = self.tmp / "app"
        self._make_tree(app, uploads="static/uploads", audio="static/audio")
        db = sqlite3.connect(str(app / "data" / "classrally.db"))
        db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
        db.commit()
        db.close()
        (app / "static").chmod(0o644)  # static/uploads exists but cannot be reached
        self.addCleanup((app / "static").chmod, 0o755)
        result = backup.create(app, self.dest, layout="app-dir")
        self.assertEqual(result["skipped_count"], 1)
        self.assertTrue(result["skipped"][0].startswith("uploads:"))
        self.assertFalse(backup.check(self.dest)[0])

    def test_control_characters_in_a_name_stay_on_one_line(self):
        (self.root / "history" / "radek\nnavic.json").write_text("{}", encoding="utf-8")
        result = backup.create(self.root, self.dest)
        self.assertEqual(result["skipped_count"], 1)
        self.assertIn("radek\\x0anavic", result["skipped"][0])
        ok, message = backup.check(self.dest)
        self.assertFalse(ok)
        self.assertNotIn("\n", message)

    def test_unreadable_last_run_is_a_problem(self):
        backup.create(self.root, self.dest)
        (self.dest / "last-run.json").write_text("{nedopsane", encoding="utf-8")
        ok, message = backup.check(self.dest)
        self.assertFalse(ok)
        self.assertIn("last-run.json", message)

    def test_settings_that_would_destroy_backups_are_refused(self):
        with self.assertRaises(backup.BackupError):
            backup.create(self.root, self.dest, keep_daily=0)
        with self.assertRaises(backup.BackupError):
            backup.create(self.root, self.dest, keep_monthly=0)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            backup.main(["create", "--data-root", str(self.root), "--dest", str(self.dest), "--keep-daily", "0"])
        for inside in (self.root / "history" / "zalohy", self.root / "uploads"):
            with self.assertRaisesRegex(backup.BackupError, "zalohovane slozce"):
                backup.create(self.root, inside)
        backup.create(self.root, self.root / "zalohy")  # next to the folders is fine

    def test_restore_is_all_or_nothing(self):
        (self.root / "uploads" / "velky.png").write_bytes(os.urandom(300_000))
        archive = self.dest / backup.create(self.root, self.dest)["archive"]
        real_open = open
        cut = []

        class DiskFull:  # writes a little, then the disk is full
            def __init__(self, f):
                self.f = f

            def write(self, chunk):
                self.f.write(chunk[:1000])
                cut.append(self.f.name)
                raise OSError(28, "No space left on device")

            def close(self):
                self.f.close()

        def opener(path, mode="r", *args, **kwargs):
            f = real_open(path, mode, *args, **kwargs)
            return DiskFull(f) if "w" in mode and Path(path).name == "velky.png" else f

        for target, existed in ((self.tmp / "obnova" / "nova", False), (self.tmp / "prazdna", True)):
            if existed:
                target.mkdir()
            with mock.patch.object(backup, "open", opener, create=True):
                with self.assertRaises(OSError) as ctx:
                    backup.restore(archive, target)
            self.assertEqual(ctx.exception.errno, 28)
            self.assertTrue(cut and cut[-1].endswith("velky.png"))  # the file was cut short ...
            if existed:  # ... and removed with everything else
                self.assertEqual(list(target.iterdir()), [])
                backup.restore(archive, target)  # retry into the same folder works
                self.assertEqual(len((target / "uploads" / "velky.png").read_bytes()), 300_000)
            else:
                self.assertFalse((self.tmp / "obnova").exists())

    def test_main_restores_the_callers_umask(self):
        before = os.umask(0o022)
        try:
            self._quiet_main("list", "--dest", str(self.dest))
            self.assertEqual(os.umask(0o022), 0o022)
        finally:
            os.umask(before)

    @unittest.skipUnless(backup.fcntl, "flock is POSIX only")
    def test_second_run_at_the_same_time_does_nothing(self):
        self.dest.mkdir()
        fd = os.open(self.dest / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        backup.fcntl.flock(fd, backup.fcntl.LOCK_EX)
        self.assertEqual(backup.create(self.root, self.dest), {"busy": True})
        self.assertEqual(os.listdir(self.dest / "daily"), [])

    @unittest.skipUnless(hasattr(os, "geteuid"), "POSIX only")
    def test_root_must_not_touch_a_database_it_does_not_own(self):
        with mock.patch.object(backup.os, "geteuid", return_value=0), \
                mock.patch.object(backup.Path, "stat", lambda self, **kw: os.stat_result((0o100600, 0, 0, 1, 1000, 1000, 1, 0, 0, 0))):
            with self.assertRaises(backup.BackupError) as ctx:
                backup.create(self.root, self.dest)
        self.assertIn("ne jako root", str(ctx.exception))
        self.assertFalse(self.dest.exists())


class TestGameAnalysis(unittest.TestCase):
    """Per-question log of a game, didactic options, history record and exports."""

    QUESTIONS = [
        {"id": "c1", "type": "choice", "prompt": "Port DNS?", "options": ["25", "53", "80", "110"],
         "correct_index": 1, "explanation": "DNS = 53"},
        {"id": "m1", "type": "multiselect", "prompt": "Sudá?", "options": ["1", "2", "3", "4"],
         "correct_indices": [1, 3]},
        {"id": "p1", "type": "poll", "prompt": "Bavilo vás to?", "options": ["ano", "ne"]},
        {"id": "o1", "type": "openended", "prompt": "Jazyk?", "accepted_answers": ["Python"]},
    ]

    def setUp(self):
        self.q = server.QuizState([dict(x) for x in self.QUESTIONS])
        self.q.get_ready_sec = 0
        self.p = {n: self.q.register_player(n) for n in ("Ema", "Jan", "Olga")}

    def _answer(self, name, choice):
        r = self.p[name]
        self.q.submit_answer(r["player_id"], r["player_secret"], choice)

    def _play(self):
        """Ema right everywhere, Jan wrong / partial, Olga never answers."""
        self.q.host_action("start")
        self._answer("Ema", 1)
        self._answer("Jan", 3)
        self.q.host_action("reveal")
        self.q.host_action("next")
        self._answer("Ema", [1, 3])
        self._answer("Jan", [1])  # partial credit
        self.q.host_action("reveal")
        self.q.host_action("next")
        self._answer("Ema", 0)
        self._answer("Jan", 0)
        self.q.host_action("reveal")
        self.q.host_action("next")
        self._answer("Ema", "python")
        self._answer("Jan", "Java")
        self.q.host_action("reveal")
        self.q.host_action("next")  # -> finished, history saved

    def test_question_log_records_what_the_class_answered(self):
        self._play()
        self.assertEqual(self.q.phase, "finished")
        log = self.q.question_log
        self.assertEqual([e["type"] for e in log], ["choice", "multiselect", "poll", "openended"])
        choice, multi, poll, open_ = log
        self.assertEqual((choice["players"], choice["answered"], choice["correct_count"]), (3, 2, 1))
        self.assertEqual(choice["counts"], [0, 1, 0, 1])
        self.assertEqual(choice["correct"], 1)
        ema, jan = self.p["Ema"]["player_id"], self.p["Jan"]["player_id"]
        self.assertTrue(choice["results"][ema]["ok"])
        self.assertFalse(choice["results"][jan]["ok"])
        self.assertNotIn(self.p["Olga"]["player_id"], choice["results"])
        # partial credit earns points but is not "correct"
        self.assertGreater(multi["results"][jan]["points"], 0)
        self.assertFalse(multi["results"][jan]["ok"])
        self.assertEqual(multi["correct_count"], 1)
        # a poll is logged but not scored
        self.assertNotIn("correct_count", poll)
        self.assertEqual(poll["counts"], [2, 0])
        self.assertEqual(open_["top_answers"], [["java", 1, False], ["python", 1, True]])

    def test_open_answers_follow_the_grading_verdict(self):
        q = server.QuizState([
            {"id": "k", "type": "openended", "prompt": "Hlavni mesto Danska?", "accepted_answers": ["Kodaň"]},
            {"id": "c", "type": "openended", "prompt": "Prikaz pro zmenu adresare?", "accepted_answers": ["CD"],
             "case_sensitive": True},
        ])
        q.get_ready_sec = 0
        ids = [q.register_player(n) for n in ("A", "B", "C")]
        q.host_action("start")
        for r, text in zip(ids, ("kodan", "Kodan", "Praha")):  # a missing hacek is accepted
            q.submit_answer(r["player_id"], r["player_secret"], text)
        q.host_action("next")
        for r, text in zip(ids, ("cd", "CD", "cd")):
            q.submit_answer(r["player_id"], r["player_secret"], text)
        q.host_action("next")
        first, second = q.question_log
        self.assertEqual(first["correct_count"], 2)
        self.assertEqual(first["top_answers"], [["kodan", 2, True], ["praha", 1, False]])
        self.assertEqual(second["top_answers"], [["cd", 2, False], ["CD", 1, True]])  # case matters here
        record = {"questions": q._question_summary_locked()}
        self.assertEqual([i["top_wrong"] for i in server.game_items(record)], ["praha", "cd"])

    def test_open_answers_are_grouped_like_the_grading_compares_them(self):
        q = server.QuizState([{"id": "o", "type": "openended", "prompt": "?", "accepted_answers": ["a b"]}])
        q.get_ready_sec = 0
        ids = [q.register_player(n) for n in ("A", "B")]
        q.host_action("start")
        q.submit_answer(ids[0]["player_id"], ids[0]["player_secret"], "a b")
        q.submit_answer(ids[1]["player_id"], ids[1]["player_secret"], "a  b")  # graded wrong
        q.host_action("next")
        self.assertEqual(q.question_log[0]["top_answers"], [["a  b", 1, False], ["a b", 1, True]])

    def test_slides_are_not_in_the_analysis_and_fastest_finger_needs_the_speed_bonus(self):
        questions = [{"id": "s", "type": "slide", "prompt": "Uvod", "body": "..."}] + [
            {"id": f"c{i}", "type": "choice", "prompt": f"Q{i}", "options": ["a", "b"], "correct_index": 0}
            for i in range(3)]
        for speed in (True, False):
            q = server.QuizState([dict(x) for x in questions])
            q.get_ready_sec = 0
            q.set_timing(speed_bonus=speed)
            r = q.register_player("Rychla")
            q.host_action("start")
            q.host_action("next")  # the slide
            for _ in range(3):
                q.submit_answer(r["player_id"], r["player_secret"], 0)
                q.host_action("next")
            self.assertEqual(q.phase, "finished")
            keys = [a["key"] for a in q.public_state(host_view=True)["awards"]]
            self.assertEqual("fastest_finger" in keys, speed)
            record = {"questions": q._question_summary_locked()}
            self.assertEqual([i["type"] for i in server.game_items(record)], ["choice"] * 3)

    def test_host_gets_the_summary_after_the_game_students_do_not(self):
        self._play()
        host = self.q.public_state(host_view=True)
        self.assertEqual(len(host["question_summary"]), 4)
        self.assertNotIn("results", host["question_summary"][0])
        self.assertNotIn("question_summary", self.q.public_state(player_id=self.p["Ema"]["player_id"]))

    def test_history_record_and_exports(self):
        self._play()
        files = sorted(server.HISTORY_DIR.glob("game_*.json"), key=lambda f: f.stat().st_mtime)
        record = json.loads(files[-1].read_text(encoding="utf-8"))
        self.assertEqual(len(record["questions"]), 4)
        self.assertTrue(record["speed_bonus"])
        by_name = {p["name"]: p for p in record["players"]}
        self.assertEqual([bool(a and a.get("ok")) for a in by_name["Ema"]["answers"]], [True, True, False, True])
        self.assertNotIn("ok", by_name["Ema"]["answers"][2])  # the poll has no right answer
        self.assertEqual(by_name["Olga"]["answers"], [None, None, None, None])

        lines = server.game_results_to_csv(record).lstrip("﻿").splitlines()
        self.assertEqual(lines[0], "Poradi;Jmeno;Body;Spravne;Odpovedi;O1;O2;O4")  # the poll gives no points
        rows = {line.split(";")[1]: line.split(";") for line in lines[1:]}
        self.assertEqual(rows["Ema"][3:], ["3", "3", "1", "1", "1"])
        self.assertEqual(rows["Jan"][3:], ["0", "3", "0", "0", "0"])
        self.assertEqual(rows["Olga"][3:], ["0", "0", "", "", ""])

        items = server.game_items(record)
        self.assertEqual([i["percent"] for i in items], [33, 33, None, 33])
        self.assertEqual(items[0]["top_wrong"], "D) 110")
        self.assertEqual(items[3]["top_wrong"], "java")
        self.assertIn("Uspesnost %", server.game_items_csv(record))

        overview = server.history_overview(record)
        self.assertNotIn("questions", overview)
        self.assertTrue(overview["has_details"])
        self.assertFalse(any("answers" in p or "player_id" in p for p in overview["players"]))
        self.assertFalse(any("player_id" in p for p in server.history_detail(record)["players"]))

    def test_speed_bonus_can_be_switched_off(self):
        q = {"correct_index": 0, "options": ["a", "b"]}
        late = {"choice": 0, "time": 30.0}
        self.assertEqual(server.QuizState._calculate_points(q, "choice", late, 0.0), 700)
        self.assertEqual(server.QuizState._calculate_points(q, "choice", late, 0.0, speed_bonus_on=False), 1000)
        self.q.set_timing(speed_bonus=False, question_sec=120)
        self.q.host_action("start")
        self.q.question_started_at -= 35  # a slow, correct answer
        self._answer("Ema", 1)
        self.q.host_action("reveal")
        self.assertEqual(self.q.players[self.p["Ema"]["player_id"]].score, 1000)

    def test_podium_only_hides_other_students_ranking(self):
        self.q.set_timing(show_leaderboard=False)
        self.q.host_action("start")
        self._answer("Ema", 1)
        self.q.host_action("reveal")
        jan = self.q.public_state(player_id=self.p["Jan"]["player_id"])
        self.assertEqual(jan["players"], [])
        self.assertIsNone(jan["me"]["rank"])
        self.assertIsNone(jan["me"]["behind"])
        self.assertFalse(jan["show_leaderboard"])
        self.assertEqual(len(self.q.public_state(host_view=True)["players"]), 3)  # the teacher still sees all
        self.q.host_action("next")
        self.q.host_action("next")
        self.q.host_action("next")
        self.q.host_action("next")
        self.assertEqual(self.q.phase, "finished")
        olga = self.q.public_state(player_id=self.p["Olga"]["player_id"])
        self.assertEqual([p["rank"] for p in olga["players"]], [1, 2, 3])
        self.assertEqual(olga["me"]["rank"], 3)  # third place may see the medal ...
        self.q.players[self.q.register_player("Petr")["player_id"]].score = -1
        petr_id = [pid for pid, pl in self.q.players.items() if pl.name == "Petr"][0]
        self.assertIsNone(self.q.public_state(player_id=petr_id)["me"]["rank"])  # ... fourth place sees nothing

    def test_reset_clears_the_log(self):
        self._play()
        self.q.host_action("reset")
        self.assertEqual(self.q.question_log, [])


class TestExamAnalysis(unittest.TestCase):
    """Item analysis, exports and saving of exams (results survive a restart)."""

    QUESTIONS = [{"id": f"q{i}", "prompt": f"Otazka {i}", "options": ["A", "B", "C"], "correct_index": 0}
                 for i in range(1, 5)]

    def setUp(self):
        user_db_module().init_db()  # the saved-exam tables (idempotent)

    def _exam(self):
        e = server.ExamState([dict(q) for q in self.QUESTIONS])
        e.update_config({"shuffle_questions": False, "shuffle_options": False})
        return e

    def _students(self, e, answers):
        """answers: name -> list of choices (None = no answer); everyone submits."""
        ids = {}
        for name in answers:
            r = e.register_student(name)
            ids[name] = (r["player_id"], r["player_secret"])
        e.open_exam()
        for name, choices in answers.items():
            pid, sec = ids[name]
            for q, c in zip(self.QUESTIONS, choices):
                if c is not None:
                    e.save_answer(pid, sec, q["id"], c)
            e.submit(pid, sec)
        return ids

    ANSWERS = {  # q1 easy, q4 separates, q2 has a popular wrong answer
        "Ada": [0, 0, 0, 0],
        "Bob": [0, 0, 0, 0],
        "Cyril": [0, 1, 0, None],
        "Dana": [0, 1, 1, 2],
        "Eva": [0, 1, None, 1],
    }

    def test_item_stats(self):
        e = self._exam()
        self._students(e, self.ANSWERS)
        stats = e.item_stats()
        self.assertEqual((stats["students"], stats["graded"]), (5, 5))
        q1, q2, q3, q4 = stats["items"]
        self.assertEqual(q1["percent"], 100)
        self.assertEqual((q2["correct"], q2["wrong"], q2["counts"], q2["top_wrong"]), (2, 3, [2, 3, 0], 1))
        self.assertEqual((q3["correct"], q3["unanswered"]), (3, 1))
        self.assertEqual((q4["correct"], q4["wrong"], q4["unanswered"]), (2, 2, 1))
        self.assertIsNone(q4["discrimination"])  # 5 students: too few for a correlation

        # ten students (the same answers twice): the corrected item-total correlation of q4
        e = self._exam()
        self._students(e, {f"{n}{k}": a for k in (1, 2) for n, a in self.ANSWERS.items()})
        q1, _, _, q4 = e.item_stats()["items"]
        self.assertIsNone(q1["discrimination"])  # everyone right: no spread
        marks, totals = [1, 1, 0, 0, 0] * 2, [4, 4, 2, 1, 1] * 2
        rest = [t - m for m, t in zip(marks, totals)]
        mm, mr = sum(marks) / 10, sum(rest) / 10
        r = (sum((m - mm) * (x - mr) for m, x in zip(marks, rest))
             / math.sqrt(sum((m - mm) ** 2 for m in marks) * sum((x - mr) ** 2 for x in rest)))
        self.assertAlmostEqual(q4["discrimination"], round(r, 2))
        self.assertGreater(q4["discrimination"], 0.5)

    def test_csv_cells_cannot_run_as_formulas(self):
        e = self._exam()
        self._students(e, {"=HYPERLINK(\"http://x\")": [0, 0, 0, 0], "-Nula": [1, 1, 1, 1]})
        import csv as csv_module
        cells = [c for text in (e.results_csv(), e.answers_csv())
                 for row in csv_module.reader(text.lstrip("\ufeff").splitlines(), delimiter=";") for c in row]
        self.assertFalse([c for c in cells if c[:1] in ("=", "+", "-", "@")])
        self.assertIn("'=HYPERLINK(\"http://x\")", cells)
        self.assertIn("'-Nula", cells)
        items = e.items_csv()
        self.assertNotIn("'", items.split("\n")[1].split(";")[-1])  # numbers stay numbers

    def test_csv_exports(self):
        e = self._exam()
        self._students(e, self.ANSWERS)
        results = e.results_csv().lstrip("﻿").splitlines()
        self.assertTrue(results[0].endswith(";O1;O2;O3;O4"))
        cyril = [line for line in results if line.startswith("Cyril;")][0].split(";")
        self.assertEqual(cyril[-4:], ["1", "0", "1", ""])
        answers = e.answers_csv()
        self.assertIn("Dana;2;Otazka 2;B) B;A) A;chybne", answers)
        self.assertIn("Eva;3;Otazka 3;;A) A;bez odpovedi", answers)
        items = e.items_csv().lstrip("﻿").splitlines()
        self.assertEqual(items[0].split(";")[-1], "Korelace s celkem")
        self.assertTrue(items[4].endswith(";"))  # 5 students: no correlation yet
        e10 = self._exam()
        self._students(e10, {f"{n}{k}": a for k in (1, 2) for n, a in self.ANSWERS.items()})
        self.assertRegex(e10.items_csv().splitlines()[4], r";0,\d+$")  # decimal comma for Czech Excel

    def test_results_are_saved_as_students_work(self):
        e = self._exam()
        e.owner_id = None
        ids = {}
        for name in ("Ada", "Bob"):
            r = e.register_student(name)
            ids[name] = (r["player_id"], r["player_secret"])
        e.open_exam()
        sid = e.session_id
        self.assertIsNotNone(sid)
        e.save_answer(*ids["Ada"], "q1", 0)
        e.record_event(*ids["Ada"], "blur")
        saved = {r["name"]: r for r in user_db_module().get_exam_session(sid)["results"]}
        self.assertEqual(saved["Ada"]["answers"], {"q1": 0})
        self.assertEqual(saved["Ada"]["blur_count"], 1)
        self.assertIsNone(saved["Ada"]["score"])
        e.submit(*ids["Ada"])
        e.end_exam()
        session = user_db_module().get_exam_session(sid)
        self.assertIsNotNone(session["ended_at"])
        self.assertFalse(session["interrupted"])
        saved = {r["name"]: r for r in session["results"]}
        self.assertEqual((saved["Ada"]["score"], saved["Bob"]["score"]), (1, 0))
        self.assertTrue(saved["Bob"]["auto_submitted"])
        self.assertEqual(session["questions"][0]["prompt"], "Otazka 1")  # a snapshot of the questions

    def test_interrupted_exam_is_graded_and_closed_after_a_restart(self):
        e = self._exam()
        r = e.register_student("Petra")
        e.open_exam()
        e.save_answer(r["player_id"], r["player_secret"], "q1", 0)
        e.save_answer(r["player_id"], r["player_secret"], "q2", 0)
        sid = e.session_id
        # the server stops here; on the next start:
        self.assertGreaterEqual(server.close_interrupted_exams(), 1)
        session = user_db_module().get_exam_session(sid)
        self.assertTrue(session["interrupted"])
        self.assertIsNotNone(session["ended_at"])
        petra = session["results"][0]
        self.assertEqual((petra["score"], petra["auto_submitted"]), (2, True))
        self.assertEqual(petra["grade"], server.ExamConfig().grade_for_percent(50))
        self.assertEqual(server.close_interrupted_exams(), 0)  # nothing left open

    def test_reopening_continues_the_same_saved_exam_and_keeps_its_settings(self):
        e = self._exam()
        a = e.register_student("Ada")
        e.open_exam()
        sid = e.session_id
        e.save_answer(a["player_id"], a["player_secret"], "q1", 0)
        e.extend(300)  # +5 minutes during the exam
        saved = user_db_module().get_exam_session(sid)
        self.assertEqual(saved["config"]["time_limit_sec"], e.config.time_limit_sec)
        e.end_exam()
        self.assertIsNotNone(user_db_module().get_exam_session(sid)["ended_at"])
        b = e.register_student("Bob")  # a latecomer
        e.open_exam()
        self.assertEqual(e.session_id, sid)
        self.assertIsNone(user_db_module().get_exam_session(sid)["ended_at"])
        e.submit(b["player_id"], b["player_secret"])
        e.end_exam()
        rows = user_db_module().get_exam_session(sid)["results"]
        self.assertEqual(sorted(r["name"] for r in rows), ["Ada", "Bob"])  # nobody twice
        self.assertEqual(len([x for x in user_db_module().list_exam_sessions() if x["id"] == sid]), 1)

    def test_reopening_after_the_saved_exam_was_deleted_saves_it_again(self):
        e = self._exam()
        a = e.register_student("Ada")
        e.open_exam()
        e.save_answer(a["player_id"], a["player_secret"], "q1", 0)
        e.end_exam()
        first = e.session_id
        self.assertTrue(user_db_module().delete_exam_session(first))
        late = e.register_student("Pozdni")
        e.open_exam()
        self.assertNotEqual(e.session_id, first)
        saved = user_db_module().get_exam_session(e.session_id)
        self.assertEqual(sorted(r["name"] for r in saved["results"]), ["Ada", "Pozdni"])  # saved right away
        e.save_answer(late["player_id"], late["player_secret"], "q1", 0)
        e.end_exam()
        rows = {r["name"]: r for r in user_db_module().get_exam_session(e.session_id)["results"]}
        self.assertEqual((rows["Ada"]["score"], rows["Pozdni"]["score"]), (1, 1))

    def test_a_second_open_does_not_restart_the_clock(self):
        e = self._exam()
        e.register_student("Ada")
        e.open_exam()
        e.opened_at -= 600  # ten minutes into the exam
        started = e.opened_at
        e.extend(300)
        limit = e.config.time_limit_sec
        e.update_config({"time_limit_sec": 1800, "allow_back": False})  # the form re-sends its values
        e.open_exam()  # second click on "Spustit"
        self.assertEqual(e.opened_at, started)
        self.assertEqual(e.config.time_limit_sec, limit)
        self.assertFalse(e.config.allow_back)  # other settings still apply
        self.assertEqual(user_db_module().get_exam_session(e.session_id)["config"]["time_limit_sec"], limit)

        # the time ran out a moment ago and nobody polled: re-opening for latecomers takes the new limit
        e.opened_at -= limit
        e.update_config({"time_limit_sec": 600})
        self.assertEqual((e.phase, e.config.time_limit_sec), ("ended", 600))

    def test_an_interrupted_exam_ends_at_its_last_activity(self):
        e = self._exam()
        r = e.register_student("Iva")
        e.open_exam()
        sid = e.session_id
        user_db_module().end_exam_session(sid, time.time())  # pretend it was opened long ago ...
        conn = user_db_module().get_conn()
        conn.execute("UPDATE exam_sessions SET opened_at = ?, ended_at = NULL WHERE id = ?", (time.time() - 7200, sid))
        conn.commit()
        conn.close()
        e.save_answer(r["player_id"], r["player_secret"], "q1", 0)  # ... re-opened and answered now
        answered_at = user_db_module().get_exam_session(sid)["results"][0]["updated_at"]
        server.close_interrupted_exams()  # (grading the row writes it again, later)
        self.assertGreaterEqual(user_db_module().get_exam_session(sid)["ended_at"], answered_at)

    def test_reset_or_new_bank_never_loses_a_running_exam(self):
        e = self._exam()
        r = e.register_student("Ota")
        e.open_exam()
        e.save_answer(r["player_id"], r["player_secret"], "q1", 0)
        sid = e.session_id
        e.reset()
        self.assertIsNone(e.session_id)
        session = user_db_module().get_exam_session(sid)
        self.assertIsNotNone(session["ended_at"])
        self.assertEqual(session["results"][0]["score"], 1)
        e.reload_questions([dict(q) for q in self.QUESTIONS])  # nothing open: nothing to close
        self.assertIsNone(e.session_id)


def user_db_module():
    import db
    return db


class TestAnalysisHTTP(_AccountHTTPBase):
    """Saved exams, history detail, options and activity over HTTP (multi-teacher server)."""

    Q = [{"id": "q1", "prompt": "Q1?", "options": ["x", "y"], "correct_index": 0},
         {"id": "q2", "prompt": "Q2?", "options": ["x", "y"], "correct_index": 1}]

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.orig = (server.ROOM_MODE, server.QUESTIONS_DIR, server.HISTORY_DIR)
        server.ROOM_MODE = "multi"
        server.QUESTIONS_DIR = cls.tmpdir / "questions"
        server.HISTORY_DIR = cls.tmpdir / "history"
        server.QUESTIONS_DIR.mkdir()
        server.HISTORY_DIR.mkdir()
        server.save_questions_to_file("sada.json", cls.Q)
        cls.uid = {nick: cls.db.create_user(nick, f"{nick.lower()}-pass", "teacher")["id"]
                   for nick in ("TeachE", "TeachF")}
        server.ADMIN_AUTH.set_password("spravce-heslo-123")

    @classmethod
    def tearDownClass(cls):
        # user ids start at 1 again in the next class's database: leave no rooms behind
        for uid in cls.uid.values():
            room = server.ROOMS._by_owner.get(uid)
            if room is not None:
                server.ROOMS.drop(room)
        server.ADMIN_AUTH.set_password(None)
        server.ROOM_MODE, server.QUESTIONS_DIR, server.HISTORY_DIR = cls.orig
        super().tearDownClass()

    def _login(self, nick):
        code, r = self._post("/api/auth/login", {"nickname": nick, "password": f"{nick.lower()}-pass"})
        self.assertEqual(code, 200, r)
        return r["token"]

    def _req(self, path, token=None, headers=None, data=None):
        req = Request(f"{self.base_url}{path}", data=json.dumps(data).encode() if data is not None else None,
                      headers={"Content-Type": "application/json", **(headers or {})},
                      method="POST" if data is not None else "GET")
        if token:
            req.add_header("Authorization", f"UserToken {token}")
        try:
            with urlopen(req) as r:
                body = r.read()
                return r.status, (json.loads(body) if r.headers.get_content_type() == "application/json"
                                  else body.decode("utf-8"))
        except HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _run_exam(self, token, students):
        host = self._req("/api/host/token", token)[1]
        self.assertEqual(self._req("/api/admin/bank/activate", token,
                                   data={"filename": "sada.json", "mode": "exam"})[0], 200)
        self._req("/api/host/exam-action", headers={"X-Host-Token": host["host_token"]}, data={"action": "reset"})
        ids = []
        for name, choices in students.items():
            r = self._req("/api/register", data={"name": name, "pin": host["pin"]})[1]
            ids.append((r, choices))
        self._req("/api/host/exam-action", headers={"X-Host-Token": host["host_token"]}, data={"action": "open"})
        for r, choices in ids:
            for qid, c in zip(("q1", "q2"), choices):
                self._req("/api/exam/answer", data={"player_id": r["player_id"], "player_secret": r["player_secret"],
                                                    "question_id": qid, "choice": c})
        self._req("/api/host/exam-action", headers={"X-Host-Token": host["host_token"]}, data={"action": "end"})
        return host

    def test_saved_exams_belong_to_their_teacher(self):
        te, tf = self._login("TeachE"), self._login("TeachF")
        self._run_exam(te, {"Ivan": [0, 1], "Jana": [1, 1]})
        code, live = self._req("/api/admin/exam/items", te)
        self.assertEqual((code, live["graded"], [i["percent"] for i in live["items"]]), (200, 2, [50, 100]))
        self.assertIn("Jana", self._req("/api/admin/exam/answers.csv", te)[1])

        exams = self._req("/api/admin/exams", te)[1]
        self.assertEqual(len(exams), 1)
        exam_id = exams[0]["id"]
        self.assertEqual((exams[0]["students"], exams[0]["avg_percent"]), (2, 75))
        self.assertEqual(self._req("/api/admin/exams", tf)[1], [])
        for kind in ("results.csv", "answers.csv", "items.csv"):
            self.assertEqual(self._req(f"/api/admin/exams/{kind}?id={exam_id}", tf)[0], 404)
            code, body = self._req(f"/api/admin/exams/{kind}?id={exam_id}", te)
            self.assertEqual(code, 200)
            self.assertTrue(body.startswith("﻿"))
        detail = self._req(f"/api/admin/exams/detail?id={exam_id}", te)[1]
        self.assertEqual(sorted(s["name"] for s in detail["students"]), ["Ivan", "Jana"])
        self.assertFalse(any("answers" in s or "player_id" in s for s in detail["students"]))
        self.assertEqual(self._req("/api/admin/exams/detail?id=../../etc", te)[0], 404)
        self.assertEqual(self._req("/api/admin/exams/delete", tf, data={"id": exam_id})[0], 404)
        self.assertEqual(self._req("/api/admin/exams/delete", te, data={"id": exam_id})[0], 200)
        self.assertEqual(self._req("/api/admin/exams", te)[1], [])

    def test_a_running_exam_cannot_be_deleted(self):
        te = self._login("TeachE")
        host = self._req("/api/host/token", te)[1]
        hdr = {"X-Host-Token": host["host_token"]}
        self._req("/api/admin/bank/activate", te, data={"filename": "sada.json", "mode": "exam"})
        self._req("/api/host/exam-action", headers=hdr, data={"action": "reset"})
        self._req("/api/register", data={"name": "Lucie", "pin": host["pin"]})
        self._req("/api/host/exam-action", headers=hdr, data={"action": "open"})
        running = [x for x in self._req("/api/admin/exams", te)[1] if x["ended_at"] is None]
        self.assertEqual(len(running), 1)
        code, r = self._req("/api/admin/exams/delete", te, data={"id": running[0]["id"]})
        self.assertEqual(code, 409, r)
        self._req("/api/host/exam-action", headers=hdr, data={"action": "end"})
        self.assertEqual(self._req("/api/admin/exams/delete", te, data={"id": running[0]["id"]})[0], 200)

    def test_history_list_is_light_and_detail_is_owner_only(self):
        te, tf = self._login("TeachE"), self._login("TeachF")
        host = self._req("/api/host/token", te)[1]
        self.assertEqual(self._req("/api/admin/bank/activate", te, data={"filename": "sada.json"})[0], 200)
        hdr = {"X-Host-Token": host["host_token"]}
        r = self._req("/api/register", data={"name": "Hrac", "pin": host["pin"]})[1]
        self._req("/api/host/action", headers=hdr, data={"action": "start"})
        self._req("/api/submit", data={"player_id": r["player_id"], "player_secret": r["player_secret"], "choice": 0})
        for _ in range(4):  # 2 questions; "next" after the end must not save the game again
            self._req("/api/host/action", headers=hdr, data={"action": "next"})
        games = self._req("/api/admin/history", te)[1]
        self.assertEqual(len(games), 1)
        self.assertTrue(games[0]["has_details"])
        self.assertNotIn("questions", games[0])
        game_id = games[0]["id"]
        detail = self._req(f"/api/admin/history/detail?game_id={game_id}", te)[1]
        self.assertEqual(len(detail["questions"]), 2)
        self.assertEqual(detail["players"][0]["answers"][0]["ok"], True)
        self.assertEqual(self._req(f"/api/admin/history/detail?game_id={game_id}", tf)[0], 404)
        code, body = self._req(f"/api/admin/history/items.csv?game_id={game_id}", te)
        self.assertEqual(code, 200)
        self.assertIn("Uspesnost %", body)
        code, body = self._req(f"/api/admin/history/export?game_id={game_id}", te)
        self.assertIn("O1;O2", body)

    def test_options_are_set_per_room(self):
        te, tf = self._login("TeachE"), self._login("TeachF")
        code, r = self._req("/api/admin/timing", te, data={"speed_bonus": False, "show_leaderboard": False})
        self.assertEqual((code, r["speed_bonus"], r["show_leaderboard"]), (200, False, False))
        room_e = server.ROOMS.for_owner(self.uid["TeachE"])
        room_f = server.ROOMS.for_owner(self.uid["TeachF"])
        self.assertEqual((room_e.quiz.speed_bonus, room_e.quiz.show_leaderboard), (False, False))
        self.assertEqual((room_f.quiz.speed_bonus, room_f.quiz.show_leaderboard), (True, True))

    def test_activity_ignores_rooms_nobody_joined(self):
        registry = server.RoomRegistry()
        with mock.patch.object(server, "ROOMS", registry), \
                mock.patch.object(type(server.WS_MANAGER), "connection_count", new_callable=mock.PropertyMock,
                                  return_value=0):
            room = registry.create(None, quiz=server.QuizState(list(self.Q)), exam=server.ExamState(list(self.Q)))
            activity = server.server_activity()
            self.assertEqual((activity["busy"], activity["rooms"], activity["rooms_recent"]), (False, 1, 0))
            room.quiz.register_player("Ema")
            room.touch()
            activity = server.server_activity()
            self.assertEqual((activity["busy"], activity["rooms_recent"], activity["players"]), (True, 1, 1))

    def test_activity_is_for_the_server_admin_only(self):
        te = self._login("TeachE")
        self.assertEqual(self._req("/api/admin/activity")[0], 401)
        self.assertEqual(self._req("/api/admin/activity", te)[0], 403)
        code, r = self._req("/api/admin/login", data={"password": "spravce-heslo-123"})
        self.assertEqual(code, 200, r)
        code, activity = self._req("/api/admin/activity", headers={"Authorization": f"Bearer {r['token']}"})
        self.assertEqual(code, 200)
        self.assertEqual(set(activity), {"busy", "rooms", "rooms_recent", "games_running", "players",
                                         "exams_open", "exam_students_writing", "websocket_clients"})
        self.assertFalse(any(isinstance(v, (list, dict)) for v in activity.values()))  # counts, no names



class _FakeCollector(BaseHTTPRequestHandler):
    """Stands in for the central bug collector: records what it receives."""
    received: list = []
    status = 201

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        type(self).received.append({"path": self.path, "origin": self.headers.get("Origin"),
                                    "admin": self.headers.get("X-Admin-Token"),
                                    "json": json.loads(body)})
        answer = json.dumps({"id": len(type(self).received)}).encode()
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, *args):
        pass


_PNG_1X1 = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
            "2mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class TestBugReportBridge(unittest.TestCase):
    """POST /api/bug-report: validation, redaction, forwarding, limits, widget assets."""

    @classmethod
    def setUpClass(cls):
        cls.collector = ThreadingHTTPServer(("127.0.0.1", 0), _FakeCollector)
        threading.Thread(target=cls.collector.serve_forever, daemon=True).start()
        cls.widget_dir = Path(tempfile.mkdtemp())
        (cls.widget_dir / "widget.js").write_text("window.InvoAIMBuggy={};")
        (cls.widget_dir / "widget.css").write_text("a{}")
        (cls.widget_dir / "capture.css").write_text("b{}")
        (cls.widget_dir / "build-manifest.json").write_text(
            json.dumps({"assets": {"widget.js": {"sha256": "abcdef0123456789"}}}))
        cls.patches = [
            mock.patch.object(server.bugreport, "URL", f"http://127.0.0.1:{cls.collector.server_address[1]}"),
            mock.patch.object(server.bugreport, "PROJECT_KEY", "classrally"),
            mock.patch.object(server.bugreport, "ORIGIN", "https://classrally.example.com"),
            mock.patch.object(server.bugreport, "WIDGET_DIR", cls.widget_dir),
        ]
        for patch in cls.patches:
            patch.start()
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base_url = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.collector.shutdown()
        for patch in cls.patches:
            patch.stop()
        shutil.rmtree(cls.widget_dir)

    def setUp(self):
        _FakeCollector.received = []
        _FakeCollector.status = 201
        server.BUG_REPORT_LIMITER_IP._attempts.clear()
        server.BUG_REPORT_LIMITER_ALL._attempts.clear()

    def _req(self, path, data=None, headers=None, raw=None):
        body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
        req = Request(f"{self.base_url}{path}", data=body, method="POST" if body is not None else "GET",
                      headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urlopen(req) as r:
                return r.status, r.read(), r.headers
        except HTTPError as e:
            return e.code, e.read(), e.headers

    def _report(self, **extra):
        data = {"popis": "Tlačítko Odpovědět nereaguje", "typ": "CHYBA", "page": "play",
                "url": "/play?pin=123456", "viewport": {"w": 390, "h": 844}, **extra}
        code, body, _ = self._req("/api/bug-report", data)
        return code, json.loads(body)

    def test_config_points_to_versioned_widget(self):
        code, body, _ = self._req("/api/bug-report/config")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body), {"enabled": True, "script": "/buggy/widget.js?v=abcdef012345",
                                            "style": "/buggy/widget.css?v=abcdef012345"})

    def test_widget_assets_are_served_and_nothing_else(self):
        code, body, headers = self._req("/buggy/widget.js")
        self.assertEqual((code, body), (200, b"window.InvoAIMBuggy={};"))
        self.assertTrue(headers["Content-Type"].startswith("application/javascript"))
        self.assertEqual(self._req("/buggy/build-manifest.json")[0], 404)
        self.assertEqual(self._req("/buggy/../server.py")[0], 404)

    def test_report_is_forwarded_with_origin_and_without_admin_token(self):
        code, result = self._report(screenshotDataUrl=_PNG_1X1, clientRequestId="1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed")
        self.assertEqual(code, 201, result)
        self.assertEqual(result, {"id": 1, "screenshotSaved": True})
        sent = _FakeCollector.received[0]
        self.assertEqual(sent["path"], "/api/reports")
        self.assertEqual(sent["origin"], "https://classrally.example.com")
        self.assertIsNone(sent["admin"])
        payload = sent["json"]
        # Only fields of the collector's ReportCreate schema, with their types
        # (e.g. "oblast" is a marked screenshot area object, not a page name).
        self.assertLessEqual(set(payload), {"projectKey", "popis", "typ", "url", "technicke_detaily",
                                            "screenshot_data_url", "userAgent", "reporter"})
        self.assertIsInstance(payload["technicke_detaily"], dict)
        self.assertEqual(payload["technicke_detaily"]["stranka"], "play")
        self.assertEqual(payload["projectKey"], "classrally")
        self.assertEqual(payload["url"], "/play")  # no PIN or other query in the report
        self.assertEqual(payload["technicke_detaily"]["role"], "anonymous")
        self.assertNotIn("reporter", payload)  # pupils are never named
        self.assertEqual(payload["screenshot_data_url"], _PNG_1X1)

    def test_repeated_click_is_one_report(self):
        rid = "2c9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed"
        first = self._report(clientRequestId=rid)
        second = self._report(clientRequestId=rid)
        self.assertEqual(first, second)
        self.assertEqual(len(_FakeCollector.received), 1)

    def test_secrets_and_emails_are_redacted(self):
        code, _ = self._report(popis="heslo: Tajne123 a mail jan@skola.cz, token " + "a" * 40)
        self.assertEqual(code, 201)
        popis = _FakeCollector.received[0]["json"]["popis"]
        self.assertNotIn("Tajne123", popis)
        self.assertNotIn("jan@skola.cz", popis)
        self.assertNotIn("a" * 40, popis)

    def test_invalid_reports_are_rejected_before_forwarding(self):
        self.assertEqual(self._report(popis="  ")[0], 400)
        self.assertEqual(self._report(screenshotDataUrl="data:image/png;base64,AAAA")[1]["code"],
                         "screenshot_invalid")
        self.assertEqual(self._report(screenshotDataUrl="data:image/svg+xml;base64,PHN2Zz4=")[0], 400)
        code, _, _ = self._req("/api/bug-report", raw=b"not json")
        self.assertEqual(code, 400)
        self.assertEqual(_FakeCollector.received, [])

    def test_too_large_body_is_refused(self):
        import socket
        sock = socket.create_connection(("127.0.0.1", self.httpd.server_address[1]), timeout=5)
        sock.sendall((f"POST /api/bug-report HTTP/1.0\r\nHost: x\r\nContent-Type: application/json\r\n"
                      f"Content-Length: {server.bugreport.MAX_BODY_BYTES + 1}\r\n\r\n{{}}").encode())
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
        sock.close()
        self.assertIn(b" 413 ", data.split(b"\r\n", 1)[0])
        self.assertEqual(_FakeCollector.received, [])

    def test_collector_failure_is_reported_not_retried(self):
        _FakeCollector.status = 500
        code, result = self._report()
        self.assertEqual((code, result["code"]), (502, "unavailable"))
        self.assertEqual(len(_FakeCollector.received), 1)

    def test_rate_limit_per_address(self):
        with mock.patch.object(server.BUG_REPORT_LIMITER_IP, "max_requests", 2):
            self.assertEqual(self._report()[0], 201)
            self.assertEqual(self._report()[0], 201)
            self.assertEqual(self._report()[0], 429)
        self.assertEqual(len(_FakeCollector.received), 2)

    def test_teacher_is_named_pupil_is_not(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.addCleanup(_db.set_db_path, _db._DB_PATH)
        _db.set_db_path(os.path.join(tmp, "bug.db"))
        _db._migrated = False
        _db.init_db()
        teacher = _db.create_user("BugTeacher", "Heslo-dlouhe-12345", role="teacher")["id"]
        pupil = _db.create_user("BugPupil", "heslo-dlouhe-2")["id"]
        for uid, role, name in ((teacher, "teacher", {"name": "BugTeacher"}), (pupil, "student", None)):
            _FakeCollector.received = []
            token = _db.create_session(uid)
            code, body, _ = self._req("/api/bug-report", {"popis": "x", "page": "host"},
                                      headers={"Authorization": f"UserToken {token}"})
            self.assertEqual(code, 201, body)
            payload = _FakeCollector.received[0]["json"]
            self.assertEqual(payload["technicke_detaily"]["role"], role)
            self.assertEqual(payload.get("reporter"), name)

    def test_disabled_without_configuration(self):
        with mock.patch.object(server.bugreport, "URL", ""):
            self.assertEqual(self._report()[0], 404)
            code, body, _ = self._req("/api/bug-report/config")
            self.assertEqual(json.loads(body), {"enabled": False})
            self.assertEqual(self._req("/buggy/widget.js")[0], 404)

    def test_every_page_loads_the_host_script(self):
        for name in ("play", "host", "admin", "exam", "profile", "account", "landing"):
            html = (server.STATIC_DIR / f"{name}.html").read_text(encoding="utf-8")
            self.assertIn('<script src="/static/buggy-host.js" defer></script>', html, name)


class TestReadingTime(unittest.TestCase):
    """Longer questions get reading time on top of the teacher's setting (#91, #95)."""

    SHORT = {"id": "s", "prompt": "Q?", "options": ["A", "B", "C", "D"], "correct_index": 0}
    LONG = {"id": "l", "prompt": "P" * 150, "options": ["o" * 25] * 4, "correct_index": 0}  # 250 chars

    def test_short_question_has_no_bonus(self):
        self.assertEqual(server.reading_bonus_sec(self.SHORT), 0)
        self.assertEqual(server.reading_bonus_sec({"prompt": "x" * server.READING_BASE_CHARS}), 0)
        self.assertEqual(server.reading_bonus_sec(None), 0)

    def test_bonus_grows_with_text_and_is_capped(self):
        # 250 chars: (250 - 80) / 15 = 11.3 -> 12 s
        self.assertEqual(server.reading_bonus_sec(self.LONG), 12)
        self.assertEqual(server.reading_bonus_sec({"prompt": "x" * (server.READING_BASE_CHARS + 1)}), 1)
        self.assertEqual(server.reading_bonus_sec({"prompt": "x" * 5000}), server.READING_BONUS_MAX_SEC)
        self.assertEqual(server.reading_bonus_sec({"type": "ordering", "prompt": "x" * 80, "items": ["y" * 30]}), 2)

    def test_state_reports_setting_and_time_as_played(self):
        qs = server.QuizState([self.LONG, self.SHORT])
        qs.set_timing(question_sec=30)
        qs.register_player("Alice")
        qs.host_action("start")
        st = qs.public_state()
        self.assertEqual(st["question_duration_sec"], 30)  # the teacher's setting stays as set
        self.assertEqual(st["reading_bonus_sec"], 12)
        self.assertEqual(st["question_time_sec"], 42)
        self.assertTrue(st["adaptive_time"])
        self.assertGreater(st["phase_time_left"], 30)

    def test_question_ends_after_extended_time(self):
        qs = server.QuizState([self.LONG, self.SHORT])
        qs.set_timing(question_sec=30)
        qs.register_player("Alice")
        qs.host_action("start")
        started = qs.question_started_at
        # 3 s get-ready, then 30 s + 12 s reading time to answer
        with mock.patch("time.time", return_value=started + 38):
            self.assertEqual(qs.public_state()["phase"], "question")  # base 30 s already over
        with mock.patch("time.time", return_value=started + 45.5):
            st = qs.public_state()
        self.assertEqual(st["phase"], "reveal")
        self.assertAlmostEqual(qs.reveal_started_at, started + 45)

    def test_switched_off_keeps_exact_time(self):
        qs = server.QuizState([self.LONG])
        qs.set_timing(question_sec=30, adaptive_time=False)
        qs.register_player("Alice")
        qs.host_action("start")
        st = qs.public_state()
        self.assertEqual(st["reading_bonus_sec"], 0)
        self.assertEqual(st["question_time_sec"], 30)
        self.assertFalse(st["adaptive_time"])


class TestPlayAnswerAreaStatic(unittest.TestCase):
    """/play keeps a half-typed answer across state updates (#94) and runs a smooth countdown (#93)."""

    def setUp(self):
        self.html = (server.STATIC_DIR / "play.html").read_text(encoding="utf-8")

    def test_answer_area_rebuilds_only_on_change(self):
        self.assertIn("if (key === optionsKey) return;", self.html)
        self.assertEqual(self.html.count("keepDraft(input, state);"), 2)  # open answer + word cloud

    def test_countdown_is_drawn_per_frame(self):
        self.assertIn("requestAnimationFrame(frame)", self.html)
        self.assertIn("question_time_sec", self.html)
        self.assertNotIn("transition: width 1s linear", self.html)

    def test_admin_shows_reading_time_option(self):
        admin = (server.STATIC_DIR / "admin.html").read_text(encoding="utf-8")
        self.assertIn('id="timingAdaptive"', admin)
        self.assertIn("adaptive_time:", admin)
        self.assertIn("gameSetWarnings(editorQuestions)", admin)


class TestGameFlow(unittest.TestCase):
    """Game flow (#97): one player per device, nobody waits for a player who
    stopped reacting, a short beat after everyone answered, reaction times and
    the timeline kept with the history record."""

    DEVICE_A = "a" * 32
    DEVICE_B = "b" * 32

    def _bank(self, n=6):
        return [{"id": f"f{i}", "prompt": f"Q{i}?", "options": ["A", "B", "C", "D"], "correct_index": 0,
                 "explanation": "E"} for i in range(n)]

    def _game(self, n=6, get_ready=0):
        qs = server.QuizState(self._bank(n))
        qs.set_timing(get_ready_sec=get_ready)
        return qs

    @staticmethod
    def _answer(qs, player, choice=0):
        qs.submit_answer(player["player_id"], player["player_secret"], choice)

    @staticmethod
    def _time_out(qs):
        """Let the current question run out of time."""
        with mock.patch("time.time", return_value=qs._question_deadline_locked() + 0.1):
            return qs.public_state()

    # --- one player per device ---

    def test_same_device_joins_as_the_same_player(self):
        qs = self._game()
        first = qs.register_player("Jan", device_id=self.DEVICE_A)
        again = qs.register_player("Jan", device_id=self.DEVICE_A)  # a double tap on "Join"
        self.assertEqual(again["player_id"], first["player_id"])
        self.assertEqual(again["player_secret"], first["player_secret"])
        self.assertTrue(again["rejoined"])
        self.assertNotIn("rejoined", first)
        self.assertEqual(len(qs.players), 1)
        renamed = qs.register_player("Jan Novák", device_id=self.DEVICE_A)  # back to the join screen
        self.assertEqual(renamed["player_id"], first["player_id"])
        self.assertEqual(qs.players[first["player_id"]].name, "Jan Novák")
        self.assertEqual([e["e"] for e in qs.events], ["join", "rejoin", "rejoin"])
        self.assertEqual(qs.events[-1]["renamed_from"], "Jan")

    def test_rejoin_during_the_game_keeps_the_points(self):
        qs = self._game()
        ema = qs.register_player("Ema", device_id=self.DEVICE_A)
        qs.register_player("Olga", device_id=self.DEVICE_B)
        qs.host_action("start")
        self._answer(qs, ema)
        qs.host_action("reveal")
        score = qs.players[ema["player_id"]].score
        self.assertGreater(score, 0)
        again = qs.register_player("Ema", device_id=self.DEVICE_A)
        self.assertEqual(again["player_id"], ema["player_id"])
        self.assertEqual(qs.players[ema["player_id"]].score, score)
        self.assertEqual(len(qs.players), 2)

    def test_taken_nickname_is_refused_for_another_device(self):
        qs = self._game()
        qs.register_player("Jan", device_id=self.DEVICE_A)
        with self.assertRaises(server.GameError) as ctx:
            qs.register_player("  jan ", device_id=self.DEVICE_B)
        self.assertEqual(ctx.exception.code, "name_taken")
        with self.assertRaises(server.GameError):
            qs.register_player("JAN")  # no device id at all
        self.assertEqual(len(qs.players), 1)
        qs.register_player("Jan B.", device_id=self.DEVICE_B)
        with self.assertRaises(server.GameError):  # renaming onto a taken nickname
            qs.register_player("Jan", device_id=self.DEVICE_B)
        self.assertEqual(sorted(p.name for p in qs.players.values()), ["Jan", "Jan B."])
        self.assertIn("name_taken", [e["e"] for e in qs.events])

    def test_malformed_device_id_is_ignored(self):
        qs = self._game()
        for i, device in enumerate(("short", "short", "x" * 200, "<script>" * 4, 12345)):
            qs.register_player(f"P{i}", device_id=device)
        self.assertEqual(len(qs.players), 5)
        self.assertTrue(all(p.device_id == "" for p in qs.players.values()))

    def test_logged_in_account_joins_as_the_same_player(self):
        qs = self._game()
        with mock.patch.object(server.user_db, "get_user", return_value=None):  # no profile avatar
            first = qs.register_player("Ucet", user_id=424242)
            again = qs.register_player("Ucet", user_id=424242, device_id=self.DEVICE_B)  # another device
        self.assertEqual(again["player_id"], first["player_id"])
        self.assertEqual(len(qs.players), 1)

    # --- timing ---

    def test_get_ready_comes_on_top_of_the_answer_time(self):
        qs = self._game(get_ready=3)
        qs.set_timing(question_sec=20)
        qs.register_player("A")
        qs.host_action("start")
        started = qs.question_started_at
        st = qs.public_state()
        self.assertAlmostEqual(st["answers_open_at"], started + 3)
        self.assertAlmostEqual(st["question_ends_at"], started + 23)
        self.assertEqual(st["question_time_sec"], 20)
        with mock.patch("time.time", return_value=started + 22.5):
            self.assertEqual(qs.public_state()["phase"], "question")
        with mock.patch("time.time", return_value=started + 23.1):
            st = qs.public_state()
        self.assertEqual(st["phase"], "reveal")
        self.assertEqual(st["end_reason"], "timeout")
        self.assertAlmostEqual(qs.reveal_started_at, started + 23)

    def test_everyone_answered_gives_a_short_beat_before_the_result(self):
        qs = self._game()
        a, b = qs.register_player("A"), qs.register_player("B")
        qs.host_action("start")
        self._answer(qs, a)
        st = qs.public_state()
        self.assertFalse(st["all_answered"])
        self.assertEqual((st["waited_count"], st["waiting_for"]), (2, 1))
        self._answer(qs, b, 1)
        st = qs.public_state()
        self.assertEqual(st["phase"], "question")
        self.assertTrue(st["all_answered"])
        self.assertEqual(st["waiting_for"], 0)
        done = qs.all_answered_at
        self.assertAlmostEqual(st["question_ends_at"], done + server.ALL_ANSWERED_GRACE_SEC)
        self.assertLessEqual(st["phase_time_left"], 2)
        with mock.patch("time.time", return_value=done + server.ALL_ANSWERED_GRACE_SEC + 0.05):
            st = qs.public_state()
        self.assertEqual(st["phase"], "reveal")
        self.assertEqual(st["end_reason"], "all_answered")
        self.assertAlmostEqual(qs.reveal_started_at, done + server.ALL_ANSWERED_GRACE_SEC)

    def test_explanation_keeps_the_result_longer(self):
        long_q = {"id": "x", "prompt": "Q?", "options": ["A", "B"], "correct_index": 0, "explanation": "E" * 150}
        self.assertEqual(server.explanation_bonus_sec(long_q), 6)  # (150 - 60) / 15
        self.assertEqual(server.explanation_bonus_sec({"explanation": "E" * 5000}), server.EXPLANATION_BONUS_MAX_SEC)
        self.assertEqual(server.explanation_bonus_sec({"type": "poll", "explanation": "E" * 500}), 0)
        self.assertEqual(server.explanation_bonus_sec({"explanation": "Short."}), 0)
        qs = server.QuizState([long_q, dict(long_q, id="y")])
        qs.set_timing(reveal_sec=5, get_ready_sec=0)
        qs.register_player("A")
        qs.host_action("start")
        qs.host_action("reveal")
        st = qs.public_state()
        self.assertEqual((st["reveal_duration_sec"], st["reveal_time_sec"]), (5, 11))
        self.assertAlmostEqual(st["reveal_ends_at"], qs.reveal_started_at + 11)
        with mock.patch("time.time", return_value=qs.reveal_started_at + 10):
            self.assertEqual(qs.public_state()["phase"], "reveal")
        with mock.patch("time.time", return_value=qs.reveal_started_at + 11.1):
            self.assertEqual(qs.public_state()["phase"], "question")
        qs.set_timing(adaptive_time=False)
        self.assertEqual(qs.public_state()["reveal_time_sec"], 5)

    # --- nobody waits for a player who does not react ---

    def test_nobody_waits_for_a_player_who_stopped_reacting(self):
        qs = self._game()
        a, b = qs.register_player("Aktivni"), qs.register_player("Spici")
        qs.host_action("start")
        for _ in range(server.INACTIVE_AFTER_MISSED):
            self._answer(qs, a)
            self.assertFalse(qs.public_state()["all_answered"])  # still waiting for Spici
            self.assertEqual(self._time_out(qs)["end_reason"], "timeout")
            qs.host_action("next")
        self.assertTrue(qs.players[b["player_id"]].inactive)
        host = {p["name"]: p for p in qs.public_state(host_view=True)["players"]}
        self.assertTrue(host["Spici"]["inactive"])
        self.assertEqual(host["Spici"]["missed"], server.INACTIVE_AFTER_MISSED)
        self.assertFalse(host["Aktivni"]["inactive"])
        self.assertTrue(qs.public_state(b["player_id"])["me"]["inactive"])
        self.assertNotIn("inactive", qs.public_state()["players"][0])  # not in the public view
        # Aktivni alone completes the class now
        self._answer(qs, a)
        st = qs.public_state()
        self.assertTrue(st["all_answered"])
        self.assertEqual(st["waited_count"], 1)
        qs.host_action("reveal")
        qs.host_action("next")
        # one answer and Spici is waited for again
        self._answer(qs, b, 1)
        self.assertFalse(qs.players[b["player_id"]].inactive)
        self.assertFalse(qs.public_state()["all_answered"])  # Aktivni has not answered yet
        kinds = [e["e"] for e in qs.events]
        self.assertEqual(kinds.count("inactive"), 1)
        self.assertIn("active_again", kinds)
        flow = server.game_flow({"events": qs.events, "questions": qs.question_log})
        self.assertEqual(flow["inactive_players"], ["Spici"])

    def test_skipped_or_early_shown_questions_are_not_misses(self):
        qs = self._game()
        a, b = qs.register_player("A"), qs.register_player("B")
        qs.host_action("start")
        for _ in range(server.INACTIVE_AFTER_MISSED + 1):
            self._answer(qs, a)
            qs.host_action("reveal")  # the teacher shows the result before the time is up
            qs.host_action("next")
        qs.host_action("next")  # and skips a question
        self.assertEqual(qs.players[b["player_id"]].missed_streak, 0)
        self.assertFalse(qs.players[b["player_id"]].inactive)

    def test_late_joiner_is_not_waited_for(self):
        qs = self._game()
        early = qs.register_player("Early")
        qs.host_action("start")
        with mock.patch("time.time", return_value=qs.question_started_at + 2):
            qs.register_player("Late")
            self._answer(qs, early)
            st = qs.public_state()
        self.assertTrue(st["all_answered"])
        qs.host_action("reveal")
        late = next(p for p in qs.players.values() if p.name == "Late")
        self.assertEqual(late.missed_streak, 0)  # joined with the answers already up: not a miss
        self.assertEqual([e for e in qs.events if e["e"] == "join"][-1]["during"], "question")

    def test_disconnected_device_is_not_waited_for(self):
        qs = self._game()
        qs.room_key = "flow-test-room"  # presence known: nobody has a WebSocket in this room
        here, gone = qs.register_player("Here"), qs.register_player("Gone")
        qs.host_action("start")
        self.assertEqual(qs.public_state()["waited_count"], 2)  # both seen just now
        qs.players[gone["player_id"]].last_seen -= server.OFFLINE_AFTER_SEC + 1
        self._answer(qs, here)
        st = qs.public_state(host_view=True)
        self.assertTrue(st["all_answered"])
        self.assertFalse({p["name"]: p for p in st["players"]}["Gone"]["online"])
        self.assertIn({"e": "offline", "player": "Gone"},
                      [{k: e[k] for k in ("e", "player")} for e in qs.events if e["e"] == "offline"])
        qs.mark_seen(gone["player_id"])  # the device polls again
        players = {p["name"]: p for p in qs.public_state(host_view=True)["players"]}
        self.assertTrue(players["Gone"]["online"])
        self.assertEqual(qs.events[-1]["e"], "online")

    def test_kicking_the_last_missing_player_completes_the_class(self):
        qs = self._game()
        a, b = qs.register_player("A"), qs.register_player("B")
        qs.host_action("start")
        self._answer(qs, a)
        self.assertFalse(qs.public_state()["all_answered"])
        qs.kick_player(b["player_id"])
        self.assertTrue(qs.public_state()["all_answered"])
        self.assertEqual(qs.events[-2]["e"], "kick")

    def test_keep_waiting_resets_the_miss_streak(self):
        qs = self._game()
        b = qs.register_player("B")
        qs.players[b["player_id"]].missed_streak = server.INACTIVE_AFTER_MISSED
        self.assertTrue(qs.players[b["player_id"]].inactive)
        qs.keep_waiting(b["player_id"])
        self.assertFalse(qs.players[b["player_id"]].inactive)
        self.assertEqual(qs.events[-1]["e"], "keep_waiting")
        with self.assertRaises(ValueError):
            qs.keep_waiting("nobody")

    # --- reaction ---

    def test_reaction_stats(self):
        self.assertEqual(server.reaction_stats([]), {"avg_sec": None, "median_sec": None, "fastest_sec": None})
        self.assertEqual(server.reaction_stats([4, 2, 9]), {"avg_sec": 5.0, "median_sec": 4.0, "fastest_sec": 2.0})
        self.assertEqual(server.reaction_stats([1, 2, 3, 10])["median_sec"], 2.5)

    def test_reaction_times_per_question_and_player(self):
        qs = self._game(get_ready=3)
        a, b, c = qs.register_player("A"), qs.register_player("B"), qs.register_player("C")
        qs.host_action("start")
        started = qs.question_started_at
        for player, sec in ((a, 2.0), (b, 4.0), (c, 9.0)):
            with mock.patch("time.time", return_value=started + 3 + sec):
                self._answer(qs, player)
        with mock.patch("time.time", return_value=started + 12 + server.ALL_ANSWERED_GRACE_SEC + 0.1):
            st = qs.public_state(b["player_id"])
        self.assertEqual(st["phase"], "reveal")
        self.assertEqual((st["question_stats"]["avg_sec"], st["question_stats"]["median_sec"],
                          st["question_stats"]["fastest_sec"]), (5.0, 4.0, 2.0))
        self.assertEqual(st["me"]["sec"], 4.0)
        entry = qs.question_log[0]
        self.assertEqual((entry["end_reason"], entry["waited"]), ("all_answered", 3))
        self.assertAlmostEqual(entry["duration_sec"], 9 + server.ALL_ANSWERED_GRACE_SEC, places=1)
        self.assertEqual(qs.players[c["player_id"]].reaction_times, [9.0])

    # --- the timeline and the analysis ---

    def test_history_keeps_the_timeline_and_reaction(self):
        qs = self._game(n=2)
        a = qs.register_player("A", device_id=self.DEVICE_A)
        qs.register_player("B")
        qs.register_player("A", device_id=self.DEVICE_A)  # a double tap in the lobby
        qs.host_action("start")
        self._answer(qs, a)
        self._time_out(qs)  # B never answers: the question runs out of time
        qs.host_action("next")
        self._answer(qs, a)
        self._time_out(qs)
        saved = []
        real_save = server.save_game_history
        with mock.patch.object(server, "save_game_history",
                               side_effect=lambda quiz, class_id=None: saved.append(real_save(quiz, class_id))):
            qs.host_action("next")  # the last question: the game is over and saved
        record = saved[0]
        self.assertEqual(record["timing"]["get_ready_sec"], 0)
        self.assertEqual(record["timing"]["inactive_after_missed"], server.INACTIVE_AFTER_MISSED)
        kinds = [e["e"] for e in record["events"]]
        for kind in ("start", "question", "answer", "end", "finished"):
            self.assertIn(kind, kinds)
        rows = {p["name"]: p for p in record["players"]}
        self.assertEqual((rows["A"]["answered"], rows["A"]["missed"]), (2, 0))
        self.assertEqual((rows["B"]["answered"], rows["B"]["missed"], rows["B"]["inactive"]), (0, 2, False))
        self.assertIsNotNone(rows["A"]["avg_sec"])
        self.assertIsNone(rows["B"]["avg_sec"])
        self.assertIsNotNone(record["reaction"]["avg_sec"])
        detail = server.history_detail(record)
        self.assertNotIn("events", detail)
        self.assertEqual(detail["flow"]["ended"], {"timeout": 2})
        lobby = [e for e in detail["flow"]["events"] if e["e"] == "rejoin"]
        self.assertEqual(len(lobby), 1)  # kept from the lobby, before the start
        self.assertLessEqual(lobby[0]["t"], 0)
        self.assertEqual(qs.events, [])  # saved with the game: the next lobby starts afresh
        self.assertTrue(detail["flow"]["has_timeline"])
        self.assertIn("avg_sec", detail["items"][0])
        self.assertEqual(detail["items"][0]["end_reason"], "timeout")
        self.assertNotIn("events", server.history_overview(record))
        self.assertIn("Prumerna reakce (s)", server.game_items_csv(record))

    def test_old_records_without_flow_data_still_load(self):
        detail = server.history_detail({"questions": [{"type": "choice", "players": 2, "answered": 1,
                                                       "correct_count": 1}], "players": []})
        self.assertEqual(detail["flow"]["ended"], {"unknown": 1})
        self.assertFalse(detail["flow"]["has_timeline"])
        self.assertIsNone(detail["items"][0]["avg_sec"])

    def test_game_log_line_has_counts_but_no_names(self):
        qs = self._game(n=1)
        qs.room_key = "abcdef123456"
        a = qs.register_player("Tajne Jmeno")
        out = io.StringIO()
        with mock.patch.object(server, "GAME_LOG", True), contextlib.redirect_stdout(out), \
                mock.patch.object(server, "save_game_history"):
            qs.host_action("start")
            self._answer(qs, a)
            qs.host_action("reveal")
            qs.host_action("next")
        log = out.getvalue()
        self.assertIn("[game abcdef] start: 1 questions, 1 players", log)
        self.assertIn("q1/1 ended (host): answered 1/1, waited for 1", log)
        self.assertIn("finished: 1 players", log)
        self.assertNotIn("Tajne", log)

    def test_pages_use_the_game_flow(self):
        host = (server.STATIC_DIR / "host.html").read_text(encoding="utf-8")
        self.assertIn("['classrally.host', hostToken]", host)  # WebSocket, token not in the URL
        self.assertIn("function fadeAudio(", host)              # music fades, never a hard cut
        self.assertIn("scheduleQuestionMusic(state)", host)
        self.assertNotIn("playStinger({", host)                 # no random stinger at the result
        self.assertIn("handleAllAnswered(state)", host)
        self.assertIn("state.question.top_answers", host)       # open answers on the projector
        play = (server.STATIC_DIR / "play.html").read_text(encoding="utf-8")
        self.assertIn("device_id: deviceId()", play)
        self.assertIn("if (joining) return;", play)              # a double tap joins once
        self.assertIn("e.code === 'name_taken'", play)
        self.assertIn("state.answers_open_at", play)             # the countdown starts with the answers

    def test_open_question_shows_the_class_answers_on_the_projector(self):
        q = {"id": "o", "type": "openended", "prompt": "Port SSH?", "accepted_answers": ["22"]}
        qs = server.QuizState([q])
        qs.set_timing(get_ready_sec=0)
        a, b = qs.register_player("A"), qs.register_player("B")
        qs.host_action("start")
        qs.submit_answer(a["player_id"], a["player_secret"], "22")
        qs.submit_answer(b["player_id"], b["player_secret"], "23")
        qs.host_action("reveal")
        host_q = qs.public_state(host_view=True)["question"]
        self.assertEqual(host_q["accepted_answers"], ["22"])
        self.assertEqual(sorted(row[0] for row in host_q["top_answers"]), ["22", "23"])
        self.assertNotIn("top_answers", qs.public_state(a["player_id"])["question"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
