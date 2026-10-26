import contextlib
import glob
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from mem0_oss import cli, installer


class InstallerTestCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.cursor = os.path.join(self.home, ".cursor")

    def read(self, *parts):
        with open(os.path.join(self.cursor, *parts), encoding="utf-8") as handle:
            return json.load(handle)

    def write(self, name, data):
        os.makedirs(self.cursor, exist_ok=True)
        with open(os.path.join(self.cursor, name), "w", encoding="utf-8") as handle:
            json.dump(data, handle)


class TestInstall(InstallerTestCase):
    def test_fresh_install_writes_all_pieces(self):
        installer.install_cursor(home=self.home)
        hooks = self.read("hooks.json")
        self.assertEqual(
            sorted(hooks["hooks"]),
            ["afterAgentResponse", "beforeSubmitPrompt", "sessionStart"],
        )
        for entries in hooks["hooks"].values():
            for entry in entries:
                self.assertIn(installer.BIN_PATH, entry["command"])
                self.assertNotIn("{{MEM0_OSS_BIN}}", entry["command"])
        mcp = self.read("mcp.json")
        self.assertEqual(
            mcp["mcpServers"]["mem0-oss"],
            {"command": installer.BIN_PATH, "args": ["mcp"]},
        )
        skill = os.path.join(self.cursor, "skills", "mem0-oss", "SKILL.md")
        with open(skill, encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn(installer.BIN_PATH, body)
        self.assertNotIn("../../bin/mem0-oss", body)
        self.assertTrue(
            os.path.isfile(os.path.join(self.cursor, "rules", "mem0-oss.mdc"))
        )

    def test_preserves_foreign_entries(self):
        self.write(
            "hooks.json",
            {
                "version": 1,
                "hooks": {
                    "beforeSubmitPrompt": [{"command": "other-tool check"}],
                    "beforeShellExecution": [{"command": "guard"}],
                },
            },
        )
        self.write("mcp.json", {"mcpServers": {"other": {"command": "x"}}})
        installer.install_cursor(home=self.home)
        hooks = self.read("hooks.json")
        commands = [e["command"] for e in hooks["hooks"]["beforeSubmitPrompt"]]
        self.assertIn("other-tool check", commands)
        self.assertEqual(hooks["hooks"]["beforeShellExecution"], [{"command": "guard"}])
        self.assertIn("other", self.read("mcp.json")["mcpServers"])

    def test_idempotent_no_duplicate_entries_no_second_backup(self):
        installer.install_cursor(home=self.home)
        installer.install_cursor(home=self.home)
        hooks = self.read("hooks.json")
        for entries in hooks["hooks"].values():
            self.assertEqual(len(entries), 1)
        # 第二次内容已是目标状态,不应产生新备份
        self.assertEqual(
            glob.glob(os.path.join(self.cursor, "hooks.json.bak.*")), []
        )

    def test_backup_created_when_existing_content_changes(self):
        self.write("hooks.json", {"version": 1, "hooks": {}})
        installer.install_cursor(home=self.home)
        self.assertEqual(
            len(glob.glob(os.path.join(self.cursor, "hooks.json.bak.*"))), 1
        )

    def test_corrupt_json_refused_and_untouched(self):
        os.makedirs(self.cursor, exist_ok=True)
        path = os.path.join(self.cursor, "hooks.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{broken")
        with self.assertRaises(installer.InstallError):
            installer.install_cursor(home=self.home)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "{broken")

    def test_non_dict_toplevel_refused_and_untouched(self):
        """能解析但顶层不是 object 的配置,要和解析失败一样被拒绝。

        断言的重点是"文件字节不变":一个改写完了才报错的拒绝不算拒绝。
        """
        for name in ("hooks.json", "mcp.json"):
            os.makedirs(self.cursor, exist_ok=True)
            path = os.path.join(self.cursor, name)
            raw = '["not", "an", "object"]'
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(raw)
            with self.assertRaises(installer.InstallError):
                installer.install_cursor(home=self.home)
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), raw, name)
            os.unlink(path)

    def test_non_list_event_value_refused_and_untouched(self):
        """非数组的事件值直接迭代会把 dict 拆成键名写回去,用户的 hook 就没了。"""
        for bad in ({"command": "other-tool"}, "other-tool"):
            path = os.path.join(self.cursor, "hooks.json")
            self.write("hooks.json", {"version": 1, "hooks": {"beforeSubmitPrompt": bad}})
            with open(path, encoding="utf-8") as handle:
                before = handle.read()
            with self.assertRaises(installer.InstallError):
                installer.install_cursor(home=self.home)
            with open(path, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), before, repr(bad))
            os.unlink(path)

    def test_non_dict_hooks_value_refused_and_untouched(self):
        path = os.path.join(self.cursor, "hooks.json")
        self.write("hooks.json", {"version": 1, "hooks": ["oops"]})
        with open(path, encoding="utf-8") as handle:
            before = handle.read()
        with self.assertRaises(installer.InstallError):
            installer.install_cursor(home=self.home)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before)

    def test_non_dict_mcp_servers_refused_and_untouched(self):
        path = os.path.join(self.cursor, "mcp.json")
        self.write("mcp.json", {"mcpServers": ["oops"]})
        with open(path, encoding="utf-8") as handle:
            before = handle.read()
        with self.assertRaises(installer.InstallError):
            installer.install_cursor(home=self.home)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before)

    def test_project_rule_target(self):
        project = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, project, True)
        installer.install_cursor(home=self.home, project_dir=project)
        self.assertTrue(
            os.path.isfile(os.path.join(project, ".cursor", "rules", "mem0-oss.mdc"))
        )

    def test_skill_backup_on_preexisting_foreign_file(self):
        """skills/mem0-oss/ 不是凭空冒出来的:一个从 mem0 云端迁移过来的用户
        完全可能已经在这个目录放了别的 SKILL.md。install 不能无声覆盖它。"""
        skill_dst = os.path.join(self.cursor, "skills", "mem0-oss", "SKILL.md")
        os.makedirs(os.path.dirname(skill_dst))
        with open(skill_dst, "w", encoding="utf-8") as handle:
            handle.write("这是用户自己的技能说明,不是我们的")
        installer.install_cursor(home=self.home)
        self.assertEqual(
            len(glob.glob(skill_dst + ".bak.*")), 1
        )
        with open(skill_dst, encoding="utf-8") as handle:
            self.assertIn(installer.BIN_PATH, handle.read())

    def test_rule_backup_on_preexisting_foreign_file(self):
        rule_dst = os.path.join(self.cursor, "rules", "mem0-oss.mdc")
        os.makedirs(os.path.dirname(rule_dst))
        with open(rule_dst, "w", encoding="utf-8") as handle:
            handle.write("不是我们写的内容")
        installer.install_cursor(home=self.home)
        self.assertEqual(len(glob.glob(rule_dst + ".bak.*")), 1)

    def test_install_uninstall_project_leaves_no_rule(self):
        project = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, project, True)
        installer.install_cursor(home=self.home, project_dir=project)
        rule_path = os.path.join(project, ".cursor", "rules", "mem0-oss.mdc")
        self.assertTrue(os.path.isfile(rule_path))
        installer.uninstall_cursor(home=self.home, project_dir=project)
        self.assertFalse(os.path.exists(rule_path))


class TestUninstall(InstallerTestCase):
    def test_removes_only_ours(self):
        self.write(
            "hooks.json",
            {"version": 1, "hooks": {"beforeSubmitPrompt": [{"command": "other-tool"}]}},
        )
        installer.install_cursor(home=self.home)
        installer.uninstall_cursor(home=self.home)
        hooks = self.read("hooks.json")
        self.assertEqual(
            hooks["hooks"]["beforeSubmitPrompt"], [{"command": "other-tool"}]
        )
        self.assertNotIn("afterAgentResponse", hooks["hooks"])
        self.assertNotIn("mem0-oss", self.read("mcp.json")["mcpServers"])
        self.assertFalse(
            os.path.exists(os.path.join(self.cursor, "skills", "mem0-oss", "SKILL.md"))
        )
        self.assertFalse(
            os.path.exists(os.path.join(self.cursor, "rules", "mem0-oss.mdc"))
        )

    def test_uninstall_on_clean_home_is_quiet(self):
        installer.uninstall_cursor(home=self.home)  # 不抛异常即通过

    def test_uninstall_preserves_foreign_sibling_in_skill_dir(self):
        """skills/mem0-oss/ 目录本身可能不是我们独占的:uninstall 只删自己
        写的那一个文件,绝不 rmtree 整个目录带走用户放在里面的东西。"""
        installer.install_cursor(home=self.home)
        sibling = os.path.join(self.cursor, "skills", "mem0-oss", "other.md")
        with open(sibling, "w", encoding="utf-8") as handle:
            handle.write("用户自己的文件")
        installer.uninstall_cursor(home=self.home)
        self.assertFalse(
            os.path.exists(os.path.join(self.cursor, "skills", "mem0-oss", "SKILL.md"))
        )
        self.assertTrue(os.path.exists(sibling))

    def test_non_dict_toplevel_refused_on_uninstall_and_untouched(self):
        """卸载路径同样会写回这个文件,所以形状不对时也必须拒绝、且不留痕迹。"""
        os.makedirs(self.cursor, exist_ok=True)
        path = os.path.join(self.cursor, "hooks.json")
        raw = '["not", "an", "object"]'
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(raw)
        with self.assertRaises(installer.InstallError):
            installer.uninstall_cursor(home=self.home)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), raw)
        self.assertEqual(glob.glob(path + ".bak.*"), [])

    def test_non_list_event_value_refused_on_uninstall_and_untouched(self):
        self.write("hooks.json", {"version": 1, "hooks": {"sessionStart": "other-tool"}})
        path = os.path.join(self.cursor, "hooks.json")
        with open(path, encoding="utf-8") as handle:
            before = handle.read()
        with self.assertRaises(installer.InstallError):
            installer.uninstall_cursor(home=self.home)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), before)
        self.assertEqual(glob.glob(path + ".bak.*"), [])

    def test_uninstall_on_foreign_only_hooks_is_noop(self):
        """从没装过 mem0-oss 的 hooks.json 上跑 uninstall 应该是纯粹的空操作:
        不重新格式化、不产生备份 —— 否则就是拿着"卸载"的名义动了不是自己的
        文件。"""
        self.write(
            "hooks.json",
            {"version": 1, "hooks": {"beforeSubmitPrompt": [{"command": "other-tool"}]}},
        )
        path = os.path.join(self.cursor, "hooks.json")
        with open(path, encoding="utf-8") as handle:
            before = handle.read()
        installer.uninstall_cursor(home=self.home)
        with open(path, encoding="utf-8") as handle:
            after = handle.read()
        self.assertEqual(before, after)
        self.assertEqual(glob.glob(path + ".bak.*"), [])


class TestCliWiring(unittest.TestCase):
    """验证 cli.py 的 install/uninstall 只是把参数转发给 installer 模块 ——
    真正的文件操作已经在上面两个 TestCase 里用真实临时 HOME 验证过了,这里
    只测转发本身(参数解析、--project 传递、enabled 豁免)。用 monkeypatch
    换掉 installer.install_cursor / installer.uninstall_cursor,绝不能让
    这里的任何一个用例调用到真正的实现,否则会写到调用者的真实 $HOME。
    """

    def setUp(self):
        self._saved_env = dict(os.environ)
        self.addCleanup(self._restore_env)
        # enabled 豁免的关键前提:install/uninstall 在没配置 MEM0_BASE_URL
        # 时也要能跑,因为它们本身就是用来完成配置的命令。
        os.environ.pop("MEM0_BASE_URL", None)
        self.calls = []

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._saved_env)

    def run_cli(self, argv):
        # _cmd_install/_cmd_uninstall print() 到 stdout;吞掉它,不让测试
        # 输出被命令本身的正常提示信息弄脏。
        with contextlib.redirect_stdout(io.StringIO()):
            return cli.main(argv)

    def _patch(self, name, result):
        original = getattr(installer, name)

        def fake(**kwargs):
            self.calls.append((name, kwargs))
            return result

        setattr(installer, name, fake)
        self.addCleanup(setattr, installer, name, original)

    def test_install_dispatches_to_installer_without_base_url(self):
        self._patch("install_cursor", ["写入 fake"])
        self.assertNotIn("MEM0_BASE_URL", os.environ)
        code = self.run_cli(["install", "cursor"])
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [("install_cursor", {"project_dir": None})])

    def test_install_project_flag_passes_cwd(self):
        self._patch("install_cursor", ["写入 fake"])
        code = self.run_cli(["install", "cursor", "--project"])
        self.assertEqual(code, 0)
        self.assertEqual(
            self.calls, [("install_cursor", {"project_dir": os.getcwd()})]
        )

    def test_install_error_surfaces_as_exit_1(self):
        def fake(**_kwargs):
            raise installer.InstallError("坏了")

        original = installer.install_cursor
        installer.install_cursor = fake
        self.addCleanup(setattr, installer, "install_cursor", original)
        code = self.run_cli(["install", "cursor"])
        self.assertEqual(code, 1)

    def test_uninstall_dispatches_to_installer_without_base_url(self):
        self._patch("uninstall_cursor", ["删除 fake"])
        self.assertNotIn("MEM0_BASE_URL", os.environ)
        code = self.run_cli(["uninstall", "cursor"])
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [("uninstall_cursor", {"project_dir": None})])

    def test_uninstall_project_flag_passes_cwd(self):
        self._patch("uninstall_cursor", ["删除 fake"])
        code = self.run_cli(["uninstall", "cursor", "--project"])
        self.assertEqual(code, 0)
        self.assertEqual(
            self.calls, [("uninstall_cursor", {"project_dir": os.getcwd()})]
        )
