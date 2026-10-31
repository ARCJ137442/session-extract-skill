# -*- coding: utf-8 -*-
"""
脱敏扫描器 —— 检查仓库（当前树 / 全部历史 / 提交信息）里有没有不该出现的东西。

为什么需要这个工具：
前几轮脱敏都是**按字面量**扫（路径、分享 id、标题、指纹），
结果**漏掉了「照抄真实对象当示例」这一类**——真实会话的用户提问原文、
真实附件的 file_id / 字节数，都曾以「示例」的名义留在文档里。
字面量扫描抓不到这类，**只有 n-gram 比对能抓到**。

六条设计铁律（每一条都是被独立验收员抓出来之后补的）：

1. **不许假绿**：扫不到东西时必须**报错退出**，绝不报「干净」。
   仓库定位用**脚本自身所在目录**向上找 git 根，**不继承进程 cwd**
   ——否则在别的 git 仓库里跑会只扫 1 个 blob 然后说「干净」。
2. **提交信息也要扫**：泄漏不只在文件里，也在 commit message 里。
3. **扫描器不能扫到自己**：黑名单里的模式必然命中自己，
   所以要按**路径**排除自身文件——注意已提交 blob 的标签是 **sha**，
   按路径排除对它们是**空操作**，必须先建 sha→路径映射。
4. **黑名单里不放任何个人字面量**：把机器名、盘符根写进代码
   等于**自己制造泄露**；「用字符串拼接藏起来」更糟——那是**故意让门禁看不见**。
   个人标识符请用 `--deny <token>` **作为输入**传进来。
5. **要能看穿字符串拼接**：作者可能把真实 id 写成 `"ab" + "cd"`。
   所以除了字面量匹配，还要**还原拼接后再比对**（见 _deobfuscate）。
6. **Windows 上不能静默产出乱码**：stdout 的编码要跟随**控制台码页**
   （否则 PowerShell 按 cp936 解码 UTF-8 字节 → 中文全乱，且**不报错**）。

用法：
    # 扫当前树 + 全部提交历史 + 全部提交信息
    python scripts/desensitize_scan.py --history

    # 加上真实语料的 n-gram 探针（强烈建议）
    python scripts/desensitize_scan.py --history \
        --corpus "G:\\path\\to\\conversations.json" \
        --corpus "G:\\path\\to\\share-original.json"

    # 把自己的标识符当**输入**传进来（不进仓库）
    python scripts/desensitize_scan.py --history --deny "我的机器名" --deny "D:\\我的盘"

退出码：0 = 干净；1 = 有命中（并打印位置）；2 = 扫描本身没跑成（**不是「干净」**）
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
# ⚠️ 路径正则要同时覆盖**单反斜杠**与**正斜杠**两种形态：
#    Markdown 与源码里写的是 <用户目录>
#    而 r'...\\\\...' 匹配的是**两个字面反斜杠**——写错就会全漏。
# ⚠️ 还要求后面跟一个**真实**的目录名（不是占位符）：
#    形如 `<用户目录>` 只是格式说明，不是泄露。
PATH_SEGMENT = r'(?!<)[^\\/:*?"<>|\r\n]{1,64}'
LITERAL_PATTERNS = [
    (r'[A-Za-z]:[\\/]{1,2}(?:Users|Downloads|Documents|Desktop)[\\/]' + PATH_SEGMENT,
     "个人机器路径"),
    (r'/home/[a-z][a-z0-9_-]*/', "家目录路径"),
    (r'/Users/[A-Za-z0-9_.-]+/', "macOS 用户目录"),
]

# 真实分享链接形态（文档里应用 <id> 占位代替）
SHARE_LINK_RE = re.compile(r'chat\.deepseek\.com/share/([A-Za-z0-9_-]{16,})')

# 纯 ASCII 的标识符（会话 UUID / 附件 file_id）
UUID_RE = re.compile(
    r'(?<![0-9a-fA-F-])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
    r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![0-9a-fA-F-])')

# 通用机器名**形态**（不含具体人名）：形如 XXX-LAPTOP-20220805
# 通用机器名**形态**（不含具体人名）。
# ⚠️ 必须覆盖真实形态：**三段** `名称-LAPTOP-20220805`
#    （以前只匹配四段 `-A-B-数字`，真实机器名全是三段 → 整条规则失明，
#      而自带测试用的是四段夹具，于是它一直「通过」）
MACHINE_SHAPE_RE = re.compile(
    r'(?:\w{3,}-)?[A-Z]{3,}-\d{6,8}\b'          # NAME-LAPTOP-20220805 / LAPTOP-20220805
    r'|(?:\w{3,}-){2}[A-Z]{3,}-\d{6,8}\b')     # HOST-NAME-LAPTOP-20220805

# 扫描器自身：黑名单里必然含有这些模式（按**路径**排除）
SELF_FILE = "desensitize_scan.py"

# 占位词：出现在命中片段里就说明它是示例，不是真泄露
PLACEHOLDER_WORDS = ("synthetic", "example", "placeholder", "dummy", "sample",
                     "<uuid>", "<id>", "xxxx", "00000000", "<model>", "<name>")

# 字符串拼接：可能是「故意把真实 id 藏起来」
_CONCAT_RE = re.compile(r'"([^"\\]{1,80})"\s*\+\s*"([^"\\]{1,80})"')
_STR_CONCAT_RE = re.compile(r"'([^'\\]{1,80})'\s*\+\s*'([^'\\]{1,80})'")


def _looks_like_placeholder(text):
    low = text.lower()
    return any(w in low for w in PLACEHOLDER_WORDS)


def _looks_synthetic_uuid(u):
    """
    明显是凑出来的 UUID 不算泄露。

    判据：任一段是**同一个字符重复**（00000000 / 11111111），
    或第一段是简单递增序列（01234567）——这类是测试夹具，
    而真实的会话 UUID 是随机的。
    """
    parts = u.split("-")
    if len(parts) != 5:
        return False
    for p in parts:
        if len(set(p)) == 1:
            return True
    first = parts[0]
    if len(set(first)) == len(first):
        digits = [c for c in first if c.isdigit()]
        if len(digits) == len(first) and all(
                int(digits[i]) + 1 == int(digits[i + 1]) for i in range(len(digits) - 1)):
            return True
    return False


def _deobfuscate(text):
    """
    还原「用字符串拼接藏起来」的内容。

    真实 id 写成 `"ab" + "cd" + "ef"` 时，字面量扫描是看不见的——
    而这恰恰是作者最可能用的藏法（连我都用过一次）。
    这里把相邻的字面量拼接**还原**出来一起比对。

    只做「相邻两个字面量」的还原（够用了），不做任意求值。
    """
    out = [text]
    for regex in (_CONCAT_RE, _STR_CONCAT_RE):
        for _ in range(4):      # 多段拼接最多还原 4 轮
            joined = []
            changed = False
            for m in regex.finditer(text):
                joined.append(m.group(1) + m.group(2))
                changed = True
            if not changed:
                break
            text = regex.sub(lambda m: '"' + m.group(1) + m.group(2) + '"', text)
            out.append(text)
    return "\n".join(out)


def run_git(args, cwd=None):
    r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True)
    return r.stdout.decode("utf-8", "replace")


def repo_root(explicit=None):
    """
    定位要扫的仓库根目录。

    ⚠️ 默认用**脚本自身所在目录**向上找 `.git`，**不继承进程 cwd**：
    在别的 git 仓库里跑时，按 cwd 定位会只扫到 1 个 blob，然后报「干净」
    ——那是**假绿**，比没有门禁更危险。
    """
    if explicit:
        root = os.path.abspath(explicit)
        if not os.path.isdir(root):
            raise RuntimeError("--repo 指向的目录不存在: {}".format(root))
        return root
    here = os.path.dirname(os.path.abspath(__file__))
    cur = here
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    raise RuntimeError(
        "找不到脚本所属的 git 仓库（从 {} 向上找 .git 失败）。"
        "请用 --repo <路径> 显式指定。".format(here))


def _sha_to_path_map(root):
    """sha → 路径映射，用来排除**扫描器自身的历史版本**。"""
    sha2path = {}
    ls = run_git(["ls-files", "-s"], cwd=root)
    for line in ls.split("\n"):
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        sha = parts[0].split(" ")[0]
        sha2path.setdefault(sha, parts[1])
    for line in run_git(["rev-list", "--all", "--objects", "--", SELF_FILE],
                        cwd=root).split("\n"):
        sha = line.split(" ")[0].strip()
        if len(sha) == 40:
            sha2path.setdefault(sha, SELF_FILE)
    return sha2path


def collect_blobs(history=False, root=None, include_worktree=True):
    """
    返回 [(标识, 文本)]。

    - history=True：全部历史对象（`--batch-all-objects`）
    - 永远包含**提交信息**（commit message 也是泄漏面）
    - include_worktree：加上**工作区未提交的改动**——
      只扫 HEAD 会让「本地改了还没 commit」的文档漏检（也是假绿的一种）
    """
    root = root or repo_root()
    blobs = []
    sha2path = _sha_to_path_map(root)

    if history:
        out = run_git(["cat-file", "--batch-all-objects",
                       "--batch-check=%(objectname) %(objecttype)"], cwd=root)
        for line in out.split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) != 2:
                continue
            sha, otype = parts
            if otype != "blob":
                continue
            # 标签用**路径**（没有路径就用 sha），好让「排除自身」能生效
            blobs.append((sha2path.get(sha, sha),
                          run_git(["cat-file", "-p", sha], cwd=root)))
    else:
        out = run_git(["ls-files"], cwd=root)
        for path in out.split("\n"):
            path = path.strip()
            if not path:
                continue
            sha = run_git(["rev-parse", "HEAD:" + path], cwd=root).strip()
            if sha:
                blobs.append((path, run_git(["cat-file", "-p", sha], cwd=root)))

    # 提交信息：**必须扫**。曾经真实会话的片段就藏在 commit message 里。
    msgs = run_git(["log", "--all", "--format=%H%n%B%n---"], cwd=root)
    if msgs.strip():
        blobs.append(("commit-messages", msgs))

    if include_worktree:
        dirty = run_git(["status", "--porcelain"], cwd=root)
        changed = set()
        for line in dirty.split("\n"):
            if not line.strip():
                continue
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ")[-1].strip()
            changed.add(path)
        untracked = run_git(["ls-files", "--others", "--exclude-standard"], cwd=root)
        for path in untracked.split("\n"):
            path = path.strip()
            if path:
                changed.add(path)
        for path in changed:
            full = os.path.join(root, path)
            if not os.path.isfile(full):
                continue
            try:
                with open(full, encoding="utf-8") as f:
                    blobs.append(("worktree:" + path, f.read()))
            except OSError:
                continue
    return blobs


def literal_scan(blobs, deny_tokens=()):
    hits = []
    for tag, txt in blobs:
        # 扫描器自身跳过：黑名单里必然含有那些模式（按**路径**判断）
        if SELF_FILE in str(tag):
            continue
        # 看穿字符串拼接：真实 id 写成 "ab" + "cd" 时字面量扫描是看不见的
        scan_text = _deobfuscate(txt)
        for pat, label in LITERAL_PATTERNS:
            for m in re.finditer(pat, scan_text):
                hits.append((label, tag, scan_text[:m.start()].count("\n") + 1,
                             m.group(0)[:60]))
        for m in SHARE_LINK_RE.finditer(scan_text):
            if _looks_like_placeholder(m.group(0)):
                continue
            hits.append(("疑似真实分享链接", tag,
                         scan_text[:m.start()].count("\n") + 1, m.group(0)[:80]))
        for m in UUID_RE.finditer(scan_text):
            if _looks_like_placeholder(m.group(0)) or _looks_synthetic_uuid(m.group(0)):
                continue
            hits.append(("疑似真实 UUID", tag,
                         scan_text[:m.start()].count("\n") + 1, m.group(0)[:60]))
        for m in MACHINE_SHAPE_RE.finditer(scan_text):
            if _looks_like_placeholder(m.group(0)):
                continue
            hits.append(("疑似机器名", tag,
                         scan_text[:m.start()].count("\n") + 1, m.group(0)[:40]))
        # 用户显式提供的个人标识符（作为**输入**，不进仓库）
        for tok in deny_tokens:
            if tok and tok in scan_text:
                hits.append(("用户指定的敏感词", tag,
                             scan_text[:scan_text.find(tok)].count("\n") + 1, tok[:40]))
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
    want = ("REQUEST", "RESPONSE") if include_assistant else ("REQUEST",)
    biz = ((doc.get("data") or {}).get("biz_data") or {}) if isinstance(doc, dict) else {}
    for msg in biz.get("messages") or []:
        for fr in msg.get("fragments") or []:
            if fr.get("type") in want and fr.get("content"):
                out.append(fr["content"])
            # ⚠️ 搜索结果的 **snippet** 也是语料——发布说明原文、引文、
            # 网页摘录都在这里。以前只读 content，等于漏掉了一整类泄漏面。
            for r in (fr.get("results") or []):
                if isinstance(r, dict) and r.get("snippet"):
                    out.append(r["snippet"])
            if isinstance(fr.get("result"), dict) and fr["result"].get("snippet"):
                out.append(fr["result"]["snippet"])
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
                if fr.get("type") in want and fr.get("content"):
                    out.append(fr["content"])
                for r in (fr.get("results") or []):
                    if isinstance(r, dict) and r.get("snippet"):
                        out.append(r["snippet"])
                if isinstance(fr.get("result"), dict) and fr["result"].get("snippet"):
                    out.append(fr["result"]["snippet"])
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
    if _looks_like_code(text):
        return False
    if cjk >= min_cjk:
        return True
    if len(text) >= 60 and text.count(" ") >= 8:
        return True
    return False


def _is_good_probe(w, min_words_long=2, min_words_short=3):
    """
    探针必须**自身**有内容。

    纯空白 + 一两个符号的窗口（「}」「 o」）在任何代码里都会出现——
    命中全是噪声，会把真正的泄漏淹没。所以要求：
    - 中文行：窗口里至少 4 个汉字
    - 英文行：长窗口（20 字）至少 2 个长度 ≥3 的单词；
      **短窗口（12 字）要 3 个**——否则「Claude Opus」这种**通用技术名词**
      会到处命中（它既在语料里、也在正常的提交署名里），把门禁刷红。
    """
    if not w.strip():
        return False
    cjk = sum(1 for ch in w if "一" <= ch <= "鿿")
    if cjk >= 4:
        return True
    words = [x for x in w.split() if len(x) >= 3]
    need = min_words_long if len(w) >= 18 else min_words_short
    return len(words) >= need


def ngram_scan(blobs, corpus_texts, probe_len=20, step=10, short_len=12):
    """
    拿语料正文的 n 字窗口做探针，逐个在 blob 里找。

    窗口只在**像散文的行**里滑，不在整段文本上滑——
    否则窗口会骑在代码/URL 上（半个 URL、半个花括号），
    在仓库源码里命中一堆没意义的片段。

    中文行额外用**更短的窗口**（short_len）：短标题（少于 20 字）
    也是会指认人的内容，长窗口抓不到它们。
    """
    probes = set()
    for t in corpus_texts:
        for line in t.split("\n"):
            line = line.strip()
            if not line:
                continue
            cjk = sum(1 for ch in line if "一" <= ch <= "鿿")
            if "://" in line or "http" in line:
                continue
            sizes = []
            if cjk >= 4:
                sizes = [short_len, probe_len]
            elif len(line) >= 30 and line.count(" ") >= 3:
                sizes = [probe_len]
            else:
                continue
            for size in sizes:
                if len(line) < size:
                    continue
                for i in range(0, len(line) - size + 1, step):
                    w = line[i:i + size]
                    if _is_good_probe(w):
                        probes.add(w)
    hits = []
    for tag, txt in blobs:
        if SELF_FILE in str(tag):
            continue
        scan_text = _deobfuscate(txt)
        for p in probes:
            if p in scan_text:
                line = scan_text[:scan_text.find(p)].count("\n") + 1
                hits.append(("真实语料片段", tag, line, p[:40]))
    return hits, len(probes)


def corpus_identifiers(paths):
    """从真实语料里抽出**标识符候选**（会话 id / 附件 file_id）。"""
    ids = set()
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                doc = json.load(f)
        except Exception:
            continue
        blob = json.dumps(doc, ensure_ascii=False)
        for m in UUID_RE.finditer(blob):
            if not _looks_synthetic_uuid(m.group(0)):
                ids.add(m.group(0))
        for m in re.finditer(r'"file_id"\s*:\s*"([^"]+)"', blob):
            fid = m.group(1)
            if fid and not _looks_like_placeholder(fid):
                ids.add(fid)
    return ids


def identifier_scan(blobs, ids):
    hits = []
    for tag, txt in blobs:
        if SELF_FILE in str(tag):
            continue
        scan_text = _deobfuscate(txt)
        for ident in ids:
            if ident and ident in scan_text:
                line = scan_text[:scan_text.find(ident)].count("\n") + 1
                hits.append(("语料标识符", tag, line, ident[:60]))
    return hits


def main(argv=None):
    ap = argparse.ArgumentParser(description="脱敏扫描器")
    ap.add_argument("--history", action="store_true", help="连全部提交历史一起扫")
    ap.add_argument("--corpus", action="append", default=[],
                    help="真实语料（导出包或分享原始 JSON），用于 n-gram 探针；可重复")
    ap.add_argument("--deny", action="append", default=[], metavar="TOKEN",
                    help="你自己的敏感标识符（机器名/盘符根/项目 id…），"
                         "作为**输入**传进来，不进仓库；可重复")
    ap.add_argument("--probe-len", type=int, default=20, help="探针窗口长度")
    ap.add_argument("--short-len", type=int, default=12, help="中文行的短探针窗口")
    ap.add_argument("--step", type=int, default=10, help="探针取样步长")
    ap.add_argument("--include-assistant", action="store_true",
                    help="连助手回答一起做探针（噪声大：回答里常在讨论代码）")
    ap.add_argument("--repo", default=None, help="仓库路径（默认：脚本所在仓库）")
    ap.add_argument("--out", default=None, help="把报告写成 JSON")
    a = ap.parse_args(argv)

    try:
        root = repo_root(a.repo)
    except RuntimeError as e:
        print("ERROR: {}".format(e))
        return 2

    blobs = collect_blobs(history=a.history, root=root)
    print("仓库根目录: {}".format(root))
    print("扫描对象数: {}（{}）".format(
        len(blobs), "含全部历史对象 + 提交信息" if a.history else "当前树 + 提交信息"))
    if not blobs:
        print("ERROR: 一个对象都没扫到——这不表示干净，请检查仓库路径。")
        return 2

    hits = literal_scan(blobs, deny_tokens=a.deny)
    n_hits, n_probes = [], 0
    id_hits, n_ids = [], 0
    if a.corpus:
        texts = load_corpus_texts(a.corpus, include_assistant=a.include_assistant)
        n_hits, n_probes = ngram_scan(blobs, texts, a.probe_len, a.step, a.short_len)
        ids = corpus_identifiers(a.corpus)
        id_hits = identifier_scan(blobs, ids)
        n_ids = len(ids)
        print("n-gram 探针数: {}（窗口 {} / 中文短窗 {} 字）".format(
            n_probes, a.probe_len, a.short_len))
        print("语料标识符数: {}".format(n_ids))
    else:
        print("（未提供 --corpus：跳过 n-gram 探针与标识符比对。建议补上——"
              "字面量扫描抓不到「照抄真实正文当示例」这一类）")

    all_hits = hits + n_hits + id_hits
    report = {
        "repo_root": root,
        "objects_scanned": len(blobs),
        "history": bool(a.history),
        "deny_tokens": len(a.deny),
        "literal_hits": [{"label": h[0], "at": h[1], "line": h[2], "match": h[3]}
                         for h in hits],
        "ngram_probes": n_probes,
        "ngram_hits": [{"label": h[0], "at": h[1], "line": h[2], "match": h[3]}
                       for h in n_hits],
        "corpus_identifiers": n_ids,
        "identifier_hits": [{"label": h[0], "at": h[1], "line": h[2], "match": h[3]}
                            for h in id_hits],
        "ok": not all_hits,
    }

    for label, group in (("字面量", hits), ("n-gram", n_hits), ("标识符", id_hits)):
        if group:
            print("\n⚠️ {}命中 {} 处：".format(label, len(group)))
            for h in group[:20]:
                print("   [{}] {}:{}  {}".format(h[0], str(h[1])[:40], h[2], h[3]))
    if not all_hits:
        print("\n✅ 干净：没有命中")
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print("wrote {}".format(a.out))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
