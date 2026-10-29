---
name: session-extract
description: |
  Use when a user provides a session ID, session JSONL file, a DeepSeek
  export bundle, or a DeepSeek share link, and needs to understand and
  continue that work.
  Also triggered by phrases like "恢复上下文", "接手工作", "之前做到哪了",
  "从聊天记录恢复", "交接", "handoff", "打捞会话", "找回聊天记录".
  Automatically detects the platform (Codex / Claude Code / DeepSeek) from the
  session file, can search a whole DeepSeek corpus by topic keyword, and can
  fetch attachment bytes from a DeepSeek share link.
  NOT when the current conversation already has sufficient context.
---

# Session Extract

## 概述

**核心能力**：从 Codex、Claude Code 的会话 JSONL 文件或 DeepSeek 导出包中提取上下文，
自动检测平台，生成结构化的交接报告。

一个脚本覆盖三个平台，通过 session ID 自动定位文件并检测格式；
DeepSeek 导出包是**单文件装全部会话**，另提供「按话题检索整个语料库」的能力。

## 平台差异

| 维度 | Codex | Claude Code | DeepSeek（导出包） | DeepSeek（分享链接） |
|------|-------|-------------|-------------------|----------------------|
| 文件位置 | `~/.codex/sessions/YYYY/MM/DD/` | `~/.claude/projects/<project-dir>/` | 导出包内的 `conversations.json`（**单文件装全部会话**） | `https://chat.deepseek.com/share/<id>`（**公开链接，无需登录**） |
| 会话结构 | 线性 JSONL | 线性 JSONL | **聊天树**（`mapping`，节点带 `parent`/`children`） | 线性消息数组 |
| 定位方式 | session ID | session ID | **关键词 → 全语料检索**（标题只是自动摘要，靠不住） | share_id / 分享链接 |
| 标题 | 原始标题 | 原始 / `custom-title` | **自动生成的摘要**，常与用户记忆的措辞不符 | 占位符「来自分享的对话」，真实名要 `--title` 传入 |
| 核心恢复入口 | `task_complete`（Agent 自生成报告） | `file-history-snapshot`（文件变更链逆推） | fragment 正文全文检索 + 聊天树线性化 | 分享 API 正文 + 附件字节直下 |
| 元数据 | 第 1 行 `session_meta` | 首条 `user` 消息的 metadata | 会话对象 `{id, title, inserted_at, updated_at, mapping}` | `{data:{biz_data:{title, model_type, messages[]}}}` |
| Agent 输出 | `event_msg` 中的 `agent_message` | `assistant` 类型消息 | `RESPONSE` fragment（`THINK` 是思考链） | `RESPONSE` fragment + `TIP` fragment（导出包没有） |
| 工具调用 | `response_item` 中的 `tool_use` | `assistant.message.content` 中的 `tool_use` | `TOOL_SEARCH`/`SEARCH` fragment（`TOOL_OPEN`/`TOOL_FIND` **载荷未导出**） | `TOOL_SEARCH` + `TOOL_OPEN`（**`TOOL_OPEN` 载荷完整**） |
| 附件 | — | — | **只有元数据，字节不在包里** | **字节可直下** |
| 快照时效 | 实时落盘 | 实时落盘 | **导出时刻的快照，可能滞后于最新会话** | 分享当时的**最终态**（中间轮次已丢失） |

## 跨平台注意

| 问题 | 解决方案 |
|------|----------|
| Python 可执行名 | Windows: `python`，macOS/Linux: `python3` |
| 脚本路径 | 先定位 `[path-to-skill]`，再运行 `[path-to-skill]/scripts/session_extract.py`，不要假定目标仓库有 `scripts/session_extract.py` |
| 路径格式 | 脚本内用 `os.path.expanduser()` + `os.path.join()` |
| 编码 | 统一 `encoding='utf-8'` |
| 隔离模式 `python -I` | 解释器**不会**把脚本目录放进 `sys.path`，`from extract_codex import ...` 会 `ModuleNotFoundError`；脚本已显式 `sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))` 兜底 |

## 脚本路径约定

`[path-to-skill]` 表示本 skill 的安装目录，即当前 `SKILL.md` 所在目录。
Agent 在任意工作区触发本 skill 时，必须先定位 `[path-to-skill]`，
再调用 `[path-to-skill]/scripts/...` 下的 bundled scripts。
不要假设当前工作区或目标项目中存在可用的 `scripts/` 目录。

## 工作流程

```
① 定位文件（自动检测平台） → ② 单次提取 → ③ 核对仓库 → ④ 合成报告
```

### ① 定位文件

> 下面的 `[path-to-skill]/scripts/session_extract.py` 指的是本 skill 目录下的脚本，不是当前工作仓库的 `scripts/` 目录。执行前先把 `[path-to-skill]` 替换为当前 `SKILL.md` 所在目录，避免误跑到目标仓库。

```powershell
# Windows PowerShell：按 session ID 自动定位（推荐）
python "[path-to-skill]/scripts/session_extract.py" --session <session-id>

# Windows PowerShell：直接指定文件（自动检测平台）
python "[path-to-skill]/scripts/session_extract.py" <session.jsonl>

# Windows PowerShell：强制指定平台
python "[path-to-skill]/scripts/session_extract.py" --platform codex <file>
python "[path-to-skill]/scripts/session_extract.py" --platform claude <file>
```

```bash
# macOS/Linux：按 session ID 自动定位（推荐）
python3 "[path-to-skill]/scripts/session_extract.py" --session <session-id>

# macOS/Linux：直接指定文件（自动检测平台）
python3 "[path-to-skill]/scripts/session_extract.py" <session.jsonl>

# macOS/Linux：强制指定平台
python3 "[path-to-skill]/scripts/session_extract.py" --platform codex <file>
python3 "[path-to-skill]/scripts/session_extract.py" --platform claude <file>
```

脚本会自动搜索 `~/.codex/sessions/` 和 `~/.claude/projects/` 两个目录。

> DeepSeek 导出包**没有** session ID 定位这一说——它是单文件装全部会话，
> 走下面「语料库检索模式」和「DeepSeek 打捞模式」。

### ② 单次提取

脚本输出统一格式：

| Section | 说明 |
|---------|------|
| **SESSION INFO** | 平台、标题、分支、版本等 |
| **STATISTICS** | 消息计数、文件变更数等 |
| **TOOL USE DISTRIBUTION** | 工具调用频率 |
| **USER MESSAGES** | 用户实际输入 |
| **FILE CHANGE CHAIN** | 文件变更时间线 |
| **AGENT OUTPUTS** | Agent 文本输出（含 task_complete） |
| **WORK INFERENCE** | 自动判断工作状态 |

## 语料库检索模式（DeepSeek 导出包）

**触发场景**：用户只记得**话题**，不记得会话 ID，也不知道标题长什么样。
这是 DeepSeek 场景的常态——它的标题是自动生成的摘要，跟用户记忆里的措辞经常对不上。

```powershell
# Windows PowerShell：按关键词检索整个语料库（--kw 可重复）
python "[path-to-skill]/scripts/session_extract.py" --scan <conversations.json> --kw 某同步插件 --kw 上传 --kw 同步

# 限制条数 / 输出机器可读 JSON
python "[path-to-skill]/scripts/session_extract.py" --scan <conversations.json> --kw 某同步插件 --top 10 --json
```

```bash
# macOS/Linux
python3 "[path-to-skill]/scripts/session_extract.py" --scan <conversations.json> --kw 某同步插件 --kw 上传 --kw 同步
```

打分规则（`scripts/extract_deepseek.py` 的 `scan_corpus`）：

| 维度 | 做法 | 为什么 |
|------|------|--------|
| 正文命中 | fragment 的 `content` + `results[].title/url` + `files[].file_name` 全量计数 | 用户记忆里的原话通常只在正文 |
| 标题命中 | 视作该关键词**额外命中 100 次** | 标题是自动摘要，命中标题 ≈ 这场对话就是在讲它 |
| 词频惩罚 | 按 IDF 加权（`log((N+1)/(df+1))+1`） | 「同步」「上传」这类通用词命中再多，也不该压过「某同步插件」这种专有词 |
| 命中位置 | 记录 `keyword@FRAGMENT_TYPE` | 一眼分辨「真话题」还是「顺带提到」 |
| 大小写 | 不敏感 | `某同步插件` / `某专有词小写` / `某专有词全大写` 都要命中 |

输出按加权得分降序，**同分时标题命中多的优先，再同则更新的会话优先**。
检索结果里 `title_match` 非空的，就是真·主题候选。

### DeepSeek 打捞模式

> **打捞前先核对数据源**：分别算 **zip 内层 `conversations.json`** 与 **解压出的文件夹里那个** 的
> sha256 并比对，不一致时**以 zip 为准**——文件夹名带的日期不作数（实战踩过）。
> 产物 `_meta.source.sha256` 会记录实际用的那份源文件。

检索出候选 ID 后，打捞成**独立 JSON 文件**（schema `rescued-session/v1`）：

```powershell
# Windows PowerShell
python "[path-to-skill]/scripts/session_extract.py" --platform deepseek <conversations.json> --ids <会话 id>,<会话 id> --out <out.json> --reason "打捞原因" --markdown --verify

# 一个会话一份文件（+ .md 预览 + 附件清单）
python "[path-to-skill]/scripts/session_extract.py" --platform deepseek <conversations.json> --ids <会话 id>,<会话 id> --outdir <dir> --markdown
```

| 参数 | 说明 |
|------|------|
| `--ids <id>[,<id>]` | 会话 ID 或 ID 前缀，逗号分隔（与 `--scan` 结果配合使用） |
| `--all` | 导出全部会话（慎用，语料大时产物很大） |
| `--out <path>` | 输出**单个** JSON（所有会话装一起），默认 `rescued_deepseek.json`（当前目录） |
| `--outdir <dir>` | **一个会话一份文件**（`<日期>_<id前8位>_<标题>.json`），与交付产物布局一致 |
| `--reason "<text>"` | 打捞原因，写进产物的 `rescue_reason`，方便日后回溯 |
| `--markdown` | 额外写一份 `.md` 预览（跳过 `THINK`，搜索结果渲染成链接列表） |
| `--verify` | 跑**多重集比较**自证：transcript vs 源 mapping，无丢失、无重复则 PASS |

产物结构：

```
_meta           # schema / platform / source(file, sha256, conversation_count_in_source) / extraction
conversations[] # id, title, inserted_at, updated_at, rescue_reason, stats, attachments, transcript, raw_mapping
```

`transcript` 是**给人读的线性视图**（聊天树从 `root` 深度优先线性化，含 `seq`/`depth`/`role`/`label`），
`raw_mapping` 是**给机器兜底的原始结构**（逐字保留）。两个都留，任何重新分析都不丢信息。

`attachments` 是附件清单（`file_id` / `file_name` / `file_size_bytes` / 对应问题）。
**注意：DeepSeek 导出只存附件元数据，图片二进制不在导出包里**——有附件时会额外写一份
`<base>_attachments.json`，明说「缺了什么、每个附件回答的是哪一问」。

### DeepSeek 分享链接打捞模式

> **与导出包路径互补，不是重复**：导出包捞不到附件字节，**分享链接能**。
> 两条路径各有独占内容（见下表），**并集才是完整历史**。

| 维度 | 账号导出包 | 分享链接 |
|------|-----------|---------|
| 输入 | `conversations.json`（需账号导出） | `/share/<id>` 公开链接，**无需登录** |
| 会话结构 | 聊天树 `mapping` → DFS 线性化 | 线性消息数组，无需线性化 |
| 附件 | 只有元数据（**无字节**） | **字节可直下** ← 独有 |
| `TOOL_OPEN` 载荷 | 未导出 | **完整** ← 独有 |
| 标题 | 真实会话名 | 占位符「来自分享的对话」，**需 `--title` 传真实名** |
| 轮次 | 快照时刻的状态 | **只有最终态**（用户编辑/删掉的中间轮次不在其中） |

```powershell
# Windows PowerShell
python "[path-to-skill]/scripts/session_extract.py" --platform deepseek-share <share_id|share_url> --outdir <dir> --title "真实会话名" --reason "打捞原因" --markdown --verify

# macOS/Linux
python3 "[path-to-skill]/scripts/session_extract.py" --platform deepseek-share <share_id|share_url> --outdir <dir> --title "真实会话名"
```

| 参数 | 说明 |
|------|------|
| `<share_id \| share_url>` | 位置参数。裸 share_id 或完整分享链接都行，模块内部归一化 |
| `--title "<name>"` | **真实会话名**。分享 API 只给占位符，不传则 `title_is_placeholder=true`，与导出包路径对不上 |
| `--outdir <dir>` | 产物目录；**附件字节落在 `<dir>/附件/`**，清单里 `local_path` 用正斜杠相对路径 |
| `--out <path>` | 输出单个 JSON（默认 `rescued_deepseek_share.json`） |
| `--verify` | 多重集比较自证：transcript vs 分享 API 原始响应 |

**跨源并集时的三个坑**（不处理会误判成「打捞丢失」）：

1. **容器键名不同** —— 导出包是 `raw_mapping`，分享链接是 `raw_messages`（它没有聊天树）。并集代码要**同时认这两个键**。
2. **`TOOL_OPEN` 载荷差异是来源差异** —— 分享侧有 `result{url,title,snippet}`，导出侧根本没导出。不是 bug。
3. **`[!reference:N]` 必须先归一再比正文** —— 分享侧剥离了行内引用标记（引用关系移到 `references` 数组），导出侧保留着。实测 48 个共有节点里 19 个报文本差异**全是这个原因**，归一后相似度 0.959–0.991。

`transcript[].sources[]` 与 `_meta.sources[]` 已标注每个节点来自哪条路径，并集时按它标注出处。

### 跨源并集模式

两条路径都跑完之后，求并集——**并集才是完整历史**。

```powershell
python "[path-to-skill]/scripts/session_extract.py" --merge <导出包产物.json> --merge-with <分享链接产物.json> --outdir <dir> --markdown
```

产物仍是 `rescued-session/v1`（**不升 v2**）：并集信息放在 `_meta.merge`，来源清单放在
`_meta.sources[]`，节点出处放在 `transcript[].sources[]`。单源产物不需要这些块，
所以**并集产物与单源产物对下游是同构的**。

**合并算法（`scripts/merge_deepseek.py`）**：

| 环节 | 规则 |
|------|------|
| 节点对齐键 | `node_id`——两条路径的编号空间一致（实测分享链接 48 个 node_id 全是导出包的子集，导出包多出 2 轮） |
| fragment 对齐键 | **`(node_id, type, 节点内序号)`**。⚠️ 不能只用 `node_id`——一个节点里有多个 fragment，只按 node_id 合并会把整个节点压成一条，**248 条 fragment 会塌成 50 条** |
| 正文 | 取**导出包**版本（保留 `[!reference:N]` 行内标记与更完整 THINK 链）；分享链接的差异化文本另存 `content_share`，信息不丢而主版本不被改写 |
| **列表型字段** | **逐条按主键合并**（`results` 按 `url`、`files` 按 `file_id`），**不能整列取一侧** |
| 附件 | 按**归一化 file_id** 合并——导出包是裸 UUID、分享链接带 `file-` 前缀，不归一化会把 4 个附件算成 8 个 |
| `bytes_available` | **或**关系：任一来源拿到字节就算有 |
| 容器兜底 | `raw_mapping`（导出包）与 `raw_messages`（分享链接）**两个都保留**，不做取舍 |

> ⚠️ **「字段空不空」不足以决定怎么合并——要按字段语义分两类**：
>
> - **空字段**（`None` / `""` / `[]` / `{}`）→ 可以整体替换
> - **列表型字段** → 必须逐条按主键合并
>
> 踩过的坑：导出包的 `TOOL_SEARCH` 结果**非空但贫瘠**——每条只有 `{title, url}`，
> 而分享链接侧每条还有 `snippet` / `site_name` / `published_at` / `cite_index`。
> 「只在该字段为空时才补齐」的规则对非空字段不生效，于是**整列富载荷被丢弃**：
> 实测 494 条结果的 **119,948 字符 snippet 归零**，丢掉的正包括 某 issue 那条关键线索的
> 发布说明原文。修法：按 `url` 逐条合并后，缺失的 snippet 才会被补上
> （实测修复后 snippet 字符差 **0**）。
>
> **推论**：跨源合并时，**不能只比"字段有没有"，要比"字段语义是否同类"**——
> 否则最容易被整列替换吃掉的是**证据最密的那一侧**。

**并集后自检的不变式**：按合并键比较多重集——导出包的每个 fragment 键都在、
分享链接的每个 fragment 键都在、没有凭空多出的键。**外加**：
比对两侧 `results` 的 snippet 条数与字符总数，差必须为 0。

### ③ 核对仓库状态

```bash
git log --oneline -10
git status --short
git branch
```

### ④ 合成交接报告

**Codex 有 `task_complete`**：直接从 Agent 自生成报告合成。

**Claude Code 无 `task_complete`**：从文件变更链 + Agent 输出逆推。报告中标注「逆推」。

```markdown
# 上下文恢复报告

## 来源信息
- 平台: [Codex / Claude Code]
- Session ID: [...]
- 工作目录: [...]
- 分支: [...]

## 已完成的工作
[从 task_complete 或文件变更链逆推]

## 验证结果
| 检查项 | 状态 |
|--------|------|
| git log 一致性 | Y/N |
| 工作树匹配 | Y/N |

## 已知问题
## 下一步计划
```

## 提取决策树

```
检测到 task_complete 事件？
├─ YES (Codex) → 直接从 Agent 报告合成
└─ NO (Claude Code)
    ├─ 有 file-history-snapshot？
    │   ├─ YES → 从文件变更链逆推
    │   └─ NO → 从 assistant 文本输出推断
    └─ 需要深入 tool_use 记录

文件是 DeepSeek 导出包（首字符是 '['）？
└─ YES → 用户记得话题吗？
    ├─ 记得 → --scan --kw <关键词> 检索语料库 → 候选给用户挑
    └─ 不记得 → --scan 用泛词试探（如同步 / 上传 / 报错）→ 看 title_match 收敛
        → 定位后 --platform deepseek --ids <id> 打捞 → --verify 自证
        → 需要附件字节 / 缺中间轮次？→ 再走 --platform deepseek-share <share_id>
          → 跨源求并集，用 transcript[].sources[] 标注每个节点出处

用户只给了一个 DeepSeek 分享链接？
└─ YES → --platform deepseek-share <share_id|url> --title "<真实会话名>" --outdir <dir>
```

**导出包 vs 分享链接怎么选**：

| 需求 | 走哪条 |
|------|--------|
| 找会话（只知道话题） | 导出包 + `--scan --kw` |
| 要**附件字节**（图片） | **分享链接**（导出包只有元数据） |
| 要**中间轮次**（用户改过的原始提问） | **导出包**（分享链接只有最终态） |
| 要 `TOOL_OPEN` 载荷 | **分享链接**（导出包未导出该载荷） |
| 要最完整的历史 | **两条都跑，求并集** |

## 核心原则

1. **脚本优先** — 收到 session ID 后，第一步必须运行本 skill 自带脚本，例如 Windows PowerShell: `python "[path-to-skill]/scripts/session_extract.py" --session <id>`；macOS/Linux: `python3 "[path-to-skill]/scripts/session_extract.py" --session <id>`。脚本搜不到再用手搜或其他方法。**Why:** Codex 会话文件名含 `rollout` 前缀和完整时间戳（如 `rollout-2026-06-03T09-16-01-<id>.jsonl`），手搜难以匹配，脚本内置跨平台递归定位一次命中。DeepSeek 场景同理：先 `--scan --kw <关键词>`，别手撸 grep。
2. **先验证，再报告** — 会话记录可能过时，核对 `git log` / `git status` 确认一致性
3. **不加幻觉** — 没有 task_complete 就标注「逆推」
4. **单次遍历** — 所有信息一次提取
5. **空值过滤** — 过滤空消息
6. **不破坏已有工作** — 只读取不写入
7. **忠实优先于美化** — 线性化只是**视图**，不是**替换**。打捞产物必须保留原始结构（DeepSeek 的 `raw_mapping`）作为兜底，任何重分析都从那份原始数据来。**Why:** 聊天树一旦被"整理"成线性消息，分支归属信息就永久丢了；而用户事后往往需要回到原树核对。
8. **打捞后必须自证** — 打捞完立刻跑多重集比较（`--verify`）：源 fragment 签名多重集 == 产物 transcript 多重集，且 `raw_mapping` 与源逐字相等。只比长度不算数——长度相同也可能是"丢一个重复一个"。**Why:** 静默丢数据比报错更危险，用户不会发现少了一段。
9. **不臆造** — 目标会话不在导出里时，如实报告"不在本次导出中"，并附证据（sha256 / 命中计数 / 数据时间边界），把候选全部交给用户挑，而不是拿一个"最像的"糊弄过去。
10. **如实报告导出格式本身的信息损失** — 缺失的载荷要指名道姓，不能含糊带过。DeepSeek 的 `TOOL_OPEN`/`TOOL_FIND` 载荷未导出、附件只有元数据没有图片本体——这些是**导出格式的固有限制**，不是 Agent 漏捞了。产出「附件清单 + 每张图对应的提问」，并明确告知用户需回客户端取图。**Why:** 用户会以为"Agent 明明看到了会话，怎么没把图给我"——含糊其辞比明说"做不到"更伤信任。
11. **元数据 ≠ 内容，要显式区分** — 附件、版本号、文件大小这类**描述性字段**不等于内容本身。清单里用 `bytes_available` / `bytes_available_in_export` 这类布尔字段把「有描述」和「有实体」分开标，跨源比对时才不会把缺字节当成缺记录。**Why:** 一个写着 `file_size: <字节数>` 的条目很容易被当成"图片就在那儿"，而实际上那 <字节数> 字节根本不在包里。
12. **单一来源 ≠ 完整，跨源求并集并标注出处** — 账号导出包是某时刻的快照，分享链接只有最终态，两边各有独占内容（导出包多中间轮次与 某 issue 那类线索，分享链接多附件字节与 `TOOL_OPEN` 载荷）。**并集才是完整历史**，并用 `transcript[].sources[]` / `_meta.sources[]` 标注每个节点来自哪条路径。**Why:** 任何单一来源都有它自己的盲区，不标注出处就无法判断"某段内容到底从哪来、能不能信"。
13. **产物必须自带溯源** — 每份产物都要记下**它从哪来**：源文件路径 + sha256（或 share_id + 端点 + 响应 sha256）、抓取时间、逐项产物的来源。**Why:** 打捞产物会脱离原会话独立流传；没有溯源，三天后没人能证明它是哪一版对话、对应哪一份源数据。

## 已知坑与应对

| 坑 | 表现 | 应对 |
|----|------|------|
| Codex 会话文件名不透明 | 文件名格式 `rollout-YYYY-MM-DDTHH-MM-SS-<session-id>.jsonl`，`find` 用 session ID 前缀搜不到 | 用 `--session <id>` 让脚本自动递归搜索 |
| Codex JSONL 格式与文档描述不一致 | `replay_state.prompt_input` 才是用户消息，不是 `event_msg` | 脚本已处理；如需手动解析，先 `head -1` 看 `session_meta` 确认是 Codex 格式 |
| 大文件（>100MB）流式处理 | Codex 长会话 JSONL 可达 100MB+，`json.load` 全量读取会 OOM | 脚本逐行流式；手动提取也要逐行 `json.loads` |
| 脚本 `--help` 不可用 | 脚本把未知参数当作文件路径解析，不识别 `--help` | 查阅本 SKILL.md 作为唯一文档来源 |
| **DeepSeek 标题是自动摘要** | 标题由 DeepSeek 自动生成，和用户记忆里的措辞**经常**对不上——用户报的原话可能在正文里，一个字都不在标题里；但**也可能完全一致**（实战中目标会话真实标题就是「目标会话」） | **必须搜正文**：标题命中只是加权项，不能只搜标题。反过来也别假定标题一定对不上——两条路都要留 |
| **解压出的文件夹可能是旧快照** | DeepSeek 导出包是下载时刻的快照，而用户解压出来的文件夹可能来自更早的一次下载、且文件名日期与内容不一致 | 打捞前必须分别计算 **zip 内层文件** 与 **解压出的文件夹文件** 的 sha256 并比对，二者不一致时**以 zip 为准**；不要相信文件夹名，也不要相信单次 `md5sum` 的输出（本次即被误报为两者相同） |
| **DeepSeek 聊天树不是列表** | `mapping` 是树，重新生成答案会 fork 出兄弟分支；朴素遍历 `mapping.values()` 会把两条矛盾分支拼在一起 | 从 `root` 深度优先线性化，并补捞 root 不可达的孤儿节点（脚本已实现） |
| **DeepSeek `TOOL_OPEN`/`TOOL_FIND` 载荷未导出** | 这两类 fragment 只有 `type`，没有 `content`/`results` | 如实告知用户这是导出本身的信息损失，无法补救；不要假装"内容都在" |
| **DeepSeek 附件无法打捞** | 导出包只记录附件的 `file_id` / `file_name` / `file_size`，**图片本体不在包内**，zip 里也没有附件目录（只有 `user.json` + `conversations.json` 两个文件） | 导出包路径只能产出「附件清单 + 每张图对应的提问」并标注 `bytes_available_in_export: false`；**要字节就换分享链接路径**（`--platform deepseek-share`），它能直下原图 |
| **单一来源 ≠ 完整历史** | 导出包是某时刻的快照，分享链接只有最终态（用户编辑/删掉的中间轮次不在其中）；两边各有独占内容 | 跨源求**并集**，并用 `transcript[].sources[]` / `_meta.sources[]` **标注每个节点的出处**，别假装单一来源就是全的 |
| **跨源比对正文前必须归一** | 分享侧剥离了 `[!reference:N]` 行内引用标记（引用关系移到 `references` 数组），导出侧保留着 | 归一后再比。实测 48 个共有节点里 19 个报文本差异**全是这个原因**，归一后相似度 0.959–0.991 |
| **资源 URL 不在 DOM 里** | 站点 API 返回了资源路径（如 `signed_path`）但页面**完全不渲染**它——DOM 里没有节点、resource timeline 里也没有请求 | 去前端 bundle 反查 URL 构造函数：在 `main.<hash>.js` 里 `grep -o -E ".{150}signedPath.{250}"`，顺藤摸瓜找到域名与路径前缀的拼接逻辑（DeepSeek 是 `files.deepseeksvc.com` + `/api` + `signed_path` + `?ty=p`），再验证 `Content-Type` 是 `image/*` 而非 `text/html`。**比在 DOM 里瞎找快一个数量级** |
| **失效端点返回 200 + SPA HTML** | 老接口 `/api/v0/share?share_id=` 返回 **HTTP 200**，内容却是首页 HTML | 显式校验响应体：JSON 前缀不是 `{` 就直接报错，别把 HTML 当 JSON 解析 |

### 反面教材：绕路3轮才回到脚本

> **场景** (2026-06-03)：用户调用 `/session-extract <session-uuid>`
>
> Agent 没有先用脚本，而是手动搜了3轮：
> 1. `ls` 当前项目的 `.jsonl` 文件 → 只列出了 Claude Code 会话，没找到目标
> 2. `find ~/.claude/projects -name "<session-uuid>*"` → 无结果
> 3. `find ~/.codex/sessions -name "*<session-uuid>*"` → 无结果（因为 Codex 文件名带 rollout 前缀和时间戳）
> 4. `find` 更广泛范围搜索 → 仍然无结果
>
> 最终用本 skill 自带脚本 `python "[path-to-skill]/scripts/session_extract.py" --session <session-uuid>-...` **一次命中**，脚本在 `~/.codex/sessions/2026/06/03/` 下递归找到完整文件名 `rollout-2026-06-03T09-16-01-<session-uuid>-...jsonl`。
>
> **教训**：脚本已内置递归搜索 + 文件名模式匹配，手搜只会浪费回合。先跑脚本，搜不到再想别的办法。

### 实战案例：DeepSeek 导出包会话打捞

> **场景** (2026-10-29)：用户要从 DeepSeek 网页版导出的 `conversations.json` 里
> 找回一条「某笔记软件 某同步插件 的设备卡在『状态显示为只能下载不能上传』，只能下载没法上传」的会话。
> 用户报的是**状态描述**，真实标题是 `目标会话`（id `<会话 id>`，248 fragments / 25 轮提问 / 4 个附件）。

**处理过程**：

1. **先搜，别猜**。`--scan --kw 某同步插件 --kw 上传 --kw 同步` 扫全语料 fragment 正文，
   出来三个 `title_match` 含 `某专有词小写` 的候选（`<会话 id>` / `<会话 id>` / `<会话 id>`），
   外加一堆只命中通用词「上传」的无关会话（如 `某无关会话`）。
   → **标题命中是区分「真话题」和「顺带提到」的关键信号**，必须把 `title_match` 一起看。
   → 注意标题**既可能**和用户措辞对不上（`<会话 id>` 的「目标会话」vs 用户报的
   「状态显示为只能下载不能上传」），**也可能**完全一致（`<会话 id>` 的「目标会话」）。
   两条路都要留，别预设。
2. **先证明数据边界，再下结论**。第一轮用的是从旧 zip 解压出来的文件夹（较少条会话，
   sha256 `<sha256 前缀 A>…`，数据止于 2026-10-23），`grep -F "1 Sync"` 0 命中。
   → 曾一度误判为「两份导出 md5 完全相同」，**实际是那次 `md5sum` 误报**：
   两个 zip 大小都不同（<大小 A> B vs <大小 B> B），内层 `conversations.json` 的 sha256
   分别是 `<sha256 前缀 A>…`(较少条会话) 和 `<sha256 前缀 B>…`(较多条会话)。
   → **正确做法**：分别算 **zip 内层文件** 与 **解压出的文件夹文件** 的 sha256 并比对，
   不一致时**以 zip 为准**；不要相信文件夹名，也不要相信单次 `md5sum` 的输出。
3. **打捞并自证**。`--platform deepseek --ids ...` 产出 `rescued-session/v1` JSON，
   `--verify` 做多重集比较：源 fragment 签名多重集 == 产物 transcript 多重集，
   `raw_mapping` 与源逐字相等 → **PASS**（三条会话全部 PASS，missing=0 duplicate=0）。
4. **附件只能给清单**。目标会话带 4 个 FILE 附件，但导出 zip 里只有
   `user.json`(<几百字节>) + `conversations.json`——**没有附件目录**，`FILE` fragment 只有
   `file_id`/`file_name`/`file_size`。
   → 产出「附件清单 + 每张图对应的提问原文」，并明确告知用户**图片本体必须回客户端取**。

**教训**：

- 只搜标题 = 什么都搜不到。**搜正文**。
- 通用词（「同步」「上传」）命中再多也不是话题；**标题命中 + IDF 加权**才能把真候选顶上来。
- 快照滞后是**待验证的假设**，不是结论：先算 sha256、查时间边界，再下判断。
- 打捞完必须 `--verify`：**没丢没重**要拿证据说，不能靠"看着对"。
- 导出格式本身缺的东西（图片、工具载荷）要**指名道姓**，别让用户以为是你漏捞了。
