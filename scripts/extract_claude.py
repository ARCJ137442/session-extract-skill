"""
Claude Code JSONL 提取器。

Claude Code 会话格式是角色驱动的：
- 首条 user 消息含 sessionId / cwd / gitBranch / version
- user: 用户输入（text / tool_result / command-message）
- assistant: Agent 输出（text + tool_use 块）
- system: 本地命令 / API 错误 / turn 时长
- file-history-snapshot: 文件变更快照（trackedFileBackups）
- queue-operation: 用户排队输入
- custom-title / agent-name: 会话标题

核心价值点: file-history-snapshot（精确的文件变更链，用于逆推工作摘要）。
"""
import html
import re
from collections import Counter


# 与 session-review skill 的 scan_common.NOISE_PREFIXES 保持同步：
# 命令包装 / 系统提醒 / 任务通知不是用户说的话，不进可读消息列表。
NOISE_PREFIXES = (
    "<local-command",
    "<command",
    "<system-reminder",
    "<task-notification",
    "<task-",
    "<agent-message",
    "<teammate-message",
    "Another Claude session sent a message:",
    "This came from another Claude session",
    "Background agent ",
    "Base directory for this skill:",
    "```\nBase directory",
)

NOISE_PATTERNS = (
    re.compile(r"^\d+\s+background agents?\s+(?:were\s+)?stopped\b", re.IGNORECASE),
)


def _is_noise(text):
    stripped = (text or "").strip()
    folded = stripped.casefold()
    return (
        any(folded.startswith(prefix.casefold()) for prefix in NOISE_PREFIXES)
        or any(pattern.search(stripped) for pattern in NOISE_PATTERNS)
    )


def extract(filepath):
    """
    单次遍历 Claude Code JSONL 文件，返回统一结构的 dict。
    """
    result = _empty_result()

    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            import json
            obj = json.loads(line)
            t = obj.get("type", "")

            _parse_session_info(obj, result)

            if t == "user":
                _parse_user(obj, result)
            elif t == "assistant":
                _parse_assistant(obj, result)
            elif t == "system":
                _parse_system(obj, result)
            elif t == "file-history-snapshot":
                _parse_file_snapshot(obj, result)
            elif t == "queue-operation":
                _parse_queue_op(obj, result)
            elif t in ("custom-title", "agent-name"):
                _parse_title(obj, result)

    return result


def _empty_result():
    return {
        "platform": "claude",
        "session_info": {},
        "user_msgs": [],
        "agent_texts": [],
        "tool_uses": [],
        "tool_use_counts": Counter(),
        "file_changes": [],
        "file_change_set": set(),
        "system_events": [],
        "queue_ops": [],
        "session_title": "",
        "total_user_msgs": 0,
        "total_agent_msgs": 0,
        "total_tool_results": 0,
        "api_errors": 0,
        "turn_aborted": 0,
        "compacted_summaries": [],
    }


def _parse_session_info(obj, result):
    """从任意事件补齐会话元信息；有些 Claude JSONL 前几行不是 user。"""
    info = result["session_info"]
    if not info:
        info.update({
            "sessionId": "",
            "cwd": "",
            "branch": "",
            "version": "",
            "entrypoint": "",
        })

    field_map = {
        "sessionId": "sessionId",
        "cwd": "cwd",
        "gitBranch": "branch",
        "version": "version",
        "entrypoint": "entrypoint",
    }
    for source_key, target_key in field_map.items():
        value = obj.get(source_key, "")
        if value and not info.get(target_key):
            info[target_key] = value


def _parse_user(obj, result):
    """用户消息: text / tool_result / command-message。"""
    result["total_user_msgs"] += 1
    msg = obj.get("message", {})
    content = msg.get("content", "")
    is_meta = obj.get("isMeta", False)
    ts = obj.get("timestamp", "")

    if isinstance(content, list):
        for c in content:
            if c.get("type") == "tool_result":
                result["total_tool_results"] += 1
            elif c.get("type") == "text":
                text = _readable_user_text(c.get("text", ""))
                if text:
                    _append_user_msg(result, text, is_meta, ts)
    elif isinstance(content, str):
        text = _readable_user_text(content)
        if text:
            _append_user_msg(result, text, is_meta, ts)


def _append_user_msg(result, text, is_meta, timestamp):
    """合并堆叠 slash command 展开产生的相邻重复指令。"""
    if result["user_msgs"]:
        prev = result["user_msgs"][-1]
        if prev["text"] == text[:2000] and prev["timestamp"][:19] == timestamp[:19]:
            return
    result["user_msgs"].append({
        "text": text[:2000],
        "isMeta": is_meta,
        "timestamp": timestamp,
    })


def _readable_user_text(text):
    """保留 slash command 的真实参数，过滤 wrapper 本体。

    Claude 把 `/goal xxx` 等命令记录为 XML-like wrapper；wrapper 是噪音，
    但 command-args 往往正是用户指令，交接时不能丢。
    """
    text = (text or "").strip()
    if not text:
        return ""
    command_args = _extract_command_args(text)
    if command_args:
        return command_args
    if _is_noise(text):
        return ""
    return text


def _extract_command_args(text):
    match = re.search(r"<command-args>(.*?)</command-args>", text, flags=re.DOTALL)
    if not match:
        return ""
    args = html.unescape(match.group(1)).strip()
    return " ".join(args.split())


def _parse_assistant(obj, result):
    """Agent 输出: text + tool_use。"""
    result["total_agent_msgs"] += 1
    msg = obj.get("message", {})
    content = msg.get("content", [])
    ts = obj.get("timestamp", "")

    if isinstance(content, list):
        for c in content:
            if c.get("type") == "text":
                text = c.get("text", "").strip()
                if text and not _is_assistant_noise(text):
                    result["agent_texts"].append({"text": text[:2000], "timestamp": ts})
            elif c.get("type") == "tool_use":
                result["tool_use_counts"][c.get("name", "unknown")] += 1


def _is_assistant_noise(text):
    return (text or "").strip() in {"No response requested."}


def _parse_system(obj, result):
    """系统事件: local_command / api_error。"""
    subtype = obj.get("subtype", "")
    content = obj.get("content", "")
    if subtype == "api_error":
        result["api_errors"] += 1
    if subtype == "local_command" and isinstance(content, str):
        command_args = _extract_command_args(content)
        if command_args:
            _append_user_msg(result, command_args, False, obj.get("timestamp", ""))
    if subtype in ("local_command", "api_error"):
        result["system_events"].append({
            "subtype": subtype,
            "content": (content[:200] if isinstance(content, str) else ""),
        })


def _parse_file_snapshot(obj, result):
    """文件变更快照: trackedFileBackups → 变更链。"""
    snap = obj.get("snapshot", {})
    backups = snap.get("trackedFileBackups", {})
    if backups:
        files = list(backups.keys())
        result["file_changes"].append({
            "timestamp": snap.get("timestamp", ""),
            "files": files,
        })
        result["file_change_set"].update(files)


def _parse_queue_op(obj, result):
    """排队操作: 用户在 Agent 忙时追加的输入。

    task-notification 等 harness 通知也走 queue-operation 事件，
    但那不是用户输入，过滤掉；多行内容压成单行便于列表展示。"""
    content = obj.get("content", "")
    text = content.strip() if content else ""
    if not text or _is_noise(text):
        return
    result["queue_ops"].append({
        "text": " ".join(text.split())[:500],
        "timestamp": obj.get("timestamp", ""),
    })


def _parse_title(obj, result):
    """会话标题: custom-title / agent-name。"""
    if obj.get("type") == "custom-title":
        title = obj.get("customTitle", "")
        if title:
            result["session_title"] = title
    elif obj.get("type") == "agent-name":
        name = obj.get("agentName", "")
        if name and not result["session_title"]:
            result["session_title"] = name
