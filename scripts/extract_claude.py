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
from collections import Counter


def extract(filepath):
    """
    单次遍历 Claude Code JSONL 文件，返回统一结构的 dict。
    """
    result = _empty_result()

    with open(filepath, "r", encoding="utf-8") as f:
        first_line = True
        for line in f:
            import json
            obj = json.loads(line)
            t = obj.get("type", "")

            if first_line and t == "user":
                _parse_session_info(obj, result)
                first_line = False

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

            if first_line:
                first_line = False

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
    """首条 user 消息 → 会话元信息。"""
    result["session_info"] = {
        "sessionId": obj.get("sessionId", ""),
        "cwd": obj.get("cwd", ""),
        "branch": obj.get("gitBranch", ""),
        "version": obj.get("version", ""),
        "entrypoint": obj.get("entrypoint", ""),
    }


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
                text = c.get("text", "").strip()
                if text:
                    result["user_msgs"].append({
                        "text": text[:2000],
                        "isMeta": is_meta,
                        "timestamp": ts,
                    })
    elif isinstance(content, str):
        text = content.strip()
        if text:
            result["user_msgs"].append({
                "text": text[:2000],
                "isMeta": is_meta,
                "timestamp": ts,
            })


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
                if text:
                    result["agent_texts"].append({"text": text[:2000], "timestamp": ts})
            elif c.get("type") == "tool_use":
                result["tool_use_counts"][c.get("name", "unknown")] += 1


def _parse_system(obj, result):
    """系统事件: local_command / api_error。"""
    subtype = obj.get("subtype", "")
    content = obj.get("content", "")
    if subtype == "api_error":
        result["api_errors"] += 1
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
    """排队操作: 用户在 Agent 忙时追加的输入。"""
    content = obj.get("content", "")
    if content and content.strip():
        result["queue_ops"].append({
            "text": content.strip()[:500],
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
