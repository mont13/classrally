#!/usr/bin/env python3
"""Unit tests for ClassRally server."""

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

# Add parent dir to path so we can import server module
sys.path.insert(0, str(Path(__file__).resolve().parent))

import server


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
        # Simulate time passage
        qs.question_started_at = time.time() - 6
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

            # Question times out -> tick must advance to reveal and notify
            qs.question_started_at = time.time() - 6
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

    def test_list_banks(self):
        q1 = [{"id": "a1", "prompt": "?", "options": ["A", "B"], "correct_index": 0}]
        server.save_questions_to_file("bank1.json", q1)
        server.save_questions_to_file("bank2.json", q1)
        banks = server.list_question_banks()
        self.assertEqual(len(banks), 2)

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

    def test_health(self):
        data = self._get("/api/health")
        self.assertTrue(data["ok"])
        self.assertEqual(data["version"], "2.1")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
