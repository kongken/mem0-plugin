"""每会话一个小状态文件。

存在的理由只有一个:UserPromptSubmit 拿得到 prompt,Stop 拿得到
last_assistant_message,但两者是两个独立进程 —— 要把一轮对话拼起来,
中间得有个地方放 prompt。顺带承载 recalled / auth_warned 两个标记。
"""

import json
import os
import re
import tempfile
import time

from . import config as config_mod

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")

MAX_AGE_SECONDS = 7 * 24 * 3600
PROMPT_MAX_AGE_SECONDS = 3600


def safe_session_id(session_id):
    """session_id 来自 hook 的 stdin,直接当文件名会有路径穿越风险。"""
    if not session_id:
        return "default"
    cleaned = _UNSAFE.sub("_", str(session_id))[:128]
    return cleaned or "default"


def _sessions_dir(cfg):
    return os.path.join(cfg.state_dir, "sessions")


def path_for(cfg, session_id):
    return os.path.join(_sessions_dir(cfg), safe_session_id(session_id) + ".json")


def load(cfg, session_id):
    try:
        with open(path_for(cfg, session_id), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_atomic(path, data):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory, prefix=".tmp-", delete=False
    )
    try:
        json.dump(data, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(handle.name, path)
    except Exception:
        handle.close()
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def update(cfg, session_id, **fields):
    """增量更新会话状态。写失败一律吞掉:这个模块跑在 hook 子进程里,持久化不了状态
    是可以接受的降级,让异常冒到 hook 进程里不是。返回值始终是合并后的内存状态,
    即使没能落盘 —— 调用方拿到的语义保持一致。

    已知限制:load→修改→写入 不是原子的,两个 hook 进程近乎同时写同一个 session 时
    可能丢掉一个布尔标记(recalled / auth_warned)。实际流程里两个 hook 顺序触发,
    窗口极窄,丢一个标记的后果也只是多召回一次,不值得引入文件锁。
    """
    data = load(cfg, session_id)
    data.update(fields)
    try:
        _write_atomic(path_for(cfg, session_id), data)
    except Exception as exc:  # noqa: BLE001
        config_mod.debug_log(cfg, "state write failed: %s" % exc)
    return data


def clear_prompt(cfg, session_id):
    data = load(cfg, session_id)
    if not data:
        return
    data.pop("prompt", None)
    data.pop("prompt_ts", None)
    try:
        _write_atomic(path_for(cfg, session_id), data)
    except Exception as exc:  # noqa: BLE001
        config_mod.debug_log(cfg, "state write failed: %s" % exc)


def cleanup(cfg, max_age_seconds=MAX_AGE_SECONDS):
    """删掉陈旧的会话文件,返回删除数量。失败一律吞掉。"""
    directory = _sessions_dir(cfg)
    cutoff = time.time() - max_age_seconds
    removed = 0
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(directory, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.unlink(path)
                removed += 1
        except OSError:
            continue
    return removed
