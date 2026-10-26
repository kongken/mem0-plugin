"""hooks 与 slash command 共用的入口。

两类子命令的退出码语义不同:
- hook 恒返回 0。UserPromptSubmit 的非零会抹掉用户 prompt,
  Stop 的非零会干扰会话结束 —— 记忆功能不该有权做这些事。
- 交互子命令按正常 CLI 语义,失败返回 1。
"""

import argparse
import json
import os
import sys

from . import client as client_mod
from . import config as config_mod
from . import cursor_hooks, hooks, installer, mcp_server, privacy

CLOUD_KEY_PREFIX = "m0-"
OSS_KEY_PREFIX = "m0sk_"

_HOST_SOURCES = {"claude": "claude-code", "codex": "codex"}

_HOOK_HANDLERS = {
    "session-start": hooks.session_start,
    "user-prompt": hooks.user_prompt,
    "stop": hooks.stop,
}

_CURSOR_HANDLERS = {
    "before-submit-prompt": cursor_hooks.before_submit_prompt,
    "after-agent-response": cursor_hooks.after_agent_response,
    "session-start": cursor_hooks.session_start,
}

_ALWAYS_ALLOW = {"continue": True}


def _log_crash_safely(message):
    """诊断本身不能再抛 —— config 加载也可能失败,而 hook 路径上逃出去的
    任何异常都会伤到用户会话(UserPromptSubmit 上会抹掉刚敲的 prompt)。
    """
    try:
        config_mod.debug_log(config_mod.load(), message)
    except BaseException:  # noqa: BLE001
        pass


def _run_cursor_hook(event):
    """Cursor 侧的 fail-open。before-submit-prompt 恒放行:没有输出时
    Cursor 的默认行为未必是放行,所以要显式说。"""
    result = None
    try:
        payload = _read_stdin_json()
        if payload is not None:
            result = _CURSOR_HANDLERS[event](config_mod.load(), payload)
    except BaseException as exc:  # noqa: BLE001
        _log_crash_safely("cursor hook %s crashed: %s" % (event, exc))
        result = None
    if result is None and event == "before-submit-prompt":
        result = _ALWAYS_ALLOW
    if result is not None:
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0


def _read_stdin_json():
    try:
        raw = sys.stdin.read()
    except (OSError, UnicodeDecodeError):
        return None
    if not raw.strip():
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _run_hook(event, host="claude"):
    """任何失败都收敛成 exit 0 且不输出。

    接的是 BaseException 而不是 Exception:SystemExit 与 KeyboardInterrupt
    都继承自 BaseException,绕得过 except Exception,而 UserPromptSubmit 上的
    非零退出会抹掉用户刚敲的那条 prompt。hook 进程本身有 ≤5s 的超时上限兜底,
    吞掉中断不会造成挂死。
    """
    try:
        payload = _read_stdin_json()
        if payload is None:
            return 0
        cfg = config_mod.load()
        text = _HOOK_HANDLERS[event](
            cfg, payload, source=_HOST_SOURCES.get(host, "claude-code")
        )
        if text:
            sys.stdout.write(text + "\n")
    except BaseException as exc:  # noqa: BLE001
        _log_crash_safely("hook %s (host=%s) crashed: %s" % (event, host, exc))
    return 0


def _cmd_status(cfg, _args):
    print("mem0-oss status")
    print("  base_url            %s" % (cfg.base_url or "(未设置 —— 插件已停用)"))
    print("  user_id (生效值)     %s" % cfg.user_id)
    print("  user_id (原始值)     %s" % cfg.raw_user_id)
    print("  scope               %s" % cfg.scope)
    if cfg.scope == "project":
        print("  scope_key           %s" % config_mod.scope_key(os.getcwd()))
    print("  agent_id            %s" % (cfg.agent_id or "(无)"))
    print("  top_k               %d" % cfg.top_k)
    print("  auto_recall         %s (%s)" % (cfg.auto_recall, cfg.recall_frequency))
    print("  auto_capture        %s" % cfg.auto_capture)
    print("  threat_scan         %s" % cfg.threat_scan)
    print("  state_dir           %s" % cfg.state_dir)

    if not cfg.enabled:
        print("\n错误:MEM0_BASE_URL 未设置,所有 hook 都会直接退出。")
        return 1

    if cfg.api_key.startswith(CLOUD_KEY_PREFIX):
        print(
            "\n错误:MEM0_API_KEY 看起来是 Mem0 Platform 的云端密钥(m0- 前缀)。"
            "\n      OSS 服务端签发的是 %s 前缀的密钥,云端密钥会被拒绝(401),"
            "\n      而 401 是 fail-open 的 —— 表现为装好了但什么都没召回。" % OSS_KEY_PREFIX
        )
        return 1

    api = client_mod.Client(cfg.base_url, cfg.api_key, cfg.request_timeout_ms)
    try:
        memories = api.get_all(cfg.user_id, cfg.agent_id)
    except client_mod.Mem0Error as exc:
        print("\n错误:连接服务端失败 —— %s" % exc)
        return 1

    print("\n连接正常,当前作用域下有 %d 条记忆。" % len(memories))
    return 0


def _cmd_search(cfg, args):
    api = client_mod.Client(cfg.base_url, cfg.api_key, cfg.request_timeout_ms)
    # 检索的 query 也要脱敏:它会被服务端交给它配置的 embedding 提供方,
    # 粘错一次凭据就等于把凭据发出机器。与 hooks.user_prompt、MCP 的
    # _tool_search 完全同一口径 —— 这条 CLI 现在也是 skill 教 Codex/Cursor
    # 的 agent 直接 shell 调用的入口,不能是三条路里唯一漏的那条。
    try:
        memories = api.search(
            privacy.redact(args.query), cfg.user_id, cfg.agent_id, cfg.top_k
        )
    except client_mod.Mem0Error as exc:
        print("搜索失败:%s" % exc)
        return 1
    if not memories:
        print("没有匹配的记忆。")
        return 0
    for item in memories:
        print(hooks.format_entry(item))
    return 0


def _cmd_add(cfg, args):
    api = client_mod.Client(cfg.base_url, cfg.api_key, cfg.request_timeout_ms)
    try:
        api.add(
            [{"role": "user", "content": privacy.redact(args.text)}],
            cfg.user_id,
            agent_id=cfg.agent_id,
            metadata={"source": "mem0-oss-cli"},
        )
    except client_mod.Mem0Error as exc:
        print("写入失败:%s" % exc)
        return 1
    print("已写入。")
    return 0


def _cmd_profile(cfg, _args):
    api = client_mod.Client(cfg.base_url, cfg.api_key, cfg.request_timeout_ms)
    try:
        memories = api.get_all(cfg.user_id, cfg.agent_id)
    except client_mod.Mem0Error as exc:
        print("读取失败:%s" % exc)
        return 1
    if not memories:
        print("当前作用域下还没有记忆。")
        return 0
    for item in memories:
        print(hooks.format_entry(item))
    return 0


def _cmd_delete(cfg, args):
    api = client_mod.Client(cfg.base_url, cfg.api_key, cfg.request_timeout_ms)
    try:
        api.delete(args.memory_id)
    except client_mod.Mem0Error as exc:
        print("删除失败:%s" % exc)
        return 1
    print("已删除 %s。" % args.memory_id)
    return 0


def _cmd_install(cfg, args):
    try:
        actions = installer.install_cursor(
            project_dir=os.getcwd() if args.project else None
        )
    except installer.InstallError as exc:
        print("安装失败:%s" % exc)
        return 1
    for line in actions:
        print(line)
    return 0


def _cmd_uninstall(cfg, args):
    try:
        actions = installer.uninstall_cursor(
            project_dir=os.getcwd() if args.project else None
        )
    except installer.InstallError as exc:
        print("卸载失败:%s" % exc)
        return 1
    for line in actions:
        print(line)
    print("已卸载。")
    return 0


def _build_parser():
    parser = argparse.ArgumentParser(prog="mem0-oss", description="self-hosted mem0 的 Claude Code 集成")
    subparsers = parser.add_subparsers(dest="command")

    hook_parser = subparsers.add_parser("hook", help="供 Claude Code hooks 调用")
    hook_parser.add_argument("event", choices=sorted(_HOOK_HANDLERS))
    hook_parser.add_argument(
        "--host", choices=sorted(_HOST_SOURCES), default="claude",
        help="调用方宿主,决定捕获记忆的 metadata.source",
    )

    cursor_parser = subparsers.add_parser("cursor-hook", help="供 Cursor hooks 调用")
    cursor_parser.add_argument("event", choices=sorted(_CURSOR_HANDLERS))

    subparsers.add_parser("status", help="配置与连通性体检")
    subparsers.add_parser("profile", help="列出当前作用域的全部记忆")
    subparsers.add_parser("mcp", help="起 stdio MCP server(供宿主 MCP 配置调用)")

    search_parser = subparsers.add_parser("search", help="语义搜索")
    search_parser.add_argument("query", nargs="+")

    add_parser = subparsers.add_parser("add", help="手动写入一条记忆")
    add_parser.add_argument("text", nargs="+")

    delete_parser = subparsers.add_parser("delete", help="按 id 删除")
    delete_parser.add_argument("memory_id")

    install_parser = subparsers.add_parser(
        "install", help="把 hooks/MCP/skill/rule 写进宿主配置(支持:cursor)"
    )
    install_parser.add_argument("host", choices=("cursor",))
    install_parser.add_argument(
        "--project", action="store_true",
        help="rule 写入当前项目的 .cursor/rules/ 而非用户级",
    )

    uninstall_parser = subparsers.add_parser(
        "uninstall", help="摘除 install 写入的条目(支持:cursor)"
    )
    uninstall_parser.add_argument("host", choices=("cursor",))
    uninstall_parser.add_argument(
        "--project", action="store_true",
        help="连同当前项目 .cursor/rules/ 里 install --project 写入的 rule 一起摘除",
    )

    return parser


_COMMANDS = {
    "status": _cmd_status,
    "search": _cmd_search,
    "add": _cmd_add,
    "profile": _cmd_profile,
    "delete": _cmd_delete,
    "install": _cmd_install,
    "uninstall": _cmd_uninstall,
}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        # argparse 用 SystemExit(2) 报参数错误,而 SystemExit 继承自 BaseException,
        # 绕得过 _run_hook 的 except Exception。hook 路径必须压成 0:
        # UserPromptSubmit 上的非零退出会抹掉用户刚敲的那条 prompt,
        # 一个 hooks.json 里的拼写错误不该有这种后果。
        if argv and argv[0] in ("hook", "cursor-hook"):
            if argv[0] == "cursor-hook":
                # 连事件名都解析不出来,只能按最保守口径恒放行
                sys.stdout.write(json.dumps(_ALWAYS_ALLOW) + "\n")
            return 0
        raise

    if args.command is None:
        parser.print_help()
        return 1

    if args.command == "hook":
        return _run_hook(args.event, args.host)

    if args.command == "cursor-hook":
        return _run_cursor_hook(args.event)

    # argparse 的 nargs="+" 收成列表,拼回原始文本
    if hasattr(args, "query"):
        args.query = " ".join(args.query)
    if hasattr(args, "text"):
        args.text = " ".join(args.text)

    cfg = config_mod.load()

    if args.command == "mcp":
        return mcp_server.serve(cfg)

    if not cfg.enabled and args.command not in ("status", "install", "uninstall"):
        print("MEM0_BASE_URL 未设置。先跑 mem0-oss status 看配置。")
        return 1

    return _COMMANDS[args.command](cfg, args)
