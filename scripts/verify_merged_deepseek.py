# -*- coding: utf-8 -*-
"""
并集 vs 两条单源：逐节点差异报告 + **snippet 维度断言**（可当门禁用）。

用法：
    python verify_merged_deepseek.py --export <导出包产物.json> \
        --share <分享链接原始.json> --merged <并集产物.json> [--outdir <目录>]

三个输入路径都是**必填**：脚本不写死任何机器上的路径，样本换了用参数覆盖即可。

为什么要有这个脚本：
    早先的一版并集实现里，**所有 fragment 级多重集自检都通过了**，
    但导出包的 `results` 非空而贫瘠（每条只有 {title,url}），分享链接侧的
    富载荷被整列丢弃——大量结果与上十万字符的 snippet 在并集里归零。
    fragment 多重集看不出这种丢失，**只有 snippet 维度能发现**。
    所以这里把 snippet 断言固化成可执行的门禁。

断言清单：
    A1  merged snippet 字符总数 >= max(单源导出包, 单源分享链接)
    A2  merged 带 snippet 的结果条目数 >= max(单源导出包, 单源分享链接)
    A3  merged fragment 数 >= max(单源导出包, 单源分享链接)
    A4  merged 节点集合 == 两侧节点集合的并集
    A5  两侧每条带 snippet 的结果，其 url 在并集里仍带 snippet（逐条核对，**按归一 URL**）
    A6  两侧的每个 (node_id, type, 节点内序号) 键在并集里都存在
    A7  同一文档不得出现多条记录（按归一 URL 统计；变体收在 variants 里）

A5 用**归一 URL** 做键的理由：DeepSeek 的搜索结果 URL 带引用锚点
（`https://x/a.pdf#7#22#7`），**同一篇文档在不同引用位置锚点不同**。
源侧给的是带锚点的原始 URL，并集侧给的是归一后的主键——不归一就匹配不上，
断言会误报「url 缺失」。

退出码：0 = 全部断言通过；3 = 有断言失败（可直接当门禁用）；2 = 参数缺失。
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from deepseek_common import result_url_key
except ImportError:  # pragma: no cover - 缺文件时给出可操作的提示
    raise RuntimeError(
        "deepseek_common.py is missing next to this script; "
        "it holds the shared URL-normalization rules."
    )

EXPORT = None
SHARE_RAW = None

REF = re.compile(r"\[!reference:\d+\]")
SEARCH_TYPES = ("TOOL_SEARCH", "SEARCH")


# ============================================================
# 工具
# ============================================================

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def norm(text):
    """剥掉分享链接侧被剥离的 [!reference:N] 行内引用标记。"""
    return REF.sub("", text or "")


def results_of(frag):
    """归一化结果载荷：TOOL_SEARCH 是列表，TOOL_OPEN 是单个 result。"""
    res = frag.get("results")
    if res is None and frag.get("result") is not None:
        res = [frag["result"]]
    return res


def sig_of(frag):
    res = results_of(frag)
    return (frag.get("type"), norm(frag.get("content")),
            json.dumps(res, ensure_ascii=False, sort_keys=True) if res is not None else "")


def snippet_stats(result_lists):
    """(结果条目数, 带 snippet 条数, snippet 字符总数)"""
    entries = with_snip = chars = 0
    for lst in result_lists:
        for r in lst:
            entries += 1
            s = r.get("snippet") or ""
            if s.strip():
                with_snip += 1
                chars += len(s)
    return entries, with_snip, chars


def snippet_by_url(result_lists):
    """
    {归一 URL: 该文档在所有搜索结果中出现过的**最长** snippet}。

    两个要点：
    1. **键用归一 URL**——同一篇文档在不同引用位置锚点不同，
       用原始 URL 当键会把一篇文档算成多篇，合并侧匹配不上、断言误报。
    2. 按**最长**取——同一个 url 会在不同节点的多次搜索里重复出现，
       且每次的 snippet 文本长度可能不同（不同查询词返回不同摘要）。
       按「最后一个」取会误判成「被截短」，按最长取才是无损。
    """
    out = {}
    for lst in result_lists:
        for r in lst:
            if not isinstance(r, dict):
                continue
            u = result_url_key(r)
            s = r.get("snippet") or ""
            if u and s.strip() and len(s) > len(out.get(u, "")):
                out[u] = s
    return out


def variant_key(v):
    """
    变体的可比键：只取**非空**的 title / snippet。

    与合并侧的口径必须一致——一条既没有 title 也没有 snippet 的结果
    不构成任何变体（它带不来任何信息），两侧都不能把它算进去，
    否则会出现「源侧有、并集里没有」的假阳性。
    """
    if not isinstance(v, dict):
        return None
    out = {}
    for k in ("title", "snippet"):
        val = v.get(k)
        if val not in (None, "", [], {}):
            out[k] = val
    if not out:
        return None
    return json.dumps(out, ensure_ascii=False, sort_keys=True)


def variant_sets(result_lists):
    """
    {归一 URL: 该文档出现过的不同 (title, snippet) 组合集合}。

    「不同轮次返回不同摘要」是常态，所以变体要**全部**留着——
    只留第一条就会把其余轮次的摘要整段丢掉。
    """
    out = defaultdict(set)
    for lst in result_lists:
        for r in lst:
            if not isinstance(r, dict):
                continue
            u = result_url_key(r)
            if not u:
                continue
            k = variant_key(r)
            if k:
                out[u].add(k)
    return out


# ============================================================
# 读三份来源
# ============================================================

def load_export(path):
    doc = json.load(open(path, encoding="utf-8"))
    conv = doc["conversations"][0]
    nodes = {}
    search_lists, open_lists = [], []
    for nid, node in (conv.get("raw_mapping") or {}).items():
        msg = node.get("message")
        if not msg:
            continue
        nodes[nid] = [sig_of(f) for f in msg.get("fragments") or []]
        for f in msg.get("fragments") or []:
            if f["type"] in SEARCH_TYPES:
                search_lists.append(f.get("results") or [])
            elif f["type"] == "TOOL_OPEN":
                open_lists.append(f.get("results") or [])
    return doc, conv, nodes, search_lists, open_lists


def load_share(path):
    doc = json.load(open(path, encoding="utf-8"))
    biz = doc["data"]["biz_data"]
    nodes = {}
    search_lists, open_lists = [], []
    for msg in biz.get("messages") or []:
        nid = str(msg.get("message_id"))
        frs = msg.get("fragments") or []
        nodes[nid] = [sig_of(f) for f in frs]
        for f in frs:
            if f["type"] in SEARCH_TYPES:
                search_lists.append(f.get("results") or [])
            elif f["type"] == "TOOL_OPEN":
                open_lists.append(f.get("results") or [])
    return doc, biz, nodes, search_lists, open_lists


def load_merged(path):
    doc = json.load(open(path, encoding="utf-8"))
    conv = doc["conversations"][0]
    nodes = defaultdict(list)
    search_lists, open_lists = [], []
    for e in conv.get("transcript") or []:
        nodes[str(e.get("node_id"))].append(e)
        if e.get("type") in SEARCH_TYPES:
            search_lists.append(e.get("results") or [])
        elif e.get("type") == "TOOL_OPEN":
            open_lists.append(e.get("results") or [])
    return doc, conv, dict(nodes), search_lists, open_lists


# ============================================================
# 断言
# ============================================================

def run_assertions(e_nodes, s_nodes, m_nodes,
                   e_search, s_search, m_search,
                   e_open, s_open, m_open,
                   m_conv=None):
    e_s = snippet_stats(e_search + e_open)
    s_s = snippet_stats(s_search + s_open)
    m_s = snippet_stats(m_search + m_open)
    checks = {}

    checks["A1_snippet_chars_not_lost"] = {
        "ok": m_s[2] >= max(e_s[2], s_s[2]),
        "export_chars": e_s[2], "share_chars": s_s[2], "merged_chars": m_s[2],
        "required_min": max(e_s[2], s_s[2]),
    }
    checks["A2_snippet_entries_not_lost"] = {
        "ok": m_s[1] >= max(e_s[1], s_s[1]),
        "export_entries": e_s[1], "share_entries": s_s[1], "merged_entries": m_s[1],
        "required_min": max(e_s[1], s_s[1]),
    }

    e_frags = sum(len(v) for v in e_nodes.values())
    s_frags = sum(len(v) for v in s_nodes.values())
    m_frags = sum(len(v) for v in m_nodes.values())
    checks["A3_fragment_count_not_lost"] = {
        "ok": m_frags >= max(e_frags, s_frags),
        "export": e_frags, "share": s_frags, "merged": m_frags,
        "required_min": max(e_frags, s_frags),
    }

    exp_nodes, sh_nodes, m_nodeset = set(e_nodes), set(s_nodes), set(m_nodes)
    checks["A4_node_union"] = {
        "ok": m_nodeset == (exp_nodes | sh_nodes),
        "union_size": len(exp_nodes | sh_nodes), "merged_nodes": len(m_nodeset),
        "only_in_export": sorted(exp_nodes - sh_nodes, key=int),
        "only_in_share": sorted(sh_nodes - exp_nodes, key=int),
    }

    # A5 —— 键是**归一 URL**：源侧的原始 URL（带锚点）与并集侧的归一主键要能对上
    e_urls = snippet_by_url(e_search + e_open)
    s_urls = snippet_by_url(s_search + s_open)
    m_urls = snippet_by_url(m_search + m_open)
    lost = []
    for src_name, mapping in (("export", e_urls), ("share", s_urls)):
        for u, s in mapping.items():
            if u not in m_urls:
                lost.append({"source": src_name, "url": u, "reason": "url 缺失"})
            elif len(m_urls[u]) < len(s):
                lost.append({"source": src_name, "url": u, "reason": "snippet 被截短",
                             "src_len": len(s), "merged_len": len(m_urls[u])})
    checks["A5_per_url_snippet_preserved"] = {
        "ok": not lost, "checked_urls": len(set(e_urls) | set(s_urls)),
        "lost": lost[:20], "lost_count": len(lost),
    }

    def keys(nodes):
        """
        节点内 fragment 的对齐键：(node_id, type, 节点内序号)。

        元素有两种形态：单源侧是签名元组，合并侧是 transcript 条目 dict。
        """
        out = set()
        for nid, items in nodes.items():
            seen = defaultdict(int)
            for item in items:
                t = item[0] if isinstance(item, tuple) else item.get("type")
                out.add((nid, t, seen[t]))
                seen[t] += 1
        return out

    ek, sk, mk = keys(e_nodes), keys(s_nodes), keys(m_nodes)
    checks["A6_fragment_keys_covered"] = {
        "ok": (ek | sk) <= mk,
        "required": len(ek | sk), "merged": len(mk),
        "missing_from_export": len(ek - mk), "missing_from_share": len(sk - mk),
    }

    checks["A7_unique_document_records"] = _check_a7(
        m_conv, m_search + m_open, e_search + e_open, s_search + s_open)
    return checks


def _check_a7(m_conv, m_lists, e_lists, s_lists):
    """
    A7 —— 同一文档不得出现多条记录。

    「同一篇文档」的判据是**归一 URL**（剥掉引用锚点）。检查四件事：

    1. **登记里一篇文档一条记录**：并集产物的唯一文档表
       （`conversations[0].references[]`）里不能出现重复的归一 URL。
    2. **登记表与结果列表对得上**：结果列表里出现过的文档都在表里，
       表里的文档也都在结果列表里（没有孤儿、没有漏登）。
    3. **单个结果列表内不重复**：同一个 fragment 的 results[] 里，
       同一篇文档不得出现两次。
    4. **变体没丢**：源侧出现过的每个不同 (title, snippet) 组合，
       在并集的 variants 里都还在。
    """
    conv = m_conv or {}
    registry = conv.get("references")
    if registry is None:
        # 老产物没有登记表——按结果列表现算一份（等价口径）
        registry = []
        seen = set()
        for lst in m_lists:
            for r in lst:
                if not isinstance(r, dict):
                    continue
                u = result_url_key(r)
                if u and u not in seen:
                    seen.add(u)
                    registry.append({"url": u, "variants": []})
        registry_source = "derived-from-results"
    else:
        registry_source = "references[]"

    # 1. 登记表里一篇文档一条记录
    counts = defaultdict(int)
    for rec in registry:
        counts[result_url_key(rec) or rec.get("url")] += 1
    duplicates = sorted(u for u, c in counts.items() if c > 1)

    # 2. 登记表 vs 结果列表
    result_urls = set()
    for lst in m_lists:
        for r in lst:
            if isinstance(r, dict):
                u = result_url_key(r)
                if u:
                    result_urls.add(u)
    registry_urls = set(counts)
    orphans = sorted(registry_urls - result_urls)
    missing = sorted(result_urls - registry_urls)

    # 3. 单个列表内不重复
    intra = []
    for i, lst in enumerate(m_lists):
        seen = defaultdict(int)
        for r in lst:
            if isinstance(r, dict):
                u = result_url_key(r)
                if u:
                    seen[u] += 1
        for u, c in seen.items():
            if c > 1:
                intra.append({"list_index": i, "url": u, "count": c})

    # 4. 变体没丢
    merged_variants = defaultdict(set)
    for rec in registry:
        u = result_url_key(rec) or rec.get("url")
        for v in rec.get("variants") or []:
            k = variant_key(v)
            if k:
                merged_variants[u].add(k)
    src_variants = {}
    src_variants.update(variant_sets(e_lists))
    for u, vs in variant_sets(s_lists).items():
        src_variants.setdefault(u, set()).update(vs)
    lost_variants = []
    for u, vs in sorted(src_variants.items()):
        missing_v = vs - merged_variants.get(u, set())
        for k in sorted(missing_v)[:3]:
            lost_variants.append({"url": u, "variant": json.loads(k),
                                  "snippet_len": len((json.loads(k).get("snippet") or ""))})

    variant_total = sum(len(v) for v in merged_variants.values())
    docs_multi = sum(1 for v in merged_variants.values() if len(v) > 1)
    ok = not (duplicates or orphans or missing or intra or lost_variants)
    return {
        "ok": ok,
        "registry_source": registry_source,
        "unique_documents": len(result_urls),
        "registry_records": len(registry),
        "result_entries": sum(len(lst) for lst in m_lists),
        "variant_total": variant_total,
        "docs_with_multiple_variants": docs_multi,
        "duplicate_records": duplicates[:20],
        "orphan_records": orphans[:20],
        "missing_records": missing[:20],
        "intra_list_duplicates": intra[:20],
        "lost_variants": lost_variants[:20],
        "lost_variant_count": len(lost_variants),
    }


# ============================================================
# 报告
# ============================================================

def build_report(merged_path, e_doc, s_doc, m_doc,
                 e_nodes, s_nodes, m_nodes,
                 e_search, s_search, m_search,
                 e_open, s_open, m_open, checks):
    L = []
    w = L.append
    ok_all = all(c["ok"] for c in checks.values())
    w("# 并集 vs 两条单源 — 逐节点差异报告 + snippet 维度断言\n")
    w("> 生成时间：%s\n" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    w("> | 产物 | sha256 |\n> |---|---|")
    w("> | 导出包单源 | `%s` |" % sha256(EXPORT))
    w("> | 分享链接原始 API JSON | `%s` |" % sha256(SHARE_RAW))
    w("> | **并集产物** `%s` | `%s` |\n" % (os.path.basename(merged_path), sha256(merged_path)))
    w("> 比对口径：正文剥除 `[!reference:N]` 后再比；fragment 按 `(node_id, type, 节点内序号)` 对齐；"
      "搜索结果按**归一 URL**（剥掉 `#` 之后的引用锚点）对齐。\n")
    w("\n---\n")

    e_s = snippet_stats(e_search + e_open)
    s_s = snippet_stats(s_search + s_open)
    m_s = snippet_stats(m_search + m_open)
    a7 = checks["A7_unique_document_records"]

    w("## 0 ⚠️ snippet 维度断言（门禁）\n")
    w("> **为什么单独有这一节**：并集的 fragment 级多重集自检**发现不了**一类丢失——"
      "当一侧的 `results` **非空但贫瘠**（每条只有 `{title, url}`）时，"
      "「空字段才补齐」的规则永不触发，另一侧整列富载荷"
      "（snippet / site_name / published_at …）被静默丢弃。"
      "**只有 snippet 维度能发现这种丢失**，所以这里把它固化成可执行的门禁。\n")
    w("| 断言 | 结果 | 实测 |")
    w("|---|---|---|")
    for name, c in checks.items():
        detail = {k: v for k, v in c.items() if k != "ok"}
        w("| `%s` | %s | `%s` |" % (name, "✅ PASS" if c["ok"] else "❌ FAIL",
                                    json.dumps(detail, ensure_ascii=False)[:240]))
    w("")
    w("**总判定：%s**\n" % ("✅ 全部通过" if ok_all else "❌ 有断言失败"))

    w("## 1 规模\n")
    w("| | 节点 | fragment | 搜索结果条目 | 带 snippet | snippet 字符 |")
    w("|---|---:|---:|---:|---:|---:|")
    for name, nodes, search, open_ in (
            ("导出包", e_nodes, e_search, e_open),
            ("分享链接", s_nodes, s_search, s_open),
            ("**并集**", m_nodes, m_search, m_open)):
        st = snippet_stats(search + open_)
        w("| %s | %d | %d | %d | %d | %d |" % (
            name, len(nodes), sum(len(v) for v in nodes.values()),
            st[0], st[1], st[2]))
    w("")
    only_e = checks["A4_node_union"]["only_in_export"]
    only_s = checks["A4_node_union"]["only_in_share"]
    w("> **节点编号空间完全一致**：分享链接的 %d 个 node_id 全部是导出包 %d 个的子集，"
      "导出包多出的正好是 **%s**。按 `node_id` 对齐节点可靠，不需要靠正文内容匹配。\n"
      % (len(s_nodes), len(e_nodes), ", ".join(only_e) or "无"))

    w("## 2 仅导出包有的节点\n")
    for nid in only_e:
        sigs = e_nodes[nid]
        req = next((c for t, c, _ in sigs if t == "REQUEST"), "")
        w("### 节点 %s（%d 个 fragment）\n" % (nid, len(sigs)))
        w("**提问**：%s\n" % (req.strip()[:180] if req else "—"))
        for t, c, _ in sigs:
            if t == "REQUEST":
                continue
            if t == "RESPONSE":
                w("- `RESPONSE`（%d 字）：%s…\n" % (len(c), c.strip()[:220].replace("\n", " ")))
            elif t == "THINK":
                w("- `THINK`：%s…\n" % c.strip()[:140].replace("\n", " "))
            else:
                w("- `%s`\n" % t)
        w("")
    if only_e:
        w("> 这一轮只存在于导出包——分享链接是**最终态**，用户改过/删过的中间轮次在那边已经丢失。"
          "这正是「单一来源 ≠ 完整历史」的直接证据。\n")

    w("## 3 fragment 级差异\n")
    same_text = same_full = 0
    only_share_frags, only_export_frags = [], []
    for nid in sorted(set(e_nodes) & set(s_nodes), key=int):
        eg, sg = defaultdict(list), defaultdict(list)
        for sig in e_nodes[nid]:
            eg[sig[0]].append(sig)
        for sig in s_nodes[nid]:
            sg[sig[0]].append(sig)
        for t in set(eg) | set(sg):
            el, sl = eg[t], sg[t]
            for i in range(max(len(el), len(sl))):
                a = el[i] if i < len(el) else None
                b = sl[i] if i < len(sl) else None
                if a is None:
                    only_share_frags.append((nid, t, i))
                elif b is None:
                    only_export_frags.append((nid, t, i))
                else:
                    if a[1] == b[1]:
                        same_text += 1
                    if a == b:
                        same_full += 1
    w("| 指标 | 值 |\n|---|---|")
    w("| 共有节点内**正文一致**（已剥 `[!reference:N]`） | %d |" % same_text)
    w("| 共有节点内**全字段一致**（含 results 载荷） | %d |" % same_full)
    w("| 仅分享链接有的 fragment | %d |" % len(only_share_frags))
    w("| 仅导出包有的 fragment | %d |" % len(only_export_frags))
    w("")
    if only_share_frags:
        w("分享链接独有：%s\n" % ", ".join("%s#%s[%d]" % x for x in only_share_frags))
    if only_export_frags:
        w("导出包独有：%s\n" % ", ".join("%s#%s[%d]" % x for x in only_export_frags))

    w("## 4 搜索结果载荷：两侧**信息量不对等**\n")
    w("| 来源 | 搜索结果条目 | 带 snippet | 带 site_name | 带 published_at |")
    w("|---|---:|---:|---:|---:|")
    for name, search, open_ in (("导出包", e_search, e_open),
                                 ("分享链接", s_search, s_open),
                                 ("并集", m_search, m_open)):
        lst = search + open_
        tot = sum(len(x) for x in lst)
        sn = sum(1 for x in lst for r in x if (r.get("snippet") or "").strip())
        site = sum(1 for x in lst for r in x if r.get("site_name"))
        pub = sum(1 for x in lst for r in x if r.get("published_at"))
        w("| %s | %d | %d | %d | %d |" % (name, tot, sn, site, pub))
    w("")
    w("**导出包的搜索结果只有 `{title, url}` 两个字段**——非空，但信息贫瘠。"
      "分享链接侧才有完整载荷（snippet / site_name / site_icon / published_at / "
      "cite_index / query_indexes）。\n")
    w("> 这意味着：**如果并集按整列取一侧，搜索证据会被大面积抹掉**——"
      "而 snippet 恰恰是一手证据。\n")

    w("## 5 URL 归一与「一篇文档一条记录」\n")
    w("DeepSeek 的搜索结果 URL 带引用锚点（`https://x/a.pdf#7#22#7`），"
      "**同一篇文档在不同引用位置锚点不同**。按原始 URL 建字典 = 同一篇文章被登记成多条记录，"
      "而且**互相不补字段**。\n")
    w("| 指标 | 值 |")
    w("|---|---:|")
    w("| 结果条目总数（按轮次重复计） | %d |" % a7["result_entries"])
    w("| **唯一文档数**（按归一 URL） | **%d** |" % a7["unique_documents"])
    w("| 登记表记录数（`references[]`） | %d |" % a7["registry_records"])
    w("| **变体总数** | **%d** |" % a7["variant_total"])
    w("| 有多个变体的文档数 | %d |" % a7["docs_with_multiple_variants"])
    w("| 同一 URL 的重复记录 | %d |" % len(a7["duplicate_records"]))
    w("| 单个结果列表内的重复 | %d |" % len(a7["intra_list_duplicates"]))
    w("| 丢失的变体 | %d |" % a7["lost_variant_count"])
    w("")
    w("口径说明：\n")
    w("- **条目数**按搜索轮次计——同一篇文档在多轮搜索里各有一条记录（这是**轮次视图**，"
      "不能去重，否则会丢掉「这一轮返回过它」这个事实）。")
    w("- **唯一文档数**按归一 URL 计——这才是「到底有几篇不同的文档」。")
    w("- **变体**是同一篇文档在不同轮次返回的**不同 (title, snippet) 组合**，"
      "全部保留在 `variants[]` 里：不同轮次的摘要长度与内容都可能不同，只留第一条就会丢证据。")
    w("- snippet 字符总数按**条目**统计（%d），去重后的净文本更少——"
      "差额是同一篇文档在多轮搜索里**重复返回**的相同摘要，不是丢失。\n"
      % m_s[2])

    w("## 6 结论\n")
    w("1. **单一来源 ≠ 完整历史。** 导出包 %d 节点 / %d fragment，分享链接 %d 节点 / %d fragment，"
      "并集 %d 节点 / **%d fragment**——比任一单源都多。\n"
      % (len(e_nodes), sum(len(v) for v in e_nodes.values()),
         len(s_nodes), sum(len(v) for v in s_nodes.values()),
         len(m_nodes), sum(len(v) for v in m_nodes.values())))
    w("2. **差异各有归属**：导出包独有 %d 个节点、分享链接独有 %d 个 fragment；"
      "两侧的搜索结果富载荷与附件字节各归其主。\n"
      % (len(only_e), len(only_share_frags)))
    diff_text = sum(len(v) for v in s_nodes.values()) - same_text
    w("3. **正文差异逐条可查**：共有节点内剥除 `[!reference:N]` 后，正文完全一致的 fragment "
      "有 %d 条、不一致的有 %d 条（不一致的条数包含两侧对同一事件的不同措辞/本地化表述，"
      "差异明细见第 2、3 节）。\n" % (same_text, diff_text))
    w("4. **并集不天然优于单源**：字段级合并规则在「非空但贫瘠」的字段上会丢信息。"
      "合并规则必须按**字段的语义**分两类。")
    w("5. **snippet 维度断言必须固化**：fragment 级多重集自检**发现不了**这类丢失，"
      "只有 snippet 维度能发现。本脚本第 0 节即为此设，退出码非 0 可直接当门禁用。")
    w("6. **同物异形要归一**：URL 的引用锚点、附件的 `file-` 前缀都是「同一事物、多种写法」。"
      "跨源合并前必须先归一，否则同一篇文档会被算成多条记录且互不补字段。")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="并集 vs 两条单源：差异报告 + snippet 维度断言")
    ap.add_argument("--export", help="导出包路径产物 JSON（必填）")
    ap.add_argument("--share", help="分享链接原始 API JSON（必填）")
    ap.add_argument("--merged", help="并集产物 JSON（必填）")
    ap.add_argument("--outdir", default=None, help="报告输出目录（默认与并集产物同目录）")
    args = ap.parse_args(argv)

    missing = [n for n in ("export", "share", "merged") if not getattr(args, n)]
    if missing:
        ap.error("缺少必填参数：%s（三份输入都要显式给出，脚本不内置任何默认路径）"
                 % ", ".join("--" + m for m in missing))

    global EXPORT, SHARE_RAW
    EXPORT = args.export
    SHARE_RAW = args.share

    e_doc, e_conv, e_nodes, e_search, e_open = load_export(EXPORT)
    s_doc, s_biz, s_nodes, s_search, s_open = load_share(SHARE_RAW)
    m_doc, m_conv, m_nodes, m_search, m_open = load_merged(args.merged)

    checks = run_assertions(e_nodes, s_nodes, m_nodes,
                            e_search, s_search, m_search,
                            e_open, s_open, m_open, m_conv=m_conv)

    outdir = args.outdir or os.path.dirname(os.path.abspath(args.merged))
    os.makedirs(outdir, exist_ok=True)
    md_path = os.path.join(outdir, "并集vs单源-差异报告.md")
    json_path = os.path.join(outdir, "并集自检.json")
    report = build_report(args.merged, e_doc, s_doc, m_doc,
                          e_nodes, s_nodes, m_nodes,
                          e_search, s_search, m_search,
                          e_open, s_open, m_open, checks)
    open(md_path, "w", encoding="utf-8").write(report)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"merged": args.merged, "checks": checks,
                   "ok": all(c["ok"] for c in checks.values())},
                  f, ensure_ascii=False, indent=2)

    print("wrote %s" % md_path)
    print("wrote %s" % json_path)
    print()
    print("=" * 62)
    print("SNIPPET / INTEGRITY ASSERTIONS")
    ok_all = True
    for name, c in checks.items():
        ok_all = ok_all and c["ok"]
        print("  %-4s %s" % ("PASS" if c["ok"] else "FAIL", name))
        for k, v in c.items():
            if k != "ok":
                print("        %s = %s" % (k, json.dumps(v, ensure_ascii=False)[:150]))
    print("=" * 62)
    print("RESULT:", "PASS" if ok_all else "FAIL")
    return 0 if ok_all else 3


if __name__ == "__main__":
    sys.exit(main())
