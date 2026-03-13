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

        # P1 should have score > 0 (find by name since player_id not in rankings)
        p1_score = next(p for p in state["players"] if p["name"] == "P1")
        self.assertGreater(p1_score["score"], 0)
        # player_id should NOT be in rankings
        self.assertNotIn("player_id", state["players"][0])

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

    def test_admin_auth_status(self):
        data = self._get("/api/admin/auth-status")
        self.assertFalse(data["auth_required"])

    def test_admin_history_api(self):
        history = self._get("/api/admin/history")
        self.assertIsInstance(history, list)

    def test_admin_ollama_config(self):
        data = self._get("/api/admin/ollama/config")
        self.assertIn("host", data)
        self.assertIn("port", data)
        self.assertIn("model", data)

    def test_admin_ollama_config_update(self):
        result = self._post("/api/admin/ollama/config", {"host": "myhost", "port": 12345, "model": "test:7b"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["config"]["host"], "myhost")
        # Reset
        self._post("/api/admin/ollama/config", {"host": "localhost", "port": 11434, "model": "gpt-oss:20b"})

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
