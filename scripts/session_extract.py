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
from collections import Counter

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


def detect_platform(filepath, max_lines=100):
    """扫描前几条事件检测平台；Claude 文件不保证首行是 user。"""
    normalized = os.path.normcase(os.path.normpath(filepath))
    parts = set(normalized.split(os.sep))
    if ".claude" in parts and "projects" in parts:
        return "claude"
    if ".codex" in parts and ("sessions" in parts or "archived_sessions" in parts):
        return "codex"

    claude_types = {
        "user", "assistant", "system", "file-history-snapshot",
        "queue-operation", "custom-title", "agent-name",
        "mode", "permission-mode",
    }
    codex_types = {
        "session_meta", "turn_context", "response_item",
        "event_msg", "user_message", "agent_reasoning",
    }

    with open(filepath, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i >= max_lines:
                break
            if not line.strip():
                continue
            obj = json.loads(line)
            event_type = obj.get("type", "")
            if event_type == "session_meta":
                return "codex"
            if "sessionId" in obj and event_type in claude_types:
                return "claude"
            if event_type in codex_types:
                return "codex"
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
        raise ValueError(f"Unknown session platform for {filepath}")


def warn(message):
    """诊断信息走 stderr，保证 --json 的 stdout 始终是纯 JSON。"""
    print(message, file=sys.stderr)


# ============================================================
# Output
# ============================================================

def clip(text, limit):
    """截断并显式标记省略，避免读者分不清截断和原文。"""
    text = text or ""
    if len(text) > limit:
        return text[:limit].rstrip() + " …[截断]"
    return text


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
    # 两个口径分开标注：raw = 该类型的事件行数（含 tool_result 等），
    # readable = 过滤噪音后真正可读的消息数。
    readable_user = len([m for m in result["user_msgs"] if not m.get("isMeta")])
    print("=" * 60)
    print("STATISTICS")
    print(f"  User events (raw):  {result['total_user_msgs']}")
    print(f"  Readable user msgs: {readable_user}")
    print(f"  Agent outputs:      {result['total_agent_msgs']}")
    if platform == "claude":
        print(f"  Tool results:       {result['total_tool_results']}")
        print(f"  API errors:         {result['api_errors']}")
        print(f"  Queue operations:   {len(result['queue_ops'])}")
    else:
        print(f"  Turn aborted:       {result['turn_aborted']}")
        print(f"  Compacted:          {len(result['compacted_summaries'])}")
    if platform == "codex":
        print("  Files changed:      N/A（Codex 无文件快照事件，改动看 task_complete/git 记录）")
    else:
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
    # 交接场景里"最后几条"才是当前状态，首条只用于确认任务起点，
    # 所以展示 first 3 + last 5。
    real_user_msgs = [m for m in result["user_msgs"] if not m.get("isMeta")]
    if real_user_msgs:
        print("=" * 60)
        total = len(real_user_msgs)
        if total <= 8:
            shown = list(enumerate(real_user_msgs, 1))
            print(f"USER MESSAGES ({total} total)")
        else:
            head = list(enumerate(real_user_msgs[:3], 1))
            tail = list(enumerate(real_user_msgs[-5:], total - 4))
            shown = head + [None] + tail
            print(f"USER MESSAGES ({total} total, showing first 3 + last 5)")
        for item in shown:
            if item is None:
                print(f"  ... ({total - 8} 条省略) ...")
                print()
                continue
            i, m = item
            ts = m["timestamp"][:19] if m["timestamp"] else ""
            print(f"  [{i}] ({ts}) {clip(m['text'], 300)}")
            print()

    # --- FILE CHANGE CHAIN ---
    # 按文件聚合（首次/末次快照时间 + 次数），避免同一文件逐快照刷屏。
    if result["file_changes"]:
        print("=" * 60)
        per_file = {}
        for fc in result["file_changes"]:
            ts = fc["timestamp"][:19] if fc["timestamp"] else ""
            for f in fc["files"]:
                entry = per_file.setdefault(f, {"first": ts, "last": ts, "count": 0})
                entry["count"] += 1
                if ts:
                    if not entry["first"] or ts < entry["first"]:
                        entry["first"] = ts
                    if ts > entry["last"]:
                        entry["last"] = ts
        print(f"FILE CHANGE CHAIN ({len(result['file_changes'])} snapshots, {len(per_file)} files)")
        for f, entry in sorted(per_file.items(), key=lambda kv: -kv[1]["count"]):
            span = entry["first"] if entry["first"] == entry["last"] else f"{entry['first']} → {entry['last']}"
            print(f"  [{entry['count']:>3} 次] {span}")
            print(f"        {f}")
        print()

    # --- LAST AGENT OUTPUTS ---
    if result["agent_texts"]:
        print("=" * 60)
        task_completes = [a for a in result["agent_texts"] if a.get("is_task_complete")]
        regular = [a for a in result["agent_texts"] if not a.get("is_task_complete")]

        if task_completes:
            print(f"TASK COMPLETES ({len(task_completes)} total, showing last 3)")
            for tc in task_completes[-3:]:
                ts = tc["timestamp"][:19] if tc["timestamp"] else ""
                print(f"  [{ts}] {clip(tc['text'], 500)}")
                print()

        if regular:
            print(f"LAST AGENT OUTPUTS ({len(regular)} total, showing last 3)")
            for at in regular[-3:]:
                ts = at["timestamp"][:19] if at["timestamp"] else ""
                print(f"  [{ts}] {clip(at['text'], 500)}")
                print()

    # --- QUEUE OPERATIONS (Claude Code only) ---
    if result["queue_ops"]:
        print("=" * 60)
        print(f"QUEUED OPERATIONS ({len(result['queue_ops'])})")
        for qo in result["queue_ops"]:
            print(f"  - {clip(qo['text'], 200)}")
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


def print_json(result):
    """输出机器可读结构，兼容 Counter/set。"""
    payload = dict(result)
    payload["readable_user_msgs"] = len([m for m in result["user_msgs"] if not m.get("isMeta")])

    def normalize(value):
        if isinstance(value, Counter):
            return dict(value)
        if isinstance(value, set):
            return sorted(value)
        if isinstance(value, dict):
            return {k: normalize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [normalize(v) for v in value]
        return value

    print(json.dumps(normalize(payload), ensure_ascii=False, indent=2))


def print_usage():
    print("Usage:")
    print("  python session_extract.py <session.jsonl>")
    print("  python session_extract.py --session <session-id>")
    print("  python session_extract.py --summary <session.jsonl>")
    print("  python session_extract.py --platform codex|claude <file>")
    print("  python session_extract.py --json --session <session-id>")


# ============================================================
# CLI
# ============================================================

def main():
    args = sys.argv[1:]

    if any(arg in ("--help", "-h") for arg in args):
        print_usage()
        sys.exit(0)

    if not args:
        print_usage()
        sys.exit(1)

    mode = "full"
    output_json = "--json" in args
    filepath = None
    platform = None
    home = None
    if "--home" in args:
        home_index = args.index("--home")
        if home_index + 1 < len(args):
            home = args[home_index + 1]

    i = 0
    while i < len(args):
        if args[i] == "--json":
            output_json = True
            i += 1
        elif args[i] == "--session" and i + 1 < len(args):
            session_id = args[i + 1]
            i += 2
            detected_platform, detected_path = find_session(session_id, home)
            if not detected_path:
                warn(f"ERROR: Cannot find session file for ID {session_id}")
                warn("  Searched: ~/.codex/sessions/ and ~/.claude/projects/")
                sys.exit(1)
            platform = detected_platform
            filepath = detected_path
            if not output_json:
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
        warn("ERROR: No input file specified")
        sys.exit(1)

    if not os.path.isfile(filepath):
        warn(f"ERROR: File not found: {filepath}")
        sys.exit(1)

    if platform is None:
        platform = detect_platform(filepath)
        if platform == "unknown":
            warn(f"ERROR: Could not detect session platform for {filepath}")
            sys.exit(1)

    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    if size_mb > 10:
        warn(f"NOTE: File is {size_mb:.1f}MB, streaming line-by-line")

    result = extract(filepath, platform)

    # 自检：大文件却解析不到任何工具调用，通常是会话格式演进导致
    # 匹配落空（曾发生：Codex tool_use → function_call），显式警告
    # 而不是静默输出空统计。
    if not result["tool_use_counts"] and size_mb > 1:
        warn("WARNING: 解析到 0 次工具调用但文件超过 1MB —— 会话格式可能已演进，")
        warn("         工具统计可能失真，请核对 JSONL 事件结构。")

    if output_json:
        print_json(result)
    elif mode == "full":
        print_full(result)
    elif mode == "summary":
        print_summary_only(result)


if __name__ == "__main__":
    main()
