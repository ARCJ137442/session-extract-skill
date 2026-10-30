# -*- coding: utf-8 -*-
"""
把任意 rescued-session/v1 产物转成「**目标工具可直接吃**」的会话 JSON。

存在的理由：**跨工具搬运是重复劳动**。
把一份 DeepSeek 会话喂给 ChatGPT 网页版，要做的事每次都一样——
把 fragment 流折叠成 role/content、把工具调用收成引用映射、
把 URL 锚点归一、给思考链找个不污染对话的落脚点。
这些规则是**平台无关**的，不该每次都烧在一个 300 行的临时脚本里。

用法（三选一，都是零长脚本）：

    # 1) 分享链接 → ChatGPT 可用 JSON，一条命令
    python scripts/extract_deepseek_share.py <share_url> --format openai > chatgpt.json

    # 2) 已有产物（任意平台）→ 转换
    python scripts/to_openai.py <product.json> --out chatgpt.json

    # 3) 管道
    python scripts/extract_deepseek_share.py <share_url> --format json \
        | python scripts/to_openai.py > chatgpt.json

输出结构（顶层 keys）：
    schema / title / note / meta / messages / references / raw_*
    - messages[] 字段对齐 OpenAI chat messages（role / content）
    - reasoning   DeepSeek 思考链（**不混进 content**，否则污染对话）
    - citations   正文 [reference:N] 标记 → 来源 URL 的映射
    - references  去重后的来源清单（按归一 URL，同文档的变体全留在 variants）
    - fragments   原始 fragment 全字段（无损；--slim 可去掉）
    - raw_messages / raw_mapping  源容器逐字副本（无损兜底）

保真原则：**只搬运，不改写**。
正文一字不动；派生字段只做**新增**（url_normalized / url_anchor），
绝不覆盖源数据里的任何字段。
"""
import argparse
import json
import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
try:
    from deepseek_common import norm_url, url_anchor
except ImportError:  # pragma: no cover
    raise RuntimeError("deepseek_common.py is missing next to this script.")

SCHEMA = "chatgpt-import/v1"

# 会话正文里可能出现的行内引用标记：分享链接侧是 [reference:N]，
# 账号导出包侧是 [!reference:N]（见 docs/deepseek-share.md 第四节）。
REF_MARKER_RE = None  # 运行时用 re 编译，避免顶层 import re 时的额外依赖


def _ref_marker_re():
    global REF_MARKER_RE
    if REF_MARKER_RE is None:
        import re
        REF_MARKER_RE = re.compile(r"\[!?reference:(\d+)\]")
    return REF_MARKER_RE


def _frag_results(frag):
    """fragment 的结果载荷，统一成列表（TOOL_SEARCH 是 results[]，TOOL_OPEN 是 result{}）。"""
    if not isinstance(frag, dict):
        return []
    if frag.get("results") is not None:
        return frag["results"] or []
    if frag.get("result") is not None:
        return [frag["result"]]
    return []


def _build_fragment_index(product):
    """
    建立 fragment id → (node_id, type, results) 的索引。

    引用标记 [reference:N] 里的 N 就是 fragment id，必须靠它把
    「正文里的一句话」和「它引用的那个网页」对上。
    优先用源容器（raw_messages / raw_mapping）——那里有 fragment id；
    没有源容器时退化为「同节点同类型」匹配（精度略降但不丢内容）。
    """
    by_id = {}
    by_node_type = {}

    def register(node_id, frag):
        if not isinstance(frag, dict):
            return
        fid = frag.get("id")
        if fid is not None:
            by_id.setdefault(str(fid), (node_id, frag.get("type"), _frag_results(frag)))
        by_node_type.setdefault((str(node_id), frag.get("type")), []).append(frag)

    raw_messages = product.get("raw_messages")
    if isinstance(raw_messages, list):
        for msg in raw_messages:
            nid = str(msg.get("message_id"))
            for frag in (msg.get("fragments") or []):
                register(nid, frag)
        return by_id, by_node_type

    raw_mapping = product.get("raw_mapping")
    if isinstance(raw_mapping, dict):
        for node in raw_mapping.values():
            if not isinstance(node, dict):
                continue
            nid = str(node.get("id"))
            for frag in (node.get("fragments") or []):
                register(nid, frag)
        return by_id, by_node_type

    return by_id, by_node_type


def _entries_by_node(product):
    """transcript 按 node_id 分组，保持原有顺序。"""
    order = []
    grouped = {}
    for e in product.get("transcript") or []:
        nid = str(e.get("node_id"))
        if nid not in grouped:
            grouped[nid] = []
            order.append(nid)
        grouped[nid].append(e)
    return order, grouped


def _merge_reference(store, result, msg_index):
    """把一条搜索/打开结果并进全局来源清单（按归一 URL 合并，保留变体）。"""
    if not isinstance(result, dict):
        return
    url = result.get("url")
    if not url:
        return
    key = result.get("url_normalized") or norm_url(url)
    rec = store.get(key)
    if rec is None:
        rec = store[key] = {
            "url": key,
            "url_original": url,
            "url_anchor": result.get("url_anchor") or url_anchor(url),
            "title": None,
            "snippet": None,
            "site_name": result.get("site_name"),
            "published_at": result.get("published_at"),
            "found_by": [],
            "opened": False,
            "cited_in": [],
            "variants": [],
        }
    for k in ("title", "snippet"):
        if result.get(k) and not rec.get(k):
            rec[k] = result.get(k)
    variant = {k: result.get(k) for k in
               ("title", "snippet", "site_name", "published_at") if result.get(k) is not None}
    if variant and variant not in rec["variants"]:
        rec["variants"].append(variant)
    if msg_index is not None and msg_index not in rec["cited_in"]:
        rec["cited_in"].append(msg_index)


def convert(product, title=None, note=None, slim=False, source_meta=None):
    """
    把 rescued-session/v1 产物转成目标工具可直接吃的结构。

    product   —— conversations[0]（或整份 {conversations:[...]} 文档）
    slim      —— True 时去掉 fragments / raw_* 兜底，产出小文件（上传更快）
    """
    if isinstance(product, dict) and "conversations" in product:
        doc = product
        conv = product["conversations"][0]
    else:
        doc = {"_meta": {}, "conversations": [product]}
        conv = product

    by_id, by_node_type = _build_fragment_index(conv)
    order, grouped = _entries_by_node(conv)

    messages = []
    ref_store = {}
    idx = 0

    for nid in order:
        entries = grouped[nid]
        types = [e.get("type") for e in entries]

        req_parts = [e.get("content") for e in entries
                     if e.get("type") == "REQUEST" and e.get("content")]
        rsp_parts = [e.get("content") for e in entries
                     if e.get("type") == "RESPONSE" and e.get("content")]
        think_parts = [e.get("content") for e in entries
                       if e.get("type") == "THINK" and e.get("content")]
        tip_parts = [e.get("content") for e in entries
                     if e.get("type") == "TIP" and e.get("content")]

        queries, opened = [], []
        for e in entries:
            if e.get("type") in ("TOOL_SEARCH", "SEARCH"):
                for q in (e.get("queries") or []):
                    q = q.get("query") if isinstance(q, dict) else q
                    if q and q not in queries:
                        queries.append(q)
            elif e.get("type") == "TOOL_OPEN":
                opened.extend(e.get("results") or [])

        if req_parts and not rsp_parts:
            role, content = "user", "\n\n".join(req_parts).strip()
        elif rsp_parts:
            role, content = "assistant", "\n\n".join(rsp_parts).strip()
        else:
            # 只有工具调用或空节点（罕见）：并进上一条 assistant
            if entries and messages and messages[-1]["role"] == "assistant":
                prev = messages[-1]
                if think_parts:
                    prev["reasoning"] = (prev.get("reasoning", "") + "\n\n" +
                                          "\n\n".join(think_parts).strip()).strip()
                continue
            continue

        if not content:
            continue
        idx += 1

        entry = {"index": idx, "role": role, "content": content}

        if role == "assistant":
            if think_parts:
                entry["reasoning"] = "\n\n".join(think_parts).strip()
            if tip_parts:
                entry["tips"] = tip_parts
            if queries:
                entry["search_queries"] = queries
            if opened:
                entry["opened_pages"] = [
                    {"url": r.get("url_normalized") or norm_url(r.get("url")),
                     "url_original": r.get("url"),
                     "title": r.get("title"),
                     "snippet": r.get("snippet")}
                    for r in opened if isinstance(r, dict)
                ]
            citations = _resolve_citations(entries, content, by_id, by_node_type, nid,
                                          ref_store, idx)
            if citations:
                entry["citations"] = citations
        else:
            if tip_parts:
                entry["tips"] = tip_parts

        # 工具调用的来源统一登记
        for e in entries:
            if e.get("type") in ("TOOL_SEARCH", "SEARCH", "TOOL_OPEN"):
                for r in (e.get("results") or []):
                    _merge_reference(ref_store, r, idx if role == "assistant" else None)

        if not slim:
            entry["fragments"] = entries
        messages.append(entry)

    out = {
        "schema": SCHEMA,
        "title": title or conv.get("title"),
        "note": note or (
            "本文件是会话的无损转写。messages[] 字段对齐 OpenAI chat messages（role/content），"
            "可整份上传到 ChatGPT 网页版（Projects 文件上传）或直接喂给任意 chat API；"
            "reasoning 是思考链（不混进 content，避免污染对话）；"
            "citations 把正文里的 [reference:N] 标记映射回来源 URL；"
            "references 是按归一 URL 去重的来源清单（同一文档的不同变体收在 variants）。"
        ),
        "meta": {
            "source_schema": (doc.get("_meta") or {}).get("schema"),
            "source_platform": (doc.get("_meta") or {}).get("platform"),
            "source_sha256": ((doc.get("_meta") or {}).get("source") or {}).get("sha256"),
            "source_share_url": ((doc.get("_meta") or {}).get("source") or {}).get("share_url"),
            "converted_at": _now_iso(),
            "message_count": len(messages),
            "user_turns": sum(1 for m in messages if m["role"] == "user"),
            "assistant_turns": sum(1 for m in messages if m["role"] == "assistant"),
            "reference_count": len(ref_store),
            "lossless": not slim,
            "original_title": conv.get("title"),
            "original_title_is_placeholder": conv.get("title_is_placeholder"),
        },
        "messages": messages,
        "references": sorted(ref_store.values(),
                             key=lambda r: (-len(r.get("cited_in") or []), r.get("url") or "")),
    }
    if source_meta:
        out["meta"].update(source_meta)
    if not slim:
        # 源容器逐字兜底：任何重新分析都能从这份原样数据重来
        for key in ("raw_messages", "raw_mapping"):
            if conv.get(key) is not None:
                out[key] = conv[key]
    return out


def _resolve_citations(entries, content, by_id, by_node_type, node_id, ref_store=None, msg_index=None):
    """
    把引用标记解析成「标记 → 来源列表」。

    ⚠️ **两条路径的引用语义完全不同**，必须分开处理：

    - **分享链接路径**：RESPONSE fragment 带 `references[]`，里面的 `id` 是
      **fragment id**（指向某个 TOOL_OPEN / TOOL_SEARCH 片段）。
    - **账号导出包路径**：导出格式**根本不携带引用关系**——RESPONSE fragment
      只有 `content` 和 `type`。正文里的 `[!reference:N]` 里的 N 是
      **该消息内搜索结果列表的下标**（把该消息所有 TOOL_SEARCH/SEARCH 的
      results 按顺序拍平后取第 N 个）。

    两种都支持，才能让两条路径的产物都有可点的来源。
    """
    marker_re = _ref_marker_re()
    citations = []
    seen = set()

    # ---- A. 分享链接路径：references[] 里的 id 是 fragment id ----
    for e in entries:
        if e.get("type") != "RESPONSE":
            continue
        for ref in (e.get("references") or []):
            rid = str(ref.get("id"))
            marker = "[reference:%s]" % rid
            if rid in seen:
                continue
            seen.add(rid)
            hit = by_id.get(rid)
            if hit:
                _nid, _rtype, results = hit
                cands = [{"url": r.get("url_normalized") or norm_url(r.get("url")),
                          "url_original": r.get("url"),
                          "title": r.get("title"),
                          "snippet": r.get("snippet")}
                         for r in results if isinstance(r, dict)]
            else:
                # 没有 fragment id 就退化为「同节点同类型」
                cands = []
                for frag in by_node_type.get((str(node_id), ref.get("type")), []):
                    for r in _frag_results(frag):
                        if isinstance(r, dict):
                            cands.append({"url": norm_url(r.get("url")),
                                          "url_original": r.get("url"),
                                          "title": r.get("title"),
                                          "snippet": r.get("snippet")})
            if not cands and ref.get("urls"):
                cands = [{"url": norm_url(u), "url_original": u, "title": None, "snippet": None}
                         for u in ref["urls"]]
            citations.append({
                "marker": marker,
                "ref_type": ref.get("type"),
                "ref_id": ref.get("id"),
                "appears_in_content": bool(marker_re.search(content)),
                "sources": cands,
            })
            for c in cands:
                if ref_store is not None:
                    _merge_reference(ref_store, c, msg_index)

    # ---- B. 账号导出包路径：没有 references[]，标记里的 N 是结果下标 ----
    if not citations:
        flat = []
        for e in entries:
            if e.get("type") in ("TOOL_SEARCH", "SEARCH"):
                flat.extend(r for r in (e.get("results") or []) if isinstance(r, dict))
        if flat:
            for m in marker_re.finditer(content):
                n = int(m.group(1))
                src = flat[n] if 0 <= n < len(flat) else None
                cands = []
                if src:
                    cands = [{"url": r.get("url_normalized") or norm_url(r.get("url")),
                              "url_original": r.get("url"),
                              "title": r.get("title"),
                              "snippet": r.get("snippet")}
                             for r in [src]]
                citations.append({
                    "marker": m.group(0),
                    "ref_type": "result-index",
                    "ref_id": n,
                    "appears_in_content": True,
                    "sources": cands,
                })
                for c in cands:
                    if ref_store is not None:
                        _merge_reference(ref_store, c, msg_index)
    return citations


def _now_iso():
    import datetime
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="把 rescued-session/v1 产物转成 ChatGPT/OpenAI 可直接使用的 JSON")
    ap.add_argument("product", nargs="?", default=None,
                    help="产物 JSON 路径；省略则从 stdin 读（用于管道）")
    ap.add_argument("--out", default=None, help="输出文件（省略则写 stdout）")
    ap.add_argument("--title", default=None, help="覆盖标题")
    ap.add_argument("--slim", action="store_true",
                    help="去掉 fragments / raw_* 兜底，产出小文件（上传更快）")
    ap.add_argument("--stats", action="store_true", help="只打印统计，不产出全文")
    a = ap.parse_args(argv)

    if a.product:
        with open(a.product, encoding="utf-8") as f:
            doc = json.load(f)
    else:
        raw = sys.stdin.read()
        if not raw.strip():
            ap.error("没有输入：给一个产物文件路径，或用管道喂 stdin")
        doc = json.loads(raw)

    out = convert(doc, title=a.title, slim=a.slim)

    if a.stats:
        sys.stderr.write(json.dumps(out["meta"], ensure_ascii=False, indent=2) + "\n")

    text = json.dumps(out, ensure_ascii=False, indent=2)
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
        sys.stderr.write("wrote {}\n".format(a.out))
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
