#!/usr/bin/env python3
"""脱离 hook 进程独立跑的一次写入。

由 hooks.stop 以子进程方式拉起,输入从 stdin 读 JSON。走 stdin 而不是
argv,是因为对话原文进 argv 会出现在 ps 输出里。

无论发生什么都 exit 0:这是个后台进程,失败没人在意,更不该留下痕迹。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mem0_oss import client as client_mod  # noqa: E402


def main():
    # 父进程显式按 UTF-8 编码写入,这里就必须显式按 UTF-8 解码:json.load(sys.stdin)
    # 走的是 locale 编码,LANG=C 下每一条非 ASCII 的捕获都会被静默丢掉。
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except (ValueError, OSError, UnicodeDecodeError) as exc:
        sys.stderr.write("[mem0-oss] capture input unreadable: %s\n" % exc)
        return

    api = client_mod.Client(
        payload["base_url"], payload.get("api_key", ""), payload.get("timeout_ms", 30000)
    )
    try:
        api.add(
            payload["messages"],
            payload["user_id"],
            agent_id=payload.get("agent_id", ""),
            metadata=payload.get("metadata"),
        )
    except client_mod.Mem0Error as exc:
        sys.stderr.write("[mem0-oss] capture failed: %s\n" % exc)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("[mem0-oss] capture crashed: %s\n" % exc)
    sys.exit(0)
