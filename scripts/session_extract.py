#!/usr/bin/env python3
"""
Unified Session Extractor — 支持 Codex、Claude Code 和 DeepSeek 三种会话格式。

自动检测平台，单次遍历提取上下文。

用法：
    python session_extract.py --session <session-id>
    python session_extract.py <session.jsonl>
    python session_extract.py --summary <session.jsonl>
    python session_extract.py --platform codex|claude <file>
    python session_extract.py --platform deepseek <conversations.json> --ids <id>[,<id>] --out <out.json>
    python session_extract.py --platform deepseek <conversations.json> --ids <id>[,<id>] --outdir <dir>
    python session_extract.py --platform deepseek-share <share_id|share_url> --outdir <dir>
    python session_extract.py --scan <conversations.json> --kw 某专有词 --kw 上传
"""
import json
import os
import sys
import glob

# 脚本同目录的兄弟模块（extract_codex / extract_claude / extract_deepseek）。
# 用 `python -I`（隔离模式）运行时，解释器不会把脚本目录放进 sys.path，
# 这里显式补上，否则 `from extract_codex import ...` 会 ModuleNotFoundError。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from extract_codex import extract as extract_codex
from extract_claude import extract as extract_claude
import extract_deepseek
import extract_deepseek_share
import merge_deepseek


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


def detect_platform(filepath, scan_lines=2000):
    """
    通过文件内容检测平台。

    Codex / Claude Code 是 JSONL（一会话一行）；DeepSeek 导出包是**单个 JSON 数组**
    装下全部会话（首字符是 '['），或者单会话 JSON 对象（含 'mapping' 键）。

    ⚠️ **不能只看第一行**。实测 142 个真实 Claude Code 会话里，**59 个（41.5%）**
    的第一行不是 `type=="user"` 的消息，而是 `last-prompt` / `mode` / `ai-title` /
    `file-history-snapshot` 这类**旁路记录**——只看第一行会把它们判成 unknown
    （最新的 8 个会话 8/8 全错）。
    → 改成**扫前 N 行**（实测需要到第 43~174 行），找第一条像消息的记录；
    扫不到再按别的特征判。
    """
    with open(filepath, "r", encoding="utf-8-sig") as f:
        head = f.read(4096).lstrip("\ufeff \t\r\n")
        if not head:
            raise ValueError("empty file: {}".format(filepath))
        if head.startswith("["):
            return "deepseek"
        f.seek(0)
        try:
            first = json.loads(f.readline())
        except ValueError:
            # 整个文件是一个 JSON 对象（单会话 DeepSeek 导出）
            f.seek(0)
            first = json.load(f)

    if not isinstance(first, dict):
        return "unknown"
    if first.get("type") == "session_meta":
        return "codex"
    if "mapping" in first:
        return "deepseek"

    # —— 第一行不是消息时，往后扫若干行 ——
    # Claude Code 的消息行特征：type 是 user/assistant，且带 sessionId / uuid / parentUuid
    # ⚠️ 扫描窗口要够大：实测某些 fork/rewind 旁路 transcript 里，第一条真正的
    # user/assistant 出现在**第 43 行甚至第 174 行**（前面全是
    # last-prompt / mode / permission-mode / attachment 这类旁路记录）。
    with open(filepath, "r", encoding="utf-8-sig") as f:
        f.seek(0)
        for _ in range(scan_lines):
            line = f.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            rtype = rec.get("type")
            if rtype in ("user", "assistant") and (
                    "sessionId" in rec or "uuid" in rec or "parentUuid" in rec):
                return "claude"
            if rtype == "session_meta":
                return "codex"
            if "mapping" in rec:
                return "deepseek"
    return "unknown"

def extract(filepath, platform=None):
    """根据平台分派到对应提取器。"""
    if platform is None:
        platform = detect_platform(filepath)

    if platform == "codex":
        return extract_codex(filepath)
    elif platform == "claude":
        return extract_claude(filepath)
    elif platform == "deepseek":
        return extract_deepseek.extract(filepath)
    else:
        # ⚠️ 不再静默降级成 codex。旧代码在这里打一条 WARNING 就按 codex 解析，
        # 用户会拿到一份**全是 N/A 的假报告**而且 exit 0 —— 比崩溃更伤信任。
        raise ValueError(
            "无法识别会话格式（platform=unknown）。"
            "请用 --platform 显式指定：codex / claude / deepseek。"
            "（若这是 Claude Code 会话，常见原因是文件头几行是旁路记录，"
            "现在会自动往后扫 2000 行，但仍认不出时请手动指定。）"
        )


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
# DeepSeek: 语料库检索 + 打捞
# ============================================================

def print_scan(results, keywords, corpus_count):
    """语料库检索结果（人类可读）。"""
    print(f"corpus: {corpus_count} conversations | keywords: {keywords}")
    print("-" * 70)
    if not results:
        print("no hit")
        return
    for r in results:
        print(f"[{r['score']:>9.1f}] {str(r['id'])[:8]} | {str(r['inserted_at'])[:10]} "
              f"| {r['title']}")
        print(f"        body_hits={r['hits']} title_match={r['title_match']}")
        loc = ", ".join(f"{k}:{v}" for k, v in r["hit_locations"].items())
        print(f"        where={loc}")


def print_deepseek_summary(result):
    """打捞产物摘要（兼容导出包与分享链接两种来源）。"""
    meta = result.get("_meta", {})
    src = meta.get("source", {})
    kind = src.get("kind") or "export-bundle"
    print("=" * 60)
    print(f"RESCUED SESSION (deepseek-web-{kind})")
    if src.get("file"):
        print(f"  Source file:   {src.get('file')}")
    if src.get("share_id"):
        print(f"  Share id:      {src.get('share_id')}")
        print(f"  Share url:     {src.get('share_url')}")
        print(f"  Endpoint:      {src.get('endpoint')}")
    print(f"  Source sha256: {src.get('sha256')}")
    in_source = src.get("conversation_count_in_source")
    if in_source is None and src.get("message_count_in_source") is not None:
        in_source = src.get("message_count_in_source")
    print(f"  Conversations: {meta.get('conversation_count')} / "
          f"{in_source} in source")
    reason = meta.get("extraction", {}).get("rescue_reason")
    if reason:
        print(f"  Reason:        {reason}")
    print()
    for c in result.get("conversations", []):
        st = c.get("stats", {})
        print("=" * 60)
        print(f"  id:       {c.get('id')}")
        print(f"  title:    {c.get('title')}")
        print(f"  inserted: {c.get('inserted_at')}")
        print(f"  updated:  {c.get('updated_at')}")
        print(f"  fragments:{st.get('fragment_count')} "
              f"(user {st.get('user_turn_count')} / assistant {st.get('assistant_turn_count')} / "
              f"think {st.get('thinking_count')} / tool {st.get('tool_fragment_count')})")
        print(f"  by_type:  {st.get('by_type')}")
        print(f"  chars:    {st.get('content_chars')}")
        print()


def run_scan(scan_file, keywords, top, as_json):
    """语料库检索模式。"""
    if not keywords:
        print("ERROR: --scan 需要至少一个 --kw <keyword>")
        sys.exit(1)
    corpus = extract_deepseek.load_corpus(scan_file)
    results = extract_deepseek.scan_corpus(corpus, keywords, top)
    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print_scan(results, keywords, len(corpus))
    return 0


def _write_json(path, obj):
    """统一按 UTF-8 + 不转义中文写 JSON。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _openai_variant_path(out_path):
    """openai 形态产物的文件名：避免出现 export.openai.openai.json 这种双后缀。"""
    stem = os.path.splitext(out_path)[0]
    if stem.endswith(".openai"):
        return stem + ".chatgpt.json"
    return stem + ".openai.json"


def _write_openai_variant(meta, convs, path, title=None):
    """
    额外写一份「ChatGPT 网页版可直接上传」的产物。

    这是「不需要写长脚本」的落地方式：打捞完直接得到一个能上传到
    ChatGPT 网页版的 messages 结构，不必再自己写一遍转换器
    （折叠 fragment、映射引用、归一 URL 锚点）。

    ⚠️ 该产物带 schema/meta/references/citations 等扩展字段，
    **不能直接 POST 给严格的 chat API**——那种场合用
    `to_openai.py --strict`（只留 role / content）。
    """
    try:
        import to_openai
    except ImportError:
        print("WARNING: 找不到 to_openai.py，跳过 openai 形态产物")
        return None
    doc = to_openai.convert({"_meta": meta, "conversations": convs}, title=title)
    _write_json(path, doc)
    print(f"wrote {path}")
    return path


def attachment_manifest(meta, conv):
    """
    附件清单。

    DeepSeek 导出只存附件**元数据**（file_id / file_name / file_size），
    图片二进制不在导出包里——这份清单说明「缺了什么、每个附件回答的是哪一问」。
    """
    return {
        "_meta": meta,
        "conversation_id": conv.get("id"),
        "conversation_title": conv.get("title"),
        "note": (
            "DeepSeek export stores attachment METADATA ONLY "
            "(file_id / file_name / file_size). The image bytes are NOT in "
            "the export zip - they must be re-fetched from the DeepSeek client."
        ),
        "attachments": conv.get("attachments") or [],
    }


def run_deepseek_share_extract(share, title, out, outdir, reason, markdown, verify,
                               as_shape="rescued"):
    """
    DeepSeek 分享链接打捞：公开分享页，不需要账号导出包。

    与导出包路径互补——导出包捞不到附件字节，分享链接能。
    附件落在 <outdir>/附件/ 下，attachments[].local_path 用正斜杠相对路径。
    """
    try:
        result = extract_deepseek_share.extract(
            share, title=title, reason=reason, outdir=outdir
        )
    except Exception as e:
        print(f"ERROR: 分享链接提取失败: {type(e).__name__}: {e}")
        sys.exit(2)

    meta = result.get("_meta", {})
    convs = result.get("conversations", [])

    if outdir:
        outdir = os.path.abspath(outdir)
        os.makedirs(outdir, exist_ok=True)
        for c in convs:
            base = extract_deepseek.safe_filename(c)
            jp = os.path.join(outdir, base + ".json")
            _write_json(jp, {"_meta": meta, "conversations": [c]})
            print(f"wrote {jp}")
            if as_shape == "openai":
                _write_openai_variant(meta, [c],
                                      os.path.join(outdir, base + ".openai.json"),
                                      title=title)
            if c.get("attachments"):
                ap_ = os.path.join(outdir, base + "_attachments.json")
                _write_json(ap_, attachment_manifest(meta, c))
                print(f"wrote {ap_}")
            if markdown:
                mp = os.path.join(outdir, base + ".md")
                extract_deepseek.write_markdown(mp, c)
                print(f"wrote {mp}")
    else:
        out_path = os.path.abspath(out or "rescued_deepseek_share.json")
        out_dir = os.path.dirname(out_path)
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)
        _write_json(out_path, result)
        print(f"wrote {out_path}")
        if as_shape == "openai":
            _write_openai_variant(
                meta, convs,
                _openai_variant_path(out_path),
                title=title)

    print_deepseek_summary(result)

    if verify:
        # 模块的 extract() 不回传原始响应，校验时重新取一次分享 API 原始文档
        try:
            _raw, doc = extract_deepseek_share.fetch_share(
                extract_deepseek_share.normalise_share_id(share)
            )
        except Exception as e:
            print(f"WARNING: 重新取分享 API 原文失败，校验未执行: {type(e).__name__}: {e}")
            doc = None
        ok_all = True
        print("=" * 60)
        print("VERIFY (多重集比较: transcript vs 分享 API 原始响应)")
        for c in convs:
            if doc is None:
                break
            v = extract_deepseek_share.verify_payload(c, doc)
            ok_all = ok_all and v.get("ok", False)
            print(f"  {str(c.get('id'))[:8]} | {c.get('title')} | "
                  f"{'PASS' if v.get('ok') else 'FAIL'}")
            for k, val in (v.get("checks") or {}).items():
                print(f"      {'OK ' if val else 'BAD'} {k}")
            print(f"      source={v.get('source_fragment_count')} "
                  f"output={v.get('output_fragment_count')} "
                  f"missing={v.get('missing_count')} duplicate={v.get('duplicate_count')}")
        print("=" * 60)
        if doc is None:
            # ⚠️ 校验没跑成，**不能报 PASS**——「没验」和「验过了」是两回事，
            # 报个假的 PASS 会让下游以为产物已被自证。
            print("VERIFY RESULT: SKIPPED (未能取回原始响应，本次未校验)")
            sys.exit(4)
        print("VERIFY RESULT:", "PASS" if ok_all else "FAIL")
        if not ok_all:
            sys.exit(3)


def run_deepseek_extract(filepath, ids, extract_all, out, outdir, reason, markdown, verify,
                         as_shape="rescued"):
    """DeepSeek 打捞：抽出指定会话，写成独立 JSON（必要时附 .md 预览和附件清单）。"""
    if not ids and not extract_all:
        print("ERROR: --platform deepseek 需要 --ids <id>[,<id>...] 或 --all")
        print("  不知道 ID 时先用：--scan <conversations.json> --kw <关键词>")
        sys.exit(1)

    try:
        result = extract_deepseek.extract(filepath, ids=ids or None, reason=reason)
    except ValueError as e:
        print(f"ERROR: {e}")
        sys.exit(2)

    meta = result.get("_meta", {})
    convs = result.get("conversations", [])

    if outdir:
        # 一个会话一份文件（+ .md 预览 + 附件清单），与交付产物布局一致
        outdir = os.path.abspath(outdir)
        os.makedirs(outdir, exist_ok=True)
        for c in convs:
            base = extract_deepseek.safe_filename(c)
            jp = os.path.join(outdir, base + ".json")
            _write_json(jp, {"_meta": meta, "conversations": [c]})
            print(f"wrote {jp}")
            if as_shape == "openai":
                _write_openai_variant(meta, [c],
                                      os.path.join(outdir, base + ".openai.json"))
            if c.get("attachments"):
                ap_ = os.path.join(outdir, base + "_attachments.json")
                _write_json(ap_, attachment_manifest(meta, c))
                print(f"wrote {ap_}")
            if markdown:
                mp = os.path.join(outdir, base + ".md")
                extract_deepseek.write_markdown(mp, c)
                print(f"wrote {mp}")
    else:
        out_path = os.path.abspath(out or "rescued_deepseek.json")
        out_dir = os.path.dirname(out_path)
        if out_dir and not os.path.isdir(out_dir):
            os.makedirs(out_dir, exist_ok=True)
        _write_json(out_path, result)
        print(f"wrote {out_path}")
        if as_shape == "openai":
            _write_openai_variant(meta, convs,
                                  os.path.splitext(out_path)[0] + ".openai.json")

        with_attachments = [c for c in convs if c.get("attachments")]
        if with_attachments:
            ap_ = os.path.splitext(out_path)[0] + "_attachments.json"
            _write_json(ap_, {
                "_meta": meta,
                "conversation_count": len(with_attachments),
                "conversations": [
                    {
                        "conversation_id": c.get("id"),
                        "conversation_title": c.get("title"),
                        "note": attachment_manifest(meta, c)["note"],
                        "attachments": c.get("attachments") or [],
                    }
                    for c in with_attachments
                ],
            })
            print(f"wrote {ap_}")

        if markdown:
            base = os.path.splitext(out_path)[0]
            for c in convs:
                mp = os.path.join(os.path.dirname(base), extract_deepseek.safe_filename(c) + ".md")
                extract_deepseek.write_markdown(mp, c)
                print(f"wrote {mp}")

    print_deepseek_summary(result)

    if verify:
        corpus = extract_deepseek.load_corpus(filepath)
        ok_all = True
        print("=" * 60)
        print("VERIFY (多重集比较: transcript vs 源 mapping)")
        for c in convs:
            v = extract_deepseek.verify_payload(c, corpus)
            ok_all = ok_all and v.get("ok", False)
            print(f"  {str(c.get('id'))[:8]} | {c.get('title')} | "
                  f"{'PASS' if v.get('ok') else 'FAIL'}")
            for k, val in (v.get("checks") or {}).items():
                print(f"      {'OK ' if val else 'BAD'} {k}")
            print(f"      source={v.get('source_fragment_count')} "
                  f"output={v.get('output_fragment_count')} "
                  f"missing={v.get('missing_count')} duplicate={v.get('duplicate_count')}")
        print("=" * 60)
        print("VERIFY RESULT:", "PASS" if ok_all else "FAIL")
        if not ok_all:
            sys.exit(3)
    return 0


# ============================================================
# CLI
# ============================================================

USAGE = """Usage:
  python session_extract.py <session.jsonl>
  python session_extract.py --session <session-id>
  python session_extract.py --summary <session.jsonl>
  python session_extract.py --platform codex|claude <file>
  python session_extract.py --platform deepseek <conversations.json> --ids <id>[,<id>] [--out <out.json> | --outdir <dir>]
  python session_extract.py --platform deepseek-share <share_id|share_url> [--out <out.json> | --outdir <dir>]
  python session_extract.py --merge <export-product.json> --merge-with <share-product.json> [--outdir <dir>]
  python session_extract.py --scan <conversations.json> --kw <keyword> [--kw <keyword>...] [--top N]

Output shape:
  --as rescued   rescued-session/v1（默认，忠实产物）
  --as openai    ChatGPT / OpenAI 可直接用的 messages 结构（少写一段转换脚本）

Common flags:
  --title "..."   真实会话名（分享链接侧的标题是占位符，必须人工指定）
  --reason "..."  打捞理由，写进产物的 rescue_reason
  --markdown      额外写 .md 预览
  --verify        多重集自证（没丢没重）"""


def main():
    args = sys.argv[1:]

    if not args:
        print(USAGE)
        sys.exit(1)

    mode = "full"
    filepath = None
    platform = None
    home = None

    # --- 新增（DeepSeek 相关）---
    scan_file = None
    keywords = []
    top = 25
    ids = []
    extract_all = False
    out = None
    outdir = None
    reason = None
    title = None
    merge_export = None
    merge_share = None
    markdown = False
    verify = False
    as_json = False
    as_shape = "rescued"

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
        elif args[i] == "--scan" and i + 1 < len(args):
            scan_file = args[i + 1]
            i += 2
        elif args[i] == "--kw" and i + 1 < len(args):
            keywords.append(args[i + 1])
            i += 2
        elif args[i] == "--top" and i + 1 < len(args):
            top = int(args[i + 1])
            i += 2
        elif args[i] == "--ids" and i + 1 < len(args):
            ids.extend([x.strip() for x in args[i + 1].split(",") if x.strip()])
            i += 2
        elif args[i] == "--all":
            extract_all = True
            i += 1
        elif args[i] == "--out" and i + 1 < len(args):
            out = args[i + 1]
            i += 2
        elif args[i] == "--outdir" and i + 1 < len(args):
            outdir = args[i + 1]
            i += 2
        elif args[i] == "--reason" and i + 1 < len(args):
            reason = args[i + 1]
            i += 2
        elif args[i] == "--title" and i + 1 < len(args):
            title = args[i + 1]
            i += 2
        elif args[i] == "--merge" and i + 1 < len(args):
            merge_export = args[i + 1]
            i += 2
        elif args[i] == "--merge-with" and i + 1 < len(args):
            merge_share = args[i + 1]
            i += 2
        elif args[i] == "--markdown":
            markdown = True
            i += 1
        elif args[i] == "--verify":
            verify = True
            i += 1
        elif args[i] == "--json":
            as_json = True
            i += 1
        elif args[i] == "--as" and i + 1 < len(args):
            as_shape = args[i + 1].lower()
            i += 2
            if as_shape not in ("rescued", "openai"):
                print("ERROR: --as 只能是 rescued 或 openai")
                sys.exit(1)
        elif args[i] in ("--help", "-h"):
            print(USAGE)
            sys.exit(0)
        else:
            filepath = args[i]
            i += 1

    # --- 语料库检索模式（DeepSeek 导出包）---
    if scan_file:
        if not os.path.isfile(scan_file):
            print(f"ERROR: File not found: {scan_file}")
            sys.exit(1)
        sys.exit(run_scan(scan_file, keywords, top, as_json))

    # --- 跨源并集：导出包 × 分享链接 ---
    # 必须在 "No input file specified" 检查之前——并集模式不需要位置参数。
    if merge_export or merge_share:
        if not (merge_export and merge_share):
            print("ERROR: --merge <导出包产物.json> 需要同时给 --merge-with <分享链接产物.json>")
            sys.exit(1)
        for p in (merge_export, merge_share):
            if not os.path.isfile(p):
                print(f"ERROR: File not found: {p}")
                sys.exit(1)
        argv = ["--export", merge_export, "--share", merge_share]
        if out:
            argv += ["--out", out]
        if outdir:
            argv += ["--outdir", outdir]
        if markdown:
            argv += ["--markdown"]
        sys.exit(merge_deepseek.main(argv))

    if not filepath:
        print("ERROR: No input file specified")
        print(USAGE)
        sys.exit(1)

    # --- DeepSeek 分享链接分支：参数是 share_id / URL，不是文件路径 ---
    # 必须排在 os.path.isfile() 检查之前。
    if platform == "deepseek-share":
        sys.exit(run_deepseek_share_extract(
            filepath, title, out, outdir, reason, markdown, verify,
            as_shape=as_shape
        ))

    if not os.path.isfile(filepath):
        print(f"ERROR: File not found: {filepath}")
        sys.exit(1)

    if platform is None:
        try:
            platform = detect_platform(filepath)
        except ValueError as e:
            print(f"ERROR: {e}")
            sys.exit(1)

    # --- DeepSeek 分支：打捞成独立 JSON ---
    if platform == "deepseek":
        sys.exit(run_deepseek_extract(filepath, ids, extract_all, out, outdir, reason,
                                      markdown, verify, as_shape=as_shape))

    if platform == "unknown":
        # ⚠️ 同样不再静默降级：一份「全是 N/A」的报告 + exit 0 是最坏的结果。
        # 明确报错，并告诉用户怎么指定。
        print("ERROR: Could not detect platform from file content.")
        print("  用 --platform 显式指定：codex / claude / deepseek")
        print("  例：python session_extract.py --platform claude " + filepath)
        sys.exit(1)

    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    if size_mb > 10:
        print(f"NOTE: File is {size_mb:.1f}MB, streaming line-by-line")
        print()

    result = extract(filepath, platform)

    if as_json:
        # ⚠️ 以前 --json 只在 --scan 路径被消费，Claude/Codex 路径**完全无视**它
        # —— 一个静默的空开关：用户加了 --json 却拿到人读文本，还以为没生效。
        # 现在它对所有路径都有效：输出机器可读的 JSON。
        out = {
            "platform": result.get("platform"),
            "session_info": result.get("session_info") or {},
            "session_title": result.get("session_title") or "",
            "user_msgs": result.get("user_msgs") or [],
            "agent_texts": result.get("agent_texts") or [],
            "tool_uses": result.get("tool_uses") or [],
            "tool_use_counts": dict(result.get("tool_use_counts") or {}),
            "file_changes": result.get("file_changes") or [],
            "system_events": result.get("system_events") or [],
            "queue_ops": result.get("queue_ops") or [],
            "compacted_summaries": result.get("compacted_summaries") or [],
            "totals": {
                "user_msgs": result.get("total_user_msgs", 0),
                "agent_msgs": result.get("total_agent_msgs", 0),
                "tool_results": result.get("total_tool_results", 0),
                "api_errors": result.get("api_errors", 0),
                "turn_aborted": result.get("turn_aborted", 0),
            },
        }
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0

    if mode == "full":
        print_full(result)
    elif mode == "summary":
        print_summary_only(result)


if __name__ == "__main__":
    main()
