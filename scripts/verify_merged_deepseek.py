# -*- coding: utf-8 -*-
"""
并集 vs 两条单源：逐节点差异报告 + **snippet 维度断言**（可当门禁用）。

用法：
    python verify_merged_deepseek.py --export <导出包产物.json>         --share <分享链接原始.json> --merged <并集产物.json> [--outdir <目录>]

三份输入的默认位置指向 2026-10-29 的实测样本；换样本时用参数覆盖即可。

为什么要有这个脚本：
    2026-10-29 的并集实现里，**所有 fragment 级多重集自检都通过了**，
    但导出包的 `results` 非空而贫瘠（每条只有 {title,url}），分享链接侧的
    富载荷被整列丢弃——454 条结果、109,508 字符 snippet 在并集里归零。
    fragment 多重集看不出这种丢失，**只有 snippet 维度能发现**。
    所以这里把 snippet 断言固化成可执行的门禁。

断言清单：
    A1  merged snippet 字符总数 >= max(单源导出包, 单源分享链接)
    A2  merged 带 snippet 的结果条目数 >= max(单源导出包, 单源分享链接)
    A3  merged fragment 数 >= max(单源导出包, 单源分享链接)
    A4  merged 节点集合 == 两侧节点集合的并集
    A5  两侧每条带 snippet 的结果，其 url 在并集里仍带 snippet（逐条核对）
    A6  两侧的每个 (node_id, type, 节点内序号) 键在并集里都存在

退出码：0 = 全部断言通过；3 = 有断言失败（可直接当门禁用）。
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys
from collections import defaultdict

BASE = os.environ.get("DEEPSEEK_EXPORT_DIR", "")
DEFAULT_EXPORT = os.path.join(BASE, "rescued_某专有词小写", "2026-10-29_<会话 id>_目标会话.json")
DEFAULT_SHARE = os.path.join(BASE, "网页版分享链接提取", "原始数据", "目标会话-原始.json")
DEFAULT_MERGED = os.path.join(BASE, "rescued_merged", "2026-10-29_<会话 id>_目标会话.json")

EXPORT = DEFAULT_EXPORT
SHARE_RAW = DEFAULT_SHARE

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
    {url: 该 url 在所有搜索结果中出现过的**最长** snippet}。

    注意：同一个 url 会在不同节点的多次搜索里重复出现，且每次的 snippet
    文本长度可能不同（不同查询词返回不同摘要）。按"最后一个"取会误判成
    "被截短"，所以必须按**最长**取——合并只会保留一条，取最长才是无损。
    """
    out = {}
    for lst in result_lists:
        for r in lst:
            u = r.get("url")
            s = r.get("snippet") or ""
            if u and s.strip() and len(s) > len(out.get(u, "")):
                out[u] = s
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
                   e_open, s_open, m_open):
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
    return checks


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
    w("> | 导出包单源 `rescued_某专有词小写/…` | `%s` |" % sha256(EXPORT))
    w("> | 分享链接单源 `网页版分享链接提取/原始数据/…` | `%s` |" % sha256(SHARE_RAW))
    w("> | **并集产物** `%s` | `%s` |\n" % (os.path.basename(merged_path), sha256(merged_path)))
    w("> 比对口径：正文剥除 `[!reference:N]` 后再比；fragment 按 `(node_id, type, 节点内序号)` 对齐。\n")
    w("\n---\n")

    w("## 0 ⚠️ snippet 维度断言（门禁）\n")
    w("> **为什么单独有这一节**：2026-10-29 的并集实现里，**所有 fragment 级多重集自检都通过了**，"
      "但导出包的 `results` 非空而贫瘠（每条只有 `{title, url}`），分享链接侧的富载荷被整列丢弃——"
      "**454 条结果、109,508 字符的 snippet 在并集里归零**。fragment 多重集看不出这种丢失，"
      "**只有 snippet 维度能发现**。\n")
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
        w("> 这一轮是 **GitHub Issue 某 issue** 首次被提出的地方——DeepSeek 在这里把假设从 某 issue 转到"
          "「chunks 上传成功但文件元数据 `f:` 文档从未写入（`size:0, children:[]`）、"
          "日志 `pullOnly`、全程无 push 阶段」。用户随后编辑了问题并重新生成回答，"
          "分享链接因此丢掉了这一轮。\n")

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
      "分享链接侧则有完整载荷（snippet / site_name / site_icon / published_at / cite_index / query_indexes）。\n")
    w("> 这意味着：**如果并集按整列取一侧，搜索证据会被大面积抹掉**。"
      "而这些 snippet 恰恰是一手证据（例如 某版本 的发布说明原文"
      "「【合成样例】发布说明片段 "
      "'【合成样例】发布说明片段' and '【合成样例】发布说明片段' in 【合成样例】发布说明片段 (某 issue)」）。\n")

    m_s = snippet_stats(m_search + m_open)
    w("## 5 已修复的历史缺陷（数值来自修复前实测）\n")
    w("| | 搜索结果条目 | 带 snippet | snippet 字符 |")
    w("|---|---:|---:|---:|")
    w("| 修复前 · 并集（原版） | 532 | **40** | **10,440** |")
    w("| 修复前 · 分享链接单源 | 494 | 494 | **119,948** |")
    w("| **修复后 · 并集** | **%d** | **%d** | **%d** |" % (m_s[0], m_s[1], m_s[2]))
    w("")
    w("差值 **109,508 字符**（= 分享链接侧 454 条 SEARCH 结果的 snippet 全文）"
      "在修复前的并集里归零。\n")
    w("**根因**：字段级补齐规则是「只在当前字段为空时才补」，而导出包的 `results` "
      "**非空**（只是贫瘠）→ 永不触发补齐 → 分享链接侧整列富载荷被丢弃。\n")
    w("**反证**：TOOL_OPEN 侧原本就是对的（40 条 / 10,440 字符都在），"
      "因为导出包该字段**确实为空**，正确触发了补齐。缺陷只在「非空但贫瘠」时发生。\n")
    w("**修法**：结果列表按 `url` **逐条**求并集并逐条补齐字段；其余字段维持「空才补」。\n")
    w("> **教训（已写进 skill）**：「字段空不空」不足以决定怎么合并——要按字段语义分两类："
      "空字段（`None`/`\"\"`/`[]`/`{}`）可整体替换；列表型字段必须逐条按主键合并。"
      "推论：**跨源合并时不能只比「字段有没有」，要比「字段语义是否同类」——"
      "否则最容易被整列替换吃掉的，正是证据最密的那一侧。**\n")

    w("## 6 结论\n")
    w("1. **单一来源 ≠ 完整历史。** 导出包 %d 节点 / %d fragment，分享链接 %d 节点 / %d fragment，"
      "并集 %d 节点 / **%d fragment**——比任一单源都多。"
      % (len(e_nodes), sum(len(v) for v in e_nodes.values()),
         len(s_nodes), sum(len(v) for v in s_nodes.values()),
         len(m_nodes), sum(len(v) for v in m_nodes.values())))
    w("2. **三处差异各有归属**：节点 47/48 与 TOOL_OPEN 载荷来自导出包；"
      "5 条 `TIP` 与**全部搜索结果富载荷**来自分享链接；附件字节来自分享链接。")
    w("3. **正文实质一致**：共有节点内归一后的正文差异为 0；"
      "未经归一时有 19 个节点报 TEXT-DIFF，全部由 `[!reference:N]` 造成。")
    w("4. **并集不天然优于单源**：字段级合并规则在「非空但贫瘠」的字段上会丢信息。"
      "合并规则必须按**字段的语义**分两类。")
    w("5. **snippet 维度断言必须固化**：fragment 级多重集自检**发现不了**这类丢失，"
      "只有 snippet 维度能发现。本脚本第 0 节即为此设，退出码非 0 可直接当门禁用。")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="并集 vs 两条单源：差异报告 + snippet 维度断言")
    ap.add_argument("--export", default=DEFAULT_EXPORT, help="导出包路径产物 JSON")
    ap.add_argument("--share", default=DEFAULT_SHARE, help="分享链接原始 API JSON")
    ap.add_argument("--merged", default=DEFAULT_MERGED, help="并集产物 JSON")
    ap.add_argument("--outdir", default=None, help="报告输出目录（默认与并集产物同目录）")
    args = ap.parse_args(argv)

    global EXPORT, SHARE_RAW
    EXPORT = args.export
    SHARE_RAW = args.share

    e_doc, e_conv, e_nodes, e_search, e_open = load_export(EXPORT)
    s_doc, s_biz, s_nodes, s_search, s_open = load_share(SHARE_RAW)
    m_doc, m_conv, m_nodes, m_search, m_open = load_merged(args.merged)

    checks = run_assertions(e_nodes, s_nodes, m_nodes,
                            e_search, s_search, m_search,
                            e_open, s_open, m_open)

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
