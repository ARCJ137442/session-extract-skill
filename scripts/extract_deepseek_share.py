"""
DeepSeek 网页版**分享链接**提取器。

与 `extract_deepseek.py`（账号导出包路径）是**互补的两条路径**，不是重复：

    账号导出包路径                        分享链接路径（本模块）
    ─────────────────────────────         ──────────────────────────────
    conversations.json（需账号导出）      /share/<id> 公开链接，无需登录
    聊天树 mapping → DFS 线性化           线性消息数组，无需线性化
    附件只有元数据（无字节）              附件**字节可直下** ← 独有
    TOOL_OPEN / TOOL_FIND 载荷未导出      TOOL_OPEN 载荷完整 ← 独有
    标题是真实会话名                      title 是占位符「来自分享的对话」

关键端点（均无需登录，仅需常规浏览器 UA / Referer）:

    会话正文  GET https://chat.deepseek.com/api/v0/share/content?share_id=<ID>
    附件原图  GET https://files.deepseeksvc.com/api<signed_path>&ty=p
    附件缩略  GET https://files.deepseeksvc.com/api<signed_path>&ty=t
              （ty=r 返回 400，不可用）

    * 老接口 /api/v0/share?share_id= 已失效，会返回 SPA 首页 HTML —— 不要用。
    * 附件域名 `files.deepseeksvc.com` 与路径前缀 `/api` **不在页面 DOM 里**，
      来自前端 bundle 的 URL 构造逻辑；分享页也**完全不渲染附件**，
      DOM 中查不到任何附件节点，只能从 JSON 的 signed_path 拿。
      通用套路见 docs/deepseek-share.md。

Schema（与 extract_deepseek.py 同为 rescued-session/v1，但容器不同）:
    顶层:      {code, msg, data:{biz_data:{title, model_type, messages[]}}}
    message:   {message_id, role, status, inserted_at, fragments[], model, ...}
    fragment:  REQUEST     {type, content}            用户提问
                RESPONSE    {type, content, references[]} 模型回答
                THINK       {type, content}            模型思考
                TOOL_SEARCH {type, queries[], results[]} 联网搜索
                TOOL_OPEN   {type, result{}}          打开网页（导出包会丢这个载荷）
                FILE        {type, files[]}            附件（含 signed_path）
                TIP         {type, content}            附加提示

与导出包路径的**已知差异**（跨源并集时必须知道，否则会误判为"打捞丢失"）:
    1. 分享链接只有**最终态**——用户在网页端编辑/删除掉的中间轮次不在其中；
       账号导出包保留的是另一时刻的快照。**并集才是完整历史。**
    2. 分享 API 会剥离正文里的 `[!reference:N]` 行内引用标记（引用关系移到
       `references` 数组）；导出包保留行内标记。跨源比对正文前必须先归一。
    3. 分享 API 有 `TIP` fragment，导出包没有。
    4. TOOL_OPEN 的载荷只在分享链接侧存在。
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter

# 同目录共享模块（标题占位符识别、URL 锚点归一）。
# 用 **append** 而不是 insert(0)：脚本目录排在 sys.path 末尾，
# 不会遮蔽标准库同名模块（目录里若有个 json.py 也不会劫持 import json）。
# 放末尾也能兼容 `python -I`（隔离模式不把脚本目录加进 sys.path）。
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
try:
    from deepseek_common import (
        console_encoding,
        setup_streams,
        is_placeholder_title,
        normalize_title,
        norm_url,
        url_anchor,
        strip_derived_result_fields,
        result_url_key,
    )
except ImportError:  # pragma: no cover - 极少见：模块文件缺失
    raise RuntimeError(
        "deepseek_common.py is missing next to this script; "
        "the two DeepSeek paths must share one normalisation rule set."
    )

# stdout 编码跟随**控制台码页**（见 deepseek_common.console_encoding 的说明）：
# 钉死 UTF-8 反而会让 PowerShell 按 cp936 解码 → 中文静默乱码。
# 需要字节级保真的产物请用 --out / --outdir（文件侧显式 utf-8）。
setup_streams()

PLATFORM = "deepseek-web-share"
SCHEMA = "rescued-session/v1"

SHARE_PAGE = "https://chat.deepseek.com/share/"
SHARE_API = "https://chat.deepseek.com/api/v0/share/content?share_id="
# 失效端点（写在这里是为了让后来的人别再踩）：
#   https://chat.deepseek.com/api/v0/share?share_id=<ID>  → 200，但返回 SPA 首页 HTML
DEAD_API = "https://chat.deepseek.com/api/v0/share?share_id="
FILE_HOST = "https://files.deepseeksvc.com/api"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 账号导出路径里，标题是真实会话名；分享链接侧是占位符。
# ⚠️ 占位符文案**会变**：实测见过「来自分享的对话」，也见过英文的
# 「Shared Conversation」。写死 `== "来自分享的对话"` 会让 title_is_placeholder
# 静默变成 False —— 不报错，只是给出一个假的「一切正常」信号，
# 而下游正是靠这个字段决定「要不要人工确认标题」。
# 归一规则统一放在 deepseek_common（提取侧与合并侧同一把尺子）。
PLACEHOLDER_TITLE = "来自分享的对话"
# ⚠️ 这里**不要**再抄一份占位符名单：判定统一走
# deepseek_common.is_placeholder_title（名单更全，还处理空白归一）。
# 本文件曾因此留下一份不完整的副本（少了带尾空格的变体），差点误导后来的人。
# 所以：名单只存在于 deepseek_common 一处。

# Fragment 类型 -> (role, 人类可读标签)。与 extract_deepseek.py 保持一致，
# 另加 TIP（分享链接侧才有）。
FRAGMENT_ROLES = {
    "REQUEST": ("user", "用户提问"),
    "RESPONSE": ("assistant", "模型回答"),
    "THINK": ("assistant", "模型思考"),
    "TOOL_SEARCH": ("tool", "联网搜索"),
    "SEARCH": ("tool", "搜索"),
    "TOOL_OPEN": ("tool", "打开网页"),
    "TOOL_FIND": ("tool", "查找"),
    "FILE": ("user", "附件"),
    "TIP": ("assistant", "附加提示"),
}

TZ = datetime.timezone(datetime.timedelta(hours=8))


# ============================================================
# Source access
# ============================================================

def normalise_share_id(value):
    """接受 share_id 裸串或完整分享链接，返回 share_id。"""
    v = (value or "").strip()
    if not v:
        raise ValueError("empty share id")
    m = re.search(r"/share/([A-Za-z0-9_-]+)", v)
    if m:
        return m.group(1)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", v):
        raise ValueError("not a share id or share url: {!r}".format(value))
    return v


def http_get(url, referer="https://chat.deepseek.com/", timeout=60):
    """GET 一个 URL，返回 (status, content_type, body_bytes)。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


def fetch_share(share_id):
    """
    取分享链接的会话正文。

    返回 (raw_text, doc)。raw_text 保留**原始字节**（算 sha256 用），
    doc 是解析后的 JSON。
    """
    url = SHARE_API + share_id
    status, ctype, body = http_get(url)
    text = body.decode("utf-8")

    # 老接口的坑：如果有人把 SHARE_API 改错，返回的是 SPA 首页 HTML，
    # 解析会炸或静默给出空内容——这里显式拦一下。
    if not text.lstrip().startswith("{"):
        raise RuntimeError(
            "share API did not return JSON (status={}, content-type={}). "
            "Endpoint may have changed; check {} first.".format(status, ctype, DEAD_API)
        )
    doc = json.loads(text)
    if doc.get("code") not in (0, None):
        raise RuntimeError("share API error: code={} msg={}".format(doc.get("code"), doc.get("msg")))
    # ⚠️ 不存在 / 已失效的分享链接，API 返回的是
    #    {"code":0,"data":{"biz_code":1,"biz_msg":"share does not exist","biz_data":null}}
    # —— HTTP 200、code 也是 0，不拦的话后面会炸成
    # "AttributeError: 'NoneType' object has no attribute 'get'"，用户完全看不懂。
    data = doc.get("data") or {}
    biz = data.get("biz_data") or {}
    if not biz.get("messages"):
        biz_msg = data.get("biz_msg") or ""
        raise RuntimeError(
            "分享链接不存在或已失效（share_id={}{}）。"
            "请确认链接是否完整、是否已被撤销。".format(
                share_id, "，API: {}".format(biz_msg) if biz_msg else ""))
    return text, doc


def file_sha256(path):
    """本地文件校验和。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def text_sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def iso(epoch):
    """epoch 秒 → 本地 ISO（+08:00），与账号导出包的字符串格式对齐。"""
    if not epoch:
        return None
    return datetime.datetime.fromtimestamp(float(epoch), TZ).isoformat()


# ============================================================
# Walking
# ============================================================

def iter_fragments(biz):
    """遍历分享链接会话的全部 fragment：(node_id, message, fragment)。"""
    for msg in biz.get("messages") or []:
        node_id = str(msg.get("message_id"))
        for frag in msg.get("fragments") or []:
            yield node_id, msg, frag


def frag_results(frag):
    """
    归一化「结果载荷」。

    分享链接侧 TOOL_SEARCH 有 results[]，TOOL_OPEN 有单个 result{}；
    账号导出侧 TOOL_OPEN 载荷根本没导出。统一成列表，跨源比对才有的比。
    """
    if frag.get("results") is not None:
        return frag["results"]
    if frag.get("result") is not None:
        return [frag["result"]]
    return None


def enrich_results(results):
    """
    给结果条目补**派生**字段：`url_normalized`（剥掉引用锚点）+ `url_anchor`。

    为什么：DeepSeek 的搜索结果 URL 带引用锚点（`.../paper.pdf#7#22#7`），
    **同一篇文档在不同引用位置锚点不同**。按原始 URL 建字典 = 同一篇文章
    被登记成多条记录、互相不补字段（实测 296 条结果里 262 个唯一原始 URL，
    只有 253 个唯一文档；按原始 URL 去重会吞掉 8,160 字符的 snippet）。

    ⚠️ 必须是**副本**：raw_messages 要保持逐字原样（自证断言
    `raw_messages_identical` 依赖它），就地改会污染兜底数据。
    原始 `url` 字段也保留不动 —— 那是源数据，锚点是引用位置的真实信息。
    """
    out = []
    for r in results or []:
        if not isinstance(r, dict):
            out.append(r)
            continue
        url = r.get("url")
        item = dict(r)
        item["url_normalized"] = norm_url(url)
        item["url_anchor"] = url_anchor(url)
        out.append(item)
    return out


def frag_signature(frag):
    """
    fragment 的多重集签名——用于「打捞后自证没丢没重」。

    与 extract_deepseek.frag_signature 保持同一形状（type / content /
    results / files），这样两条路径的产物可以直接做多重集 diff。

    ⚠️ 计算签名时必须剥掉**派生**字段（url_normalized / url_anchor）：
    transcript 里的 results 带派生字段、源 fragment 里不带，不剥就会
    把「同一份数据」判成「源里有、产物里没有」——自证会自己把自己判失败。
    """
    results = strip_derived_result_fields(frag_results(frag))
    files = frag.get("files")
    return (
        frag.get("type"),
        frag.get("content") or "",
        json.dumps(results, ensure_ascii=False, sort_keys=True) if results is not None else "",
        json.dumps(files, ensure_ascii=False, sort_keys=True) if files is not None else "",
    )


def frag_text(frag):
    """fragment 的可检索文本：正文 + 搜索结果标题/URL + 附件名。"""
    parts = []
    if frag.get("content"):
        parts.append(str(frag["content"]))
    for r in frag_results(frag) or []:
        if isinstance(r, dict):
            parts.append(str(r.get("title", "")))
            parts.append(str(r.get("url", "")))
    for f in frag.get("files") or []:
        if isinstance(f, dict):
            parts.append(str(f.get("file_name", "")))
    return "\n".join(p for p in parts if p)


# ============================================================
# Attachments — 分享链接路径的独有价值
# ============================================================

def _safe(name):
    return (re.sub(r'[\\/:*?"<>|]+', "_", name or "").strip() or "unnamed")


def download_attachments(biz, outdir, want_thumbnail=True, download=True):
    """
    下载附件**二进制**并返回清单。

    导出包路径拿不到这一步——那才是两条路径的真正分水岭：
    排查用的截图字节只有这里能拿到。

    端点形如：
        https://files.deepseeksvc.com/api<signed_path>&ty=p   原图
        https://files.deepseeksvc.com/api<signed_path>&ty=t   缩略图

    download=False 时**只收集元数据不下载**（只读模式用：清单仍要如实
    报出附件数量，不能因为没下载就写成 0 个）。
    """
    items = []
    att_dir = os.path.join(outdir, "附件") if outdir else None
    if att_dir:
        os.makedirs(att_dir, exist_ok=True)

    for node_id, msg, frag in iter_fragments(biz):
        if frag.get("type") != "FILE":
            continue
        question = ""
        for f2 in msg.get("fragments") or []:
            if f2.get("type") == "REQUEST":
                question = str(f2.get("content", ""))
                break
        for f in frag.get("files") or []:
            signed = f.get("signed_path")
            base = {
                "file_id": f.get("id") or f.get("file_id"),
                "file_name": f.get("file_name"),
                "file_size_bytes": f.get("file_size"),
                "node_id": node_id,
                "timestamp": iso(msg.get("inserted_at")),
                "question_it_answers": question,
                "width": f.get("width"),
                "height": f.get("height"),
                "audit_result": f.get("audit_result"),
                # 与导出包路径对齐的两个字段：
                #   bytes_available_in_export: 账号导出包里永远拿不到字节
                #   bytes_available:             本路径已下载到字节
                "bytes_available_in_export": False,
            }
            if not signed or not download:
                base.update(bytes_available=False, local_path=None,
                            error=("no signed_path in share payload" if not signed
                                   else "未下载（只读模式）"))
                items.append(base)
                continue

            stem, ext = os.path.splitext(_safe(f.get("file_name") or base["file_id"]))
            ext = ext or ".webp"
            variants = [("p", "", "原图")]
            if want_thumbnail:
                variants.append(("t", "_缩略图", "缩略图"))

            got = []
            for ty, suffix, variant in variants:
                url = FILE_HOST + signed + ("&" if "?" in signed else "?") + "ty=" + ty
                fname = stem + suffix + ext
                rel = ("附件/" + fname) if att_dir else None
                dest = os.path.join(att_dir, fname) if att_dir else None
                try:
                    status, ctype, data = http_get(url)
                    if status == 200 and data[:4] not in (b"<htm", b"<!DO"):
                        if dest:
                            with open(dest, "wb") as fh:
                                fh.write(data)
                        got.append({
                            "variant": variant,
                            "local_path": rel,
                            "bytes": len(data),
                            "content_type": ctype,
                            "sha256": hashlib.sha256(data).hexdigest()[:16],
                            "download_url": url,
                        })
                    else:
                        got.append({"variant": variant, "download_url": url,
                                    "error": "HTTP {} / {}".format(status, ctype)})
                except Exception as e:  # noqa: BLE001 — 网络异常要如实记录
                    got.append({"variant": variant, "download_url": url, "error": repr(e)})

            primary = next((g for g in got if g.get("variant") == "原图"), None)
            base.update({
                "bytes_available": bool(primary and primary.get("local_path")),
                "local_path": (primary or {}).get("local_path"),
                "sha256": (primary or {}).get("sha256"),
                "downloaded_bytes": (primary or {}).get("bytes"),
                "variants": got,
            })
            items.append(base)
    items.sort(key=lambda x: x.get("timestamp") or "")
    return items


# ============================================================
# Extraction
# ============================================================

def build_transcript(biz):
    """线性消息数组 → transcript 条目列表（与导出包路径同形状）。"""
    entries = []
    seq = 0
    for node_id, msg, frag in iter_fragments(biz):
        ftype = frag.get("type")
        role, label = FRAGMENT_ROLES.get(ftype, ("unknown", ftype))
        entry = {
            "seq": seq,
            "node_id": node_id,
            "depth": 0,
            "type": ftype,
            "role": role,
            "label": label,
            "model": msg.get("model"),
            "timestamp": iso(msg.get("inserted_at")),
            # 节点出处：跨源并集时用来标注「这条来自哪条路径」
            "sources": ["share"],
        }
        if frag.get("content"):
            entry["content"] = frag["content"]
        results = frag_results(frag)
        if results is not None:
            # 补派生字段（副本，不动源数据）
            entry["results"] = enrich_results(results)
        if frag.get("files") is not None:
            entry["files"] = frag["files"]
        if frag.get("queries") is not None:
            entry["queries"] = frag["queries"]
        if frag.get("references") is not None:
            entry["references"] = frag["references"]
        entries.append(entry)
        seq += 1
    return entries


def build_stats(biz, transcript, attachments=None):
    """会话统计（字段名与导出包路径一致，便于横向 diff）。"""
    by_type = Counter(e["type"] for e in transcript)
    by_role = Counter(e["role"] for e in transcript)
    content_chars = sum(len(e.get("content", "")) for e in transcript)
    msgs = biz.get("messages") or []
    stamps = [m.get("inserted_at") for m in msgs if m.get("inserted_at")]
    return {
        "node_count": len(msgs),
        "fragment_count": len(transcript),
        "user_turn_count": by_type.get("REQUEST", 0),
        "assistant_turn_count": by_type.get("RESPONSE", 0),
        "thinking_count": by_type.get("THINK", 0),
        "tool_fragment_count": sum(
            n for t, n in by_type.items() if t.startswith("TOOL_") or t == "SEARCH"
        ),
        "attachment_count": by_type.get("FILE", 0),
        "attachment_files": len(attachments or []),
        "attachment_bytes_available": sum(
            1 for a in (attachments or []) if a.get("bytes_available")
        ),
        "by_type": dict(by_type),
        "by_role": dict(by_role),
        "content_chars": content_chars,
        "inserted_at": iso(min(stamps)) if stamps else None,
        "updated_at": iso(max(stamps)) if stamps else None,
    }


def build_payload(biz, raw_messages, attachments, reason=None, title=None, share_id=None):
    """单个会话的打捞产物（不含 _meta 外壳）。"""
    transcript = build_transcript(biz)
    api_title = biz.get("title") or PLACEHOLDER_TITLE
    resolved_title = title or api_title
    stats = build_stats(biz, transcript, attachments)
    return {
        "id": share_id,
        "title": resolved_title,
        # 名单化识别，不再依赖某一个文案（DeepSeek 改文案/语言就会失配）
        "title_is_placeholder": bool(title is None and is_placeholder_title(api_title)),
        "title_source": "override" if title else "share-api",
        "inserted_at": stats.get("inserted_at"),
        "updated_at": stats.get("updated_at"),
        "rescue_reason": reason,
        "stats": stats,
        "attachments": attachments or [],
        "transcript": transcript,
        # 原样保留源消息数组：transcript 只是给人读的视图，任何重分析都从这份兜底。
        # 注意键名与导出包路径不同——分享链接没有聊天树，就是一个线性数组。
        "raw_messages": raw_messages,
    }


def build_meta(raw_text, share_id, raw_messages, reason=None, mode="extract"):
    """
    打捞产物的 _meta 外壳——**落盘模式与只读模式共用同一份**。

    以前只读模式（--format json）用的是一份「精简版」_meta，缺 sources /
    extraction / 计数，跟落盘产物对不上：同一个 skill 出两种元数据形状，
    下游要写两套解析。现在统一走这里。
    """
    return {
        "schema": SCHEMA,
        "platform": PLATFORM,
        "source": {
            "kind": "share-link",
            "share_id": share_id,
            "share_url": SHARE_PAGE + share_id,
            "endpoint": "GET " + SHARE_API + share_id,
            "sha256": text_sha256(raw_text),
            "message_count_in_source": len(raw_messages),
            "fragment_count_in_source": sum(len(m.get("fragments") or []) for m in raw_messages),
            "attachment_endpoint": "GET " + FILE_HOST + "<signed_path>&ty=p",
            "captured_at": datetime.datetime.now(TZ).isoformat(),
        },
        # 跨源并集用：本路径覆盖了哪些节点
        "sources": [{
            "kind": "share-link",
            "share_id": share_id,
            "sha256": text_sha256(raw_text),
            "message_count": len(raw_messages),
            "fragment_count": sum(len(m.get("fragments") or []) for m in raw_messages),
            "node_ids": [str(m.get("message_id")) for m in raw_messages],
        }],
        "extraction": {
            "tool": "extract_deepseek_share.py",
            "mode": mode,
            "requested_ids": [share_id],
            "rescue_reason": reason,
            "note": (
                "transcript = linear message array from the share API (no chat tree "
                "to linearise); raw_messages kept verbatim for full fidelity; "
                "attachment bytes fetched from files.deepseeksvc.com."
            ),
        },
        "conversation_count": 1,
    }


def extract(share, title=None, reason=None, outdir=None, want_thumbnail=True):
    """
    提取入口：返回 rescued-session/v1 结构的 dict。

    share      —— share_id 裸串或完整分享链接
    title      —— 真实会话名（分享 API 只给占位符「来自分享的对话」，
                   真实标题得从网页端侧边栏确认后再传进来）
    reason     —— 打捞理由，写进 _meta.extraction.rescue_reason
    outdir     —— 给了就把附件字节下到 <outdir>/附件/ 并填 local_path
    """
    share_id = normalise_share_id(share)
    raw_text, doc = fetch_share(share_id)
    biz = doc["data"]["biz_data"]
    raw_messages = biz.get("messages") or []

    attachments = []
    if outdir:
        attachments = download_attachments(biz, outdir, want_thumbnail=want_thumbnail)

    payload = build_payload(
        biz, raw_messages, attachments,
        reason=reason, title=title, share_id=share_id,
    )

    meta = build_meta(raw_text, share_id, raw_messages, reason=reason, mode="extract")
    # 自证随产物一起走：之前只有只读模式的 _meta 带 verification，
    # 落盘产物没有 —— 那等于「每份产物自带无损自证」这句话只在一种形态上成立。
    try:
        meta["verification"] = verify_payload(payload, doc)
    except Exception as e:  # noqa: BLE001 — 自证失败不能盖住主产物
        meta["verification"] = {"ok": False, "error": repr(e)}
    return {"_meta": meta, "conversations": [payload]}


# ============================================================
# 校验 — 打捞后必须自证没丢没重
# ============================================================

def verify_payload(payload, doc):
    """
    多重集比较：打捞产物 vs 分享 API 原始响应。

    只比长度不够——必须比较 fragment 签名的**多重集**，才能同时证明
    「没有丢失」和「没有重复」。
    """
    biz = doc["data"]["biz_data"]
    src = Counter()
    for _node_id, _msg, frag in iter_fragments(biz):
        src[frag_signature(frag)] += 1

    out = Counter()
    for e in payload.get("transcript") or []:
        out[frag_signature(e)] += 1

    missing = src - out   # 源里有、产物里没有 → 丢失
    extra = out - src     # 产物里有、源里没有 → 重复

    atts = payload.get("attachments") or []
    checks = {
        "no_missing": not missing,
        "no_duplicate": not extra,
        "raw_messages_identical": payload.get("raw_messages") == biz.get("messages"),
        "seq_contiguous": [e["seq"] for e in payload.get("transcript") or []]
        == list(range(len(payload.get("transcript") or []))),
        "count_matches": len(payload.get("transcript") or []) == sum(src.values()),
        "stats_fragment_count_matches": payload.get("stats", {}).get("fragment_count")
        == len(payload.get("transcript") or []),
        "attachments_have_local_path": all(
            (not a.get("bytes_available")) or a.get("local_path")
            for a in atts
        ),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "source_fragment_count": sum(src.values()),
        "output_fragment_count": len(payload.get("transcript") or []),
        "missing_count": sum(missing.values()),
        "duplicate_count": sum(extra.values()),
        "attachments_total": len(atts),
        "attachments_with_bytes": sum(1 for a in atts if a.get("bytes_available")),
    }


# ============================================================
# Markdown 预览
# ============================================================

def render_markdown(payload, attachment_prefix="附件"):
    """
    人类可读的 Markdown 文本（不落盘）。

    跳过 THINK，搜索结果渲染成链接列表，附件渲染成本地图片引用
    （能显示才算真的捞到了）。
    """
    lines = ["# {}".format(payload.get("title")), ""]
    stats = payload.get("stats", {})
    lines.append(
        "- id: `{}`\n- inserted: {}\n- updated: {}\n- stats: {} fragments "
        "({} user / {} assistant)\n- 附件: {} 个，其中 {} 个已取到字节\n".format(
            payload.get("id"),
            payload.get("inserted_at"),
            payload.get("updated_at"),
            stats.get("fragment_count"),
            stats.get("user_turn_count"),
            stats.get("assistant_turn_count"),
            len(payload.get("attachments") or []),
            sum(1 for a in (payload.get("attachments") or []) if a.get("bytes_available")),
        )
    )
    if payload.get("title_is_placeholder"):
        lines.append("> ⚠️ 标题是分享链接的占位符，真实会话名需从网页端侧边栏确认\n")

    att_by_node = {}
    for a in payload.get("attachments") or []:
        att_by_node.setdefault(a.get("node_id"), []).append(a)

    for e in payload.get("transcript") or []:
        if e.get("type") == "THINK":
            continue
        body = e.get("content", "")
        if e.get("type") == "FILE":
            lines.append("\n## [{}] {} `{}`\n".format(e.get("seq"), e.get("label"), e.get("type")))
            for a in att_by_node.get(e.get("node_id"), []):
                lines.append("- {} ({} bytes)".format(a.get("file_name"), a.get("file_size_bytes")))
                if a.get("local_path"):
                    rel = a["local_path"].replace("\\", "/")
                    if attachment_prefix and not rel.startswith(attachment_prefix):
                        rel = "{}/{}".format(attachment_prefix, os.path.basename(rel))
                    lines.append("\n![{}]({})\n".format(a.get("file_name"), rel))
                else:
                    lines.append("  （未取到字节：{}）\n".format(a.get("error") or "未知原因"))
            continue
        if e.get("type") in ("TOOL_SEARCH", "SEARCH", "TOOL_OPEN"):
            body = "\n".join(
                "- [{}]({})".format(r.get("title"), r.get("url"))
                for r in (e.get("results") or []) if isinstance(r, dict)
            )
        lines.append("\n## [{}] {} `{}`\n".format(e.get("seq"), e.get("label"), e.get("type")))
        lines.append(str(body))
    return "\n".join(lines)


def write_markdown(path, payload, attachment_prefix="附件"):
    """把 render_markdown 的结果写到文件。"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_markdown(payload, attachment_prefix))


def safe_filename(payload):
    """产出文件名：<日期>_<share_id前8位>_<标题>，去掉文件系统非法字符。"""
    base = "{}_{}_{}".format(
        str(payload.get("inserted_at") or "")[:10],
        str(payload.get("id") or "")[:8],
        payload.get("title") or "",
    )
    return re.sub(r'[\\/:*?"<>|\s]+', "_", base)[:120]


# ============================================================
# CLI — 独立可用；统一调度走 session_extract.py --platform deepseek-share
# ============================================================

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="从 DeepSeek 网页版分享链接提取完整会话（含附件二进制）")
    ap.add_argument("share", help="share_id 或完整分享链接")
    ap.add_argument("--outdir", default=None,
                    help="输出目录（附件落在 <outdir>/附件/）；不与 --format 同用")
    ap.add_argument("--format", choices=("text", "json", "openai", "raw"), default=None,
                    help="只读到 stdout，不落盘：text=可读全文，json=rescued-session/v1 产物，"
                         "openai=ChatGPT/OpenAI 可直接用的 messages 结构，raw=API 原始 JSON")
    ap.add_argument("--print", dest="do_print", action="store_true",
                    help="等价于 --format text（可读全文打到 stdout，不落盘）")
    ap.add_argument("--title", default=None, help="真实会话名（分享 API 只给占位符）")
    ap.add_argument("--reason", default=None, help="打捞理由")
    ap.add_argument("--markdown", action="store_true", help="同时写 .md 预览（需 --outdir）")
    ap.add_argument("--no-thumbnail", action="store_true", help="不下载缩略图")
    ap.add_argument("--slim", action="store_true",
                    help="--format openai 时去掉 fragments/raw 兜底，产出小文件（上传更快）")
    ap.add_argument("--verify", action="store_true", help="多重集自证")
    a = ap.parse_args(argv)

    fmt = a.format or ("text" if a.do_print else None)
    if not a.outdir and not fmt:
        ap.error("需要 --outdir（落盘）或 --format/--print（只读到 stdout）")
    if a.outdir and fmt:
        ap.error("--outdir 与 --format/--print 不能同时用")

    # ---- 只读模式：读到 stdout，不落盘，也不下载附件 ----
    if fmt:
        # 网络异常要**如实报错**而不是抛栈：以前这条分支完全没有 try/except，
        # urllib 的 HTTPError/URLError 直接把 traceback 甩到调用方脸上。
        try:
            share_id = normalise_share_id(a.share)
            raw_text, doc = fetch_share(share_id)
        except (OSError, RuntimeError, ValueError) as e:
            sys.stderr.write("ERROR: {}\n".format(e))
            return 2
        if fmt == "raw":
            sys.stdout.write(raw_text)
            return 0
        biz = doc["data"]["biz_data"]
        raw_messages = biz.get("messages") or []
        payload = build_payload(
            biz, raw_messages,
            download_attachments(biz, None, download=False),
            reason=a.reason, title=a.title, share_id=share_id)
        meta = build_meta(raw_text, share_id, raw_messages, reason=a.reason, mode="read-only")

        # 只读模式也必须**自证**：把 verify 结果写进 _meta，
        # 让「产物没丢没重」这件事随产物一起流转，而不是靠人记得跑 --verify。
        # ⚠️ 顺序很重要：必须**在 openai 分支之前**算好——否则旗舰路径
        # （--format openai）的 meta.source_verification 会是 null。
        try:
            meta["verification"] = verify_payload(payload, doc)
        except Exception as e:  # noqa: BLE001 — 自证失败不能盖住主产物
            meta["verification"] = {"ok": False, "error": repr(e)}

        if fmt == "openai":
            # 「零长脚本」的关键一步：直接把会话变成 ChatGPT 网页版 / OpenAI
            # 兼容端点能吃的结构，不用再自己写转换器。
            from to_openai import convert
            out = convert({"_meta": meta, "conversations": [payload]},
                          title=a.title, slim=a.slim)
            sys.stdout.write(json.dumps(out, ensure_ascii=False, indent=2))
            return 0

        if fmt == "json":
            sys.stdout.write(json.dumps(
                {"_meta": meta, "conversations": [payload]},
                ensure_ascii=False, indent=2))
        else:
            sys.stdout.write(render_markdown(payload))
        return 0

    outdir = os.path.abspath(os.path.expanduser(a.outdir))
    os.makedirs(outdir, exist_ok=True)

    try:
        result = extract(a.share, title=a.title, reason=a.reason, outdir=outdir,
                         want_thumbnail=not a.no_thumbnail)
    except (RuntimeError, ValueError) as e:
        print("ERROR: {}".format(e))
        return 2

    payload = result["conversations"][0]
    base = safe_filename(payload)
    jp = os.path.join(outdir, base + ".json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("wrote {}".format(jp))

    if a.markdown:
        mp = os.path.join(outdir, base + ".md")
        write_markdown(mp, payload)
        print("wrote {}".format(mp))

    if a.verify:
        # 校验要重新取一次分享 API 原始文档；取不到时**不能报 PASS**——
        # 「没验」和「验过了」是两回事（旧代码这里会甩裸 traceback）。
        try:
            _raw, doc = fetch_share(payload.get("id"))
        except Exception as e:  # noqa: BLE001 — 网络异常要如实说明
            print("=" * 60)
            print("VERIFY RESULT: SKIPPED (未能取回原始响应，本次未校验: {}: {})".format(
                type(e).__name__, e))
            return 4
        v = verify_payload(payload, doc)
        print("=" * 60)
        print("VERIFY (多重集比较: transcript vs 分享 API 原始响应)")
        for k, val in (v.get("checks") or {}).items():
            print("      {} {}".format("OK " if val else "BAD", k))
        print("      source={} output={} missing={} duplicate={}".format(
            v.get("source_fragment_count"), v.get("output_fragment_count"),
            v.get("missing_count"), v.get("duplicate_count")))
        print("      attachments={} with_bytes={}".format(
            v.get("attachments_total"), v.get("attachments_with_bytes")))
        print("VERIFY RESULT:", "PASS" if v.get("ok") else "FAIL")
        if not v.get("ok"):
            return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
