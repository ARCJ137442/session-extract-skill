#!/usr/bin/env python3
"""
Unified Session Extractor — 支持 Codex 和 Claude Code 两种会话格式。

自动检测平台，单次遍历提取上下文。

用法：
    python session_extract.py --session <session-id>
    python session_extract.py <session.jsonl>
    python session_extract.py --summary <session.jsonl>
    python session_extract.py --platform codex|claude <file>
"""
import json
import os
import sys
import glob

from extract_codex import extract as extract_codex
from extract_claude import extract as extract_claude


# ============================================================
# Platform Detection
# ============================================================

def find_session(session_id, home=None):
    """在 Codex 和 Claude Code 两个目录中搜索 session ID。"""
    if home is None:
        home = os.path.expanduser("~")

    for pattern in [
        os.path.join(home, ".codex", "sessions", "**", "*.jsonl"),
        os.path.join(home, ".codex", "archived_sessions", "*.jsonl"),
    ]:
        for f in glob.glob(pattern, recursive=True):
            if session_id in os.path.basename(f):
                return ("codex", f)

    for pattern in [
        os.path.join(home, ".claude", "projects", "**", "*.jsonl"),
    ]:
        for f in glob.glob(pattern, recursive=True):
            if session_id in os.path.basename(f):
                return ("claude", f)

    return (None, None)


def detect_platform(filepath):
    """通过文件首行检测平台。"""
    with open(filepath, "r", encoding="utf-8") as f:
        first = json.loads(f.readline())
    if first.get("type") == "session_meta":
        return "codex"
    elif first.get("type") == "user" and "sessionId" in first:
        return "claude"
    return "unknown"


def extract(filepath, platform=None):
    """根据平台分派到对应提取器。"""
    if platform is None:
        platform = detect_platform(filepath)

    if platform == "codex":
        return extract_codex(filepath)
    elif platform == "claude":
        return extract_claude(filepath)
    else:
        print("WARNING: Unknown platform, trying codex")
        return extract_codex(filepath)


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
