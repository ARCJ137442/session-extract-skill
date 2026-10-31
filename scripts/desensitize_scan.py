# -*- coding: utf-8 -*-
"""
脱敏扫描器 —— 检查仓库（当前树 / 全部历史）里有没有不该出现的东西。

为什么需要这个工具：
前两轮脱敏都是**按字面量**扫（路径、分享 id、标题、指纹），
结果**漏掉了「照抄真实对象当示例」这一类**——真实会话的用户提问原文、
真实附件的 file_id / 字节数，都曾以「示例」的名义留在文档里。
字面量扫描抓不到这类，**只有 n-gram 比对能抓到**。

所以这里做两件事：
1. **字面量扫描**：机器路径、分享 id、用户名、常见项目名等
2. **n-gram 探针**：拿真实语料（导出包 / 分享链接原始 JSON）的**对话正文**
   切成 20 字窗口，逐个在仓库所有 blob 里找——只要有一段原文被抄进去就会命中

用法：
    # 扫当前树 + 全部提交历史
    python scripts/desensitize_scan.py --history

    # 加上真实语料的 n-gram 探针（强烈建议）
    python scripts/desensitize_scan.py --history \
        --corpus "G:\\path\\to\\conversations.json" \
        --corpus "G:\\path\\to\\share-original.json"

退出码：0 = 干净；1 = 有命中（并打印具体位置）
"""
import argparse
import json
import os
import re
import subprocess
import sys

# ---------------------------------------------------------------
# 字面量黑名单（正则；命中即报）
# ---------------------------------------------------------------
LITERAL_PATTERNS = [
    (r'[A-Za-z]:\\\\Users\\\\', "个人用户目录"),
    (r'[A-Za-z]:\\\\Downloads\\\\', "个人下载目录"),
    (r'[A-Za-z]:\\\\Documents\\\\', "个人文档目录"),
    (r'/home/[a-z][a-z0-9_-]*/', "家目录路径"),
    (r'<机器名>[-_A-Za-z0-9]*', "机器名"),
]

# 真实分享链接形态（文档里应用 <id> 占位代替）
# ⚠️ 只匹配**后面跟着真实 id** 的形态：`/share/<id>` 这种格式说明是合法的。
SHARE_LINK_RE = re.compile(r'chat\.deepseek\.com/share/[A-Za-z0-9_-]{16,}')


def run_git(args, cwd=None):
    r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True)
    return r.stdout.decode("utf-8", "replace")


def collect_blobs(history=False):
    """返回 [(blob_sha, 文本)]。history=True 时含所有历史对象。"""
    if history:
        out = run_git(["cat-file", "--batch-all-objects",
                       "--batch-check=%(objectname) %(objecttype)"])
        shas = [l.split()[0] for l in out.split("\n")
                if l.strip().endswith("blob")]
    else:
        out = run_git(["ls-files"])
        shas = []
        for path in out.split("\n"):
            path = path.strip()
            if not path:
                continue
            sha = run_git(["rev-parse", "HEAD:" + path]).strip()
            if sha:
                shas.append(sha)
    blobs = []
    for sha in shas:
        txt = run_git(["cat-file", "-p", sha])
        blobs.append((sha, txt))
    return blobs


def literal_scan(blobs):
    hits = []
    for sha, txt in blobs:
        for pat, label in LITERAL_PATTERNS:
            for m in re.finditer(pat, txt):
                hits.append((label, sha, txt[:m.start()].count("\n") + 1,
                             m.group(0)[:60]))
        for m in SHARE_LINK_RE.finditer(txt):
            hits.append(("疑似真实分享链接", sha,
                         txt[:m.start()].count("\n") + 1, m.group(0)[:80]))
    return hits


def load_corpus_texts(paths, include_assistant=False):
    """
    从导出包 / 分享原始 JSON 里抽出**对话正文**（提问与回答）。

    必须**按结构**取，不能把整个 JSON 的所有字符串都当语料：
    导出包里除了对话还有各种元数据，而**对话本身往往在讨论代码**——
    把代码片段当探针，会在仓库源码里命中一堆**假阳性**。

    默认**只取 REQUEST**（用户自己打的字）：那是**最可能指认人**的内容，
    而回答里往往在讨论代码——代码片段在仓库源码里必然命中，全是噪声。
    要连回答一起扫，用 --include-assistant。

    再过滤一遍「像散文」的片段（见 _is_probably_prose）。
    """
    texts = []
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                doc = json.load(f)
        except Exception as e:
            sys.stderr.write("WARNING: 读不了语料 {}: {}\n".format(p, e))
            continue
        texts.extend(_collect_conversation_text(doc, include_assistant))
    return [t for t in texts if _is_probably_prose(t)]


def _collect_conversation_text(doc, include_assistant=False):
    out = []
    # 分享原始 JSON: {"data": {"biz_data": {"messages": [...]}}}
    # （账号导出包是**列表**，没有 data 段——两条分支都要处理）
    biz = ((doc.get("data") or {}).get("biz_data") or {}) if isinstance(doc, dict) else {}
    for msg in biz.get("messages") or []:
        for fr in msg.get("fragments") or []:
            if fr.get("type") in (("REQUEST", "RESPONSE") if include_assistant else ("REQUEST",)) and fr.get("content"):
                out.append(fr["content"])
    # 账号导出包: [ {id, title, mapping: {...}}, ... ]
    conversations = doc if isinstance(doc, list) else (
        doc.get("conversations") if isinstance(doc, dict) else [])
    for conv in conversations or []:
        if not isinstance(conv, dict):
            continue
        mapping = conv.get("mapping") or {}
        for node in mapping.values():
            if not isinstance(node, dict):
                continue
            msg = node.get("message") or {}
            for fr in msg.get("fragments") or []:
                if fr.get("type") in (("REQUEST", "RESPONSE") if include_assistant else ("REQUEST",)) and fr.get("content"):
                    out.append(fr["content"])
    return out


CODE_MARKERS = ("def ", "class ", "import ", "from ", "return ", "elif ",
                 "lambda ", "async ", "await ", "self.", "print(", "console.log",
                 "function ", "const ", "let ", "var ", "=>", "{", "}", ";",
                 "==", "!=", "None", "True", "False", "null", "true", "false")


def _looks_like_code(text):
    """粗判是不是代码片段。"""
    hits = sum(1 for m in CODE_MARKERS if m in text)
    if hits >= 4:
        return True
    indented = sum(1 for l in text.split("\n") if l.startswith("    "))
    if hits >= 2 and indented >= 3:
        return True
    return False


def _is_probably_prose(text, min_cjk=6):
    """
    只把「像人话」的片段当探针。

    对话里大量在讨论代码；代码片段在仓库源码里**必然**命中，
    会把真正的泄漏淹没在噪声里。真实会指认人的内容几乎总是自然语言，
    所以用「汉字数量」做粗筛，并额外排除明显的代码。
    """
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    # ⚠️ 先排代码，再看散文：**中文对话里也常夹代码**（用户贴一段代码来问），
    # 而代码在仓库源码里必然命中 —— 不先排除就全是噪声。
    if _looks_like_code(text):
        return False
    if cjk >= min_cjk:
        return True
    # 非中文长句（英文）也可能是指认内容，但要求更长、更像句子
    return len(text) >= 60 and text.count(" ") >= 8


def ngram_scan(blobs, corpus_texts, probe_len=20, step=10):
    """
    拿语料正文的 n 字窗口做探针，逐个在 blob 里找。

    ⚠️ 窗口只在**像散文的行**里滑，不在整段文本上滑——
    否则窗口会骑在代码/URL 上（半个 URL、半个花括号），
    在仓库源码里命中一堆没意义的片段。
    """
    probes = set()
    for t in corpus_texts:
        for line in t.split("\n"):
            line = line.strip()
            if len(line) < probe_len:
                continue
            cjk = sum(1 for ch in line if "一" <= ch <= "鿿")
            if cjk < 4:
                continue
            # 含 URL 的行也不当探针：URL 形态（如分享链接格式说明）在
            # 仓库文档里本来就会出现，命中是**良性**的，只会制造噪声
            if "://" in line or "http" in line:
                continue
            for i in range(0, len(line) - probe_len + 1, step):
                w = line[i:i + probe_len]
                if w.strip():
                    probes.add(w)
    hits = []
    for sha, txt in blobs:
        for p in probes:
            if p in txt:
                line = txt[:txt.find(p)].count("\n") + 1
                hits.append(("真实语料片段", sha, line, p[:40]))
    return hits, len(probes)


def main(argv=None):
    ap = argparse.ArgumentParser(description="脱敏扫描器")
    ap.add_argument("--history", action="store_true", help="连全部提交历史一起扫")
    ap.add_argument("--corpus", action="append", default=[],
                    help="真实语料（导出包或分享原始 JSON），用于 n-gram 探针；可重复")
    ap.add_argument("--probe-len", type=int, default=20, help="探针窗口长度")
    ap.add_argument("--step", type=int, default=10, help="探针取样步长")
    ap.add_argument("--include-assistant", action="store_true",
                    help="连助手回答一起做探针（噪声大：回答里常在讨论代码）")
    ap.add_argument("--out", default=None, help="把报告写成 JSON")
    a = ap.parse_args(argv)

    blobs = collect_blobs(history=a.history)
    print("扫描 blob 数: {}（{}）".format(
        len(blobs), "含全部历史对象" if a.history else "当前树"))

    hits = literal_scan(blobs)
    n_hits, n_probes = [], 0
    if a.corpus:
        texts = load_corpus_texts(a.corpus, include_assistant=a.include_assistant)
        n_hits, n_probes = ngram_scan(blobs, texts, a.probe_len, a.step)
        print("n-gram 探针数: {}（窗口 {} 字）".format(n_probes, a.probe_len))
    else:
        print("（未提供 --corpus：跳过 n-gram 探针。建议补上——"
              "字面量扫描抓不到「照抄真实正文当示例」这一类）")

    report = {
        "blobs_scanned": len(blobs),
        "history": bool(a.history),
        "literal_hits": [{"label": h[0], "blob": h[1], "line": h[2], "match": h[3]}
                         for h in hits],
        "ngram_probes": n_probes,
        "ngram_hits": [{"label": h[0], "blob": h[1], "line": h[2], "match": h[3]}
                       for h in n_hits],
        "ok": not hits and not n_hits,
    }

    if hits:
        print("\n⚠️ 字面量命中 {} 处：".format(len(hits)))
        for h in hits[:20]:
            print("   [{}] {}:{}  {}".format(h[0], h[1][:10], h[2], h[3]))
    if n_hits:
        print("\n⚠️ n-gram 命中 {} 处：".format(len(n_hits)))
        for h in n_hits[:20]:
            print("   [{}] {}:{}  {}".format(h[0], h[1][:10], h[2], h[3]))
    if not hits and not n_hits:
        print("\n✅ 干净：没有命中")
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print("wrote {}".format(a.out))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
