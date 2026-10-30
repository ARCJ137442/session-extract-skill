#!/usr/bin/env python3
"""
DeepSeek 跨源并集 — 把「账号导出包」与「网页版分享链接」两条路径的产物合并。

为什么需要并集
--------------
两条路径各有独占内容，**任何单一来源都不是完整历史**：

    账号导出包                          网页版分享链接
    ─────────────                      ────────────────
    快照时刻的状态（含中间轮次）        只有最终态（用户改/删掉的轮次丢失）
    附件只有元数据（无字节）            附件字节可直下
    TOOL_OPEN 载荷未导出                TOOL_OPEN 载荷完整
    有 TIP fragment？→ 没有             有 TIP fragment

合并规则
--------
1. **节点对齐键 = `node_id`**。两条路径的 node_id 编号空间一致（实测：分享链接的
   node_id 全部是导出包 node_id 的子集，导出包多出几轮被用户编辑掉的轮次）。
   不按内容做键匹配——两条路径的正文在归一 `[!reference:N]` 标记前并不可比。
2. **同一 node_id 同时存在于两条路径时**：`content` 取**导出包**版本（它保留行内引用
   标记与更完整的 THINK 链），导出包缺失的字段（如 `TOOL_OPEN` 的 `results`）用分享
   链接版本补齐。`sources[]` 标注该节点来自哪几条路径。
3. **附件按 `file_id` 合并**：以分享链接版本为主（它有 `bytes_available: true` 与
   `local_path`），导出包版本补 `file_size_bytes` 等导出侧独有字段。
4. 容器键名不同——导出包是 `raw_mapping`，分享链接是 `raw_messages`。**两个都保留**，
   不做取舍。
5. **搜索结果按「归一 URL」合并**。DeepSeek 的搜索结果 URL 带引用锚点
   （`https://x/a.pdf#7#22#7`），**同一篇文档在不同引用位置锚点不同**。
   按原始 url 建字典 = 同一篇文章被登记成多条记录、互相不补字段（一手证据就这么丢了）。
   归一规则统一放在 `deepseek_common.norm_url`，提取侧与合并侧用**同一把尺子**。

产物 schema：`rescued-session/v1`（不升 v2）。
    合并信息放在 `_meta.merge`，来源清单放在 `_meta.sources[]`，
    每个节点的出处放在 `transcript[].sources[]`。
    单源产物不需要这个块，所以并集产物与单源产物对下游是**同构**的。
    跨轮次的**唯一文档视图**放在 `conversations[0].references[]`——
    一篇文档一条记录，变体收在 `variants[]` 里。

用法：
    python merge_deepseek.py --export <export.json> --share <share.json> --out <merged.json>
    python merge_deepseek.py --export <export.json> --share <share.json> --outdir <dir> [--markdown]
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from deepseek_common import result_url_key, url_anchor
except ImportError:  # pragma: no cover - 缺文件时给出可操作的提示
    raise RuntimeError(
        "deepseek_common.py is missing next to this script; "
        "it holds the shared URL-normalization rules."
    )

SCHEMA = "rescued-session/v1"

# 结果记录里**派生**出来、合并后不再需要的字段。
# `url` 本身就是归一后的主键，再留一份 `url_normalized` 只是噪声。
_DROP_ON_MERGE = ("url_normalized",)


def load(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def node_key(entry):
    return str(entry.get("node_id"))


def _group_by_node(entries):
    """node_id -> 该节点内的 fragment 列表（保持原顺序）。"""
    groups = {}
    for e in entries:
        groups.setdefault(node_key(e), []).append(e)
    return groups


def _ordinal_keys(entries):
    """
    节点内 fragment 的匹配键：(type, 该 type 在本节点内的序号)。

    **不能只用 node_id 做键**——一个节点里有多个 fragment（REQUEST / THINK /
    TOOL_SEARCH / TOOL_OPEN / RESPONSE…），只按 node_id 合并会把整个节点压成一条，
    fragment 直接丢失（实测：一个几百条 fragment 的导出包会被压成几十条）。

    同节点内同 type 的序号在两条路径上对齐（实测：两边共有的节点全部对齐），
    分享链接独有的 fragment（如 TIP）在导出包里没有对应序号，作为新增项追加。
    """
    seen = {}
    keyed = []
    for e in entries:
        t = e.get("type")
        i = seen.get(t, 0)
        seen[t] = i + 1
        keyed.append(((t, i), e))
    return keyed


def _merge_results(cur, other):
    """
    结果列表**逐条按归一 URL 求并集**——不是按原始 url，也不是整列取一侧。

    为什么不能整列替换：导出包的 TOOL_SEARCH 结果只有 `{title, url}` 两个字段——
    **非空但信息贫瘠**。而「只在该字段为空时才补齐」的规则对非空字段不生效，
    于是分享链接侧的 snippet / site_name / site_icon / published_at /
    cite_index / query_indexes 会被整体丢弃。

    逐条按 url 合并，才能既保留导出包独有的结果（导出包独有轮次多出的那些），
    又补上分享链接侧的富字段。

    为什么按**归一** url 而不是原始 url：DeepSeek 的搜索结果 URL 带引用锚点
    （`https://x/a.pdf#7#22#7`），**同一篇文档在不同引用位置锚点不同**。
    按原始 url 建字典 = 同一篇文章被登记成多条记录，且**互相不补字段**——
    导出包侧那条贫瘠记录拿不到分享链接侧的 snippet，一手证据就这么丢了。
    归一（剥掉 `#` 之后的锚点）之后，「同一篇文档」才有唯一主键。

    合并后的每条记录都带：
      url          —— 归一后的 URL（主键，干净）
      url_original —— 该文档**首次出现**时的原始 URL（含锚点）
      url_anchor   —— 首次出现的锚点（没有则 None）
      variants     —— 各轮次里出现过的不同 (title, snippet) 组合（去重后）

    字段合并规则沿用「空字段才补齐」，但**跨变体也要补**：
    合并时把每一条参与合并的记录的字段都往主记录的空字段上补，
    否则「非空但贫瘠」的坑（导出包侧只有 title/url）会照旧吃掉分享链接侧的富字段。
    snippet 例外——取**最长**的：合并只会留一条，取最长才是无损。
    """
    cur = list(cur or [])
    other = list(other or [])
    records, order = {}, []

    def add(r):
        if not isinstance(r, dict):
            return
        key = result_url_key(r)
        if key is None:
            # 没有 url 的结果条目无法归一——**不参与去重**，原样保留。
            # （否则它们会挤进同一条记录里互相覆盖，白丢数据。）
            key = "\x00nourl\x00%d" % len(order)
            records[key] = dict(r)
            order.append(key)
            return
        base = records.get(key)
        if base is None:
            base = dict(r)
            for k in _DROP_ON_MERGE:
                base.pop(k, None)
            base["url"] = key
            base["url_original"] = r.get("url_original") or r.get("url")
            base["url_anchor"] = (
                r.get("url_anchor")
                if r.get("url_anchor") is not None
                else url_anchor(r.get("url"))
            )
            base["variants"] = []
            records[key] = base
            order.append(key)
        else:
            _merge_result_fields(base, r)
        # 变体从**每一条**参与合并的记录上收（含被合并掉的那条），
        # 否则短的那条 snippet 会被丢掉。
        v = _result_variant(r)
        if v and v not in base["variants"]:
            base["variants"].append(v)

    for r in cur:
        add(r)
    for r in other:
        add(r)
    return [records[k] for k in order]


def _merge_result_fields(base, other):
    """
    把 `other` 的字段并进 `base`：**空字段才补齐**。

    与老规则唯一的差别是调用位置——这里对**每一条**参与合并的记录都跑一遍，
    所以「非空但贫瘠」的字段也能被更富的一侧补上。
    """
    for k, v in other.items():
        if k in ("url", "url_original", "url_anchor", "variants") or k in _DROP_ON_MERGE:
            continue
        if base.get(k) in (None, "", [], {}) and v not in (None, "", [], {}):
            base[k] = v
    # snippet 取**最长**：合并只会留一条，取最长才是无损。
    if len(other.get("snippet") or "") > len(base.get("snippet") or ""):
        base["snippet"] = other.get("snippet")


def _result_variant(r):
    """结果记录里参与「变体」去重的部分：(title, snippet)，空值不入。"""
    if not isinstance(r, dict):
        return None
    v = {}
    for k in ("title", "snippet"):
        val = r.get(k)
        if val not in (None, "", [], {}):
            v[k] = val
    return v or None


def _build_reference_registry(entries):
    """
    按**归一 URL** 建立全局文档登记表：**一篇文档恰好一条记录**，
    变体（各搜索轮次里出现过的不同 title/snippet 组合）收在 `variants` 里。

    为什么需要这张表：fragment 的 `results[]` 是**按轮次**的列表——
    同一篇文档会在多轮搜索里重复出现。只看某一个 fragment 看不出
    「到底有几篇不同的文档」，也没法保证「一篇文档一条记录」。
    这张表是**跨轮次的唯一文档视图**，也是 A7 断言的对象。

    每条记录带：
      url          —— 归一后的 URL（唯一主键）
      url_original —— 该文档**首次出现**时的原始 URL（含锚点）
      url_anchor   —— 首次出现的锚点（没有则 None）
      variants     —— 去重后的不同 (title, snippet) 组合
      occurrences  —— 出现在哪些 fragment 的第几个结果位（可追溯）

    字段合并规则与 `_merge_result_fields` 一致：空字段才补齐，snippet 取最长。
    """
    registry, order = {}, []
    for e in entries or []:
        lst = e.get("results") or []
        for idx, r in enumerate(lst):
            if not isinstance(r, dict):
                continue
            key = result_url_key(r)
            if key is None:
                continue
            rec = registry.get(key)
            if rec is None:
                rec = {
                    "url": key,
                    "url_original": r.get("url_original") or r.get("url"),
                    "url_anchor": (
                        r.get("url_anchor")
                        if r.get("url_anchor") is not None
                        else url_anchor(r.get("url"))
                    ),
                    "variants": [],
                    "occurrences": [],
                }
                registry[key] = rec
                order.append(key)
            else:
                # 首次出现的原始 URL 说了算（后面出现的可能带不同锚点）
                if rec.get("url_original") in (None, "") and r.get("url"):
                    rec["url_original"] = r.get("url")
                    rec["url_anchor"] = url_anchor(r.get("url"))
            # 字段合并：空字段才补齐，snippet 取最长
            _merge_result_fields(rec, r)
            # 变体：把**每条**记录都算进去（含主记录里 snippet 被更长那条替换掉的）
            for v in (r.get("variants") or []):
                if v and v not in rec["variants"]:
                    rec["variants"].append(v)
            v = _result_variant(r)
            if v and v not in rec["variants"]:
                rec["variants"].append(v)
            occ = {
                "node_id": e.get("node_id"),
                "type": e.get("type"),
                "index": idx,
                "url": r.get("url_original") or r.get("url"),
            }
            if occ not in rec["occurrences"]:
                rec["occurrences"].append(occ)
    return [registry[k] for k in order]


def _write_back_registry(entries, registry):
    """
    把登记表里的「全局首次原始 URL / 锚点 / 变体」写回每条结果记录。

    写回之后，同一篇文档在**任何** fragment 里看到的那条记录都是一样的：
    同一个 url、同一份变体表。这样「变体收在 variants[] 里」才真正成立——
    否则跨轮次的变体只存在于某个特定的 fragment 里。
    """
    by_url = {rec.get("url"): rec for rec in registry or []}
    for e in entries or []:
        for r in e.get("results") or []:
            if not isinstance(r, dict):
                continue
            rec = by_url.get(result_url_key(r))
            if not rec:
                continue
            r["url"] = rec["url"]
            r["url_original"] = rec["url_original"]
            r["url_anchor"] = rec["url_anchor"]
            r["variants"] = [dict(v) for v in rec.get("variants") or []]


def _merge_files(cur, other):
    """附件/文件列表逐条按 file_id 求并集（file_id 形态两条路径可能不同）。"""
    def key(f):
        fid = str(f.get("file_id") or f.get("name") or "")
        return fid[5:] if fid.startswith("file-") else fid

    cur = list(cur or [])
    other = list(other or [])
    if not other:
        return cur
    by_key, order = {}, []
    for f in cur:
        k = key(f)
        if k not in by_key:
            by_key[k] = dict(f)
            order.append(k)
    for f in other:
        k = key(f)
        if k not in by_key:
            by_key[k] = dict(f)
            order.append(k)
        else:
            base = by_key[k]
            for kk, vv in f.items():
                if base.get(kk) in (None, "", [], {}) and vv not in (None, "", [], {}):
                    base[kk] = vv
    return [by_key[k] for k in order]


def merge_fragments(export_entries, share_entries):
    """按 (node_id, type, 节点内序号) 求并集。"""
    exp_groups = _group_by_node(export_entries)
    sh_groups = _group_by_node(share_entries)

    out = []
    node_sources = {}

    for node_id in sorted(set(exp_groups) | set(sh_groups),
                          key=lambda x: (len(x), x)):
        exp_list = exp_groups.get(node_id, [])
        sh_list = sh_groups.get(node_id, [])

        exp_keyed = _ordinal_keys(exp_list)
        sh_keyed = _ordinal_keys(sh_list)

        exp_by_key = {k: e for k, e in exp_keyed}
        sh_by_key = {k: e for k, e in sh_keyed}

        merged_for_node = []
        # 先按导出包的顺序走（它是更完整的视图），再补分享链接独有的 fragment
        order = [(k, e) for k, e in exp_keyed]
        for k, e in sh_keyed:
            if k not in exp_by_key:
                order.append((k, e))

        for k, e in order:
            other = sh_by_key.get(k)
            cur = dict(e)
            cur["sources"] = list(e.get("sources") or ["export"])
            if other is not None:
                for field, val in other.items():
                    if field in ("sources", "seq"):
                        continue
                    # **列表型字段必须逐条合并**，不能整列取一侧——
                    # 导出包的 results 非空但贫瘠（只有 title/url），
                    # 整列替换会把分享链接侧的 snippet 等富字段整体丢掉。
                    if field == "results":
                        cur["results"] = _merge_results(cur.get("results"), val)
                        continue
                    if field == "files":
                        cur["files"] = _merge_files(cur.get("files"), val)
                        continue
                    if cur.get(field) in (None, "", [], {}) and val not in (None, "", [], {}):
                        cur[field] = val
                # 正文两条路径的规范化方式不同（分享侧剥离了 [!reference:N] 行内标记，
                # 关系移到了 references 数组）。**不覆盖**导出包侧的正文——它才是主版本；
                # 分享侧的变体另存为 content_share，信息不丢而主版本不被改写。
                other_content = other.get("content")
                if other_content and other_content != cur.get("content"):
                    cur["content_share"] = other_content
                    cur["content_differs_between_sources"] = True
                merged = []
                for x in cur["sources"] + list(other.get("sources") or ["share"]):
                    if x not in merged:
                        merged.append(x)
                cur["sources"] = merged
            else:
                cur["sources"] = ["share"]
            merged_for_node.append(cur)

        node_sources[node_id] = {
            "export": node_id in exp_groups,
            "share": node_id in sh_groups,
        }
        out.extend(merged_for_node)

    for i, e in enumerate(out):
        e["seq"] = i
    return out, node_sources


def _attachment_key(a):
    """
    附件匹配键：file_id。

    注意两条路径的 file_id 形态不同——导出包是**裸 UUID**（`<uuid>`），
    分享链接加了 `file-` 前缀（`file-<uuid>`）。不归一化就会把同一个附件
    算成两个（实测：附件数量直接翻倍）。
    （示例一律用占位值：早期版本这里用了真实会话的 file_id 前缀。）
    """
    fid = str(a.get("file_id") or "")
    return fid[5:] if fid.startswith("file-") else fid


def merge_attachments(export_atts, share_atts):
    """按归一化 file_id 合并；分享链接版本优先（它有字节）。"""
    by_id = {}

    for a in export_atts or []:
        item = dict(a)
        item.setdefault("sources", ["export"])
        item["bytes_available"] = bool(item.get("bytes_available"))
        by_id[_attachment_key(a)] = item

    for a in share_atts or []:
        k = _attachment_key(a)
        if k in by_id:
            cur = by_id[k]
            for field, val in a.items():
                if field in ("sources", "bytes_available", "bytes_available_in_export"):
                    continue
                if cur.get(field) in (None, "", [], {}) and val not in (None, "", [], {}):
                    cur[field] = val
            # 「字节是否到手」是**或**关系：任一来源拿到就算有
            cur["bytes_available"] = bool(
                cur.get("bytes_available") or a.get("bytes_available")
            )
            merged = []
            for x in cur.get("sources", []) + list(a.get("sources") or ["share"]):
                if x not in merged:
                    merged.append(x)
            cur["sources"] = merged
        else:
            item = dict(a)
            item.setdefault("sources", ["share"])
            by_id[k] = item

    return sorted(by_id.values(), key=lambda x: str(x.get("timestamp") or ""))


def merge(export_doc, share_doc):
    exp_conv = (export_doc.get("conversations") or [{}])[0]
    sh_conv = (share_doc.get("conversations") or [{}])[0]

    exp_entries = exp_conv.get("transcript") or []
    sh_entries = sh_conv.get("transcript") or []

    merged_entries, node_sources = merge_fragments(exp_entries, sh_entries)

    # 跨轮次的**唯一文档视图**：一篇文档一条记录，变体收在 variants 里。
    # 建完表再写回各 fragment，让同一条记录在哪儿看都是同一份。
    registry = _build_reference_registry(merged_entries)
    _write_back_registry(merged_entries, registry)
    variant_count = sum(len(rec.get("variants") or []) for rec in registry)

    merged_atts = merge_attachments(
        exp_conv.get("attachments") or [], sh_conv.get("attachments") or []
    )

    exp_nodes = {node_key(e) for e in exp_entries}
    sh_nodes = {node_key(e) for e in sh_entries}

    # 统计：每个来源各自贡献了哪些节点
    only_export = sorted(exp_nodes - sh_nodes, key=lambda x: (len(x), x))
    only_share = sorted(sh_nodes - exp_nodes, key=lambda x: (len(x), x))
    both = exp_nodes & sh_nodes

    title = exp_conv.get("title")
    if not title or exp_conv.get("title_is_placeholder"):
        title = sh_conv.get("title") or title

    payload = {
        "id": exp_conv.get("id") or sh_conv.get("id"),
        "title": title,
        "inserted_at": exp_conv.get("inserted_at") or sh_conv.get("inserted_at"),
        "updated_at": exp_conv.get("updated_at") or sh_conv.get("updated_at"),
        "rescue_reason": exp_conv.get("rescue_reason") or sh_conv.get("rescue_reason"),
        "stats": {
            "fragment_count": len(merged_entries),
            "user_turn_count": sum(1 for e in merged_entries if e.get("type") == "REQUEST"),
            "assistant_turn_count": sum(1 for e in merged_entries if e.get("type") == "RESPONSE"),
            "thinking_count": sum(1 for e in merged_entries if e.get("type") == "THINK"),
            "tool_fragment_count": sum(
                1 for e in merged_entries
                if str(e.get("type", "")).startswith("TOOL_") or e.get("type") == "SEARCH"
            ),
            "attachment_count": sum(1 for e in merged_entries if e.get("type") == "FILE"),
            "by_type": _count_by(merged_entries, "type"),
            "by_role": _count_by(merged_entries, "role"),
            "content_chars": sum(len(e.get("content", "")) for e in merged_entries),
            # 搜索结果的两个口径：条目数（按轮次重复计）vs 文档数（按归一 URL 去重）
            "result_entry_count": sum(len(e.get("results") or []) for e in merged_entries),
            "unique_document_count": len(registry),
            "document_variant_count": variant_count,
        },
        "attachments": merged_atts,
        "transcript": merged_entries,
        # 跨轮次的唯一文档视图：一篇文档一条记录，变体收在 variants 里
        "references": registry,
        # 两个容器的键名不同（raw_mapping / raw_messages），并集里都保留，不做取舍
        "raw_mapping": exp_conv.get("raw_mapping"),
        "raw_messages": sh_conv.get("raw_messages"),
    }

    sources = []
    for m in (export_doc.get("_meta", {}).get("sources") or []):
        sources.append(m)
    for m in (share_doc.get("_meta", {}).get("sources") or []):
        sources.append(m)

    meta = {
        "schema": SCHEMA,
        "platform": "deepseek-web-merged",
        "source": {
            "kind": "merged",
            "export_file": (export_doc.get("_meta", {}).get("source") or {}).get("file"),
            "export_sha256": (export_doc.get("_meta", {}).get("source") or {}).get("sha256"),
            "share_id": (share_doc.get("_meta", {}).get("source") or {}).get("share_id"),
            "share_sha256": (share_doc.get("_meta", {}).get("source") or {}).get("sha256"),
        },
        "sources": sources,
        "merge": {
            "strategy": "union-by-(node_id,type,ordinal)",
            "node_counts": {
                "export": len(exp_nodes),
                "share": len(sh_nodes),
                "both": len(exp_nodes & sh_nodes),
                "only_export": len(exp_nodes - sh_nodes),
                "only_share": len(sh_nodes - exp_nodes),
                "merged": len(exp_nodes | sh_nodes),
            },
            "fragment_counts": {
                "export": len(exp_entries),
                "share": len(sh_entries),
                "merged": len(merged_entries),
                "added_by_share": len(merged_entries) - len(exp_entries),
            },
            "contributed_by": {
                "export": sorted(exp_nodes, key=lambda x: (len(x), x)),
                "share": sorted(sh_nodes, key=lambda x: (len(x), x)),
            },
            "only_in_export": only_export,
            "only_in_share": only_share,
            "document_counts": {
                # 结果条目数（按轮次重复计）vs 唯一文档数（按归一 URL 去重）
                "result_entries": sum(len(e.get("results") or []) for e in merged_entries),
                "unique_documents": len(registry),
                "variants": variant_count,
            },
            "content_rule": (
                "同一 node_id 冲突时取导出包版本（保留行内引用标记与更完整 THINK 链），"
                "导出包缺失的字段由分享链接版本补齐；分享链接独有的 fragment（如 TIP）追加。"
                "列表型字段（results/files）逐条按主键（归一 url/file_id）合并，"
                "不整列取一侧——导出包的 results 非空但贫瘠（只有 title/url），"
                "整列替换会丢掉分享链接侧的 snippet/site_name/published_at"
            ),
            "url_rule": (
                "搜索结果的主键是**归一 URL**（剥掉 # 之后的引用锚点）——"
                "同一篇文档在不同引用位置锚点不同，按原始 url 建字典会把它算成多条记录、"
                "互相不补字段。记录里 url=归一后的 URL、url_original=首次出现的原始 URL、"
                "url_anchor=首次出现的锚点，跨轮次的不同 (title, snippet) 组合收在 variants 里；"
                "跨轮次的唯一文档视图在 conversations[0].references[]"
            ),
            "note": (
                "单一来源 ≠ 完整历史：导出包是快照（含中间轮次）、分享链接是最终态"
                "（含附件字节与 TOOL_OPEN 载荷）。并集才是完整历史。"
            ),
        },
        "extraction": {
            "tool": "merge_deepseek.py",
            "mode": "merge",
            "rescue_reason": payload.get("rescue_reason"),
        },
        "conversation_count": 1,
    }

    return {"_meta": meta, "conversations": [payload]}


def _count_by(entries, key):
    from collections import Counter
    return dict(Counter(e.get(key) for e in entries))


def write_markdown(path, payload):
    lines = ["# %s（跨源并集）" % payload.get("title"), ""]
    st = payload.get("stats") or {}
    lines.append(
        "- id: `%s`\n- inserted: %s\n- updated: %s\n"
        "- fragments: %s（user %s / assistant %s）\n"
        % (
            payload.get("id"), payload.get("inserted_at"), payload.get("updated_at"),
            st.get("fragment_count"), st.get("user_turn_count"), st.get("assistant_turn_count"),
        )
    )
    for e in payload.get("transcript") or []:
        if e.get("type") == "THINK":
            continue
        body = e.get("content", "")
        if e.get("type") == "FILE":
            body = "\n".join(
                "- %s (%s bytes)" % (f.get("file_name"), f.get("file_size_bytes"))
                for f in (e.get("files") or [])
            )
        if e.get("type") in ("TOOL_SEARCH", "SEARCH"):
            body = "\n".join(
                # 用原始 URL（含锚点）做链接——锚点是「引用位置」的真实信息；
                # 归一 URL 留在 `url` 字段里做主键。
                "- [%s](%s)%s" % (
                    r.get("title"), r.get("url_original") or r.get("url"),
                    ("\n  > " + str(r.get("snippet")).replace("\n", " ")) if r.get("snippet") else "",
                )
                for r in (e.get("results") or [])
            )
        src = "+".join(e.get("sources") or [])
        lines.append("\n## [%s] %s `%s`  ⟨%s⟩\n" % (
            e.get("seq"), e.get("label"), e.get("type"), src))
        lines.append(str(body))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def main(argv=None):
    ap = argparse.ArgumentParser(description="DeepSeek 导出包 × 分享链接 跨源并集")
    ap.add_argument("--export", required=True, help="导出包路径产物的 JSON")
    ap.add_argument("--share", required=True, help="分享链接路径产物的 JSON")
    ap.add_argument("--out", help="输出单个 JSON")
    ap.add_argument("--outdir", help="输出目录（一个会话一份文件）")
    ap.add_argument("--markdown", action="store_true", help="同时写 .md 预览")
    args = ap.parse_args(argv)

    result = merge(load(args.export), load(args.share))
    payload = result["conversations"][0]
    mc = result["_meta"]["merge"]["node_counts"]

    if args.outdir:
        outdir = os.path.abspath(args.outdir)
        os.makedirs(outdir, exist_ok=True)
        base = "%s_%s_%s" % (
            (payload.get("inserted_at") or "")[:10],
            str(payload.get("id"))[:8],
            payload.get("title"),
        )
        import re
        base = re.sub(r'[\\/:*?"<>|\s]+', "_", base)[:120]
        jp = os.path.join(outdir, base + ".json")
        with open(jp, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print("wrote", jp)
        if args.markdown:
            mp = os.path.join(outdir, base + ".md")
            write_markdown(mp, payload)
            print("wrote", mp)
    else:
        out_path = os.path.abspath(args.out or "merged_deepseek.json")
        d = os.path.dirname(out_path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print("wrote", out_path)

    print()
    print("MERGE (union by node_id)")
    print("  export nodes : %d" % mc["export"])
    print("  share nodes  : %d" % mc["share"])
    print("  in both      : %d" % mc["both"])
    print("  only export  : %d  %s" % (mc["only_export"], result["_meta"]["merge"]["only_in_export"]))
    print("  only share   : %d  %s" % (mc["only_share"], result["_meta"]["merge"]["only_in_share"]))
    print("  merged       : %d" % mc["merged"])
    dc = result["_meta"]["merge"]["document_counts"]
    print()
    print("MERGE (results by normalized url)")
    print("  result entries : %d" % dc["result_entries"])
    print("  unique docs    : %d" % dc["unique_documents"])
    print("  variants       : %d" % dc["variants"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
