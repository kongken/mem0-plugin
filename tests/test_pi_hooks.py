import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from mem0_oss import cli, config, pi_hooks, state
from tests.fake_server import FakeMem0Server
from tests.test_cursor_hooks import _wait_for_capture
from tests.test_config import make_git_repo

LONG_PROMPT = "一条足够长的用户输入,确保会触发召回检索"


class PiHookTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.server = FakeMem0Server().start()
        self.addCleanup(self.server.stop)

    def make_cfg(self, **overrides):
        env = {
            "MEM0_BASE_URL": self.server.base_url,
            "MEM0_API_KEY": "m0sk_test",
            "MEM0_USER_ID": "alice",
            "MEM0_STATE_DIR": self.tmp,
        }
        env.update(overrides)
        return config.load(env=env, cwd="/tmp/proj")


class TestUserPrompt(PiHookTestCase):
    def test_recalls_and_wraps_memories_as_text(self):
        self.server.set_response(
            "POST", "/search", 200, {"results": [{"id": "1", "memory": "喜欢 vim"}]}
        )
        got = pi_hooks.user_prompt(
            self.make_cfg(), {"session_id": "s1", "cwd": "/tmp/proj", "prompt": LONG_PROMPT}
        )
        self.assertIn("Recalled Memories", got["text"])
        self.assertIn("喜欢 vim", got["text"])

    def test_no_output_when_nothing_recalled(self):
        got = pi_hooks.user_prompt(
            self.make_cfg(), {"session_id": "s1", "cwd": "/tmp/proj", "prompt": LONG_PROMPT}
        )
        self.assertIsNone(got)

    def test_no_output_when_disabled(self):
        cfg = self.make_cfg(MEM0_BASE_URL="")
        self.assertIsNone(
            pi_hooks.user_prompt(cfg, {"session_id": "s1", "prompt": LONG_PROMPT})
        )

    def test_stashes_redacted_prompt_under_session_id(self):
        cfg = self.make_cfg()
        pi_hooks.user_prompt(
            cfg, {"session_id": "s1", "cwd": "/tmp/proj", "prompt": "api_key=supersecret123 记住"}
        )
        stored = state.load(cfg, "s1")
        self.assertIn("[REDACTED]", stored["prompt"])
        self.assertNotIn("supersecret123", stored["prompt"])

    def test_search_scope_comes_from_payload_cwd(self):
        repo = make_git_repo(self, [("origin", "git@github.com:org/repo.git")])
        pi_hooks.user_prompt(
            self.make_cfg(), {"session_id": "s1", "cwd": repo, "prompt": LONG_PROMPT}
        )
        req = next(r for r in self.server.requests if r["path"] == "/search")
        self.assertEqual(req["body"]["filters"]["user_id"], "alice:project:github.com/org/repo")


class TestAgentEnd(PiHookTestCase):
    def test_pairs_stash_and_captures_with_pi_source(self):
        cfg = self.make_cfg()
        pi_hooks.user_prompt(cfg, {"session_id": "s1", "cwd": "/tmp/proj", "prompt": "用户说了这句"})
        got = pi_hooks.agent_end(
            cfg, {"session_id": "s1", "cwd": "/tmp/proj", "text": "助手答了这句"}
        )
        self.assertIsNone(got)
        req = _wait_for_capture(self.server)
        self.assertIsNotNone(req, "capture 子进程没有把记忆写到服务端")
        self.assertEqual(req["body"]["metadata"]["source"], "pi")
        self.assertEqual(req["body"]["metadata"]["cwd"], "/tmp/proj")
        self.assertEqual(
            [m["role"] for m in req["body"]["messages"]], ["user", "assistant"]
        )

    def test_recall_and_capture_share_the_same_scope(self):
        # 召回与捕获按同一个 cwd 算作用域,否则就是"写进去读不出来"
        repo = make_git_repo(self, [("origin", "git@github.com:org/repo.git")])
        cfg = self.make_cfg()
        pi_hooks.user_prompt(cfg, {"session_id": "s1", "cwd": repo, "prompt": LONG_PROMPT})
        pi_hooks.agent_end(cfg, {"session_id": "s1", "cwd": repo, "text": "答"})
        capture = _wait_for_capture(self.server)
        search = next(r for r in self.server.requests if r["path"] == "/search")
        self.assertIsNotNone(capture)
        self.assertEqual(capture["body"]["user_id"], search["body"]["filters"]["user_id"])

    def test_no_capture_without_stash(self):
        pi_hooks.agent_end(self.make_cfg(), {"session_id": "s1", "text": "x"})
        self.assertIsNone(_wait_for_capture(self.server, timeout=0.5))

    def test_stash_cleared_after_capture(self):
        cfg = self.make_cfg()
        pi_hooks.user_prompt(cfg, {"session_id": "s1", "prompt": "p"})
        pi_hooks.agent_end(cfg, {"session_id": "s1", "text": "a"})
        self.assertNotIn("prompt", state.load(cfg, "s1"))


class TestSessionStart(PiHookTestCase):
    def test_quiet_when_probe_succeeds(self):
        self.assertIsNone(pi_hooks.session_start(self.make_cfg(), {"session_id": "s1"}))

    def test_auth_failure_warns_once(self):
        self.server.set_response("GET", "/memories", 401, {"detail": "nope"})
        cfg = self.make_cfg()
        first = pi_hooks.session_start(cfg, {"session_id": "s1"})
        self.assertIn("m0sk_", first["text"])
        self.assertIsNone(pi_hooks.session_start(cfg, {"session_id": "s1"}))


class TestPiHookCli(unittest.TestCase):
    """走 cli.main → config_mod.load(),读真实环境变量,所以显式清掉 MEM0_*
    并把 state 指到临时目录(同 TestCursorHookCli 的隔离理由)。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self._saved_env = dict(os.environ)
        self.addCleanup(self._restore_env)
        for name in [key for key in os.environ if key.startswith("MEM0_")]:
            del os.environ[name]
        os.environ["MEM0_STATE_DIR"] = self.tmp

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._saved_env)

    def _run(self, argv, stdin_text):
        out = io.StringIO()
        saved_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin_text)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = cli.main(argv)
        finally:
            sys.stdin = saved_stdin
        return code, out.getvalue()

    def test_garbage_stdin_outputs_nothing_and_exits_zero(self):
        for event in ("session-start", "user-prompt", "agent-end"):
            code, out = self._run(["pi-hook", event], "not json")
            self.assertEqual((code, out), (0, ""), event)

    def test_unknown_event_exits_zero_without_output(self):
        self.assertEqual(self._run(["pi-hook", "no-such-event"], ""), (0, ""))

    def test_swallows_base_exception_from_handler(self):
        original = cli._PI_HANDLERS["user-prompt"]

        def boom(cfg, payload):
            raise SystemExit(3)

        cli._PI_HANDLERS["user-prompt"] = boom
        try:
            result = self._run(["pi-hook", "user-prompt"], '{"session_id":"s1","prompt":"x"}')
        finally:
            cli._PI_HANDLERS["user-prompt"] = original
        self.assertEqual(result, (0, ""))

    def test_user_prompt_emits_single_line_json(self):
        server = FakeMem0Server().start()
        self.addCleanup(server.stop)
        server.set_response("POST", "/search", 200, {"results": [{"id": "1", "memory": "记忆甲"}]})
        os.environ["MEM0_BASE_URL"] = server.base_url
        code, out = self._run(
            ["pi-hook", "user-prompt"],
            json.dumps({"session_id": "s1", "cwd": self.tmp, "prompt": LONG_PROMPT}),
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIn("记忆甲", json.loads(out)["text"])


if __name__ == "__main__":
    unittest.main()
