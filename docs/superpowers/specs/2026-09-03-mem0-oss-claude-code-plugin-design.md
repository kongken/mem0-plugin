# mem0-oss:支持 self-hosted mem0 的 Claude Code plugin

- 日期:2026-09-03
- 状态:设计已确认,待实现计划
- 参考实现:[`@amaster.ai/pi-memory-mem0`](https://pi.dev/packages/@amaster.ai/pi-memory-mem0) v0.1.14(Pi extension)

## 1. 背景与目标

官方 mem0 Claude Code plugin(`mem0-plugins/mem0` v0.1.0)是 cloud-only 的:端点硬编码 `api.mem0.ai` / `mcp.mem0.ai`,只读 `MEM0_API_KEY` 和 `MEM0_USER_ID`,没有任何 base URL 覆盖开关。指向自建的 mem0 OSS server 需要另起一套。

**目标**:做一个 OSS-only 的 Claude Code plugin,把记忆的读(检索注入)和写(对话捕获)接到自建的 mem0 `server/` REST API 上。

**非目标**:

- 不支持 Mem0 Platform(云端)。已有官方 plugin 覆盖该场景。
- 不支持 `openmemory/`(`:8765`,`/api/v1/...`)。REST 路径与 `server/` 不兼容,如需支持是另一个 spec。
- 不实现 MCP server。使用方已自建 streamable HTTP MCP server,模型侧的记忆工具由它提供。
- 不做 embedded 模式(进程内 `mem0ai` 库 + 本地向量库)。那需要 Node/Python 重依赖,与「hooks 必须秒级返回」冲突。

## 2. 前提与既定决策

| 决策 | 取值 | 理由 |
|---|---|---|
| 后端 | mem0 `server/`(FastAPI REST) | 使用方的部署形态 |
| 认证 | `X-API-Key`(`m0sk_…`) | 服务端默认模式;`AUTH_DISABLED=true` 时可不带 |
| 落地形态 | 从零重写,OSS-only | 见 §3 的三条理由 |
| 配置载体 | 纯环境变量 | 与使用方现有 `~/.claude/settings.json` 的 `env` 块一致 |
| 实现语言 | Python 3,纯 stdlib | 需要 cwd 哈希、JSON 处理、正则脱敏、Unicode 扫描;bash+jq 做这些全是硬伤 |
| MCP | 不实现,只提供 `.mcp.json.example` | 使用方已有 |

## 3. 为什么重写而不 fork 官方 plugin

1. **云端专属组件占比高**。`.mcp.json` 指向的 `mcp.mem0.ai` 是云端专属服务,self-hosted 无对应物;`skills/mem0/`(7 个 references + 一个文档检索脚本)整套讲的是 `MemoryClient` 云端 SDK,`SKILL.md:129` 甚至明确写 `Do not use from mem0 import Memory` —— 而那正是 OSS 的进程内类。

2. **官方有两个 hook 从未生效**。按 [hooks 文档](https://code.claude.com/docs/en/hooks),只有 `SessionStart` / `UserPromptSubmit` / `Stop` / `PostModelSwitch` 四个事件的 stdout 会进入模型上下文,其余仅写 debug log。而官方:
   - `on_pre_compact.sh` 挂 `PreCompact`,`cat` 约 60 行「压缩前立刻存摘要」提示词 → stdout 只进 debug log
   - `on_task_completed.sh` 挂 `TaskCompleted`,`cat` 约 15 行「存学习」提示词 → 同上

   两段提示词模型一个字都读不到。**实现前需用 `claude --debug` 实测坐实**(文档可能滞后于实现),但设计不押在未验证的行为上。

3. **写入路径设计过时**。官方 `on_stop.sh` 每次 Claude 结束回答都后台跑 239 行的 `on_pre_compact.py`,机械拼接最近 30 条用户消息 + 改动文件 + 20 条 bash 命令,写成一条 `session_state` 记忆。信噪比极低,且依赖 `Stop` 事件的 `transcript_path` 字段 —— 而该字段在 `Stop` 的 stdin 契约里并未列出(`UserPromptSubmit` 列了)。见 §6。

## 4. REST 契约

来源:`mem0ai/mem0` @ `43dde3b` 的 `server/main.py`、`server/auth.py`。

### 4.1 认证

`server/auth.py:106` → `APIKeyHeader(name="X-API-Key", auto_error=False)`,另支持 `Authorization: Bearer <JWT>`;`AUTH_DISABLED=1|true|yes|on` 时全部放行。本 plugin 只用 `X-API-Key`,未配置 key 时不发认证头。

### 4.2 用到的端点

| 用途 | 方法与路径 | 请求体 / 参数 |
|---|---|---|
| 检索 | `POST {base}/search` | `{query, filters:{user_id, agent_id?}, top_k}` |
| 写入 | `POST {base}/memories` | `{messages:[{role,content}], user_id, agent_id?, metadata?, infer:true}` |
| 列举 | `GET {base}/memories?user_id=&agent_id=` | — |
| 删除 | `DELETE {base}/memories/{memory_id}` | — |

约束与注意:

- 写入端 `user_id` / `agent_id` / `run_id` **至少给一个**,否则 400(`main.py:351`)。
- `SearchRequest`(`main.py:189`)同时接受顶层 `user_id` 和 `filters`;本设计统一走 `filters`,与 Pi 的 `SelfHostedProvider` 一致,减少两边行为分叉。
- 响应统一按 `{"results": [...]}` 解析;条目字段取 `id` / `memory` / `created_at` / `updated_at` / `score`。响应不是该形状时按空结果处理。
- 与云端的差异:云端是 `POST /v2/memories/search/` 和 `POST /v1/memories/`,认证头 `Authorization: Token m0-…`。**路径和认证头都不同,不能混用。**

## 5. 组件与仓库布局

```
mem0-plugin/
├── .claude-plugin/
│   ├── plugin.json           插件元数据
│   └── marketplace.json      仓库自身即 marketplace,可直接 marketplace add
├── bin/
│   └── mem0-oss              Python 3 单文件 CLI,唯一逻辑所在
├── hooks/
│   └── hooks.json            3 个事件,全部转调 bin/mem0-oss hook <event>
├── commands/
│   └── mem0.md               /mem0 <sub> → 调 bin/mem0-oss
├── .mcp.json.example         指向使用方自建 MCP 的模板(可选)
├── tests/
│   ├── fake_server.py
│   ├── test_wire.py
│   ├── test_hooks.py
│   └── test_privacy.py
├── .github/workflows/test.yml
└── README.md
```

**命名**:可执行文件叫 `mem0-oss` 而非 `mem0`。Claude Code 会把每个已装 plugin 的 `bin/` 目录加入会话 PATH,叫 `mem0` 会与真实的 mem0 CLI 冲突。

**单一逻辑源**:hooks 和 slash command 都只是 `bin/mem0-oss` 的调用方,不含业务逻辑。只有一处需要测试。

### 5.1 CLI 子命令

```
mem0-oss hook session-start      # 读 stdin JSON,写 stdout
mem0-oss hook user-prompt
mem0-oss hook stop

mem0-oss status                  # 配置体检:连通性、认证、作用域、前缀错配
mem0-oss search <query>
mem0-oss add <text>
mem0-oss profile                 # 列举当前作用域全部记忆
mem0-oss delete <memory_id>
```

`commands/mem0.md` 用 `$ARGUMENTS` 透传,得到 `/mem0 status`、`/mem0 search foo` 这样的调用形式。

**不实现 `dedup`**。Pi 的 dedup 在 self-hosted 模式下限制很多(向服务端要 1000 条上限,达到即 fail closed,只对确证少于 999 条的作用域可用),价值有限;正常写入走 `infer:true`,mem0 服务端自身的去重和冲突处理是主要防线。需要时另开 spec。

## 6. Hook 设计

### 6.1 只挂 stdout 有效的事件

| 事件 | 职责 | 超时 |
|---|---|---|
| `SessionStart` | ① 认证探活,失败时打一行可见警告 ② 清理陈旧状态文件 | 5s |
| `UserPromptSubmit` | ① 检索注入(受 `autoRecall` 控制)② 将 prompt 存入会话状态文件,供 `Stop` 配对 | 3s |
| `Stop` | 读 stdin 的 `last_assistant_message`,与状态文件中的 prompt 配成一轮,后台写入(受 `autoCapture` 控制) | 立即返回 |

**不挂** `PreCompact` / `TaskCompleted`(stdout 无效,见 §3.2),**不挂** `PreToolUse`(官方用它拦截 `MEMORY.md` 写入以强推自家产品,与本项目无关)。

`SessionStart` **不做检索**。`recallFrequency=session` 的语义是「本会话第一条用户输入时检索一次」,而 `SessionStart` 手里没有 query;该开关完全在 `UserPromptSubmit` 内处理。

`SessionStart` 的认证探活是一次 `GET {base}/memories?user_id=<作用域后的 id>`,超时 5s,**只看状态码,丢弃响应体**。目的是让配置错误在会话开始时就暴露,而不是等用户敲完第一条 prompt 才发现什么都没召回。探活本身失败(网络不通、超时)不输出任何东西 —— 服务端临时不可用不该在每次开会话时刷屏;只有 401/403 这种「配置错了,永远不会自愈」的情况才警告。

### 6.2 对话捕获不解析 transcript

`UserPromptSubmit` 的 stdin 带 prompt 全文,`Stop` 的 stdin 直接带 `last_assistant_message`。一轮对话的两半从 hook 输入即可取全,中间只需一个 per-session 状态文件传递 prompt。

本节的字段清单已对照 Claude Code 2.1.236 的 `Stop` hook 输入构造核实过:实际带的是 `session_id`、`cwd`、`transcript_path`、`stop_hook_active`、`last_assistant_message`、`background_tasks`、`session_crons`,**没有 `turn_number`**。本节此前把 `turn_number` 列为 `Stop` 的输入字段,是错的;`metadata` 里也不再放这个字段(放了也永远是 null)。

因此:

- 不抄官方那 239 行 transcript JSONL parser;
- 不依赖 `Stop` 的 `transcript_path`(该字段在 `Stop` 的 stdin 契约中未列出);
- 写入内容是原始的 user + assistant 两条消息,配 `infer:true`,由 mem0 服务端自行抽取事实 —— 这也是 Pi `autoCapture` 的原意。

### 6.3 一个完整回合的数据流

```
用户提交 prompt
  └─ UserPromptSubmit
       ├─ 判定:BASE_URL 已设 && autoRecall && len(prompt) >= MIN_PROMPT_CHARS
       │    && (frequency != session || 本会话尚未检索过)
       ├─ POST {base}/search {query, filters:{user_id}, top_k}
       ├─ 逐条:注入扫描 → 命中则 [BLOCKED UNTRUSTED MEMORY: <类别>]
       │         未命中则 [UNTRUSTED MEMORY DATA] <JSON 引号包裹的原文>
       ├─ 截断:单条 1000 字符 / 整块 8 KiB / 50 行
       ├─ stdout 输出 "## Recalled Memories (mem0 OSS)" 块 → 进入模型上下文
       └─ 将 prompt 写入 <state>/sessions/<session_id>.json

模型回答结束
  └─ Stop
       ├─ 判定:BASE_URL 已设 && autoCapture && 状态文件中有配对的 prompt
       ├─ 两条消息分别过凭据脱敏
       ├─ 起 detached 子进程(`start_new_session=True`)POST {base}/memories
       │    {messages:[{role:user},{role:assistant}], user_id, agent_id?, infer:true,
       │     metadata:{source:"claude-code", session_id, cwd}}
       ├─ 清理状态文件中的 prompt
       └─ 主进程立即 exit 0,不等待写入结果
```

### 6.4 作用域

沿用 Pi 的方案(`privacy.js:5-10`):

```
scope == "exact"   → user_id = MEM0_USER_ID
scope == "project" → user_id = f"{MEM0_USER_ID}:project:{key}"
  MEM0_SCOPE_KEY=raw(默认)且拿得到 remote → key = scope_key(cwd)
  其余情况                                → key = sha256(scope_key(cwd))[:12]
scope_key(cwd)     = 归一化的 git remote url(优先 origin,否则按名字取第一个)
                     拿不到(非 git 目录 / 无 remote / git 不可用)时为 realpath(cwd)
```

> **2026-10-20 修订**:哈希输入从 `realpath(cwd)` 改为 `scope_key(cwd)`。按目录哈希时,
> 同一仓库 clone 在不同路径、开 worktree、从子目录启动都会落进不同作用域,记忆互相看不到。
> 归一化去掉协议、用户名/凭据、端口、结尾的 `.git` 与斜杠并整体转小写,使 scp 式、https、
> ssh、git:// 几种写法指向同一作用域。读取用 `git config --local`,不会把全局 gitconfig 里的
> 条目当成 remote。升级后旧记忆(按目录哈希写入)读不到。
>
> 同时新增 `MEM0_SCOPE_KEY=raw|hash`(默认 `raw`,只在 `project` 下生效)。`raw` 时 remote url
> 不做哈希直接进 user_id,便于在服务端辨认仓库;拿不到 remote 时仍用 `sha256(realpath(cwd))[:12]`,
> 不把本地路径原样发出去。`hash` 保留与 Pi 一致的 12 位 hex 形状,供不想让仓库地址出现在
> 服务端、或要与 Pi 对照排查的场景使用。

默认 `project`,使不同项目的记忆互不可见。需要跨项目共享同一身份时设 `MEM0_USER_ID_SCOPE=exact`。

**注意**:切换 scope 会改变 `user_id`,旧记忆不会自动迁移,读不到属于预期行为。

### 6.5 状态文件

`${MEM0_STATE_DIR}/sessions/<session_id>.json`,内容:

```json
{"prompt": "...", "prompt_ts": 1756857600, "recalled": true, "auth_warned": true}
```

- `prompt` / `prompt_ts`:供 `Stop` 配对。超过 1 小时视为过期,丢弃不写入。
- `recalled`:`recallFrequency=session` 的标记。**首次检索尝试即置位**,即使返回空或超时 —— 与 Pi 语义一致。
- `auth_warned`:认证警告每会话只打一次。

写入用「临时文件 + `os.replace`」保证原子性。启动时顺带清理 7 天前的陈旧文件。

## 7. 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MEM0_BASE_URL` | — | **未设 = 整个 plugin 静默失效**,所有 hook 直接 exit 0。总开关 |
| `MEM0_API_KEY` | — | 送 `X-API-Key`。未设则不发认证头(适配 `AUTH_DISABLED=true`) |
| `MEM0_USER_ID` | `$USER`,再退 `default-user` | 作用域标识 |
| `MEM0_AGENT_ID` | — | 可选,进 `agent_id` |
| `MEM0_USER_ID_SCOPE` | `project` | `project` \| `exact` |
| `MEM0_SCOPE_KEY` | `raw` | `raw` \| `hash`,见 §6.4 |
| `MEM0_TOP_K` | `5` | 必须为正整数;非法值回退默认并在 `MEM0_DEBUG` 下告警 |
| `MEM0_AUTO_RECALL` | `1` | `0` 关闭检索注入 |
| `MEM0_AUTO_CAPTURE` | `1` | `0` 关闭对话捕获 |
| `MEM0_RECALL_FREQUENCY` | `user-input` | `user-input` \| `session` |
| `MEM0_RECALL_TIMEOUT_MS` | `3000` | 仅 `UserPromptSubmit` 的检索 |
| `MEM0_REQUEST_TIMEOUT_MS` | `30000` | 捕获写入与命令行操作 |
| `MEM0_MIN_PROMPT_CHARS` | `20` | 短于此长度的 prompt 不检索 |
| `MEM0_THREAT_SCAN` | `strict` | `strict` \| `off`,见 §8.2 |
| `MEM0_STATE_DIR` | `~/.claude/mem0-oss` | 状态文件目录 |
| `MEM0_DEBUG` | `0` | `1` 时向 stderr 输出诊断 |

布尔值接受 `1/true/yes/on`(不区分大小写)为真,其余为假。

## 8. 隐私与安全

### 8.1 写入前:凭据脱敏

照搬 Pi `privacy.js:11-17` 的三条正则,顺序执行:

1. `-----BEGIN … PRIVATE KEY-----` 到 `-----END … PRIVATE KEY-----` 整块 → `[REDACTED]`。**算法名必须可缺省** —— PKCS8 的 `-----BEGIN PRIVATE KEY-----` 不带算法名,而它是 `openssl genpkey` 和 Node `crypto.generateKeyPairSync` 的默认输出。参考实现 pi-memory-mem0 的 `redactMemoryText` 在这里写的是 `[^-]+ `,漏掉了这种形态,**不要照抄**。
2. `\b(Bearer)\s+[A-Za-z0-9._~+/=-]{12,}` → `$1 [REDACTED]`
3. `api_key|apikey|token|password|secret` 这组键的赋值 → `$1=[REDACTED]`。**必须同时覆盖 JSON 里的引号键与带前缀的环境变量** —— `{"api_key": "sk-…"}` 和 `MEM0_API_KEY=m0sk_…` 是最可能真的携带密钥的两种写法(README 就是教用户在 `settings.json` 的 `env` 块里配 `MEM0_API_KEY`)。Pi `privacy.js:11-17` 的 `\b(api[_-]?key|token|password|secret)\s*[:=]\s*["']?[^\s"',;]{6,}` 这两种**都漏**:`\s*[:=]` 跨不过键名的右引号,而 `\b` 在 `_` 与 `A` 之间(两侧都是词字符)不成立。因此**不要照抄**,键名前要允许任意 `XXX_` / `XXX-` 前缀,键名后要允许一个可选引号。同时收紧的代价必须验过:`MEM0_USER_ID=alice`、`MEM0_TOP_K=5`、`max_tokens: 4096`、`PUBLIC_TOKEN_COUNT=5` 这类普通配置行不得被吃掉。

user 和 assistant 两条消息都过。**检索用的 query 同样要过** —— 服务端会把 query 交给它配置的 embedding 提供方,凭据在写入路径上抹掉、却在读取路径上原样发出去是说不通的。

### 8.2 读出后:包装与注入扫描

记忆内容是外部数据 —— 它会被原样拼进模型上下文,任何能写入记忆库的人都获得了一条注入通道。

包装格式(同 Pi):`[UNTRUSTED MEMORY DATA] ` + JSON 字符串字面量。JSON 引号会转义换行,这本身就把整条降级成单个字符串,是第一层防护。

扫描分两档 —— **这里刻意偏离 Pi**。Pi 用 `scanForThreats(text,'strict')` 一刀切,命中即整条 BLOCK。放在开发者的记忆库上误杀率过高:开发者的记忆本就合法地包含 `sudo`、`rm -rf`、`curl … | sh`。

**Tier 1 —— 命中即替换为 `[BLOCKED UNTRUSTED MEMORY: <类别>]`**(正常记忆中几乎不出现,误杀率低):

| 类别 | 模式 |
|---|---|
| `control-plane` | `<system-reminder>` / `</system-reminder>`、`<function_calls>` / `<function_results>`、`<system>` / `</system>`、**行首的** `[system]`、行首 `Human:` / `Assistant:` |
| `instruction-override` | `(ignore｜disregard) (all)? (the)? (previous｜prior｜above｜preceding) (instructions｜prompts｜rules｜directions｜commands)`、`forget everything (you｜above｜before｜prior)` —— **两个分支都必须带宾语** |
| `hidden-chars` | 零宽字符 `U+200B–U+200F`、`U+FEFF`;双向覆写 `U+202A–U+202E`(Trojan Source);**「看不见的换行」`U+2028` / `U+2029` / `U+0085`** |

匹配不区分大小写;`hidden-chars` 按码点检测,不走正则文本。

`U+2028` / `U+2029` / `U+0085` 是实现期补进来的:`json.dumps` 只转义 `<0x20` 的码点,Python `re.MULTILINE` 的 `^` 只在 `\n` 之后成立 —— 这三个字符因此能同时绕过引号包装与行首锚定的 `control-plane` 模式,把整条降级成单行的那层防护和 `^\s{0,3}\[system\]` 一起失效。正常记忆里几乎不出现,误杀成本为零。

`control-plane` 只匹配上表列出的具体标记,**不能**泛化成「任意 XML/HTML 闭合标签」—— 开发者的记忆里合法地存在大量 JSX 与 HTML 片段,泛化会把它们全部误杀。

本表曾经还包含 Markdown 标题式的 `### system`、不限位置的 `[system]`、以及行首裸 `System:`,实现期(裁决 R6)移除:它们分别会误杀 `## System Design`、`参考 [System](wiki 链接)`、`System: macOS 14.5, 16GB RAM` —— 全是最普通的开发者记忆,而 Tier 1 的后果是整条丢失、不是降级。同理 `instruction-override` 的 `disregard` 分支(裁决 R8)现在要求宾语,否则 `Disregard the previous benchmark,新数字更准` 这类日常订正措辞会被整条 BLOCK。

**判据优先于列举。** 上表是判据的当前最佳近似,不是判据本身。新增任何模式前,先证明它在正常开发者记忆中几乎不出现。

**Tier 2 —— 不 BLOCK**:危险 shell 命令、URL、大块 base64。JSON 引号包装 + 显式 `[UNTRUSTED MEMORY DATA]` 标签已将其降级为数据,再拦截只剩噪声。

`MEM0_THREAT_SCAN=off` 可整体关闭 Tier 1,README 需写明代价。

### 8.3 数据流向

所有流量只发往 `MEM0_BASE_URL`。plugin 不与 `api.mem0.ai` / `mcp.mem0.ai` 通信。README 需明确:开启 `autoCapture` 后,每轮对话的原文(脱敏后)都会发往该地址。

## 9. 错误处理

统一 fail-open,三级:

| 情形 | 行为 |
|---|---|
| `MEM0_BASE_URL` 未设 | 静默 exit 0,无任何输出 |
| 网络错误 / 超时 / 非 2xx / 响应非 JSON | exit 0,不注入;`MEM0_DEBUG=1` 时向 stderr 输出一行 |
| **401 / 403** | exit 0 不注入,**但**输出一行可见警告,每会话一次(`auth_warned` 标记)。由 `SessionStart` 的探活触发;若探活未覆盖(如会话中途轮换了 key),则由随后首次失败的 `UserPromptSubmit` 触发 |

硬性约束:

- **永不 exit 2**。`UserPromptSubmit` 的 exit 2 会抹掉用户的 prompt,`Stop` 的 exit 2 会阻止会话结束。
- **捕获写入永不阻塞 `Stop`**。用 `subprocess.Popen(..., start_new_session=True)` 起脱离进程组的子进程,stdio 全部重定向到 `DEVNULL`(`MEM0_DEBUG=1` 时 stderr 除外),主进程不 `wait()` 直接返回。子进程必须脱离会话,否则 Claude Code 回收 hook 进程组时会把它一起杀掉,写入静默丢失。
- 顶层 `try/except` 兜底,任何未预期异常都收敛为 exit 0。

401/403 之所以破例可见:静默失效是最坑的失败模式 —— 使用方会以为装好了,实际什么都没召回。

## 10. 测试

纯 stdlib 实现使测试无需任何依赖。

| 层次 | 文件 | 内容 |
|---|---|---|
| Wire 契约 | `test_wire.py` + `fake_server.py` | `http.server` 起假 OSS server,断言请求路径、`X-API-Key` 头、body JSON 形状。**优先级最高** —— REST 契约是唯一可能读错的东西 |
| Hook 行为 | `test_hooks.py` | 喂真实 stdin JSON 样本(自 `claude --debug` 采集),断言 stdout 与 exit code |
| 隐私 | `test_privacy.py` | 表驱动,每条脱敏正则与每个 Tier 1 类别各一正例一反例;含 `userId:project:<hash>` 的哈希稳定性 |
| 端到端 | `test_hooks.py` | `MEM0_BASE_URL` 指向 fake server,跑完整 `UserPromptSubmit → Stop` 回合,断言假 server 收到配对正确的两条消息 |
| 降级 | `test_hooks.py` | 服务端返回 500 / 401 / 超时 / 非 JSON 时,断言 exit 0 且 stdout 为空(401 的 SessionStart 除外) |

运行:`python3 -m unittest discover tests`。CI 用一个 GitHub Action,矩阵覆盖 Python 3.9–3.13(macOS 系统 python3 版本跨度较大)。

## 11. 部署与迁移

### 11.1 安装

```
/plugin marketplace add https://github.com/kongken/mem0-plugin
/plugin install mem0-oss@mem0-plugin
```

配置写进 `~/.claude/settings.json` 的 `env` 块。

### 11.2 两个已知的坑(README 必须写)

1. **必须卸载官方 mem0 plugin**。否则两个 `UserPromptSubmit` hook 都会注入(上下文里出现两份记忆块),且每条 prompt 继续发往 `api.mem0.ai`。
2. **API key 格式**。云端签发 `m0-…`,OSS 服务端签发 `m0sk_…`。沿用云端 key 会得到 401,而 401 是 fail-open 的 —— 表现为「装好了但什么都没召回」。`mem0-oss status` 专门检查这一前缀错配并明确指出。

### 11.3 验收

实现完成后按此清单验证:

1. `mem0-oss status` 报告连通、认证通过、作用域正确
2. `mem0-oss add` 写入后,`mem0-oss profile` 能读到
3. 新会话中提一个与已存记忆相关的问题,上下文出现 `## Recalled Memories (mem0 OSS)` 块
4. 一轮对话结束后,服务端出现对应的新记忆
5. `MEM0_BASE_URL` 置空后,所有 hook 无输出、无报错
6. 服务端停机时,会话正常可用,仅无记忆注入
7. 用 `claude --debug` 坐实 §3.2 中 `PreCompact` / `TaskCompleted` 的 stdout 确实不进上下文

## 12. 待办与开放问题

- §3.2 的结论需实测坐实。若实测表明 `PreCompact` 的 stdout 实际会进上下文,则可考虑补一个压缩前的摘要 hook —— 但这不影响本 spec 的其余部分。
- 服务端地址与 API key 由使用方在实现阶段提供;spec 内一律以环境变量指代,无硬编码默认值(`MEM0_BASE_URL` 无默认值,未设即停用)。
