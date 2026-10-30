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

# stdout 编码钉死：Windows 上 stdout 被重定向到文件/管道时编码会从 UTF-8
# 掉回 locale（中文 Windows = cp936），而本模块的输出含「⚠️」(U+26A0+VS16)
# 等非 cp936 字符 —— 直接 UnicodeEncodeError 崩掉，产物 0 字节。
# （写文件的地方本来就显式指定了 encoding="utf-8"，stdout 之前是漏的。）
for _stream in ("stdout", "stderr"):
    try:
        getattr(sys, _stream).reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):  # pragma: no cover - 非标准流
        pass

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
    建立 fragment 索引，供引用解析使用。

    ⚠️ **fragment id 在不同消息之间会重复**（实测同一个会话里 id=8 出现在
    四条不同消息的 fragment 上）。所以索引必须**按消息作用域**建立：
    引用标记出现在某条消息的 RESPONSE 里，它指向的必然是**同一条消息内**
    的 fragment。只建全局索引会取到「第一条同名 id」——结果就是引用指向
    一个 result 为空的片段，来源解析不出来（实测 7 处引用悬空）。

    返回：
      by_id         —— 全局 id → fragment（同名取首个，仅作兜底）
      by_node_id    —— (node_id, fragment_id) → fragment（**优先用这个**）
      by_node_type  —— (node_id, type) → [fragment]（没有 id 时的退化路径）
    """
    by_id = {}
    by_node_id = {}
    by_node_type = {}

    def register(node_id, frag):
        if not isinstance(frag, dict):
            return
        fid = frag.get("id")
        if fid is not None:
            key = str(fid)
            by_id.setdefault(key, (node_id, frag.get("type"), _frag_results(frag)))
            by_node_id.setdefault((str(node_id), key),
                                  (node_id, frag.get("type"), _frag_results(frag)))
        by_node_type.setdefault((str(node_id), frag.get("type")), []).append(frag)

    raw_messages = product.get("raw_messages")
    if isinstance(raw_messages, list):
        for msg in raw_messages:
            nid = str(msg.get("message_id"))
            for frag in (msg.get("fragments") or []):
                register(nid, frag)
        return by_id, by_node_id, by_node_type

    raw_mapping = product.get("raw_mapping")
    if isinstance(raw_mapping, dict):
        for node in raw_mapping.values():
            if not isinstance(node, dict):
                continue
            nid = str(node.get("id"))
            msg = node.get("message") or {}
            for frag in (msg.get("fragments") or []):
                register(nid, frag)
        return by_id, by_node_id, by_node_type

    return by_id, by_node_id, by_node_type


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
            # 原始 URL 优先取源产物里的 url_original（并集产物的 url 已归一，
            # 锚点只存在于 url_original 里）；没有才退回 url。
            "url_original": result.get("url_original") or url,
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

    # ⚠️ 幂等：输入**已经是**转换后的产物（chatgpt-import/v1，没有 transcript）
    # 时，不能静默产出 0 条 message——那是最坏的结果（静默丢数据）。
    # 这里把已有的 messages 直接沿用，让 `to_openai.py` 可以反复跑、可以接龙。
    if not conv.get("transcript") and isinstance(conv.get("messages"), list):
        return _reconvert(doc, conv, title=title, note=note, slim=slim,
                          source_meta=source_meta)

    by_id, by_node_frag, by_node_type = _build_fragment_index(conv)
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
                     "url_original": r.get("url_original") or r.get("url"),
                     "title": r.get("title"),
                     "snippet": r.get("snippet")}
                    for r in opened if isinstance(r, dict)
                ]
            citations = _resolve_citations(entries, content, by_id, by_node_frag,
                                          by_node_type, nid, ref_store, idx)
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
            "可整份上传到 ChatGPT 网页版（Projects 文件上传）；"
            "⚠️ 顶层与每条消息上带 schema/meta/references/citations 等扩展字段，"
            "**不能直接 POST 给严格的 chat API**——那种场合用 --strict（只留 role/content）。"
            "reasoning 是思考链（不混进 content，避免污染对话）；"
            "citations 把正文里的 [reference:N] 标记映射回来源 URL（精确来源在 sources，"
            "其余检索结果在 candidates）；"
            "references 是按归一 URL 去重的来源清单（同一文档的不同变体收在 variants）。"
        ),
        "meta": {
            "source_schema": (doc.get("_meta") or {}).get("schema"),
            "source_platform": (doc.get("_meta") or {}).get("platform"),
            "source_sha256": ((doc.get("_meta") or {}).get("source") or {}).get("sha256"),
            "source_share_url": ((doc.get("_meta") or {}).get("source") or {}).get("share_url"),
            # 源产物里的自证结果随产物一起传下去（搬运不丢证据）
            "source_verification": (doc.get("_meta") or {}).get("verification"),
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


def _reconvert(doc, conv, title=None, note=None, slim=False, source_meta=None):
    """
    已经是转换后产物（chatgpt-import/v1）时的**幂等重转**。

    为什么需要：用户可能把上一次的产物再喂回来（换 --strict、换 --slim、
    接龙加工）。如果这里静默产出 0 条 message，就是最坏的结果——
    **静默丢数据**比报错危险得多。
    """
    messages = []
    for m in conv.get("messages") or []:
        entry = {"index": len(messages) + 1,
                 "role": m.get("role"),
                 "content": m.get("content") or ""}
        for k in ("reasoning", "tips", "search_queries", "opened_pages",
                  "citations", "fragments"):
            if m.get(k):
                entry[k] = m[k]
        if not slim:
            entry.setdefault("fragments", [])
        messages.append(entry)

    out = {
        "schema": SCHEMA,
        "title": title or conv.get("title"),
        "note": note or conv.get("note"),
        "meta": dict(conv.get("meta") or {}),
        "messages": messages,
        "references": conv.get("references") or [],
    }
    out["meta"].update({
        "converted_at": _now_iso(),
        "message_count": len(messages),
        "user_turns": sum(1 for m in messages if m["role"] == "user"),
        "assistant_turns": sum(1 for m in messages if m["role"] == "assistant"),
        "lossless": not slim,
        "idempotent_reconvert": True,
    })
    if source_meta:
        out["meta"].update(source_meta)
    return out


def _resolve_citations(entries, content, by_id, by_node_frag, by_node_type, node_id,
                       ref_store=None, msg_index=None):
    """
    把引用标记解析成「标记 → 来源列表」。

    ⚠️ **两条路径的引用语义完全不同**，必须分开处理（实测出来的，不是猜的）：

    - **分享链接路径**：RESPONSE fragment 带 `references[]`（有序数组），
      正文里的 `[reference:N]` 里的 **N 是这个数组的下标**；
      数组每一项的 `id` 才是 fragment id（指向某个 TOOL_OPEN / TOOL_SEARCH）。
      实测：某条消息有 24 个 references，正文里的标记最大到 23 —— 对得上。
      把 N 当 fragment id 解读，会让一半的标记指向不相干的片段
      （而且 fragment id 在不同消息之间会重复，更容易张冠李戴）。
    - **账号导出包路径**：导出格式**根本不携带引用关系**——RESPONSE fragment
      只有 content 和 type，没有 references[]；而被引用的片段绝大多数是
      TOOL_OPEN（真正读过的页面），这些片段在导出包里**没有载荷**。
      所以这条路径**在原理上还原不出**「哪个标记指向哪个页面」，
      只能如实标成「该格式无法解析」+ 给候选来源池（见分支 B 的注释）。

    两种都支持，才能让两条路径的产物都有可点的来源。
    """
    marker_re = _ref_marker_re()
    citations = []
    seen = set()

    def resolve_ref(ref):
        """
        references[] 的一项 → (精确来源, 候选来源)。

        ⚠️ **精确 vs 候选要分开**：API 的 `references[N].urls` 给出的是
        **真正被引用的那几篇**；而 fragment 自己的 results 是这一轮检索到的
        **全部**结果（实测可达 49 条）。把全部结果塞进 `sources` 不算错
        （被引用的那篇确实在其中），但**指不出是哪一篇**——
        所以精确的进 `sources`，其余进 `candidates`。
        """
        rid = str(ref.get("id"))
        hit = by_node_frag.get((str(node_id), rid)) or by_id.get(rid)
        broad = []
        if hit:
            _nid, _rtype, results = hit
            broad = [{"url": r.get("url_normalized") or norm_url(r.get("url")),
                      "url_original": r.get("url_original") or r.get("url"),
                      "title": r.get("title"),
                      "snippet": r.get("snippet")}
                     for r in results if isinstance(r, dict)]
        else:
            for frag in by_node_type.get((str(node_id), ref.get("type")), []):
                for r in _frag_results(frag):
                    if isinstance(r, dict):
                        broad.append({"url": norm_url(r.get("url")),
                                      "url_original": r.get("url_original") or r.get("url"),
                                      "title": r.get("title"),
                                      "snippet": r.get("snippet")})
        declared = [norm_url(u) for u in (ref.get("urls") or []) if u]
        if declared:
            by_url = {c["url"]: c for c in broad}
            exact = []
            for u in declared:
                exact.append(by_url.get(u) or {"url": u, "url_original": u,
                                               "title": None, "snippet": None})
            return exact, broad
        return broad, []

    # ---- A. 分享链接路径：标记 N 是 references[] 的下标 ----
    ref_list = []
    for e in entries:
        if e.get("type") == "RESPONSE":
            ref_list.extend(e.get("references") or [])

    markers = list(marker_re.finditer(content))
    if ref_list and markers:
        for m in markers:
            n = int(m.group(1))
            ref = ref_list[n] if 0 <= n < len(ref_list) else None
            if ref is None:
                citations.append({
                    "marker": m.group(0), "marker_kind": "inline",
                    "ref_type": "out-of-range",
                    "ref_id": n, "appears_in_content": True, "sources": [],
                })
                continue
            cands, extra = resolve_ref(ref)
            citations.append({
                "marker": m.group(0),
                "marker_kind": "inline",
                "ref_type": ref.get("type"),
                "ref_id": ref.get("id"),
                "appears_in_content": True,
                "sources": cands,
            })
            if extra:
                citations[-1]["candidates"] = extra
                citations[-1]["candidate_pool_size"] = len(extra)
            for c in cands:
                if ref_store is not None:
                    _merge_reference(ref_store, c, msg_index)
    elif ref_list:
        # 正文里没有标记、但 references[] 有内容——按 fragment id 逐条登记。
        # ⚠️ 这里**不能**拼成 `[reference:N]`：正文里根本没有这种标记，
        # 拼出来会造出一个正文里不存在的标记（还可能与真的 `[reference:6]`
        # 撞名）。用 `[fragment:N]` 区分，并在 marker_kind 里标明。
        for ref in ref_list:
            rid = str(ref.get("id"))
            if rid in seen:
                continue
            seen.add(rid)
            cands, extra = resolve_ref(ref)
            entry = {
                "marker": "[fragment:%s]" % rid,
                "marker_kind": "fragment",
                "ref_type": ref.get("type"),
                "ref_id": ref.get("id"),
                "appears_in_content": False,
                "sources": cands,
            }
            if extra:
                entry["candidates"] = extra
                entry["candidate_pool_size"] = len(extra)
            citations.append(entry)
            for c in cands:
                if ref_store is not None:
                    _merge_reference(ref_store, c, msg_index)

    # ---- B. 账号导出包路径：格式不携带引用关系 —— 如实说「解析不了」 ----
    #
    # 实测结论（对照分享链接侧的真值逐条核过）：
    #   - 导出包的 RESPONSE fragment 只有 content / type，**没有 references[]**
    #   - 行内标记里，被引用的 fragment **69% 指向 TOOL_OPEN**（真正读过的页面）
    #   - 而导出包里 **96 个 TOOL_OPEN 片段全部没有载荷**（导出格式根本没导出它）
    #   → 导出包路径**在原理上就还原不出**「哪个标记指向哪个页面」
    #
    # ⚠️ 曾试过「标记 N → 该消息搜索结果列表的第 N 个」，实测 71 个标记里
    # 65 个与分享侧真值不一致——那是**把来源猜错了**。错误的来源比没有来源更糟：
    # 用户会以为「这句话的依据是这篇」，而其实不是。
    # 所以这里**不猜**：如实标成「该格式无法解析」，并附上候选来源池。
    if not citations:
        pool = []
        seen_urls = set()
        for e in entries:
            if e.get("type") in ("TOOL_SEARCH", "SEARCH"):
                for r in (e.get("results") or []):
                    if not isinstance(r, dict):
                        continue
                    u = r.get("url_normalized") or norm_url(r.get("url"))
                    if u and u not in seen_urls:
                        seen_urls.add(u)
                        pool.append({
                            "url": u,
                            "url_original": r.get("url_original") or r.get("url"),
                            "title": r.get("title"),
                            "snippet": r.get("snippet"),
                        })
        for m in markers:
            citations.append({
                "marker": m.group(0),
                "marker_kind": "inline",
                "ref_type": "unresolvable-in-export-format",
                "ref_id": int(m.group(1)),
                "appears_in_content": True,
                "sources": [],
                "note": (
                    "账号导出包格式不携带引用关系：RESPONSE fragment 没有 references[]，"
                    "且被引用的 TOOL_OPEN 片段（真正读过的页面）在导出包里没有载荷。"
                    "此处不猜来源——错误的来源比没有来源更糟。"
                    "候选来源池见 candidates。"
                    "⚠️ 需要**逐条精确**的映射，只有**纯分享链接路径**能给"
                    "（它带 references[]）；跨源并集里导出侧正文配分享侧 references[]，"
                    "两代下标语义不保证对齐——并集实测只有约一半能精确解析。"
                ),
                "candidates": pool[:25],
                "candidate_pool_size": len(pool),
            })
    return citations


def _now_iso():
    import datetime
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()


def _decode_auto(data):
    """
    按 BOM/内容自动判断编码。

    Windows 上的两个坑：
    - **PowerShell 5.1 的 `>` 重定向**写出的文件是 **UTF-16LE**（首字节 FF FE），
      不是 UTF-8 —— 用 utf-8 读会 "can't decode byte 0xff"。
    - **PowerShell 管道**（原生命令之间）会按控制台编码重编码并注入
      **UTF-8 BOM**（首字节 EF BB BF）—— 用普通 utf-8 读会 "Unexpected UTF-8 BOM"。

    所以：utf-8-sig / utf-16 / utf-8 依次尝试。
    """
    for enc in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="把 rescued-session/v1 产物转成 ChatGPT/OpenAI 可直接使用的 JSON")
    ap.add_argument("product", nargs="?", default=None,
                    help="产物 JSON 路径；省略则从 stdin 读（用于管道）")
    ap.add_argument("--out", default=None, help="输出文件（省略则写 stdout）")
    ap.add_argument("--title", default=None, help="覆盖标题")
    ap.add_argument("--slim", action="store_true",
                    help="去掉 fragments / raw_* 兜底，产出小文件（上传更快）")
    ap.add_argument("--strict", action="store_true",
                    help="输出**严格的** OpenAI chat messages 数组："
                         "只有 role 与 content 两个字段，可直接 POST 给 chat API")
    ap.add_argument("--stats", action="store_true", help="只打印统计，不产出全文")
    a = ap.parse_args(argv)

    if a.product:
        # ⚠️ 自动识别编码：PowerShell 的 `>` 可能写出 UTF-16LE，管道可能带 BOM
        with open(a.product, "rb") as f:
            doc = json.loads(_decode_auto(f.read()))
    else:
        # ⚠️ 必须显式解码：Windows 上 stdin 的默认编码是 locale（中文 = cp936），
        # 而上游产物是 UTF-8 —— 直接 sys.stdin.read() 会抛 UnicodeDecodeError
        #（实测 'gbk' codec can't decode byte 0x80）。
        raw = _decode_auto(sys.stdin.buffer.read())
        if not raw.strip():
            ap.error("没有输入：给一个产物文件路径，或用管道喂 stdin")
        doc = json.loads(raw)

    out = convert(doc, title=a.title, slim=a.slim)

    if a.stats:
        sys.stderr.write(json.dumps(out["meta"], ensure_ascii=False, indent=2) + "\n")

    if a.strict:
        # 严格形态：只要 role/content —— chat.completions 这类接口不接受
        # 顶层 schema / references / 每条消息上的 citations 等额外字段。
        out = [{"role": m["role"], "content": m["content"]} for m in out["messages"]]

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
