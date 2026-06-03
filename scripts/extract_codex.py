"""
Codex JSONL 提取器。

Codex 会话格式是事件驱动的：
- 第 1 行: session_meta（cwd, branch, commit, repo_url）
- event_msg: user_message / agent_message / task_complete / turn_aborted / context_compacted
- response_item: 工具调用（tool_use 块）
- turn_context: 模型切换 / 上下文截断
- compacted: 上下文压缩摘要

核心价值点: task_complete 事件（Agent 自生成的结构化工作总结）。
"""
import json
from collections import Counter


def extract(filepath):
    """
    单次遍历 Codex JSONL 文件，返回统一结构的 dict。
    """
    result = _empty_result()

    with open(filepath, "r", encoding="utf-8") as f:
        first_line = True
        for line in f:
            obj = json.loads(line)
            t = obj.get("type", "")

            if first_line and t == "session_meta":
                _parse_session_meta(obj, result)
                first_line = False
                continue

            if first_line:
                first_line = False

            if t == "event_msg":
                _parse_event_msg(obj, result)
            elif t == "turn_context":
                _parse_turn_context(obj, result)
            elif t == "response_item":
                _parse_response_item(obj, result)

    return result


def _empty_result():
    return {
        "platform": "codex",
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


def _parse_session_meta(obj, result):
    """第 1 行: session_meta → 仓库元信息。"""
    p = obj.get("payload", {})
    git = p.get("git", {})
    result["session_info"] = {
        "sessionId": "",
        "cwd": p.get("cwd", ""),
        "branch": git.get("branch", ""),
        "commit": git.get("commit_hash", ""),
        "repo_url": git.get("repository_url", ""),
        "model": p.get("model_provider", ""),
        "timestamp": p.get("timestamp", ""),
    }


def _parse_event_msg(obj, result):
    """事件流: user_message / agent_message / task_complete / turn_aborted / context_compacted。"""
    p = obj.get("payload", {})
    et = p.get("type", "")
    msg = p.get("message", "")
    ts = obj.get("timestamp", "")

    if et == "user_message" and msg.strip():
        result["total_user_msgs"] += 1
        result["user_msgs"].append({"text": msg.strip()[:2000], "timestamp": ts})

    elif et == "agent_message" and msg.strip():
        result["total_agent_msgs"] += 1
        result["agent_texts"].append({"text": msg.strip()[:2000], "timestamp": ts})

    elif et == "task_complete":
        last_msg = p.get("last_agent_message", "")
        if last_msg and last_msg.strip():
            result["agent_texts"].append({
                "text": last_msg.strip()[:2000],
                "timestamp": ts,
                "is_task_complete": True,
            })

    elif et == "turn_aborted":
        result["turn_aborted"] += 1

    elif et == "context_compacted":
        rh = p.get("replacement_history", "")
        if rh:
            result["compacted_summaries"].append(
                rh[:500] if isinstance(rh, str) else json.dumps(rh, ensure_ascii=False)[:500]
            )


def _parse_turn_context(obj, result):
    """Turn 切换: 模型信息。"""
    p = obj.get("payload", {})
    model = p.get("model", "")
    if model:
        result["session_info"]["last_model"] = model


def _parse_response_item(obj, result):
    """工具调用: response_item.content 中的 tool_use 块。"""
    p = obj.get("payload", {})
    content = p.get("content", [])
    if isinstance(content, list):
        for c in content:
            if c.get("type") == "tool_use":
                result["tool_use_counts"][c.get("name", "unknown")] += 1
