# session-extract

从 Codex / Claude Code 的会话 JSONL 文件，或 DeepSeek 导出的 `conversations.json` 中提取上下文，
自动检测平台，生成结构化的交接报告。

## 用法

`[path-to-skill]` 表示本 skill 的安装目录，即当前 `SKILL.md` 所在目录。
不要在目标仓库里运行相对路径命令，除非当前目录就是本 skill 目录。
推荐先定位 `[path-to-skill]`，再调用 `[path-to-skill]/scripts/...` 下的 bundled scripts。

```powershell
# Windows PowerShell：按 session ID 自动定位并提取
python "[path-to-skill]/scripts/session_extract.py" --session <session-id>

# Windows PowerShell：直接指定文件
python "[path-to-skill]/scripts/session_extract.py" <session.jsonl>

# Windows PowerShell：只输出工作摘要
python "[path-to-skill]/scripts/session_extract.py" --summary <session.jsonl>
```

```bash
# macOS/Linux：按 session ID 自动定位并提取
python3 "[path-to-skill]/scripts/session_extract.py" --session <session-id>

# macOS/Linux：直接指定文件
python3 "[path-to-skill]/scripts/session_extract.py" <session.jsonl>

# macOS/Linux：只输出工作摘要
python3 "[path-to-skill]/scripts/session_extract.py" --summary <session.jsonl>
```

## DeepSeek 导出包（单文件装全部会话）

DeepSeek 的「导出数据」是一个 `conversations.json`，里面是该账号的**全部**会话，
内部是**聊天树**（`mapping`），不是线性消息数组——所以需要单独的提取器。

**只记得话题、不记得 ID 或标题时**（DeepSeek 标题是自动摘要，常与用户记忆的措辞不符）：

```powershell
# 按关键词检索整个语料库（--kw 可重复）
python "[path-to-skill]/scripts/session_extract.py" --scan <conversations.json> --kw 某专有词 --kw 上传 --kw 同步

# 定位后打捞成独立 JSON，并跑多重集比较自证
python "[path-to-skill]/scripts/session_extract.py" --platform deepseek <conversations.json> --ids <id1>,<id2> --out <out.json> --reason "打捞原因" --markdown --verify

# 一个会话一份文件（+ .md 预览 + 附件清单）
python "[path-to-skill]/scripts/session_extract.py" --platform deepseek <conversations.json> --ids <id> --outdir <dir> --markdown
```

```bash
# macOS/Linux
python3 "[path-to-skill]/scripts/session_extract.py" --scan <conversations.json> --kw 某专有词 --kw 上传 --kw 同步
python3 "[path-to-skill]/scripts/session_extract.py" --platform deepseek <conversations.json> --ids <id> --out <out.json> --verify
```

产物 schema `rescued-session/v1`：`transcript` 是聊天树线性化后的可读视图，
`raw_mapping` 原样保留原始结构作为兜底，`attachments` 是附件元数据清单
（**图片二进制不在导出包里**，需要回客户端重新抓）。

## DeepSeek 分享链接（公开链接，无需登录）

与导出包路径**互补**，不是重复：导出包捞不到的**附件字节**，分享链接能下；
分享链接缺的**中间轮次**，导出包有。**并集才是完整历史。**

```powershell
# share_id 裸串或完整分享链接都行；附件落在 <dir>/附件/
python "[path-to-skill]/scripts/session_extract.py" --platform deepseek-share <share_id|share_url> --outdir <dir> --title "真实会话名" --markdown --verify
```

```bash
# macOS/Linux
python3 "[path-to-skill]/scripts/session_extract.py" --platform deepseek-share <share_id|share_url> --outdir <dir> --title "真实会话名"
```

分享 API 只给**占位符**标题（文案会变：见过「来自分享的对话」，也见过英文的
「Shared Conversation」），**真实会话名要用 `--title` 传入**，
否则产物里 `title_is_placeholder=true`，跟导出包路径对不上。

端点与抓取细节见 [`docs/deepseek-share.md`](docs/deepseek-share.md)。

## 喂给别的 LLM：ChatGPT / OpenAI 可直接用

> **本 skill 的迭代方向：不需要写任何长脚本，就能丝滑提取会话信息。**
> 跨工具搬运（折叠 fragment、映射引用、归一 URL 锚点、分离思考链）是**重复劳动**，
> 规则沉淀在 skill 里。

```powershell
# ① 分享链接 → ChatGPT 网页版可上传的 JSON（一条命令）
python "[path-to-skill]/scripts/extract_deepseek_share.py" <share_id|share_url> --format openai --title "真实会话名" > chatgpt.json

# ② 已有产物（任意平台）→ 转换
python "[path-to-skill]/scripts/to_openai.py" <product.json> --out chatgpt.json

# 直接 POST 给 chat API（严格形态：只留 role / content）
python "[path-to-skill]/scripts/to_openai.py" <product.json> --strict > messages.json

# ③ 管道
python "[path-to-skill]/scripts/extract_deepseek_share.py" <share_id|share_url> --format json | python "[path-to-skill]/scripts/to_openai.py" > chatgpt.json

# ④ 调度器：打捞 + 转换一次完成（额外写一份 <base>.openai.json）
python "[path-to-skill]/scripts/session_extract.py" --platform deepseek-share <share_id|share_url> --as openai --outdir <dir>
```

产物形态 `chatgpt-import/v1`：`messages[]` 字段对齐 OpenAI chat messages，
`reasoning`（思考链）单独放不混进正文，`citations` 把正文引用标记映射回来源，
`references` 按归一 URL 去重（同一文档的多个摘要收在 `variants`，不丢）。
加 `--slim` 可产出小文件（**不再是无损产物**）。

## 跨源并集

两条路径都跑完后求并集——**并集才是完整历史**：

```powershell
python "[path-to-skill]/scripts/session_extract.py" --merge <导出包产物.json> --merge-with <分享链接产物.json> --outdir <dir> --markdown
```

产物仍是 `rescued-session/v1`（**不升 v2**），并集信息在 `_meta.merge`，
节点出处标在 `transcript[].sources[]` / `_meta.sources[]`。

## 平台支持

| 平台 | 文件位置 | 核心恢复方式 |
|------|----------|-------------|
| Codex | `~/.codex/sessions/` | `task_complete` 事件 |
| Claude Code | `~/.claude/projects/` | 文件变更链逆推 |
| DeepSeek（导出包） | 导出包内 `conversations.json` | 全语料关键词检索 + 聊天树线性化 |
| DeepSeek（分享链接） | `chat.deepseek.com/share/<id>` | 分享 API 正文 + 附件字节直下 |

## 脚本

| 脚本 | 作用 |
|------|------|
| `scripts/session_extract.py` | 调度器：定位 + 平台探测 + 分发；DeepSeek 的 `--scan` / `--platform deepseek` / `--platform deepseek-share` 入口；`--as rescued\|openai` 选产物形态 |
| `scripts/extract_codex.py` | Codex 提取器，`extract(filepath) -> dict` |
| `scripts/extract_claude.py` | Claude Code 提取器，`extract(filepath) -> dict` |
| `scripts/extract_deepseek.py` | DeepSeek 导出包提取器：schema 解析、树线性化、语料检索、附件清单、`extract(filepath, ids, reason) -> dict`、多重集校验 |
| `scripts/extract_deepseek_share.py` | DeepSeek 分享链接提取器：分享 API 正文、附件字节下载、`--format text\|json\|openai\|raw`、`extract(share, title, reason, outdir) -> dict`、多重集校验 |
| `scripts/merge_deepseek.py` | 跨源并集：导出包 × 分享链接按 `(node_id, type, 节点内序号)` 求并集，保留两侧原始容器 |
| `scripts/verify_merged_deepseek.py` | 并集门禁自检：片段不丢、snippet 不丢、节点集合、逐文档 snippet 未被截短、同文档不重复 |
| `scripts/deepseek_common.py` | 两条路径**共用**的归一规则：占位符标题识别、URL 锚点归一、派生字段剥离 |
| `scripts/to_openai.py` | 产物 → ChatGPT 可用结构（messages 数组 + 引用映射 + 来源清单）；`--strict` 输出只含 `role`/`content` 的严格数组 |
| `tests/test_deepseek_paths.py` | 回归测试（stdlib unittest，**合成数据**，不引第三方依赖） |

## 注意事项

- Windows 用 `python`，macOS/Linux 用 `python3`。
- `python -I`（隔离模式）下解释器不会把脚本目录放进 `sys.path`，脚本已显式兜底。
- 所有文件读取统一 `encoding='utf-8'`；路径统一用 `os.path.expanduser()` / `os.path.join()`。
- stdout 输出统一钉死 UTF-8（Windows 重定向到文件时 locale 编码会吞掉非 ASCII 字符）。
- **脱敏**：脚本与文档里不写死个人机器路径、真实会话内容、真实标题与真实 share_id；
  回归测试一律用合成数据。
- 详细的坑、原则、实战案例见 `SKILL.md`。

## 跑测试

```powershell
python -m unittest discover -s "[path-to-skill]/tests" -v
```
