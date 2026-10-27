"""pi(pi coding agent)扩展的适配层:纯翻译,不含业务逻辑。

pi 的扩展是进程内的 TypeScript 模块,不是 shell hook,所以 `pi/extensions/mem0-oss.ts`
只负责把 pi 的事件翻译成 JSON 喂给 `mem0-oss pi-hook <事件>`,真正的召回、脱敏、
暂存、配对、捕获全部复用 hooks 模块 —— 与 Claude Code / Codex / Cursor 语义一致。

与另外三个宿主的差异:
- pi 的 before_agent_start 既拿得到 prompt、又能往本轮注入一条自定义消息,
  所以和 Claude Code 一样做 hook 级自动召回(Cursor 做不到)。
- 轮结束的 assistant 文本由扩展从 agent_end 的消息列表里取出,走 `text` 字段。
- 作用域按 payload 里的 cwd 算,而不是按 python 进程自己的 cwd:扩展跑在 pi 的
  进程里,两者没有任何保证相等,而全链路 fail-open,对不上就是"永远召回不到东西"。
- 输出协议是单行 JSON:`{"text": "..."}`,没有可输出的东西时什么都不写。
"""

import dataclasses

from . import config as config_mod
from . import hooks

SOURCE = "pi"


def _session_id(payload):
    return payload.get("session_id") or ""


def _cwd(payload):
    cwd = payload.get("cwd")
    return cwd if isinstance(cwd, str) else ""


def _scoped_to_cwd(cfg, payload):
    """把 user_id 的作用域改成按 payload 里的 cwd 算。cwd 缺失时原样返回。"""
    cwd = _cwd(payload)
    if not cwd:
        return cfg
    return dataclasses.replace(
        cfg,
        user_id=config_mod.scoped_user_id(
            cfg.raw_user_id, cwd, cfg.scope, cfg.scope_key_format
        ),
    )


def _as_output(text):
    return {"text": text} if text else None


def session_start(cfg, payload):
    """认证探活 + 清理陈旧状态。警告文本交给扩展用 ctx.ui.notify 展示。"""
    scoped = _scoped_to_cwd(cfg, payload)
    return _as_output(
        hooks.session_start(scoped, {"session_id": _session_id(payload)}, source=SOURCE)
    )


def user_prompt(cfg, payload):
    """召回 + 暂存脱敏后的 prompt。返回的文本由扩展作为隐藏的自定义消息注入本轮。"""
    scoped = _scoped_to_cwd(cfg, payload)
    return _as_output(
        hooks.user_prompt(
            scoped,
            {
                "session_id": _session_id(payload),
                "prompt": payload.get("prompt") or "",
            },
            source=SOURCE,
        )
    )


def agent_end(cfg, payload):
    """轮结束捕获,等价于 Claude Code 的 Stop。纯副作用,无输出。"""
    hooks.stop(
        _scoped_to_cwd(cfg, payload),
        {
            "session_id": _session_id(payload),
            "last_assistant_message": payload.get("text") or "",
            "cwd": _cwd(payload),
        },
        source=SOURCE,
    )
    return None
