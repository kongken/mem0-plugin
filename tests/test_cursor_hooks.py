import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from mem0_oss import cli, config, cursor_hooks, state
from tests.fake_server import FakeMem0Server
from tests.test_config import _project_id, make_git_repo


class CursorHookTestCase(unittest.TestCase):
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


def _wait_for_capture(server, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for req in server.requests:
            if req["method"] == "POST" and req["path"] == "/memories":
                return req
        time.sleep(0.05)
    return None


class TestBeforeSubmitPrompt(CursorHookTestCase):
    def test_always_returns_continue_true(self):
        got = cursor_hooks.before_submit_prompt(
            self.make_cfg(), {"conversation_id": "c1", "prompt": "hello"}
        )
        self.assertEqual(got, {"continue": True})

    def test_returns_continue_even_when_disabled(self):
        cfg = self.make_cfg(MEM0_BASE_URL="")
        self.assertEqual(cursor_hooks.before_submit_prompt(cfg, {}), {"continue": True})

    def test_stashes_redacted_prompt_under_conversation_id(self):
        cfg = self.make_cfg()
        cursor_hooks.before_submit_prompt(
            cfg, {"conversation_id": "c1", "prompt": "api_key=supersecret123 请记住这个偏好"}
        )
        stored = state.load(cfg, "c1")
        self.assertIn("[REDACTED]", stored["prompt"])
        self.assertNotIn("supersecret123", stored["prompt"])

    def test_never_searches_the_server(self):
        # Cursor 这个事件不支持注入,检索纯属浪费用户等待时间
        cfg = self.make_cfg()
        cursor_hooks.before_submit_prompt(
            cfg,
            {"conversation_id": "c1", "prompt": "一条足够长的用户输入,确认适配层不会发检索请求"},
        )
        self.assertNotIn("/search", [req["path"] for req in self.server.requests])


class TestAfterAgentResponse(CursorHookTestCase):
    def test_pairs_stash_and_captures_with_cursor_source(self):
        cfg = self.make_cfg()
        cursor_hooks.before_submit_prompt(
            cfg, {"conversation_id": "c1", "prompt": "用户说了这句"}
        )
        got = cursor_hooks.after_agent_response(
            cfg,
            {
                "conversation_id": "c1",
                "text": "助手答了这句",
                "workspace_roots": ["/tmp/proj"],
            },
        )
        self.assertIsNone(got)
        req = _wait_for_capture(self.server)
        self.assertIsNotNone(req, "capture 子进程没有把记忆写到服务端")
        self.assertEqual(req["body"]["metadata"]["source"], "cursor")
        self.assertEqual(req["body"]["metadata"]["cwd"], "/tmp/proj")
        self.assertEqual(
            [m["role"] for m in req["body"]["messages"]], ["user", "assistant"]
        )

    def test_no_capture_without_stash(self):
        cursor_hooks.after_agent_response(
            self.make_cfg(), {"conversation_id": "c1", "text": "x"}
        )
        self.assertIsNone(_wait_for_capture(self.server, timeout=0.5))

    def test_capture_scope_comes_from_payload_workspace_root(self):
        """作用域按 payload 的工作区根算,不按 hook 进程的 cwd。

        cwd 依赖的失败模式是静默的:捕获进一个作用域、MCP 召回从另一个作用域
        读,全链路 fail-open,用户只看到"永远召回不到东西"。
        """
        cfg = self.make_cfg()  # cwd=/tmp/proj
        cursor_hooks.before_submit_prompt(
            cfg, {"conversation_id": "c1", "prompt": "用户说了这句"}
        )
        cursor_hooks.after_agent_response(
            cfg,
            {
                "conversation_id": "c1",
                "text": "助手答了这句",
                "workspace_roots": ["/tmp/another-project"],
            },
        )
        req = _wait_for_capture(self.server)
        self.assertIsNotNone(req, "capture 子进程没有把记忆写到服务端")
        self.assertEqual(
            req["body"]["user_id"],
            config.scoped_user_id("alice", "/tmp/another-project", "project", "raw"),
        )
        self.assertNotEqual(req["body"]["user_id"], cfg.user_id)

    def test_capture_scope_from_workspace_root_uses_raw_key_by_default(self):
        repo = make_git_repo(self, [("origin", "git@github.com:org/repo.git")])
        cfg = self.make_cfg()
        cursor_hooks.before_submit_prompt(
            cfg, {"conversation_id": "c1", "prompt": "用户说了这句"}
        )
        cursor_hooks.after_agent_response(
            cfg,
            {"conversation_id": "c1", "text": "助手答了这句", "workspace_roots": [repo]},
        )
        req = _wait_for_capture(self.server)
        self.assertIsNotNone(req, "capture 子进程没有把记忆写到服务端")
        self.assertEqual(req["body"]["user_id"], "alice:project:github.com/org/repo")

    def test_capture_scope_from_workspace_root_honours_hash_key(self):
        repo = make_git_repo(self, [("origin", "git@github.com:org/repo.git")])
        cfg = self.make_cfg(MEM0_SCOPE_KEY="hash")
        cursor_hooks.before_submit_prompt(
            cfg, {"conversation_id": "c1", "prompt": "用户说了这句"}
        )
        cursor_hooks.after_agent_response(
            cfg,
            {"conversation_id": "c1", "text": "助手答了这句", "workspace_roots": [repo]},
        )
        req = _wait_for_capture(self.server)
        self.assertIsNotNone(req, "capture 子进程没有把记忆写到服务端")
        self.assertEqual(req["body"]["user_id"], _project_id("alice", "github.com/org/repo"))

    def test_capture_falls_back_to_cwd_scope_without_workspace_roots(self):
        cfg = self.make_cfg()
        cursor_hooks.before_submit_prompt(
            cfg, {"conversation_id": "c1", "prompt": "用户说了这句"}
        )
        cursor_hooks.after_agent_response(
            cfg, {"conversation_id": "c1", "text": "助手答了这句"}
        )
        req = _wait_for_capture(self.server)
        self.assertIsNotNone(req, "capture 子进程没有把记忆写到服务端")
        self.assertEqual(req["body"]["user_id"], cfg.user_id)

    def test_malformed_workspace_roots_fall_back_without_crashing(self):
        cfg = self.make_cfg()
        for roots in ("/tmp/not-a-list", [], [123], {"0": "/tmp/x"}):
            self.server.requests.clear()
            cursor_hooks.before_submit_prompt(
                cfg, {"conversation_id": "c1", "prompt": "用户说了这句"}
            )
            cursor_hooks.after_agent_response(
                cfg,
                {"conversation_id": "c1", "text": "助手答了这句", "workspace_roots": roots},
            )
            req = _wait_for_capture(self.server)
            self.assertIsNotNone(req, repr(roots))
            self.assertEqual(req["body"]["user_id"], cfg.user_id, repr(roots))

    def test_stash_cleared_after_capture(self):
        cfg = self.make_cfg()
        cursor_hooks.before_submit_prompt(cfg, {"conversation_id": "c1", "prompt": "p"})
        cursor_hooks.after_agent_response(cfg, {"conversation_id": "c1", "text": "a"})
        self.assertNotIn("prompt", state.load(cfg, "c1"))


class TestSessionStart(CursorHookTestCase):
    def test_quiet_when_probe_succeeds(self):
        self.assertIsNone(
            cursor_hooks.session_start(self.make_cfg(), {"conversation_id": "c1"})
        )

    def test_auth_failure_returns_additional_context_once(self):
        self.server.set_response("GET", "/memories", 401, {"detail": "nope"})
        cfg = self.make_cfg()
        first = cursor_hooks.session_start(cfg, {"conversation_id": "c1"})
        self.assertIn("m0sk_", first["additional_context"])
        self.assertIsNone(cursor_hooks.session_start(cfg, {"conversation_id": "c1"}))


class TestCursorHookCli(unittest.TestCase):
    """这一类走的是 cli.main → config_mod.load(),读的是进程的真实环境变量。

    显式清掉 MEM0_*(尤其 BASE_URL)并把 state 指到临时目录:今天这些用例
    只读不写,也碰不到服务端(cfg.enabled 为假、hooks.stop 在空 assistant 上
    就短路了),但那是被两层守卫挡住的巧合,而不是隔离。哪天守卫挪了位置,
    这一类就是唯一一处能打到调用者真实服务端和家目录的地方。
    """

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
        err = io.StringIO()
        saved_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin_text)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(argv)
        finally:
            sys.stdin = saved_stdin
        return code, out.getvalue()

    def test_garbage_stdin_still_allows(self):
        code, out = self._run(["cursor-hook", "before-submit-prompt"], "not json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"continue": True})

    def test_unknown_event_emits_allow_and_exits_zero(self):
        code, out = self._run(["cursor-hook", "no-such-event"], "")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"continue": True})

    def test_after_agent_response_outputs_nothing_on_empty_payload(self):
        code, out = self._run(["cursor-hook", "after-agent-response"], "{}")
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_before_submit_prompt_swallows_base_exception_from_handler(self):
        # SystemExit/KeyboardInterrupt 继承自 BaseException,绕得过 except Exception。
        # 它们逃出去就意味着 beforeSubmitPrompt 上用户刚敲的 prompt 被抹掉 —— 这里
        # 断言的不只是"不崩",而是崩了也要照样吐出放行 JSON。
        original = cli._CURSOR_HANDLERS["before-submit-prompt"]

        def boom(cfg, payload):
            raise SystemExit(3)

        cli._CURSOR_HANDLERS["before-submit-prompt"] = boom
        try:
            code, out = self._run(
                ["cursor-hook", "before-submit-prompt"], '{"conversation_id":"c1","prompt":"x"}'
            )
        finally:
            cli._CURSOR_HANDLERS["before-submit-prompt"] = original
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"continue": True})
