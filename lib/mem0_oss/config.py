"""环境变量解析与 user_id 作用域计算。

所有配置都从环境变量读取 —— Claude Code 没有插件级配置文件,
使用方把这些放进 ~/.claude/settings.json 的 env 块。
"""

import hashlib
import os
import re
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass

_TRUTHY = frozenset(("1", "true", "yes", "on"))
# scp 式 remote:[user@]host:path
_SCP_LIKE = re.compile(r"^(?:[^@/]+@)?([^:/]+):(.+)$")
# 每次 hook 都要跑一次 git;卡住时宁可退回目录哈希,也不能拖住用户输入
GIT_TIMEOUT_S = 2

DEFAULT_TOP_K = 5
DEFAULT_RECALL_TIMEOUT_MS = 3000
DEFAULT_REQUEST_TIMEOUT_MS = 30000
DEFAULT_MIN_PROMPT_CHARS = 20
DEFAULT_USER_ID = "default-user"


@dataclass(frozen=True)
class Config:
    base_url: str
    api_key: str
    raw_user_id: str
    user_id: str
    agent_id: str
    scope: str
    scope_key_format: str
    top_k: int
    auto_recall: bool
    auto_capture: bool
    recall_frequency: str
    recall_timeout_ms: int
    request_timeout_ms: int
    min_prompt_chars: int
    threat_scan: str
    state_dir: str
    debug: bool

    @property
    def enabled(self):
        """base_url 未设 = 整个 plugin 停用。这是总开关。"""
        return bool(self.base_url)


def _str(env, name, default=""):
    value = env.get(name)
    return value.strip() if value and value.strip() else default


def _bool(env, name, default):
    value = env.get(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in _TRUTHY


def _int(env, name, default, minimum=1):
    value = env.get(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value.strip())
    except ValueError:
        return default
    return parsed if parsed >= minimum else default


def _choice(env, name, allowed, default):
    value = _str(env, name, default).lower()
    return value if value in allowed else default


def normalize_remote_url(url):
    """把同一仓库的各种 remote 写法归一成 host/path。

    scp 式、https、ssh、git:// 都算同一个仓库:去掉协议、用户名/凭据、端口、
    结尾的 .git 和斜杠,整体转小写(GitHub/GitLab 的路径大小写不敏感)。
    """
    url = url.strip()
    if "://" in url:
        parts = urllib.parse.urlsplit(url)
        host, path = parts.hostname or "", parts.path
    else:
        match = _SCP_LIKE.match(url)
        host, path = match.groups() if match else ("", url)
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return ("%s/%s" % (host, path) if host else path).lower()


def _git_remote_url(cwd):
    """cwd 所在仓库的 remote url:优先 origin,否则按名字排第一个。拿不到返回空串。

    --local 只读仓库自己的 config,不会把 ~/.gitconfig 里的东西当成 remote;
    stdin 显式给 DEVNULL —— MCP server 的 stdin 是协议流,不能被子进程继承。
    """
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "config", "--local", "--get-regexp", r"^remote\..*\.url$"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_S,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    urls = {}
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        urls[key[len("remote.") : -len(".url")]] = value
    if "origin" in urls:
        return urls["origin"]
    return urls[min(urls)] if urls else ""


def _remote_key(cwd):
    remote = _git_remote_url(cwd)
    return normalize_remote_url(remote) if remote else ""


def scope_key(cwd):
    """project 作用域的 key:归一化的 git remote url,拿不到时退回 realpath(cwd)。

    用 remote 而不是目录,同一仓库 clone 在不同路径、开 worktree、从子目录启动
    都落在同一个作用域。非 git 目录或没有 remote 时行为与旧版一致。
    """
    return _remote_key(cwd) or os.path.realpath(cwd)


def scoped_user_id(base_user_id, cwd, scope, key_format):
    """project 作用域下把 scope_key 拼进 user_id,使各项目记忆互不可见。

    key_format="raw"(默认)直接拼归一化的 remote url,服务端上一眼能看出是
    哪个仓库;没有 remote 时仍拼目录哈希 —— 原样放本地路径会把用户名、目录
    结构发到服务端,换个路径还会变,没有意义。key_format="hash" 一律拼
    sha256 前 12 位,与 @amaster.ai/pi-memory-mem0 的 scopeMemoryUserId 形状
    一致,便于两边指向同一台服务端时对照排查。
    """
    if scope == "exact":
        return base_user_id
    remote_key = _remote_key(cwd)
    if key_format == "raw" and remote_key:
        return "%s:project:%s" % (base_user_id, remote_key)
    key = remote_key or os.path.realpath(cwd)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return "%s:project:%s" % (base_user_id, digest[:12])


def load(env=None, cwd=None):
    env = os.environ if env is None else env
    cwd = os.getcwd() if cwd is None else cwd

    raw_user_id = _str(env, "MEM0_USER_ID") or _str(env, "USER") or DEFAULT_USER_ID
    scope = _choice(env, "MEM0_USER_ID_SCOPE", ("project", "exact"), "project")
    scope_key_format = _choice(env, "MEM0_SCOPE_KEY", ("hash", "raw"), "raw")
    state_dir = _str(
        env, "MEM0_STATE_DIR", os.path.join(os.path.expanduser("~"), ".mem0-oss")
    )

    return Config(
        base_url=_str(env, "MEM0_BASE_URL").rstrip("/"),
        api_key=_str(env, "MEM0_API_KEY"),
        raw_user_id=raw_user_id,
        user_id=scoped_user_id(raw_user_id, cwd, scope, scope_key_format),
        agent_id=_str(env, "MEM0_AGENT_ID"),
        scope=scope,
        scope_key_format=scope_key_format,
        top_k=_int(env, "MEM0_TOP_K", DEFAULT_TOP_K),
        auto_recall=_bool(env, "MEM0_AUTO_RECALL", True),
        auto_capture=_bool(env, "MEM0_AUTO_CAPTURE", True),
        recall_frequency=_choice(
            env, "MEM0_RECALL_FREQUENCY", ("user-input", "session"), "user-input"
        ),
        recall_timeout_ms=_int(env, "MEM0_RECALL_TIMEOUT_MS", DEFAULT_RECALL_TIMEOUT_MS),
        request_timeout_ms=_int(env, "MEM0_REQUEST_TIMEOUT_MS", DEFAULT_REQUEST_TIMEOUT_MS),
        min_prompt_chars=_int(env, "MEM0_MIN_PROMPT_CHARS", DEFAULT_MIN_PROMPT_CHARS, minimum=0),
        threat_scan=_choice(env, "MEM0_THREAT_SCAN", ("strict", "off"), "strict"),
        state_dir=state_dir,
        debug=_bool(env, "MEM0_DEBUG", False),
    )


def debug_log(cfg, message):
    """诊断只在 MEM0_DEBUG=1 时出现,且一律走 stderr。

    stdout 是 hook 注入上下文的通道,任何诊断混进去都会被模型读到。
    """
    if cfg.debug:
        sys.stderr.write("[mem0-oss] %s\n" % message)
