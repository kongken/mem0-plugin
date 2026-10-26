import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from mem0_oss import config


def _project_id(base, key):
    """按 spec 的公式手算期望值,不经过被测代码。"""
    return "%s:project:%s" % (base, hashlib.sha256(key.encode("utf-8")).hexdigest()[:12])


def make_git_repo(test, remotes=()):
    """建一个临时 git 仓库,remotes 是 (name, url) 序列。"""
    path = tempfile.mkdtemp()
    test.addCleanup(shutil.rmtree, path, True)
    subprocess.run(["git", "init", "-q", path], check=True, capture_output=True)
    for name, url in remotes:
        subprocess.run(
            ["git", "-C", path, "remote", "add", name, url], check=True, capture_output=True
        )
    return path


class TestNormalizeRemoteUrl(unittest.TestCase):
    def test_common_spellings_of_one_repo_collapse(self):
        for url in (
            "git@github.com:org/repo.git",
            "git@github.com:org/repo",
            "https://github.com/org/repo.git",
            "https://github.com/org/repo",
            "https://github.com/org/repo/",
            "http://github.com/org/repo.git",
            "ssh://git@github.com/org/repo.git",
            "ssh://git@github.com:22/org/repo.git",
            "git://github.com/org/repo.git",
            "https://user:token@github.com/org/repo.git",
            "https://GitHub.com/Org/Repo.git",
            "  git@github.com:org/repo.git\n",
        ):
            self.assertEqual(config.normalize_remote_url(url), "github.com/org/repo", url)

    def test_nested_groups_are_kept(self):
        self.assertEqual(
            config.normalize_remote_url("git@gitlab.com:group/sub/repo.git"),
            "gitlab.com/group/sub/repo",
        )

    def test_local_path_remote_and_file_url_agree(self):
        self.assertEqual(config.normalize_remote_url("/srv/git/repo.git"), "srv/git/repo")
        self.assertEqual(config.normalize_remote_url("file:///srv/git/repo.git"), "srv/git/repo")


class TestScopeKey(unittest.TestCase):
    def test_clones_at_different_paths_share_scope(self):
        a = make_git_repo(self, [("origin", "git@github.com:org/repo.git")])
        b = make_git_repo(self, [("origin", "https://github.com/org/repo")])
        self.assertEqual(
            config.scoped_user_id("alice", a, "project", "hash"),
            _project_id("alice", "github.com/org/repo"),
        )
        self.assertEqual(
            config.scoped_user_id("alice", b, "project", "hash"),
            _project_id("alice", "github.com/org/repo"),
        )

    def test_subdirectory_shares_scope_with_repo_root(self):
        repo = make_git_repo(self, [("origin", "git@github.com:org/repo.git")])
        sub = os.path.join(repo, "pkg", "inner")
        os.makedirs(sub)
        self.assertEqual(
            config.scoped_user_id("alice", sub, "project", "hash"),
            _project_id("alice", "github.com/org/repo"),
        )

    def test_different_remotes_give_different_scopes(self):
        a = make_git_repo(self, [("origin", "git@github.com:org/a.git")])
        b = make_git_repo(self, [("origin", "git@github.com:org/b.git")])
        self.assertNotEqual(
            config.scoped_user_id("alice", a, "project", "hash"),
            config.scoped_user_id("alice", b, "project", "hash"),
        )

    def test_origin_wins_over_other_remotes(self):
        repo = make_git_repo(
            self,
            [
                ("aaa", "git@github.com:someone/fork.git"),
                ("origin", "git@github.com:org/repo.git"),
            ],
        )
        self.assertEqual(config.scope_key(repo), "github.com/org/repo")

    def test_without_origin_first_remote_by_name_wins(self):
        repo = make_git_repo(
            self,
            [
                ("zeta", "git@github.com:org/zeta.git"),
                ("alpha", "git@github.com:org/alpha.git"),
            ],
        )
        self.assertEqual(config.scope_key(repo), "github.com/org/alpha")

    def test_repo_without_remote_falls_back_to_dir(self):
        repo = make_git_repo(self)
        self.assertEqual(config.scope_key(repo), os.path.realpath(repo))

    def test_non_git_dir_falls_back_to_dir(self):
        path = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, path, True)
        self.assertEqual(
            config.scoped_user_id("alice", path, "project", "hash"),
            _project_id("alice", os.path.realpath(path)),
        )

    def test_remote_in_global_gitconfig_is_ignored(self):
        path = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, path, True)
        gitconfig = os.path.join(path, "gitconfig")
        with open(gitconfig, "w") as f:
            f.write('[remote "origin"]\n\turl = git@github.com:org/global.git\n')
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": gitconfig}):
            self.assertEqual(config.scope_key(path), os.path.realpath(path))

    def test_missing_dir_falls_back_to_dir(self):
        self.assertEqual(config.scope_key("/nonexistent/mem0-proj"), "/nonexistent/mem0-proj")


class TestRawScopeKey(unittest.TestCase):
    def test_raw_puts_normalized_remote_in_user_id(self):
        repo = make_git_repo(self, [("origin", "https://user:token@github.com/Org/Repo.git")])
        self.assertEqual(
            config.scoped_user_id("alice", repo, "project", "raw"),
            "alice:project:github.com/org/repo",
        )

    def test_raw_without_remote_still_hashes_dir(self):
        path = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, path, True)
        self.assertEqual(
            config.scoped_user_id("alice", path, "project", "raw"),
            _project_id("alice", os.path.realpath(path)),
        )

    def test_exact_scope_ignores_raw(self):
        repo = make_git_repo(self, [("origin", "git@github.com:org/repo.git")])
        self.assertEqual(config.scoped_user_id("alice", repo, "exact", "raw"), "alice")

    def test_load_honours_mem0_scope_key_raw(self):
        repo = make_git_repo(self, [("origin", "git@github.com:org/repo.git")])
        cfg = config.load(
            env={"MEM0_BASE_URL": "http://x", "MEM0_USER_ID": "alice", "MEM0_SCOPE_KEY": "raw"},
            cwd=repo,
        )
        self.assertEqual(cfg.user_id, "alice:project:github.com/org/repo")

    def test_load_uses_raw_by_default_and_on_invalid_value(self):
        repo = make_git_repo(self, [("origin", "git@github.com:org/repo.git")])
        for env_extra in ({}, {"MEM0_SCOPE_KEY": "banana"}):
            env = {"MEM0_BASE_URL": "http://x", "MEM0_USER_ID": "alice"}
            env.update(env_extra)
            cfg = config.load(env=env, cwd=repo)
            self.assertEqual(cfg.user_id, "alice:project:github.com/org/repo", repr(env_extra))

    def test_load_honours_mem0_scope_key_hash(self):
        repo = make_git_repo(self, [("origin", "git@github.com:org/repo.git")])
        cfg = config.load(
            env={"MEM0_BASE_URL": "http://x", "MEM0_USER_ID": "alice", "MEM0_SCOPE_KEY": "hash"},
            cwd=repo,
        )
        self.assertEqual(cfg.user_id, _project_id("alice", "github.com/org/repo"))


class TestScopedUserId(unittest.TestCase):
    def test_exact_scope_returns_base_unchanged(self):
        self.assertEqual(config.scoped_user_id("alice", "/tmp/proj", "exact", "hash"), "alice")

    def test_project_scope_appends_12_hex_chars(self):
        got = config.scoped_user_id("alice", "/tmp/proj", "project", "hash")
        self.assertTrue(got.startswith("alice:project:"))
        suffix = got.split(":project:")[1]
        self.assertEqual(len(suffix), 12)
        self.assertRegex(suffix, r"^[0-9a-f]{12}$")

    def test_project_scope_is_stable_across_calls(self):
        a = config.scoped_user_id("alice", "/tmp/proj", "project", "hash")
        b = config.scoped_user_id("alice", "/tmp/proj", "project", "hash")
        self.assertEqual(a, b)

    def test_different_dirs_give_different_scopes(self):
        a = config.scoped_user_id("alice", "/tmp/proj-a", "project", "hash")
        b = config.scoped_user_id("alice", "/tmp/proj-b", "project", "hash")
        self.assertNotEqual(a, b)


class TestLoad(unittest.TestCase):
    def test_disabled_when_base_url_missing(self):
        cfg = config.load(env={}, cwd="/tmp/proj")
        self.assertFalse(cfg.enabled)

    def test_enabled_with_base_url(self):
        cfg = config.load(env={"MEM0_BASE_URL": "http://localhost:8888"}, cwd="/tmp/proj")
        self.assertTrue(cfg.enabled)

    def test_base_url_trailing_slash_is_stripped(self):
        cfg = config.load(env={"MEM0_BASE_URL": "http://localhost:8888/"}, cwd="/tmp/proj")
        self.assertEqual(cfg.base_url, "http://localhost:8888")

    def test_defaults(self):
        cfg = config.load(env={"MEM0_BASE_URL": "http://x", "USER": "bob"}, cwd="/tmp/p")
        self.assertEqual(cfg.raw_user_id, "bob")
        self.assertEqual(cfg.scope, "project")
        self.assertEqual(cfg.top_k, 5)
        self.assertTrue(cfg.auto_recall)
        self.assertTrue(cfg.auto_capture)
        self.assertEqual(cfg.recall_frequency, "user-input")
        self.assertEqual(cfg.recall_timeout_ms, 3000)
        self.assertEqual(cfg.request_timeout_ms, 30000)
        self.assertEqual(cfg.min_prompt_chars, 20)
        self.assertEqual(cfg.threat_scan, "strict")
        self.assertFalse(cfg.debug)

    def test_user_id_falls_back_to_default_user(self):
        cfg = config.load(env={"MEM0_BASE_URL": "http://x"}, cwd="/tmp/p")
        self.assertEqual(cfg.raw_user_id, "default-user")

    def test_user_id_is_scoped(self):
        cfg = config.load(
            env={"MEM0_BASE_URL": "http://x", "MEM0_USER_ID": "alice"}, cwd="/tmp/p"
        )
        self.assertTrue(cfg.user_id.startswith("alice:project:"))

    def test_exact_scope_leaves_user_id_alone(self):
        cfg = config.load(
            env={
                "MEM0_BASE_URL": "http://x",
                "MEM0_USER_ID": "alice",
                "MEM0_USER_ID_SCOPE": "exact",
            },
            cwd="/tmp/p",
        )
        self.assertEqual(cfg.user_id, "alice")

    def test_bool_accepts_common_truthy_spellings(self):
        for raw in ("1", "true", "TRUE", "yes", "on"):
            cfg = config.load(env={"MEM0_BASE_URL": "http://x", "MEM0_DEBUG": raw}, cwd="/tmp/p")
            self.assertTrue(cfg.debug, raw)

    def test_bool_treats_anything_else_as_false(self):
        for raw in ("0", "false", "no", "off", "", "banana"):
            cfg = config.load(env={"MEM0_BASE_URL": "http://x", "MEM0_DEBUG": raw}, cwd="/tmp/p")
            self.assertFalse(cfg.debug, raw)

    def test_invalid_top_k_falls_back_to_default(self):
        for raw in ("0", "-3", "abc", ""):
            cfg = config.load(env={"MEM0_BASE_URL": "http://x", "MEM0_TOP_K": raw}, cwd="/tmp/p")
            self.assertEqual(cfg.top_k, 5, raw)

    def test_invalid_scope_falls_back_to_project(self):
        cfg = config.load(
            env={"MEM0_BASE_URL": "http://x", "MEM0_USER_ID_SCOPE": "banana"}, cwd="/tmp/p"
        )
        self.assertEqual(cfg.scope, "project")

    def test_invalid_recall_frequency_falls_back(self):
        cfg = config.load(
            env={"MEM0_BASE_URL": "http://x", "MEM0_RECALL_FREQUENCY": "hourly"}, cwd="/tmp/p"
        )
        self.assertEqual(cfg.recall_frequency, "user-input")

    def test_default_state_dir_is_host_agnostic(self):
        cfg = config.load(env={"MEM0_BASE_URL": "http://x"}, cwd="/tmp")
        self.assertEqual(
            cfg.state_dir, os.path.join(os.path.expanduser("~"), ".mem0-oss")
        )


if __name__ == "__main__":
    unittest.main()
