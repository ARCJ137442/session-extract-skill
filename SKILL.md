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
| 路径格式 | 脚本内用 `os.path.expanduser()` + `os.path.join()` |
| 编码 | 统一 `encoding='utf-8'` |

## 工作流程

```
① 定位文件（自动检测平台） → ② 单次提取 → ③ 核对仓库 → ④ 合成报告
```

### ① 定位文件

```bash
# 按 session ID 自动定位（推荐）
python scripts/session_extract.py --session <session-id>

# 直接指定文件（自动检测平台）
python scripts/session_extract.py <session.jsonl>

# 强制指定平台
python scripts/session_extract.py --platform codex <file>
python scripts/session_extract.py --platform claude <file>
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

1. **先验证，再报告** — 会话记录可能过时
2. **不加幻觉** — 没有 task_complete 就标注「逆推」
3. **单次遍历** — 所有信息一次提取
4. **空值过滤** — 过滤空消息
5. **不破坏已有工作** — 只读取不写入
