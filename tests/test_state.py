import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from mem0_oss import config, state


class StateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = config.load(
            env={"MEM0_BASE_URL": "http://x", "MEM0_STATE_DIR": self.tmp}, cwd="/tmp/p"
        )


class TestSafeSessionId(unittest.TestCase):
    def test_normal_id_survives(self):
        self.assertEqual(state.safe_session_id("abc-123_XYZ"), "abc-123_XYZ")

    def test_path_traversal_is_neutralised(self):
        got = state.safe_session_id("../../etc/passwd")
        self.assertNotIn("/", got)
        self.assertNotIn("..", got.replace("_", ""))

    def test_empty_falls_back(self):
        self.assertEqual(state.safe_session_id(""), "default")
        self.assertEqual(state.safe_session_id(None), "default")

    def test_length_is_capped(self):
        self.assertLessEqual(len(state.safe_session_id("a" * 500)), 128)


class TestLoadUpdate(StateTestCase):
    def test_load_missing_returns_empty_dict(self):
        self.assertEqual(state.load(self.cfg, "no-such-session"), {})

    def test_update_then_load_round_trips(self):
        state.update(self.cfg, "s1", prompt="你好", prompt_ts=123)
        got = state.load(self.cfg, "s1")
        self.assertEqual(got["prompt"], "你好")
        self.assertEqual(got["prompt_ts"], 123)

    def test_update_merges_instead_of_replacing(self):
        state.update(self.cfg, "s1", prompt="你好")
        state.update(self.cfg, "s1", recalled=True)
        got = state.load(self.cfg, "s1")
        self.assertEqual(got["prompt"], "你好")
        self.assertTrue(got["recalled"])

    def test_corrupt_file_loads_as_empty(self):
        path = state.path_for(self.cfg, "s1")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write("{ not json")
        self.assertEqual(state.load(self.cfg, "s1"), {})

    def test_sessions_are_isolated(self):
        state.update(self.cfg, "s1", prompt="a")
        state.update(self.cfg, "s2", prompt="b")
        self.assertEqual(state.load(self.cfg, "s1")["prompt"], "a")
        self.assertEqual(state.load(self.cfg, "s2")["prompt"], "b")

    def test_no_temp_files_left_behind(self):
        state.update(self.cfg, "s1", prompt="a")
        session_dir = os.path.dirname(state.path_for(self.cfg, "s1"))
        leftovers = [name for name in os.listdir(session_dir) if not name.endswith(".json")]
        self.assertEqual(leftovers, [])


class TestClearPrompt(StateTestCase):
    def test_removes_prompt_but_keeps_other_fields(self):
        state.update(self.cfg, "s1", prompt="你好", prompt_ts=1, recalled=True)
        state.clear_prompt(self.cfg, "s1")
        got = state.load(self.cfg, "s1")
        self.assertNotIn("prompt", got)
        self.assertNotIn("prompt_ts", got)
        self.assertTrue(got["recalled"])

    def test_is_safe_on_missing_session(self):
        state.clear_prompt(self.cfg, "never-existed")  # 不该抛


class TestCleanup(StateTestCase):
    def test_removes_files_older_than_max_age(self):
        state.update(self.cfg, "old", prompt="x")
        path = state.path_for(self.cfg, "old")
        stale = time.time() - (8 * 24 * 3600)
        os.utime(path, (stale, stale))
        removed = state.cleanup(self.cfg)
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(path))

    def test_keeps_fresh_files(self):
        state.update(self.cfg, "fresh", prompt="x")
        self.assertEqual(state.cleanup(self.cfg), 0)
        self.assertTrue(os.path.exists(state.path_for(self.cfg, "fresh")))

    def test_is_safe_when_dir_missing(self):
        cfg = config.load(
            env={"MEM0_BASE_URL": "http://x", "MEM0_STATE_DIR": os.path.join(self.tmp, "nope")},
            cwd="/tmp/p",
        )
        self.assertEqual(state.cleanup(cfg), 0)


class TestWriteFailureIsFailOpen(StateTestCase):
    def test_unserializable_value_does_not_raise(self):
        # json.dump 中途抛 TypeError:不该冒到调用方,也不该留下临时文件
        returned = state.update(self.cfg, "s1", junk=object())
        self.assertIn("junk", returned)  # 内存中的合并结果照常返回
        session_dir = os.path.join(self.tmp, "sessions")
        leftovers = [n for n in os.listdir(session_dir) if n.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_replace_failure_does_not_raise(self):
        original = state.os.replace

        def boom(*args, **kwargs):
            raise OSError(13, "Permission denied")

        state.os.replace = boom
        try:
            state.update(self.cfg, "s1", prompt="你好")  # 不该抛
            state.update(self.cfg, "s1", recalled=True)
            state.clear_prompt(self.cfg, "s1")           # 也不该抛
        finally:
            state.os.replace = original


if __name__ == "__main__":
    unittest.main()
