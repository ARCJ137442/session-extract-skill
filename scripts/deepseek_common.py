# -*- coding: utf-8 -*-
"""
DeepSeek 两条路径（账号导出包 / 分享链接）共用的归一规则。

为什么要有这个模块：
1. **标题占位符**：分享 API 返回的标题是占位符，但**字符串会变**
   （实测出现过「来自分享的对话」与「Shared Conversation」两种）。
   硬编码 `== "来自分享的对话"` 会让 `title_is_placeholder` 静默误判为 False——
   不报错，只是给出一个假的「一切正常」信号。改成名单 + 归一比较。
2. **URL 引用锚点**：DeepSeek 的搜索结果 URL 带引用锚点
   （如 `.../paper.pdf#7#22#7`），**同一篇文档在不同引用位置锚点不同**。
   按原始 URL 建字典 = 同一篇文章被登记成多条记录，且**互相不补字段**。
   实测（2026-10-29 样本：分享侧 494 条结果）239 个唯一原始 URL、只有 238 个唯一文档；
   按原始 URL 去重会吞掉 63,309 个字符（52.8%）的 snippet。
   统一在这里归一，提取侧与合并侧用**同一把尺子**。

规则只在这里定义一次，别的模块 import 过来用——
「同一事实两处实现」是本 skill 历史上已经踩过的坑（merge 的 url 键
与 file_id 归一化不同步）。
"""
import os
import re
import sys


def console_encoding():
    """
    stdout 该用什么编码。

    ⚠️ **不要在 Windows 上钉死 UTF-8**：PowerShell 5.1 读取子进程 stdout 时
    按 `[Console]::OutputEncoding`（控制台码页，中文默认 cp936）**解码**。
    子进程写 UTF-8 字节 → 被按 GBK 解码 → **中文全乱码，而且不报错**
    （静默损坏比崩溃更危险）。

    所以跟随**控制台真实码页**：
    - Windows：用 `GetConsoleOutputCP()` 拿真实码页（chcp 65001 → UTF-8）
    - 其他：UTF-8

    需要**字节级保真**的产物请用 `--out` / `--outdir`——
    文件侧显式 `encoding="utf-8"`，不受控制台码页影响。
    """
    if os.name != "nt":
        return "utf-8"
    try:
        import ctypes
        cp = ctypes.windll.kernel32.GetConsoleOutputCP()
        if cp == 65001:
            return "utf-8"
        if cp:
            # ⚠️ 顺序要紧：必须**先试码页本身**。
            # 以前把 "utf-8" 放在第一位，而它必然可编码 → 函数恒返回 utf-8，
            # 算出来的 cp 从未被使用 → 「跟随码页」其实是**假的**，
            # 在标准中文 Windows 上照样静默乱码。
            for enc in ("cp" + str(cp), "gbk"):
                try:
                    "x".encode(enc)
                    return enc
                except LookupError:
                    continue
    except Exception:
        pass
    return "utf-8"


def setup_streams():
    """把 stdout/stderr 的编码设成控制台码页（错误字符用 replace 兜底）。"""
    enc = console_encoding()
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding=enc, errors="replace")
        except (AttributeError, ValueError):
            pass
    return enc

# 分享链接侧的占位符标题。DeepSeek 前端可能改文案/语言，所以用名单 + 归一比较，
# 而不是「等于某一个字符串」。
PLACEHOLDER_TITLES = (
    "来自分享的对话",
    "Shared Conversation",
    "Shared conversation",
    "Shared Conversation ",
)

_WS = re.compile(r"\s+")


def normalize_title(title):
    """标题归一：去空白 + 折叠内部空白 + 去掉首尾不可见字符。"""
    if not isinstance(title, str):
        return ""
    return _WS.sub(" ", title.replace("​", "").replace("﻿", "")).strip()


def is_placeholder_title(title):
    """标题是不是「分享链接占位符」——用于判断 title_is_placeholder。"""
    t = normalize_title(title)
    if not t:
        return False
    return any(normalize_title(p) == t for p in PLACEHOLDER_TITLES)


def norm_url(url):
    """
    URL 归一（主键用）：剥掉 `#` 之后的引用锚点。

    `https://x/a.pdf#7#22#7` → `https://x/a.pdf`
    非字符串原样返回；`#` 在开头（纯锚点）时返回原值。
    """
    if not isinstance(url, str):
        return url
    base, sep, _frag = url.partition("#")
    return base if base else url


def url_anchor(url):
    """取引用锚点（不含 `#`）。没有则返回 None。"""
    if not isinstance(url, str):
        return None
    _base, sep, frag = url.partition("#")
    return frag if (sep and frag) else None


# 结果载荷里**派生**出来的字段——它们是归一产物，不是源数据。
# 计算 fragment 签名时要剥掉，否则加了派生字段会让签名整体漂移，
# 进而把「源 vs 产物」的比较结果变成噪声。
DERIVED_RESULT_KEYS = ("url_normalized", "url_anchor")


def strip_derived_result_fields(results):
    """剥掉结果里的派生字段，返回新的 list（不改原对象）。"""
    if not results:
        return results
    out = []
    for r in results:
        if not isinstance(r, dict):
            out.append(r)
            continue
        out.append({k: v for k, v in r.items() if k not in DERIVED_RESULT_KEYS})
    return out


def result_url_key(result):
    """
    结果条目的合并主键：**归一后的 URL**。

    优先用已算好的 `url_normalized`，否则现算——这样导出包路径
    （没有派生字段）和分享链接路径（带派生字段）能用同一把尺子。
    """
    if not isinstance(result, dict):
        return None
    u = result.get("url_normalized") or result.get("url")
    return norm_url(u)
