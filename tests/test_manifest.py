import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read_json(*parts):
    with open(os.path.join(ROOT, *parts), "r", encoding="utf-8") as handle:
        return json.load(handle)


class TestPluginJson(unittest.TestCase):
    def test_name_matches_directory_convention(self):
        self.assertEqual(read_json(".claude-plugin", "plugin.json")["name"], "mem0-oss")

    def test_has_version_and_description(self):
        data = read_json(".claude-plugin", "plugin.json")
        self.assertTrue(data["version"])
        self.assertTrue(data["description"])


class TestMarketplaceJson(unittest.TestCase):
    def test_lists_the_plugin(self):
        data = read_json(".claude-plugin", "marketplace.json")
        names = [entry["name"] for entry in data["plugins"]]
        self.assertIn("mem0-oss", names)

    def test_plugin_source_path_exists(self):
        for entry in read_json(".claude-plugin", "marketplace.json")["plugins"]:
            source = os.path.join(ROOT, entry["source"])
            self.assertTrue(os.path.isdir(source), entry["source"])
            # 光是目录存在没有意义 —— source 必须指向一个真的插件根
            self.assertTrue(
                os.path.isfile(os.path.join(source, ".claude-plugin", "plugin.json")),
                "%s 下没有 .claude-plugin/plugin.json" % entry["source"],
            )


class TestHooksJson(unittest.TestCase):
    def setUp(self):
        self.hooks = read_json("hooks", "hooks.json")["hooks"]

    def test_registers_exactly_the_three_effective_events(self):
        # PreCompact / TaskCompleted 的 stdout 只进 debug log,挂了也是白挂
        self.assertEqual(
            sorted(self.hooks), ["SessionStart", "Stop", "UserPromptSubmit"]
        )

    def test_every_command_points_at_the_executable(self):
        for entries in self.hooks.values():
            for entry in entries:
                for hook in entry["hooks"]:
                    self.assertIn("${CLAUDE_PLUGIN_ROOT}/bin/mem0-oss", hook["command"])

    def test_every_command_names_a_known_event(self):
        known = {"session-start", "user-prompt", "stop"}
        for entries in self.hooks.values():
            for entry in entries:
                for hook in entry["hooks"]:
                    event = hook["command"].rsplit(" ", 1)[-1]
                    self.assertIn(event, known, hook["command"])

    def test_user_prompt_timeout_is_short(self):
        # UserPromptSubmit 阻塞用户输入,超时必须小
        for entry in self.hooks["UserPromptSubmit"]:
            for hook in entry["hooks"]:
                self.assertLessEqual(hook["timeout"], 5)


class TestExecutable(unittest.TestCase):
    def test_bin_is_executable(self):
        self.assertTrue(os.access(os.path.join(ROOT, "bin", "mem0-oss"), os.X_OK))


class TestCommandFile(unittest.TestCase):
    def test_invokes_the_cli(self):
        with open(os.path.join(ROOT, "commands", "mem0.md"), "r", encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("mem0-oss", body)
        self.assertIn("$ARGUMENTS", body)


class TestTimeoutMargins(unittest.TestCase):
    """外层 hook 超时必须显著大于它包裹的内层网络超时。

    两个计时器起点不同:外层从进程启动算,内层从 urlopen 调用才算,
    中间隔着解释器启动、配置加载和文件 I/O。余量不足时外层会先杀进程,
    我们设计的静默 fail-open 就永远走不到。
    """

    MIN_MARGIN_MS = 1500

    def test_declared_timeouts_have_margin_over_network_timeouts(self):
        import sys

        sys.path.insert(0, os.path.join(ROOT, "lib"))
        from mem0_oss import config, hooks

        inner_ms = {
            "session-start": hooks.PROBE_TIMEOUT_MS,
            "user-prompt": config.DEFAULT_RECALL_TIMEOUT_MS,
            # stop 没有同步网络调用,写入在脱离的子进程里完成
        }
        for entries in read_json("hooks", "hooks.json")["hooks"].values():
            for entry in entries:
                for hook in entry["hooks"]:
                    event = hook["command"].rsplit(" ", 1)[-1]
                    if event not in inner_ms:
                        continue
                    margin = hook["timeout"] * 1000 - inner_ms[event]
                    self.assertGreaterEqual(
                        margin,
                        self.MIN_MARGIN_MS,
                        "%s 的外层超时 %dms 相对内层 %dms 只剩 %dms 余量"
                        % (event, hook["timeout"] * 1000, inner_ms[event], margin),
                    )


class TestCodexPluginJson(unittest.TestCase):
    def test_name_and_component_paths(self):
        data = read_json(".codex-plugin", "plugin.json")
        self.assertEqual(data["name"], "mem0-oss")
        self.assertEqual(data["hooks"], "./codex/hooks.json")
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "codex", "hooks.json")))
        self.assertTrue(os.path.isdir(os.path.join(ROOT, data["skills"])))

    def test_version_synced_with_claude_manifest(self):
        self.assertEqual(
            read_json(".codex-plugin", "plugin.json")["version"],
            read_json(".claude-plugin", "plugin.json")["version"],
        )


class TestCodexHooksJson(unittest.TestCase):
    def setUp(self):
        self.hooks = read_json("codex", "hooks.json")["hooks"]

    def test_registers_exactly_the_three_effective_events(self):
        self.assertEqual(
            sorted(self.hooks), ["SessionStart", "Stop", "UserPromptSubmit"]
        )

    def test_every_command_uses_plugin_root_and_codex_host(self):
        for entries in self.hooks.values():
            for entry in entries:
                for hook in entry["hooks"]:
                    self.assertIn("${PLUGIN_ROOT}/bin/mem0-oss", hook["command"])
                    self.assertIn("--host codex", hook["command"])

    def test_user_prompt_timeout_is_short(self):
        for entry in self.hooks["UserPromptSubmit"]:
            for hook in entry["hooks"]:
                self.assertLessEqual(hook["timeout"], 5)


class TestSkillFile(unittest.TestCase):
    def test_frontmatter_and_relative_bin_reference(self):
        with open(
            os.path.join(ROOT, "skills", "mem0", "SKILL.md"), encoding="utf-8"
        ) as handle:
            body = handle.read()
        self.assertTrue(body.startswith("---\n"))
        self.assertIn("name: mem0", body)
        self.assertIn("description:", body)
        # installer 靠这个字面量做路径替换,动它必须同步改 installer
        self.assertIn("../../bin/mem0-oss", body)


class TestCursorHooksJson(unittest.TestCase):
    def setUp(self):
        self.data = read_json("cursor", "hooks.json")

    def test_version_key(self):
        self.assertEqual(self.data["version"], 1)

    def test_registers_exactly_the_three_events(self):
        self.assertEqual(
            sorted(self.data["hooks"]),
            ["afterAgentResponse", "beforeSubmitPrompt", "sessionStart"],
        )

    def test_every_command_uses_placeholder_and_cursor_hook(self):
        for entries in self.data["hooks"].values():
            for entry in entries:
                self.assertIn("{{MEM0_OSS_BIN}}", entry["command"])
                self.assertIn("cursor-hook", entry["command"])

    def test_before_submit_prompt_timeout_is_short(self):
        for entry in self.data["hooks"]["beforeSubmitPrompt"]:
            self.assertLessEqual(entry["timeout"], 5)


class TestCursorPluginJson(unittest.TestCase):
    def test_name_and_version_sync(self):
        data = read_json(".cursor-plugin", "plugin.json")
        self.assertEqual(data["name"], "mem0-oss")
        self.assertEqual(
            data["version"], read_json(".claude-plugin", "plugin.json")["version"]
        )


class TestCursorRule(unittest.TestCase):
    def test_always_apply_and_data_not_instructions(self):
        path = os.path.join(ROOT, "cursor", "rules", "mem0.mdc")
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("alwaysApply: true", body)
        self.assertIn("mem0_search", body)


if __name__ == "__main__":
    unittest.main()
