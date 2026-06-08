---
name: session-extract
description: |
  Use when a user provides a session ID or session JSONL file from another Agent
  (Codex or Claude Code) and needs to understand and continue that work.
  Also triggered by phrases like "恢复上下文", "接手工作", "之前做到哪了",
  "从聊天记录恢复", "交接", "handoff".
  Automatically detects the platform (Codex vs Claude Code) from the session file.
  NOT when the current conversation already has sufficient context.
---

# Session Extract

## 概述

**核心能力**：从 Codex 或 Claude Code 的会话 JSONL 文件中提取上下文，自动检测平台，生成结构化的交接报告。

一个脚本覆盖两个平台，通过 session ID 自动定位文件并检测格式。

## 平台差异

| 维度 | Codex | Claude Code |
|------|-------|-------------|
| 文件位置 | `~/.codex/sessions/YYYY/MM/DD/` | `~/.claude/projects/<project-dir>/` |
| 核心恢复入口 | `task_complete`（Agent 自生成报告） | `file-history-snapshot`（文件变更链逆推） |
| 元数据 | 第 1 行 `session_meta` | 首条 `user` 消息的 metadata |
| Agent 输出 | `event_msg` 中的 `agent_message` | `assistant` 类型消息 |
| 工具调用 | `response_item` 中的 `tool_use` | `assistant.message.content` 中的 `tool_use` |

## 跨平台注意

| 问题 | 解决方案 |
|------|----------|
| Python 可执行名 | Windows: `python`，macOS/Linux: `python3` |
| 脚本路径 | 先定位 `[path-to-skill]`，再运行 `[path-to-skill]/scripts/session_extract.py`，不要假定目标仓库有 `scripts/session_extract.py` |
| 路径格式 | 脚本内用 `os.path.expanduser()` + `os.path.join()` |
| 编码 | 统一 `encoding='utf-8'` |

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
```

## 核心原则

1. **脚本优先** — 收到 session ID 后，第一步必须运行本 skill 自带脚本，例如 Windows PowerShell: `python "[path-to-skill]/scripts/session_extract.py" --session <id>`；macOS/Linux: `python3 "[path-to-skill]/scripts/session_extract.py" --session <id>`。脚本搜不到再用手搜或其他方法。**Why:** Codex 会话文件名含 `rollout` 前缀和完整时间戳（如 `rollout-2026-06-03T09-16-01-<id>.jsonl`），手搜难以匹配，脚本内置跨平台递归定位一次命中。
2. **先验证，再报告** — 会话记录可能过时，核对 `git log` / `git status` 确认一致性
3. **不加幻觉** — 没有 task_complete 就标注「逆推」
4. **单次遍历** — 所有信息一次提取
5. **空值过滤** — 过滤空消息
6. **不破坏已有工作** — 只读取不写入

## 已知坑与应对

| 坑 | 表现 | 应对 |
|----|------|------|
| Codex 会话文件名不透明 | 文件名格式 `rollout-YYYY-MM-DDTHH-MM-SS-<session-id>.jsonl`，`find` 用 session ID 前缀搜不到 | 用 `--session <id>` 让脚本自动递归搜索 |
| Codex JSONL 格式与文档描述不一致 | `replay_state.prompt_input` 才是用户消息，不是 `event_msg` | 脚本已处理；如需手动解析，先 `head -1` 看 `session_meta` 确认是 Codex 格式 |
| 大文件（>100MB）流式处理 | Codex 长会话 JSONL 可达 100MB+，`json.load` 全量读取会 OOM | 脚本逐行流式；手动提取也要逐行 `json.loads` |
| 脚本 `--help` 不可用 | 脚本把未知参数当作文件路径解析，不识别 `--help` / `--all` | 查阅本 SKILL.md 作为唯一文档来源 |

### 反面教材：绕路3轮才回到脚本

> **场景** (2026-06-03)：用户调用 `/session-extract 019e8b0d-3e8e-7ca0-a823-c2cad1fd8e85`
>
> Agent 没有先用脚本，而是手动搜了3轮：
> 1. `ls` 当前项目的 `.jsonl` 文件 → 只列出了 Claude Code 会话，没找到目标
> 2. `find ~/.claude/projects -name "019e8b0d*"` → 无结果
> 3. `find ~/.codex/sessions -name "*019e8b0d*"` → 无结果（因为 Codex 文件名带 rollout 前缀和时间戳）
> 4. `find` 更广泛范围搜索 → 仍然无结果
>
> 最终用本 skill 自带脚本 `python "[path-to-skill]/scripts/session_extract.py" --session 019e8b0d-...` **一次命中**，脚本在 `~/.codex/sessions/2026/06/03/` 下递归找到完整文件名 `rollout-2026-06-03T09-16-01-019e8b0d-...jsonl`。
>
> **教训**：脚本已内置递归搜索 + 文件名模式匹配，手搜只会浪费回合。先跑脚本，搜不到再想别的办法。
