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
1. **节点对齐键 = `node_id`**。两条路径的 node_id 编号空间一致（实测：分享链接 48 个
   node_id 全部是导出包 node_id 的子集，导出包多出 47/48 两轮）。
   不按内容做键匹配——两条路径的正文在归一 `[!reference:N]` 标记前并不可比。
2. **同一 node_id 同时存在于两条路径时**：`content` 取**导出包**版本（它保留行内引用
   标记与更完整的 THINK 链），导出包缺失的字段（如 `TOOL_OPEN` 的 `results`）用分享
   链接版本补齐。`sources[]` 标注该节点来自哪几条路径。
3. **附件按 `file_id` 合并**：以分享链接版本为主（它有 `bytes_available: true` 与
   `local_path`），导出包版本补 `file_size_bytes` 等导出侧独有字段。
4. 容器键名不同——导出包是 `raw_mapping`，分享链接是 `raw_messages`。**两个都保留**，
   不做取舍。

产物 schema：`rescued-session/v1`（不升 v2）。
    合并信息放在 `_meta.merge`，来源清单放在 `_meta.sources[]`，
    每个节点的出处放在 `transcript[].sources[]`。
    单源产物不需要这个块，所以并集产物与单源产物对下游是**同构**的。

用法：
    python merge_deepseek.py --export <export.json> --share <share.json> --out <merged.json>
    python merge_deepseek.py --export <export.json> --share <share.json> --outdir <dir> [--markdown]
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SCHEMA = "rescued-session/v1"


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
    fragment 直接丢失。实测导出包 248 条 fragment 在只按 node_id 合并后只剩 50 条。

    同节点内同 type 的序号在两条路径上对齐（实测 48 个共有节点全部对齐），
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

    注意两条路径的 file_id 形态不同——导出包是裸 UUID（`<附件 id>-…`），
    分享链接加了 `file-` 前缀（`file-<附件 id>-…`）。不归一化就会把同一个附件
    算成两个（实测 4 个附件会合并成 8 个）。
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
        },
        "attachments": merged_atts,
        "transcript": merged_entries,
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
            "content_rule": (
                "同一 node_id 冲突时取导出包版本（保留行内引用标记与更完整 THINK 链），"
                "导出包缺失的字段由分享链接版本补齐；分享链接独有的 fragment（如 TIP）追加"
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
                "- [%s](%s)" % (r.get("title"), r.get("url")) for r in (e.get("results") or [])
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
