"""往 Cursor 的用户级配置安装/卸载 mem0-oss。

原则:
- 只动自己的条目(识别标记:command 里同时含 mem0-oss 与 cursor-hook)
- 改既有文件前先备份(<文件名>.bak.<UTC 时间戳>);结果与现状相同则不写不备份
- 既有文件解析失败就报 InstallError,绝不覆盖用户写坏的配置。**能解析但形状不对
  的一样拒绝**:形状不对的地方走下去只会把用户的配置改坏(见 _load_json_object
  与 _event_entries 的注释),而"写坏的配置绝不覆盖"这条承诺不区分坏法。
"""

import json
import os
import shutil
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BIN_PATH = os.path.join(_ROOT, "bin", "mem0-oss")

PLACEHOLDER = "{{MEM0_OSS_BIN}}"

RULE_NOTICE = (
    "注意:Cursor 用户级 rules 目录是否生效未经官方文档确认;"
    "若召回指引没出现,改跑 mem0-oss install cursor --project 写入项目级 .cursor/rules/。"
)


class InstallError(Exception):
    pass


def _is_ours(entry):
    command = entry.get("command", "") if isinstance(entry, dict) else ""
    return "mem0-oss" in command and "cursor-hook" in command


def _load_json(path, default):
    """返回 (数据, 文件是否已存在)。解析失败拒绝继续。"""
    if not os.path.exists(path):
        return default, False
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle), True
    except ValueError as exc:
        raise InstallError("%s 不是合法 JSON,拒绝改写:%s" % (path, exc))
    except OSError as exc:
        raise InstallError("读取 %s 失败:%s" % (path, exc))


def _load_json_object(path, default):
    """_load_json 之上加一层形状校验:顶层必须是 JSON object。

    顶层是数组或标量时,下面每一处 data.get(...) 都会炸成裸 AttributeError
    (install 与 uninstall 都会),用户只看到一段 traceback。拒绝掉,理由和
    拒绝解析失败的文件完全一样:我们看不懂这个文件,就不该动它。
    """
    data, had = _load_json(path, default)
    if not isinstance(data, dict):
        raise InstallError(
            "%s 的顶层不是 JSON object(实际是 %s),拒绝改写"
            % (path, type(data).__name__)
        )
    return data, had


def _require_mapping(path, key, data):
    """取 data[key] 这一层 map。缺省返回空 map,存在但形状不对则拒绝。

    不能沿用"形状不对就当空 map"的写法:那等于把用户写坏的那一段默默丢掉
    并覆盖回去 —— 备份里还有,但活文件上的东西已经没了。
    """
    if key not in data:
        return {}
    value = data[key]
    if not isinstance(value, dict):
        raise InstallError(
            "%s 的 %s 不是 JSON object(实际是 %s),拒绝改写"
            % (path, key, type(value).__name__)
        )
    return value


def _event_entries(path, event, value):
    """取某个 hook 事件下的条目数组。不是数组就拒绝。

    这里是最容易把用户配置改坏的一处:非数组的值照样能被 for 迭代 ——
    dict 迭出键名、字符串迭出单个字符 —— 过一遍过滤再写回去,用户原来的
    hook 条目就从活文件里消失了(只剩备份)。宁可报错不装。
    """
    if not isinstance(value, list):
        raise InstallError(
            "%s 的 hooks.%s 不是数组(实际是 %s),拒绝改写"
            % (path, event, type(value).__name__)
        )
    return value


def _write_json(path, data, had_file, actions):
    new_text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if had_file:
        with open(path, "r", encoding="utf-8") as handle:
            if handle.read() == new_text:
                actions.append("跳过 %s(已是目标状态)" % path)
                return
        backup = "%s.bak.%s" % (path, time.strftime("%Y%m%d-%H%M%S", time.gmtime()))
        shutil.copy2(path, backup)
        actions.append("备份 %s -> %s" % (path, backup))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=os.path.dirname(path), prefix=".tmp-", delete=False
    )
    try:
        handle.write(new_text)
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
    actions.append("写入 %s" % path)


def _write_text(path, text, actions):
    """跟 _write_json 同一套契约(序列化后比对/先备份/原子替换),供非 JSON 的
    payload(skill、rule)复用 —— 这样这两类文件也享有跟 hooks.json/mcp.json
    一样的"改前先备份、内容不变就不动"的保护,不再被无差别覆盖。
    """
    had_file = os.path.exists(path)
    if had_file:
        with open(path, "r", encoding="utf-8") as handle:
            if handle.read() == text:
                actions.append("跳过 %s(已是目标状态)" % path)
                return
        backup = "%s.bak.%s" % (path, time.strftime("%Y%m%d-%H%M%S", time.gmtime()))
        shutil.copy2(path, backup)
        actions.append("备份 %s -> %s" % (path, backup))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=os.path.dirname(path), prefix=".tmp-", delete=False
    )
    try:
        handle.write(text)
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
    actions.append("写入 %s" % path)


def _hook_template():
    """从仓库模板读取 hook 条目,把占位符换成本机绝对路径。

    事件与超时只在 cursor/hooks.json 里定义一次 —— installer 不再重复
    一份表,改模板即改安装结果。
    """
    path = os.path.join(_ROOT, "cursor", "hooks.json")
    data, _ = _load_json(path, None)
    if not isinstance(data, dict) or not isinstance(data.get("hooks"), dict):
        raise InstallError("%s 不是合法的 Cursor hooks 模板" % path)
    resolved = {}
    for event, entries in data["hooks"].items():
        resolved[event] = []
        for entry in entries:
            command = entry.get("command", "")
            if PLACEHOLDER not in command:
                raise InstallError(
                    "%s 的 %s 条目缺少 %s 占位符" % (path, event, PLACEHOLDER)
                )
            item = dict(entry)
            item["command"] = command.replace(PLACEHOLDER, BIN_PATH)
            resolved[event].append(item)
    return resolved


def install_cursor(home=None, project_dir=None):
    home = home or os.path.expanduser("~")
    cursor_dir = os.path.join(home, ".cursor")
    actions = []

    hooks_path = os.path.join(cursor_dir, "hooks.json")
    data, had = _load_json_object(hooks_path, {"version": 1, "hooks": {}})
    data["hooks"] = _require_mapping(hooks_path, "hooks", data)
    data.setdefault("version", 1)
    for event, ours in _hook_template().items():
        existing = _event_entries(hooks_path, event, data["hooks"].get(event, []))
        entries = [e for e in existing if not _is_ours(e)]
        entries.extend(ours)
        data["hooks"][event] = entries
    _write_json(hooks_path, data, had, actions)

    mcp_path = os.path.join(cursor_dir, "mcp.json")
    data, had = _load_json_object(mcp_path, {})
    data["mcpServers"] = _require_mapping(mcp_path, "mcpServers", data)
    data["mcpServers"]["mem0-oss"] = {"command": BIN_PATH, "args": ["mcp"]}
    _write_json(mcp_path, data, had, actions)

    # skill 目标目录命名 mem0-oss 而不是 mem0:mem0 是知名的托管产品,
    # 用户从 mem0 云端迁移到本插件时,~/.cursor/skills/mem0/ 完全可能已经
    # 是他们自己的东西 —— 不能假设这个名字下面的内容是我们的。
    skill_src = os.path.join(_ROOT, "skills", "mem0", "SKILL.md")
    try:
        with open(skill_src, "r", encoding="utf-8") as handle:
            skill_body = handle.read().replace("../../bin/mem0-oss", BIN_PATH)
    except OSError as exc:
        raise InstallError("读取 %s 失败:%s" % (skill_src, exc))
    skill_dst = os.path.join(cursor_dir, "skills", "mem0-oss", "SKILL.md")
    _write_text(skill_dst, skill_body, actions)

    rule_src = os.path.join(_ROOT, "cursor", "rules", "mem0.mdc")
    try:
        with open(rule_src, "r", encoding="utf-8") as handle:
            rule_body = handle.read()
    except OSError as exc:
        raise InstallError("读取 %s 失败:%s" % (rule_src, exc))
    if project_dir:
        rule_dst = os.path.join(project_dir, ".cursor", "rules", "mem0-oss.mdc")
    else:
        rule_dst = os.path.join(cursor_dir, "rules", "mem0-oss.mdc")
    _write_text(rule_dst, rule_body, actions)
    if not project_dir:
        actions.append(RULE_NOTICE)
    return actions


def uninstall_cursor(home=None, project_dir=None):
    home = home or os.path.expanduser("~")
    cursor_dir = os.path.join(home, ".cursor")
    actions = []

    hooks_path = os.path.join(cursor_dir, "hooks.json")
    if os.path.exists(hooks_path):
        # 形状校验跟 install 同一套:卸载同样要写回这个文件,看不懂就别动。
        # 这里报错会让后面的 skill/rule 删除也一并停下 —— 与解析失败时的现有
        # 行为一致,用户改好文件再重跑即可。
        data, had = _load_json_object(hooks_path, {})
        hooks_map = _require_mapping(hooks_path, "hooks", data)
        # 只有真的从某个事件里摘掉过自己的条目才重写文件 —— 否则一个
        # 从没装过 mem0-oss 的 hooks.json 会被无意义地重新格式化并打上
        # 一份多余的备份,这本该是个空操作。
        changed = False
        for event in list(hooks_map):
            original = _event_entries(hooks_path, event, hooks_map[event])
            if not any(_is_ours(e) for e in original):
                continue
            changed = True
            kept = [e for e in original if not _is_ours(e)]
            if kept:
                hooks_map[event] = kept
            else:
                del hooks_map[event]
        if changed:
            _write_json(hooks_path, data, had, actions)

    mcp_path = os.path.join(cursor_dir, "mcp.json")
    if os.path.exists(mcp_path):
        data, had = _load_json_object(mcp_path, {})
        servers = _require_mapping(mcp_path, "mcpServers", data)
        if "mem0-oss" in servers:
            del servers["mem0-oss"]
            _write_json(mcp_path, data, had, actions)

    # 只删自己写过的那一个文件,目录为空才顺手收掉 —— 绝不 rmtree 一个
    # 我们没写过全部内容的目录(mem0-oss 这一层是我们自己起的名字,但
    # 用户理论上也可能在里面放了别的东西)。
    skill_path = os.path.join(cursor_dir, "skills", "mem0-oss", "SKILL.md")
    if os.path.exists(skill_path):
        try:
            os.unlink(skill_path)
        except OSError as exc:
            raise InstallError("删除 %s 失败:%s" % (skill_path, exc))
        actions.append("删除 %s" % skill_path)
        try:
            os.rmdir(os.path.dirname(skill_path))
        except OSError:
            pass  # 目录非空(用户自己的文件)或已经不存在,都不是错误

    if project_dir:
        rule_path = os.path.join(project_dir, ".cursor", "rules", "mem0-oss.mdc")
    else:
        rule_path = os.path.join(cursor_dir, "rules", "mem0-oss.mdc")
    if os.path.exists(rule_path):
        # 与上面 skill 的删除同口径:OSError(常见是权限)收敛成 InstallError,
        # 由 cli 打成一行"卸载失败:…",而不是甩用户一段 traceback
        try:
            os.unlink(rule_path)
        except OSError as exc:
            raise InstallError("删除 %s 失败:%s" % (rule_path, exc))
        actions.append("删除 %s" % rule_path)
    return actions
