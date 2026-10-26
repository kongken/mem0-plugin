"""Cursor hooks 的适配层:纯翻译,不含业务逻辑。

Cursor 与 Claude Code 的三点差异:
- beforeSubmitPrompt 只能 allow/block,不能注入 —— 召回不走 hook,
  由自带 MCP server + alwaysApply rule 承担(spec §6.2)。
- 轮结束的 assistant 文本在 afterAgentResponse 的 text 字段。
- 会话标识叫 conversation_id,输出协议是 JSON 而非纯文本。

翻译成 Claude Code 形状的 payload 之后全部转交 hooks 模块,
脱敏、暂存、配对、过期、捕获的语义因此三宿主完全一致。
"""

import dataclasses

from . import config as config_mod
from . import hooks


def _conversation_id(payload):
    return payload.get("conversation_id") or ""


def _cwd(payload):
    roots = payload.get("workspace_roots")
    if isinstance(roots, list) and roots and isinstance(roots[0], str):
        return roots[0]
    return ""


def _scoped_to_workspace(cfg, payload):
    """把 user_id 的作用域改成按 payload 里的工作区根算,而不是按进程 cwd。

    project 作用域(默认)下 user_id = 原始 id + 由 cwd 推出的作用域哈希。Cursor 上捕获跑在
    它起的 hook 进程里、召回跑在 MCP server 进程里,两个进程的 cwd 没有任何
    保证相等 —— 用户级 MCP server 常常以家目录为 cwd。不相等就会写进一个
    作用域、从另一个作用域读,而全链路 fail-open:表现是"永远召回不到东西"
    且哪里都不报错。payload 里的 workspace_roots[0] 是 Cursor 给的工作区根,
    比 hook 进程的 cwd 可靠,拿它算作用域至少让捕获侧不再依赖 cwd。

    只在捕获路径上用:before_submit_prompt 强制关掉了 auto_recall,那条路只
    按 conversation_id 写 state 文件,根本不碰 user_id(见 hooks.user_prompt)。
    workspace_roots 缺失或形状不对时原样返回 cfg —— 它是 Cursor 各事件的公共
    字段,但假定它一定在、一定合法就是给这里新添一处脆弱点。
    """
    root = _cwd(payload)
    if not root:
        return cfg
    return dataclasses.replace(
        cfg,
        user_id=config_mod.scoped_user_id(
            cfg.raw_user_id, root, cfg.scope, cfg.scope_key_format
        ),
    )


def before_submit_prompt(cfg, payload):
    """恒放行。这个 hook 有权抹掉用户输入,记忆功能无权行使它。"""
    # auto_recall 强制关掉:这个事件不支持注入,检索只会白白拖慢用户输入
    hooks.user_prompt(
        dataclasses.replace(cfg, auto_recall=False),
        {
            "session_id": _conversation_id(payload),
            "prompt": payload.get("prompt") or "",
        },
    )
    return {"continue": True}


def after_agent_response(cfg, payload):
    """轮结束捕获,等价于 Claude Code 的 Stop。纯副作用,无输出。"""
    hooks.stop(
        _scoped_to_workspace(cfg, payload),
        {
            "session_id": _conversation_id(payload),
            "last_assistant_message": payload.get("text") or "",
            "cwd": _cwd(payload),
        },
        source="cursor",
    )
    return None


def session_start(cfg, payload):
    """探活 + 清理。警告经 additional_context 送出(该通道有已知丢失
    bug,警告是 best-effort,召回不依赖它)。"""
    text = hooks.session_start(cfg, {"session_id": _conversation_id(payload)})
    if text:
        return {"additional_context": text}
    return None
