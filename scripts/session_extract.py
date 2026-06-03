#!/usr/bin/env python3
"""
Unified Session Extractor — 支持 Codex 和 Claude Code 两种会话格式。

自动检测平台，单次遍历提取上下文。

用法：
    # 按 session ID 自动定位（自动检测平台）
    python session_extract.py --session <session-id>

    # 直接指定文件（自动检测平台）
    python session_extract.py <session.jsonl>

    # 只输出工作摘要
    python session_extract.py --summary <session.jsonl>

    # 强制指定平台
    python session_extract.py --platform codex <file>
    python session_extract.py --platform claude <file>
"""
import json
import os
import sys
import glob
from collections import Counter


# ============================================================
# Platform Detection
# ============================================================

def find_session(session_id, home=None):
    """
    在 Codex 和 Claude Code 两个目录中搜索 session ID。
    返回 (platform, filepath) 或 (None, None)。
    """
    if home is None:
        home = os.path.expanduser("~")

    # Search Codex sessions
    codex_patterns = [
        os.path.join(home, ".codex", "sessions", "**", "*.jsonl"),
        os.path.join(home, ".codex", "archived_sessions", "*.jsonl"),
    ]
    for pattern in codex_patterns:
        for f in glob.glob(pattern, recursive=True):
            if session_id in os.path.basename(f):
                return ("codex", f)

    # Search Claude Code sessions
    claude_patterns = [
        os.path.join(home, ".claude", "projects", "**", "*.jsonl"),
    ]
    for pattern in claude_patterns:
        for f in glob.glob(pattern, recursive=True):
            if session_id in os.path.basename(f):
                return ("claude", f)

    return (None, None)


def detect_platform(filepath):
    """
    通过文件内容检测平台。
    Codex: 第一行是 session_meta 类型
    Claude Code: 第一行是 user 类型（含 sessionId 字段）
    """
    with open(filepath, "r", encoding="utf-8") as f:
        first = json.loads(f.readline())
    if first.get("type") == "session_meta":
        return "codex"
    elif first.get("type") == "user" and "sessionId" in first:
        return "claude"
    return "unknown"


# ============================================================
# Unified Extraction
# ============================================================

def extract(filepath, platform=None):
    """
    单次遍历 JSONL 文件，根据平台选择提取策略。
    返回统一结构的 dict。
    """
    if platform is None:
        platform = detect_platform(filepath)

    result = {
        "platform": platform,
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

    with open(filepath, "r", encoding="utf-8") as f:
        first_line = True
        for line in f:
            obj = json.loads(line)
            t = obj.get("type", "")

            if platform == "codex":
                _extract_codex(obj, t, result, first_line)
            else:
                _extract_claude(obj, t, result, first_line)

            if first_line:
                first_line = False

    return result


def _extract_codex(obj, t, result, first_line):
    """Codex JSONL 提取逻辑。"""

    # --- session_meta (第 1 行) ---
    if first_line and t == "session_meta":
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

    # --- event_msg ---
    if t == "event_msg":
        p = obj.get("payload", {})
        et = p.get("type", "")
        msg = p.get("message", "")

        if et == "user_message" and msg.strip():
            result["total_user_msgs"] += 1
            result["user_msgs"].append({
                "text": msg.strip()[:2000],
                "timestamp": obj.get("timestamp", ""),
            })

        elif et == "agent_message" and msg.strip():
            result["total_agent_msgs"] += 1
            result["agent_texts"].append({
                "text": msg.strip()[:2000],
                "timestamp": obj.get("timestamp", ""),
            })

        elif et == "task_complete":
            last_msg = p.get("last_agent_message", "")
            if last_msg and last_msg.strip():
                result["agent_texts"].append({
                    "text": last_msg.strip()[:2000],
                    "timestamp": obj.get("timestamp", ""),
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

    # --- turn_context ---
    elif t == "turn_context":
        p = obj.get("payload", {})
        summary = p.get("summary", "")
        model = p.get("model", "")
        if summary and summary.strip():
            result["session_info"]["last_model"] = model

    # --- response_item (工具调用) ---
    elif t == "response_item":
        p = obj.get("payload", {})
        content = p.get("content", [])
        if isinstance(content, list):
            for c in content:
                if c.get("type") == "tool_use":
                    tool_name = c.get("name", "unknown")
                    result["tool_use_counts"][tool_name] += 1

    # --- compacted ---
    elif t == "compacted":
        p = obj.get("payload", {})


def _extract_claude(obj, t, result, first_line):
    """Claude Code JSONL 提取逻辑。"""

    # --- 首条 user 消息提取 session info ---
    if first_line and t == "user":
        result["session_info"] = {
            "sessionId": obj.get("sessionId", ""),
            "cwd": obj.get("cwd", ""),
            "branch": obj.get("gitBranch", ""),
            "version": obj.get("version", ""),
            "entrypoint": obj.get("entrypoint", ""),
        }

    # --- user 消息 ---
    if t == "user":
        result["total_user_msgs"] += 1
        msg = obj.get("message", {})
        content = msg.get("content", "")
        is_meta = obj.get("isMeta", False)

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
                            "timestamp": obj.get("timestamp", ""),
                        })
        elif isinstance(content, str):
            text = content.strip()
            if text:
                result["user_msgs"].append({
                    "text": text[:2000],
                    "isMeta": is_meta,
                    "timestamp": obj.get("timestamp", ""),
                })

    # --- assistant 消息 ---
    elif t == "assistant":
        result["total_agent_msgs"] += 1
        msg = obj.get("message", {})
        content = msg.get("content", [])

        if isinstance(content, list):
            for c in content:
                if c.get("type") == "text":
                    text = c.get("text", "").strip()
                    if text:
                        result["agent_texts"].append({
                            "text": text[:2000],
                            "timestamp": obj.get("timestamp", ""),
                        })
                elif c.get("type") == "tool_use":
                    tool_name = c.get("name", "unknown")
                    result["tool_use_counts"][tool_name] += 1

    # --- system 消息 ---
    elif t == "system":
        subtype = obj.get("subtype", "")
        content = obj.get("content", "")
        if subtype == "api_error":
            result["api_errors"] += 1
        if subtype in ("local_command", "api_error"):
            result["system_events"].append({
                "subtype": subtype,
                "content": (content[:200] if isinstance(content, str) else ""),
            })

    # --- file-history-snapshot ---
    elif t == "file-history-snapshot":
        snap = obj.get("snapshot", {})
        backups = snap.get("trackedFileBackups", {})
        if backups:
            files = list(backups.keys())
            result["file_changes"].append({
                "timestamp": snap.get("timestamp", ""),
                "files": files,
            })
            result["file_change_set"].update(files)

    # --- queue-operation ---
    elif t == "queue-operation":
        content = obj.get("content", "")
        if content and content.strip():
            result["queue_ops"].append({
                "text": content.strip()[:500],
                "timestamp": obj.get("timestamp", ""),
            })

    # --- custom-title / agent-name ---
    elif t == "custom-title":
        title = obj.get("customTitle", "")
        if title:
            result["session_title"] = title
    elif t == "agent-name":
        name = obj.get("agentName", "")
        if name and not result["session_title"]:
            result["session_title"] = name


# ============================================================
# Output
# ============================================================

def print_full(result):
    """格式化输出统一结构。"""
    info = result["session_info"]
    platform = result["platform"]

    # --- SESSION INFO ---
    print("=" * 60)
    print("SESSION INFO")
    print(f"  Platform:  {platform.upper()}")
    if result["session_title"]:
        print(f"  Title:     {result['session_title']}")
    print(f"  SessionID: {info.get('sessionId', 'N/A')}")
    print(f"  CWD:       {info.get('cwd', 'N/A')}")
    if platform == "codex":
        print(f"  Branch:    {info.get('branch', 'N/A')}")
        print(f"  Commit:    {info.get('commit', 'N/A')}")
        print(f"  Repo:      {info.get('repo_url', 'N/A')}")
        print(f"  Model:     {info.get('model', 'N/A')}")
    else:
        print(f"  Branch:    {info.get('branch', 'N/A')}")
        print(f"  Version:   {info.get('version', 'N/A')}")
    print()

    # --- STATISTICS ---
    print("=" * 60)
    print("STATISTICS")
    print(f"  User messages:      {result['total_user_msgs']}")
    print(f"  Agent outputs:      {result['total_agent_msgs']}")
    if platform == "claude":
        print(f"  Tool results:       {result['total_tool_results']}")
        print(f"  API errors:         {result['api_errors']}")
        print(f"  Queue operations:   {len(result['queue_ops'])}")
    else:
        print(f"  Turn aborted:       {result['turn_aborted']}")
        print(f"  Compacted:          {len(result['compacted_summaries'])}")
    print(f"  Files changed:      {len(result['file_change_set'])}")
    print()

    # --- TOOL USE DISTRIBUTION ---
    if result["tool_use_counts"]:
        print("=" * 60)
        print("TOOL USE DISTRIBUTION")
        for tool, count in result["tool_use_counts"].most_common():
            print(f"  {tool}: {count}")
        print()

    # --- USER MESSAGES ---
    real_user_msgs = [m for m in result["user_msgs"] if not m.get("isMeta")]
    if real_user_msgs:
        print("=" * 60)
        print(f"USER MESSAGES ({len(real_user_msgs)} total, showing first 5)")
        for i, m in enumerate(real_user_msgs[:5]):
            ts = m["timestamp"][:19] if m["timestamp"] else ""
            print(f"  [{i+1}] ({ts}) {m['text'][:300]}")
            print()

    # --- FILE CHANGE CHAIN ---
    if result["file_changes"]:
        print("=" * 60)
        print(f"FILE CHANGE CHAIN ({len(result['file_changes'])} events)")
        for fc in result["file_changes"]:
            ts = fc["timestamp"][:19] if fc["timestamp"] else ""
            files_short = [os.path.basename(f) for f in fc["files"]]
            print(f"  [{ts}] {', '.join(files_short)}")
        print()
        print("  ALL CHANGED FILES:")
        for f in sorted(result["file_change_set"]):
            print(f"    {f}")
        print()

    # --- LAST AGENT OUTPUTS ---
    if result["agent_texts"]:
        print("=" * 60)
        # task_complete 优先展示
        task_completes = [a for a in result["agent_texts"] if a.get("is_task_complete")]
        regular = [a for a in result["agent_texts"] if not a.get("is_task_complete")]

        if task_completes:
            print(f"TASK COMPLETES ({len(task_completes)} total)")
            for tc in task_completes[-3:]:
                ts = tc["timestamp"][:19] if tc["timestamp"] else ""
                print(f"  [{ts}] {tc['text'][:500]}")
                print()

        if regular:
            print(f"LAST AGENT OUTPUTS ({len(regular)} total, showing last 3)")
            for at in regular[-3:]:
                ts = at["timestamp"][:19] if at["timestamp"] else ""
                print(f"  [{ts}] {at['text'][:500]}")
                print()

    # --- QUEUE OPERATIONS (Claude Code only) ---
    if result["queue_ops"]:
        print("=" * 60)
        print(f"QUEUED OPERATIONS ({len(result['queue_ops'])})")
        for qo in result["queue_ops"]:
            print(f"  - {qo['text'][:200]}")
        print()

    # --- WORK INFERENCE ---
    print("=" * 60)
    print("WORK INFERENCE")
    if platform == "codex":
        has_tc = any(a.get("is_task_complete") for a in result["agent_texts"])
        if has_tc:
            print("  task_complete 有内容 → 直接从 Agent 自生成报告合成交接报告")
        elif result["agent_texts"]:
            print("  无 task_complete 但有 agent 输出 → 从文本输出推断工作")
        else:
            print("  信息极少 → 需要深入 replay_state/response_item")
    else:
        if result["file_change_set"]:
            print("  有文件变更记录 → 从文件变更链逆推工作摘要")
        elif result["agent_texts"]:
            print("  无文件变更但有 assistant 输出 → 从文本输出推断工作")
        else:
            print("  信息极少 → 需要深入 tool_use 调用记录")
    print()


def print_summary_only(result):
    """只输出工作摘要。"""
    info = result["session_info"]
    platform = result["platform"]
    print(f"Platform: {platform.upper()}")
    print(f"Session:  {result['session_title'] or info.get('sessionId', 'N/A')}")
    print(f"Branch:   {info.get('branch', 'N/A')}")
    print(f"CWD:      {info.get('cwd', 'N/A')}")
    print()

    if result["file_change_set"]:
        print("Files changed:")
        for f in sorted(result["file_change_set"]):
            print(f"  {f}")
        print()

    if result["tool_use_counts"]:
        print("Tool usage:")
        for tool, count in result["tool_use_counts"].most_common():
            print(f"  {tool}: {count}")
        print()

    if result["agent_texts"]:
        print("Last output:")
        print(result["agent_texts"][-1]["text"][:500])


# ============================================================
# CLI
# ============================================================

def main():
    args = sys.argv[1:]

    if not args:
        print("Usage:")
        print("  python session_extract.py <session.jsonl>")
        print("  python session_extract.py --session <session-id>")
        print("  python session_extract.py --summary <session.jsonl>")
        print("  python session_extract.py --platform codex|claude <file>")
        sys.exit(1)

    mode = "full"
    filepath = None
    platform = None
    home = None

    i = 0
    while i < len(args):
        if args[i] == "--session" and i + 1 < len(args):
            session_id = args[i + 1]
            i += 2
            detected_platform, detected_path = find_session(session_id, home)
            if not detected_path:
                print(f"ERROR: Cannot find session file for ID {session_id}")
                print("  Searched: ~/.codex/sessions/ and ~/.claude/projects/")
                sys.exit(1)
            platform = detected_platform
            filepath = detected_path
            print(f"Found [{platform}]: {filepath}")
            print()
        elif args[i] == "--platform" and i + 1 < len(args):
            platform = args[i + 1].lower()
            i += 2
        elif args[i] == "--home" and i + 1 < len(args):
            home = args[i + 1]
            i += 2
        elif args[i] == "--summary" and i + 1 < len(args):
            mode = "summary"
            filepath = args[i + 1]
            i += 2
        else:
            filepath = args[i]
            i += 1

    if not filepath:
        print("ERROR: No input file specified")
        sys.exit(1)

    if not os.path.isfile(filepath):
        print(f"ERROR: File not found: {filepath}")
        sys.exit(1)

    # Auto-detect platform if not specified
    if platform is None:
        platform = detect_platform(filepath)
        if platform == "unknown":
            print("WARNING: Could not detect platform, defaulting to 'codex'")
            platform = "codex"

    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    if size_mb > 10:
        print(f"NOTE: File is {size_mb:.1f}MB, streaming line-by-line")
        print()

    result = extract(filepath, platform)

    if mode == "full":
        print_full(result)
    elif mode == "summary":
        print_summary_only(result)


if __name__ == "__main__":
    main()
