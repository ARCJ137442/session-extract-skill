# session-extract

从 Codex 或 Claude Code 会话 JSONL 文件中提取上下文，自动检测平台，生成结构化交接报告。

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

## 平台支持

| 平台 | 文件位置 | 核心恢复方式 |
|------|----------|-------------|
| Codex | `~/.codex/sessions/` | `task_complete` 事件 |
| Claude Code | `~/.claude/projects/` | 文件变更链逆推 |
