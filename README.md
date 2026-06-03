# session-extract

从 Codex 或 Claude Code 会话 JSONL 文件中提取上下文，自动检测平台，生成结构化交接报告。

## 用法

```bash
# 按 session ID 自动定位并提取
python scripts/session_extract.py --session <session-id>

# 直接指定文件
python scripts/session_extract.py <session.jsonl>

# 只输出工作摘要
python scripts/session_extract.py --summary <session.jsonl>
```

## 平台支持

| 平台 | 文件位置 | 核心恢复方式 |
|------|----------|-------------|
| Codex | `~/.codex/sessions/` | `task_complete` 事件 |
| Claude Code | `~/.claude/projects/` | 文件变更链逆推 |
