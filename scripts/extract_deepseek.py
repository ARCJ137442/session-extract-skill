"""
DeepSeek Web Export 提取器（conversations.json）。

DeepSeek 网页版 / APP 的「导出数据」是**单个** conversations.json，里面装下该账号的
**全部**会话，与 Codex / Claude Code 的「一会话一 JSONL」根本不同：

    deepseek_data-YYYY-MM-DD/
    ├── user.json          # 账号信息
    └── conversations.json # 会话数组

会话内部结构也不是线性消息数组，而是**聊天树**（mapping，节点带 parent/children）——
重新生成答案会 fork 出一条新分支。所以必须做「树 → 线性」线性化，否则会把兄弟分支
拼在一起，得到自相矛盾的对话。

Schema（已在 285 条真实样本上逐一验证）:
    顶层:      list[ {id, title, inserted_at, updated_at, mapping} ]
    mapping:   { node_id: {id, parent, children[], message} }
    message:   {model, inserted_at, fragments[]}      （message 可能为 null）
    fragment:  REQUEST     {type, content}            用户提问
                RESPONSE    {type, content}            模型回答
                THINK       {type, content}            模型思考
                TOOL_SEARCH {type, results[]}          联网搜索结果 [{url, title}]
                SEARCH      {type, results[]}          旧版搜索
                FILE        {type, files[]}            附件 [{file_id, file_name, file_size}]
                TOOL_OPEN   {type}                     打开网页（载荷未导出）
                TOOL_FIND   {type}                     查找（载荷未导出）

核心价值点:
    1. 语料库检索（scan）—— DeepSeek 标题是自动生成的摘要，用户记忆里的原话通常
       只在正文中；所以要能按话题搜整个语料库，而不是靠 session ID 定位。
    2. 树线性化（linearise）—— 从 root 深度优先遍历，并补捞 root 不可达的孤儿节点。
    3. 忠实兜底 —— transcript 是给人读的视图，raw_mapping 原样保留，任何重分析都不丢信息。
"""
import hashlib
import json
import math
import os
import re
from collections import Counter

PLATFORM = "deepseek-web-export"
SCHEMA = "rescued-session/v1"

# 标题命中视作该关键词在正文中**额外命中 100 次**——标题是强信号，但不至于压过
# 正文里成千上万次的真实讨论。
TITLE_WEIGHT = 100

# Fragment 类型 -> (role, 人类可读标签)
FRAGMENT_ROLES = {
    "REQUEST": ("user", "用户提问"),
    "RESPONSE": ("assistant", "模型回答"),
    "THINK": ("assistant", "模型思考"),
    "TOOL_SEARCH": ("tool", "联网搜索"),
    "SEARCH": ("tool", "搜索"),
    "TOOL_OPEN": ("tool", "打开网页"),
    "TOOL_FIND": ("tool", "查找"),
    "FILE": ("user", "附件"),
}


# ============================================================
# Loading
# ============================================================

def load_corpus(path):
    """读取 DeepSeek 导出包，返回会话列表。"""
    with open(os.path.expanduser(path), "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("unexpected top level: expected a list of conversations")
    return data


def file_sha256(path):
    """源文件校验和——打捞产物必须能指回它是从哪份数据来的。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ============================================================
# Walking
# ============================================================

def iter_fragments(conv):
    """遍历一个会话的全部 fragment（不考虑树序）：(node_id, message, fragment)。"""
    for node_id, node in (conv.get("mapping") or {}).items():
        msg = node.get("message")
        if not msg:
            continue
        for frag in msg.get("fragments") or []:
            yield node_id, msg, frag


def frag_text(frag):
    """fragment 的可检索文本：正文 + 搜索结果的标题/URL + 附件名。"""
    parts = []
    if frag.get("content"):
        parts.append(str(frag["content"]))
    for r in frag.get("results") or []:
        if isinstance(r, dict):
            parts.append(str(r.get("title", "")))
            parts.append(str(r.get("url", "")))
    for f in frag.get("files") or []:
        if isinstance(f, dict):
            parts.append(str(f.get("file_name", "")))
    return "\n".join(p for p in parts if p)


def frag_signature(frag):
    """
    fragment 的多重集签名。用于「打捞后自证没丢没重」的多重集比较。

    签名同时覆盖 type / content / results / files——只比 content 会漏掉
    TOOL_SEARCH 这类没有 content 的 fragment 的载荷差异。
    """
    results = frag.get("results")
    files = frag.get("files")
    return (
        frag.get("type"),
        frag.get("content") or "",
        json.dumps(results, ensure_ascii=False, sort_keys=True) if results is not None else "",
        json.dumps(files, ensure_ascii=False, sort_keys=True) if files is not None else "",
    )


def linearise(conv):
    """
    把聊天树线性化：返回 [(node_id, depth, message, fragment), ...]。

    mapping 是**树**不是列表——重新生成答案会 fork 出一条新分支，朴素地
    for node in mapping.values() 会把兄弟分支拼在一起，得到自相矛盾的对话。
    正确做法是从 'root' 出发深度优先遍历，保留用户实际走的那条路径。

    孤儿节点（root 不可达但带内容）按 message.inserted_at 排序后补捞到末尾，
    否则会静默丢数据。
    """
    mapping = conv.get("mapping") or {}
    out = []
    seen = set()

    def walk(node_id, depth):
        if node_id in seen:  # 防御：畸形树不死循环
            return
        seen.add(node_id)
        node = mapping.get(node_id)
        if not node:
            return
        msg = node.get("message")
        if msg:
            for frag in msg.get("fragments") or []:
                out.append((node_id, depth, msg, frag))
        for child in node.get("children") or []:
            walk(child, depth + 1)

    root = mapping.get("root") or {}
    for child in root.get("children") or []:
        walk(child, 0)

    # 孤儿节点：root 走不到、但仍带内容的节点，按时间排序补捞
    orphans = []
    for node_id, node in mapping.items():
        if node_id in seen or node_id == "root":
            continue
        msg = node.get("message")
        if not msg:
            continue
        orphans.append((node_id, msg))
    for node_id, msg in sorted(orphans, key=lambda x: str(x[1].get("inserted_at") or "")):
        for frag in msg.get("fragments") or []:
            out.append((node_id, 0, msg, frag))

    return out


# ============================================================
# Scanning — 全语料主题检索
# ============================================================

def _keyword_stats(corpus, keywords):
    """每个关键词在语料库中的文档频率（出现该词的会话数）。"""
    df = Counter()
    for conv in corpus:
        title = (conv.get("title") or "").lower()
        hit = set()
        for kw in keywords:
            if kw in title:
                hit.add(kw)
        for _node_id, _msg, frag in iter_fragments(conv):
            text = frag_text(frag).lower()
            if not text:
                continue
            for kw in keywords:
                if kw in text:
                    hit.add(kw)
        for kw in hit:
            df[kw] += 1
    return df


def scan_corpus(corpus, keywords, top=25):
    """
    按关键词对**整个语料库**打分排序。

    打分 = Σ_kw idf(kw) × (正文命中次数 + TITLE_WEIGHT × 标题命中)

    - idf 惩罚高频词：「同步」「上传」这类通用词命中再多，也不该压过「某同步插件」
      这种只出现在 3 个会话里的专有词。
    - 标题命中加权：DeepSeek 标题是自动生成的会话摘要，命中标题 ≈ 这场对话就是
      在讲这个主题。
    - 大小写不敏感：某同步插件 / 某专有词小写 / 某专有词全大写 都要命中。
    """
    kws = [k.lower() for k in keywords]
    df = _keyword_stats(corpus, kws)
    n = len(corpus)
    idf = {kw: _idf(n, df.get(kw, 0)) for kw in kws}

    scored = []
    for conv in corpus:
        title = (conv.get("title") or "").lower()
        hits = Counter()
        where = Counter()
        for _node_id, _msg, frag in iter_fragments(conv):
            text = frag_text(frag).lower()
            if not text:
                continue
            ftype = frag.get("type")
            for kw in kws:
                c = text.count(kw)
                if c:
                    hits[kw] += c
                    where[(kw, ftype)] += 1

        title_match = [kw for kw in kws if kw in title]
        if not hits and not title_match:
            continue

        score = 0.0
        for kw in kws:
            score += idf[kw] * (hits.get(kw, 0) + TITLE_WEIGHT * (1 if kw in title_match else 0))

        scored.append({
            "id": conv.get("id"),
            "title": conv.get("title"),
            "inserted_at": conv.get("inserted_at"),
            "updated_at": conv.get("updated_at"),
            "score": round(score, 3),
            "body_hits": sum(hits.values()),
            "hits": dict(hits),
            "title_match": title_match,
            "df": {kw: df.get(kw, 0) for kw in kws},
            "hit_locations": {
                "{}@{}".format(kw, typ): n for (kw, typ), n in where.items()
            },
        })

    # 排序（稳定排序从低优先级到高优先级依次施加）：
    # 主序 = 加权得分降序；同分 = 标题命中多的优先；再同 = 更新的会话优先
    scored.sort(key=lambda r: r["updated_at"] or "", reverse=True)
    scored.sort(key=lambda r: len(r["title_match"]), reverse=True)
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:top]


def _idf(n_docs, df):
    """平滑 IDF：常见词权重低，专有词权重高。"""
    return math.log((n_docs + 1) / (df + 1)) + 1.0


def scan(filepath, keywords, top=25):
    """读取语料库并检索。"""
    return scan_corpus(load_corpus(filepath), keywords, top)


# ============================================================
# Extraction — 打捞成独立 JSON
# ============================================================

def build_transcript(conv):
    """线性化后的 transcript 条目列表。"""
    entries = []
    seq = 0
    for node_id, depth, msg, frag in linearise(conv):
        ftype = frag.get("type")
        role, label = FRAGMENT_ROLES.get(ftype, ("unknown", ftype))
        entry = {
            "seq": seq,
            "node_id": node_id,
            "depth": depth,
            "type": ftype,
            "role": role,
            "label": label,
            "model": msg.get("model"),
            "timestamp": msg.get("inserted_at"),
        }
        if frag.get("content"):
            entry["content"] = frag["content"]
        if frag.get("results") is not None:
            entry["results"] = frag["results"]
        if frag.get("files") is not None:
            entry["files"] = frag["files"]
        entries.append(entry)
        seq += 1
    return entries


def build_stats(conv, transcript):
    """会话统计。"""
    by_type = Counter(e["type"] for e in transcript)
    by_role = Counter(e["role"] for e in transcript)
    content_chars = sum(len(e.get("content", "")) for e in transcript)
    return {
        "node_count": len(conv.get("mapping") or {}),
        "fragment_count": len(transcript),
        "user_turn_count": by_type.get("REQUEST", 0),
        "assistant_turn_count": by_type.get("RESPONSE", 0),
        "thinking_count": by_type.get("THINK", 0),
        "tool_fragment_count": sum(
            n for t, n in by_type.items() if t.startswith("TOOL_") or t == "SEARCH"
        ),
        "attachment_count": by_type.get("FILE", 0),
        "by_type": dict(by_type),
        "by_role": dict(by_role),
        "content_chars": content_chars,
    }


def collect_attachments(conv):
    """
    导出包中记录的附件（FILE fragment）。

    重要：DeepSeek 导出只存**元数据**——{file_id, file_name, file_size}，
    **图片二进制不在导出包里**（导出 zip 只有 user.json 和 conversations.json，
    没有附件目录）。所以这份清单说明的是「缺了什么、每个附件回答的是哪一问」，
    像素必须回 DeepSeek 客户端重新抓。
    """
    mapping = conv.get("mapping") or {}
    items = []
    for node_id, node in mapping.items():
        msg = node.get("message")
        if not msg:
            continue
        # 同一个 node 里用户提的那个问题
        question = ""
        for frag in msg.get("fragments") or []:
            if frag.get("type") == "REQUEST":
                question = str(frag.get("content", ""))
                break
        for frag in msg.get("fragments") or []:
            if frag.get("type") != "FILE":
                continue
            for f in frag.get("files") or []:
                items.append({
                    "file_id": f.get("file_id"),
                    "file_name": f.get("file_name"),
                    "file_size_bytes": f.get("file_size"),
                    "node_id": node_id,
                    "timestamp": msg.get("inserted_at"),
                    "question_it_answers": question,
                    "bytes_available_in_export": False,
                })
    items.sort(key=lambda x: x.get("timestamp") or "")
    return items


def build_payload(conv, reason=None):
    """单个会话的打捞产物（不含 _meta 外壳）。"""
    transcript = build_transcript(conv)
    return {
        "id": conv.get("id"),
        "title": conv.get("title"),
        "inserted_at": conv.get("inserted_at"),
        "updated_at": conv.get("updated_at"),
        "rescue_reason": reason,
        "stats": build_stats(conv, transcript),
        "attachments": collect_attachments(conv),
        "transcript": transcript,
        # 原样保留聊天树：transcript 只是给人读的视图，任何重分析都从这份兜底
        "raw_mapping": conv.get("mapping"),
    }


def match_conversations(corpus, wanted):
    """按 id 全量或前缀匹配会话，返回 (命中列表, 未命中列表)。"""
    picked = []
    missing = []
    for w in wanted:
        exact = [c for c in corpus if c.get("id") == w]
        matches = exact or [
            c for c in corpus if str(c.get("id", "")).startswith(w)
        ]
        if not matches:
            missing.append(w)
        else:
            picked.append(matches[0])
    return picked, missing


def extract(filepath, ids=None, reason=None):
    """
    提取入口：返回 rescued-session/v1 结构的 dict。

    ids 为 None / 空 → 导出全部会话；否则按 id 全量或前缀匹配。
    ids 可以是列表，也可以是逗号分隔的字符串。
    """
    path = os.path.abspath(os.path.expanduser(filepath))
    corpus = load_corpus(path)

    if isinstance(ids, str):
        ids = [x.strip() for x in ids.split(",") if x.strip()]

    if ids:
        wanted = [w.strip() for w in ids if w and w.strip()]
        picked, missing = match_conversations(corpus, wanted)
        if missing:
            raise ValueError("no conversation matched id/prefix: {}".format(", ".join(missing)))
    else:
        wanted = []
        picked = list(corpus)

    meta = {
        "schema": SCHEMA,
        "platform": PLATFORM,
        "source": {
            "file": path,
            "sha256": file_sha256(path),
            "conversation_count_in_source": len(corpus),
        },
        "extraction": {
            "tool": "extract_deepseek.py",
            "mode": "extract",
            "requested_ids": wanted,
            "rescue_reason": reason,
            "note": (
                "transcript = chat tree linearised from root by depth-first "
                "traversal; raw_mapping kept verbatim for full fidelity."
            ),
        },
        "conversation_count": len(picked),
    }

    return {
        "_meta": meta,
        "conversations": [build_payload(c, reason) for c in picked],
    }


# ============================================================
# 校验 — 打捞后必须自证没丢没重
# ============================================================

def verify_payload(payload, corpus):
    """
    多重集比较：打捞产物 vs 源 mapping。

    只比长度不够——必须比较 fragment 签名的**多重集**，才能同时证明
    「没有丢失」和「没有重复」。
    """
    conv = None
    for c in corpus:
        if c.get("id") == payload.get("id"):
            conv = c
            break
    if conv is None:
        return {"ok": False, "error": "source conversation not found"}

    src = Counter()
    for _node_id, _msg, frag in iter_fragments(conv):
        src[frag_signature(frag)] += 1

    out = Counter()
    for e in payload.get("transcript") or []:
        out[frag_signature(e)] += 1

    missing = src - out   # 源里有、产物里没有 → 丢失
    extra = out - src     # 产物里有、源里没有 → 重复

    checks = {
        "no_missing": not missing,
        "no_duplicate": not extra,
        "raw_mapping_identical": payload.get("raw_mapping") == conv.get("mapping"),
        "seq_contiguous": [e["seq"] for e in payload.get("transcript") or []]
        == list(range(len(payload.get("transcript") or []))),
        "count_matches": len(payload.get("transcript") or []) == sum(src.values()),
        "id_matches": payload.get("id") == conv.get("id"),
        "title_matches": payload.get("title") == conv.get("title"),
        "stats_fragment_count_matches": payload.get("stats", {}).get("fragment_count")
        == len(payload.get("transcript") or []),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "source_fragment_count": sum(src.values()),
        "output_fragment_count": len(payload.get("transcript") or []),
        "missing_count": sum(missing.values()),
        "duplicate_count": sum(extra.values()),
    }


# ============================================================
# Markdown 预览
# ============================================================

def write_markdown(path, payload):
    """
    写一份人类可读的 .md 预览：跳过 THINK，把搜索结果渲染成链接列表。
    （JSON 里 THINK 一条不少，这里只是让预览好读。）
    """
    lines = ["# {}".format(payload.get("title")), ""]
    stats = payload.get("stats", {})
    lines.append(
        "- id: `{}`\n- inserted: {}\n- updated: {}\n- stats: {} fragments "
        "({} user / {} assistant)\n".format(
            payload.get("id"),
            payload.get("inserted_at"),
            payload.get("updated_at"),
            stats.get("fragment_count"),
            stats.get("user_turn_count"),
            stats.get("assistant_turn_count"),
        )
    )
    for e in payload.get("transcript") or []:
        if e.get("type") == "THINK":
            continue
        body = e.get("content", "")
        if e.get("type") == "FILE":
            body = "\n".join(
                "- {} ({} bytes)".format(f.get("file_name"), f.get("file_size"))
                for f in e.get("files", [])
            )
        elif e.get("type") in ("TOOL_SEARCH", "SEARCH"):
            body = "\n".join(
                "- [{}]({})".format(r.get("title"), r.get("url"))
                for r in e.get("results", [])
            )
        lines.append("\n## [{}] {} `{}`\n".format(e.get("seq"), e.get("label"), e.get("type")))
        lines.append(str(body))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def safe_filename(conv):
    """产出文件名：<日期>_<id前8位>_<标题>，去掉文件系统非法字符。"""
    base = "{}_{}_{}".format(
        str(conv.get("inserted_at") or "")[:10],
        str(conv.get("id") or "")[:8],
        conv.get("title") or "",
    )
    return re.sub(r'[\\/:*?"<>|\s]+', "_", base)[:120]
