"""两个方向的边界处理。

写入方向:凭据脱敏 —— 对话原文会被送往服务端,不该把密钥一起送过去。
读出方向:记忆内容是外部数据。任何能往记忆库写东西的人都拿到了一条
注入通道,所以召回的内容必须先降级成"数据"再进上下文。
"""

import json
import re

# 与 @amaster.ai/pi-memory-mem0 的 redactMemoryText 保持一致
_REDACTIONS = (
    (
        re.compile(
            r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
            re.IGNORECASE | re.DOTALL,
        ),
        "[REDACTED]",
    ),
    (re.compile(r"\b(Bearer)\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE), r"\1 [REDACTED]"),
    (
        re.compile(
            r"\b((?:[A-Za-z0-9]+[_-])*(?:api[_-]?key|apikey|token|password|secret))"
            r"[\"']?\s*[:=]\s*[\"']?[^\s\"',;]{6,}",
            re.IGNORECASE,
        ),
        r"\1=[REDACTED]",
    ),
)

# Tier 1:命中即整条 BLOCK。
# 只匹配下面这些具体标记 —— 不要泛化成"任意 XML/HTML 标签",
# 开发者的记忆里遍地是 JSX 和 HTML 片段,泛化等于把真上下文全误杀。
_CONTROL_PLANE = re.compile(
    r"</?system-reminder>"
    r"|</?function_calls>"
    r"|</?function_results>"
    r"|</?system>"
    r"|^\s{0,3}\[system\]"
    r"|^\s{0,3}(?:Human|Assistant)\s*:",
    re.IGNORECASE | re.MULTILINE,
)

_INSTRUCTION_OVERRIDE = re.compile(
    r"(?:ignore|disregard)\s+(?:all\s+|any\s+)?(?:the\s+)?"
    r"(?:previous|prior|above|preceding)\s+"
    r"(?:instructions?|prompts?|rules?|directions?|commands?)"
    r"|forget\s+everything\s+(?:you|above|before|prior)",
    re.IGNORECASE,
)

# 零宽与双向覆写字符,外加三个"看不见的换行":U+2028 LINE SEPARATOR、
# U+2029 PARAGRAPH SEPARATOR、U+0085 NEL。后三个既不被 json.dumps 转义
#(它只转义 <0x20),也不被 re.MULTILINE 的 ^ 当作行首,所以它们能同时
# 绕过引号包装和行首锚定的控制面模式。正常记忆里几乎不会出现,误杀成本为零。
_HIDDEN_CODEPOINTS = frozenset(
    list(range(0x200B, 0x2010))
    + list(range(0x202A, 0x202F))
    + [0x0085, 0x2028, 0x2029, 0xFEFF]
)

BLOCKED_PREFIX = "[BLOCKED UNTRUSTED MEMORY:"
UNTRUSTED_PREFIX = "[UNTRUSTED MEMORY DATA]"


def redact(text):
    """写入前抹掉凭据。"""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def scan(text):
    """返回命中的威胁类别列表,空列表表示干净。"""
    findings = []
    if _CONTROL_PLANE.search(text):
        findings.append("control-plane")
    if _INSTRUCTION_OVERRIDE.search(text):
        findings.append("instruction-override")
    if any(ord(ch) in _HIDDEN_CODEPOINTS for ch in text):
        findings.append("hidden-chars")
    return findings


def wrap_recalled(text, scan_enabled=True):
    """把一条召回的记忆降级成不可信数据。

    JSON 引号本身就是第一层防护:换行被转义掉,整条塌成一个字符串
    字面量,伪造的控制面标记没法再自成一行。
    """
    if scan_enabled:
        findings = scan(text)
        if findings:
            return "%s %s]" % (BLOCKED_PREFIX, ", ".join(findings))
    return "%s %s" % (UNTRUSTED_PREFIX, json.dumps(text, ensure_ascii=False))
