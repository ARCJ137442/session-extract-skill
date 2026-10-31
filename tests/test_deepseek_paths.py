# -*- coding: utf-8 -*-
"""
session-extract 的回归测试（stdlib unittest，不引第三方依赖）。

运行：
    python -m unittest discover -s tests -v
    python tests/test_deepseek_paths.py

⚠️ **脱敏原则**：本文件里的所有会话内容都是**合成样例**
（形如「【合成样例】…」），不包含任何真实会话正文、真实标题、
真实链接或真实 share_id。真实数据只允许作为**运行时输入**，
不得写进仓库。

覆盖的是**曾经真实踩过的坑**，每条断言后面标注了它守住的是什么：
  1. 占位符标题漏判（"Shared Conversation" 被当成真实标题）
  2. URL 引用锚点导致同一文档被登记成多条、snippet 互相不补齐
  3. 派生字段污染源数据 / 污染 fragment 签名 → 自证把自己判失败
  4. 思考链混进正文 → 污染对话流
  5. 转换时丢 fragment / 丢引用
"""
import json
import os
import sys
import unittest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.append(_SCRIPTS)

import deepseek_common  # noqa: E402
import extract_deepseek_share as eds  # noqa: E402
import to_openai  # noqa: E402


# ============================================================
# 合成样例：一个 2 轮对话，含思考链、联网搜索、打开网页、引用标记
# ============================================================

def synthetic_biz():
    """
    构造一个「形状正确」的分享链接业务对象。

    刻意埋了三个坑：
      - 标题是英文占位符 "Shared Conversation"
      - 同一篇文档在两处出现，锚点不同（#1#1#1 / #7#22#7），摘要不同
      - RESPONSE 带 references[]，正文带 [reference:N] 标记
    """
    doc_a = "【合成样例】文档甲"
    doc_b = "【合成样例】文档乙"
    url_a = "https://example.invalid/docs/a.pdf#1#1#1"
    url_a2 = "https://example.invalid/docs/a.pdf#7#22#7"
    url_b = "https://example.invalid/docs/b.html"

    msg_user_1 = {
        "message_id": 1, "role": "USER", "inserted_at": 1700000000,
        "fragments": [
            {"id": 1, "type": "REQUEST", "content": "【合成样例】第一个问题"},
        ],
    }
    msg_asst_1 = {
        "message_id": 2, "role": "ASSISTANT", "inserted_at": 1700000010,
        "fragments": [
            {"id": 2, "type": "THINK", "content": "【合成样例】先检索两篇文档"},
            {"id": 3, "type": "TOOL_SEARCH",
             "queries": [{"query": "合成样例查询词"}],
             "results": [
                 {"url": url_a, "title": doc_a, "snippet": "【合成样例】摘要甲-短",
                  "cite_index": None, "site_name": None, "published_at": None,
                  "site_icon": None, "query_indexes": [0], "provider": None},
                 {"url": url_b, "title": doc_b, "snippet": "【合成样例】摘要乙",
                  "cite_index": None, "site_name": None, "published_at": None,
                  "site_icon": None, "query_indexes": [0], "provider": None},
             ]},
            {"id": 4, "type": "TOOL_OPEN",
             "results": [
                 {"url": url_a2, "title": doc_a, "snippet": "【合成样例】摘要甲-长",
                  "cite_index": None, "site_name": None, "published_at": None,
                  "site_icon": None, "query_indexes": [0], "provider": None},
             ]},
            {"id": 5, "type": "RESPONSE",
             "content": "【合成样例】回答引用了文档甲[reference:0]与文档乙[reference:2]。",
             # ⚠️ references[] 是**有序数组**，正文里 [reference:N] 的 N 是它的**下标**
             # （不是 fragment id）。下标 2 指向 id=3 那个 TOOL_SEARCH。
             "references": [
                 {"id": 4, "type": "TOOL_OPEN"},
                 {"id": 2, "type": "TOOL_SEARCH", "urls": []},
                 {"id": 3, "type": "TOOL_SEARCH", "urls": [url_a, url_b]},
             ]},
        ],
    }
    msg_user_2 = {
        "message_id": 3, "role": "USER", "inserted_at": 1700000020,
        "fragments": [
            {"id": 6, "type": "REQUEST", "content": "【合成样例】第二个问题"},
        ],
    }
    msg_asst_2 = {
        "message_id": 4, "role": "ASSISTANT", "inserted_at": 1700000030,
        "fragments": [
            {"id": 7, "type": "TIP", "content": "【合成样例】附加提示"},
            {"id": 8, "type": "RESPONSE", "content": "【合成样例】第二个回答",
             "references": []},
        ],
    }
    return {
        "title": "Shared Conversation",
        "model_type": "default",
        "messages": [msg_user_1, msg_asst_1, msg_user_2, msg_asst_2],
    }


def synthetic_product():
    biz = synthetic_biz()
    payload = eds.build_payload(biz, biz["messages"], [], share_id="synthetic0000")
    return {"_meta": {"schema": eds.SCHEMA, "platform": eds.PLATFORM},
            "conversations": [payload]}


# ============================================================
# 1. 占位符标题识别
# ============================================================

class TestPlaceholderTitle(unittest.TestCase):
    """守住坑 1：占位符文案会变，硬编码字符串会静默漏判。"""

    def test_english_placeholder_detected(self):
        self.assertTrue(deepseek_common.is_placeholder_title("Shared Conversation"))

    def test_chinese_placeholder_detected(self):
        self.assertTrue(deepseek_common.is_placeholder_title("来自分享的对话"))

    def test_whitespace_normalised(self):
        self.assertTrue(deepseek_common.is_placeholder_title("  Shared   Conversation  "))

    def test_real_title_not_placeholder(self):
        self.assertFalse(deepseek_common.is_placeholder_title("【合成样例】真实会话名"))

    def test_payload_marks_english_placeholder(self):
        """回归：以前 title_is_placeholder 会对英文占位符静默返回 False。"""
        payload = synthetic_product()["conversations"][0]
        self.assertEqual(payload["title"], "Shared Conversation")
        self.assertTrue(payload["title_is_placeholder"])

    def test_explicit_title_overrides(self):
        biz = synthetic_biz()
        payload = eds.build_payload(biz, biz["messages"], [], title="真实标题", share_id="x")
        self.assertFalse(payload["title_is_placeholder"])
        self.assertEqual(payload["title_source"], "override")


# ============================================================
# 2. URL 锚点归一
# ============================================================

class TestUrlNormalisation(unittest.TestCase):
    """守住坑 2：同一文档不同锚点 → 必须按归一 URL 建键。"""

    def test_norm_url_strips_anchor(self):
        self.assertEqual(deepseek_common.norm_url("https://x/a.pdf#7#22#7"),
                         "https://x/a.pdf")

    def test_norm_url_keeps_plain(self):
        self.assertEqual(deepseek_common.norm_url("https://x/a.pdf"), "https://x/a.pdf")

    def test_url_anchor(self):
        self.assertEqual(deepseek_common.url_anchor("https://x/a.pdf#7#22#7"), "7#22#7")
        self.assertIsNone(deepseek_common.url_anchor("https://x/a.pdf"))

    def test_result_url_key_prefers_normalised_field(self):
        r = {"url": "https://x/a.pdf#1#1#1", "url_normalized": "https://x/a.pdf"}
        self.assertEqual(deepseek_common.result_url_key(r), "https://x/a.pdf")

    def test_result_url_key_falls_back_to_raw(self):
        self.assertEqual(deepseek_common.result_url_key({"url": "https://x/a.pdf#1#1#1"}),
                         "https://x/a.pdf")

    def test_two_anchors_same_document(self):
        """文档甲出现两次（锚点不同）→ 归一后是同一篇。"""
        biz = synthetic_biz()
        results = []
        for msg in biz["messages"]:
            for frag in msg["fragments"]:
                results.extend(frag.get("results") or [])
        keys = {deepseek_common.result_url_key(r) for r in results}
        self.assertEqual(len(keys), 2, "文档甲的两个锚点应归一成同一篇")


# ============================================================
# 3. 派生字段不得污染源数据 / 签名
# ============================================================

class TestDerivedFieldsIsolation(unittest.TestCase):
    """守住坑 3：派生字段写进源数据会让自证把自己判失败。"""

    def test_enrich_does_not_mutate_source(self):
        biz = synthetic_biz()
        before = json.dumps(biz["messages"], ensure_ascii=False, sort_keys=True)
        eds.build_transcript(biz)
        after = json.dumps(biz["messages"], ensure_ascii=False, sort_keys=True)
        self.assertEqual(before, after, "transcript 构建不得改动源消息")

    def test_transcript_results_have_derived_fields(self):
        transcript = eds.build_transcript(synthetic_biz())
        with_results = [e for e in transcript if e.get("results")]
        self.assertTrue(with_results)
        for e in with_results:
            for r in e["results"]:
                self.assertIn("url_normalized", r)
                self.assertIn("url_anchor", r)
                self.assertEqual(r["url_normalized"],
                                 deepseek_common.norm_url(r["url"]))

    def test_signature_stable_between_source_and_transcript(self):
        """签名必须忽略派生字段，否则「源 vs 产物」比较全是噪声。"""
        biz = synthetic_biz()
        transcript = eds.build_transcript(biz)
        src_sigs = [eds.frag_signature(f)
                    for m in biz["messages"] for f in m["fragments"]]
        out_sigs = [eds.frag_signature(e) for e in transcript]
        self.assertEqual(sorted(map(str, src_sigs)), sorted(map(str, out_sigs)))

    def test_raw_messages_identical_after_build(self):
        biz = synthetic_biz()
        payload = eds.build_payload(biz, biz["messages"], [], share_id="x")
        self.assertEqual(payload["raw_messages"], biz["messages"])


# ============================================================
# 4 & 5. 转换器：消息结构 / 思考链分离 / 无损
# ============================================================

class TestToOpenAI(unittest.TestCase):
    """守住坑 4、5：思考链混进正文会污染对话；转换不能丢东西。"""

    def setUp(self):
        self.doc = synthetic_product()
        self.out = to_openai.convert(self.doc)

    def test_message_roles_and_order(self):
        roles = [m["role"] for m in self.out["messages"]]
        self.assertEqual(roles, ["user", "assistant", "user", "assistant"])

    def test_thinking_not_in_content(self):
        a = self.out["messages"][1]
        self.assertIn("【合成样例】先检索两篇文档", a["reasoning"])
        self.assertNotIn("先检索两篇文档", a["content"])

    def test_tip_preserved(self):
        self.assertTrue(any("附加提示" in t for t in self.out["messages"][3].get("tips", [])))

    def test_citations_resolved_to_urls(self):
        a = self.out["messages"][1]
        self.assertTrue(a.get("citations"))
        urls = {s["url"] for c in a["citations"] for s in c["sources"]}
        self.assertIn("https://example.invalid/docs/a.pdf", urls)

    def test_marker_is_index_into_references_array(self):
        """
        守住坑 7：分享链接路径的 `[reference:N]` 里，N 是 references[] 的**下标**，
        不是 fragment id。把它当 id 解读，会让标记指向不相干的片段。
        """
        a = self.out["messages"][1]
        cites = {c["marker"]: c for c in a["citations"]}
        self.assertIn("[reference:0]", cites)
        self.assertIn("[reference:2]", cites)
        # 下标 0 → references[0] = {id:4} = 那个 TOOL_OPEN（文档甲）
        self.assertEqual(cites["[reference:0]"]["ref_id"], 4)
        self.assertEqual(cites["[reference:0]"]["sources"][0]["url"],
                         "https://example.invalid/docs/a.pdf")
        # 下标 2 → references[2] = {id:3} = 那个 TOOL_SEARCH（文档甲 + 文档乙）
        self.assertEqual(cites["[reference:2]"]["ref_id"], 3)
        self.assertEqual(len(cites["[reference:2]"]["sources"]), 2)

    def test_markers_and_citations_one_to_one(self):
        import re
        for m in self.out["messages"]:
            markers = re.findall(r"\[!?reference:\d+\]", m["content"])
            self.assertEqual(len(markers), len(m.get("citations") or []))

    def test_references_dedup_by_normalised_url(self):
        urls = [r["url"] for r in self.out["references"]]
        self.assertEqual(len(urls), len(set(urls)), "来源清单不得有重复 URL")

    def test_variants_preserved(self):
        """文档甲的两个摘要必须都留下——只留第一条就是丢数据。"""
        rec = next(r for r in self.out["references"]
                   if r["url"] == "https://example.invalid/docs/a.pdf")
        self.assertGreaterEqual(len(rec["variants"]), 1)
        snippets = {v.get("snippet") for v in rec["variants"]} | {rec.get("snippet")}
        self.assertIn("【合成样例】摘要甲-短", snippets)
        self.assertIn("【合成样例】摘要甲-长", snippets)

    def test_all_fragments_preserved(self):
        src = [f for m in self.doc["conversations"][0]["raw_messages"]
               for f in m["fragments"]]
        got = [f for m in self.out["messages"] for f in m.get("fragments", [])]
        self.assertEqual(len(got), len(src))

    def test_raw_container_preserved(self):
        self.assertIn("raw_messages", self.out)
        self.assertEqual(self.out["raw_messages"],
                         self.doc["conversations"][0]["raw_messages"])

    def test_slim_drops_bulk_but_keeps_content(self):
        slim = to_openai.convert(self.doc, slim=True)
        self.assertNotIn("raw_messages", slim)
        for m in slim["messages"]:
            self.assertNotIn("fragments", m)
            self.assertTrue(m["content"])
        self.assertFalse(slim["meta"]["lossless"])

    def test_content_verbatim(self):
        payload = self.doc["conversations"][0]
        reqs = [e["content"] for e in payload["transcript"] if e["type"] == "REQUEST"]
        rsps = [e["content"] for e in payload["transcript"] if e["type"] == "RESPONSE"]
        self.assertEqual(self.out["messages"][0]["content"], reqs[0])
        self.assertEqual(self.out["messages"][1]["content"], rsps[0])


# ============================================================
# 账号导出包路径：引用语义完全不同
# ============================================================

def synthetic_export_product():
    """
    账号导出包路径的形状：**不携带引用关系**。

    - RESPONSE fragment 只有 content / type，**没有 references[]**
    - 正文里的 `[!reference:N]` 里的 N 指向哪个页面，**无法还原**
      （被引用的多数是 TOOL_OPEN，而导出包不导出 TOOL_OPEN 载荷）

    → 所以正确行为是**如实标「该格式无法解析」+ 给候选来源池**，不是猜。
    """
    url_x = "https://example.invalid/x.html"
    url_y = "https://example.invalid/y.html"
    msg = {
        "id": "1", "message_id": 1,
        "message": {
            "inserted_at": 1700000000, "model": "",
            "fragments": [
                {"type": "REQUEST", "content": "【合成样例】导出包路径的问题"},
                {"type": "TOOL_SEARCH",
                 "queries": [{"query": "合成样例导出包查询词"}],
                 "results": [
                     {"url": url_x, "title": "【合成样例】X"},
                     {"url": url_y, "title": "【合成样例】Y"},
                 ]},
                {"type": "RESPONSE",
                 "content": "【合成样例】答案引用了X[!reference:0]与Y[!reference:1]。"},
            ],
        },
    }
    transcript = [
        {"seq": 0, "node_id": "1", "depth": 0, "type": "REQUEST", "role": "user",
         "label": "用户提问", "model": "", "timestamp": None, "sources": ["export"],
         "content": "【合成样例】导出包路径的问题"},
        {"seq": 1, "node_id": "1", "depth": 0, "type": "TOOL_SEARCH", "role": "tool",
         "label": "联网搜索", "model": "", "timestamp": None, "sources": ["export"],
         "queries": [{"query": "合成样例导出包查询词"}],
         "results": [
             {"url": url_x, "title": "【合成样例】X",
              "url_normalized": url_x, "url_anchor": None},
             {"url": url_y, "title": "【合成样例】Y",
              "url_normalized": url_y, "url_anchor": None},
         ]},
        {"seq": 2, "node_id": "1", "depth": 0, "type": "RESPONSE", "role": "assistant",
         "label": "模型回答", "model": "", "timestamp": None, "sources": ["export"],
         "content": "【合成样例】答案引用了X[!reference:0]与Y[!reference:1]。"},
    ]
    conv = {
        "id": "synthetic", "title": "【合成样例】导出包路径会话",
        "title_is_placeholder": False, "title_source": "export",
        "stats": {}, "attachments": [],
        "transcript": transcript,
        "raw_mapping": {"1": msg},
    }
    return {"_meta": {"schema": "rescued-session/v1", "platform": "deepseek-web-export"},
            "conversations": [conv]}


class TestExportPathCitations(unittest.TestCase):
    """
    守住坑 6：账号导出包路径**不携带引用关系**。

    导出包的 RESPONSE fragment 只有 content / type，**没有 references[]**；
    而被引用的片段绝大多数是 TOOL_OPEN（真正读过的页面），
    这些片段在导出包里**没有载荷**（导出格式根本没导出）。
    → 这条路径在原理上还原不出引用关系。

    ⚠️ 曾经试过「标记 N → 该消息搜索结果列表的第 N 个」，实测与分享侧真值
    71 个标记里 65 个对不上——那是**把来源猜错了**。
    错误的来源比没有来源更糟：用户会以为「这句话的依据是这篇」，而其实不是。
    所以正确行为是**如实说解析不了**，并附候选来源池。
    """

    def setUp(self):
        self.doc = synthetic_export_product()
        self.out = to_openai.convert(self.doc)

    def test_markers_not_guessed(self):
        """解析不了就标解析不了——不能猜。"""
        assistant = [m for m in self.out["messages"] if m["role"] == "assistant"][0]
        cites = assistant.get("citations") or []
        self.assertEqual(len(cites), 2, "两个行内标记都要出现在 citations 里")
        for c in cites:
            self.assertEqual(c["sources"], [], "没有可靠来源时不得编造")
            self.assertEqual(c["ref_type"], "unresolvable-in-export-format")
            self.assertTrue(c.get("note"))

    def test_candidates_offered(self):
        """解析不了也要给候选来源池（用户至少能自己去点）。"""
        assistant = [m for m in self.out["messages"] if m["role"] == "assistant"][0]
        cites = assistant.get("citations") or []
        cand_urls = {c2["url"] for c in cites for c2 in c.get("candidates") or []}
        self.assertIn("https://example.invalid/x.html", cand_urls)
        self.assertIn("https://example.invalid/y.html", cand_urls)

    def test_marker_text_preserved(self):
        assistant = [m for m in self.out["messages"] if m["role"] == "assistant"][0]
        self.assertEqual(assistant["citations"][0]["marker"], "[!reference:0]")

    def test_every_inline_marker_has_a_citation(self):
        import re
        for m in self.out["messages"]:
            markers = re.findall(r"\[!?reference:\d+\]", m["content"])
            cites = m.get("citations") or []
            self.assertEqual(len(markers), len(cites),
                             "行内标记数必须与解析出的引用数一致")

    def test_search_results_still_in_reference_pool(self):
        """来源池本身不受影响——页面还在，只是「哪句对应哪页」丢了。"""
        urls = {r["url"] for r in self.out["references"]}
        self.assertIn("https://example.invalid/x.html", urls)
        self.assertIn("https://example.invalid/y.html", urls)

    def test_raw_mapping_preserved(self):
        self.assertIn("raw_mapping", self.out)


# ============================================================
# 引用标记的「精确 vs 候选」与 marker_kind
# ============================================================

class TestCitationPrecision(unittest.TestCase):
    """
    守住坑 8：TOOL_SEARCH 片段的结果是「这一轮检索到的全部」，
    而 API 的 `references[N].urls` 给出的是**真正被引用的那几篇**。

    把全部结果塞进 `sources` 不算错（被引用的那篇确实在其中），
    但**指不出是哪一篇**——所以精确的进 `sources`，其余进 `candidates`。
    """

    def _product(self, refs, content, search_results):
        msg = {
            "message_id": 1, "role": "ASSISTANT", "inserted_at": 1700000000,
            "fragments": [
                {"id": 3, "type": "TOOL_SEARCH", "queries": [{"query": "合成样例"}],
                 "results": search_results},
                {"id": 5, "type": "RESPONSE", "content": content, "references": refs},
            ],
        }
        transcript = [
            {"seq": 0, "node_id": "1", "depth": 0, "type": "TOOL_SEARCH", "role": "tool",
             "label": "联网搜索", "model": "", "timestamp": None, "sources": ["share"],
             "queries": [{"query": "合成样例"}], "results": search_results},
            {"seq": 1, "node_id": "1", "depth": 0, "type": "RESPONSE", "role": "assistant",
             "label": "模型回答", "model": "", "timestamp": None, "sources": ["share"],
             "content": content, "references": refs},
        ]
        return {"_meta": {"schema": "rescued-session/v1", "platform": "deepseek-web-share"},
                "conversations": [{
                    "id": "synthetic", "title": "【合成样例】", "title_is_placeholder": False,
                    "title_source": "override", "stats": {}, "attachments": [],
                    "transcript": transcript, "raw_messages": [msg]}]}

    def test_declared_urls_are_the_exact_sources(self):
        u_a = "https://example.invalid/a.html"
        u_b = "https://example.invalid/b.html"
        doc = self._product(
            refs=[{"id": 3, "type": "TOOL_SEARCH", "urls": [u_b]}],
            content="【合成样例】引用了B[reference:0]。",
            search_results=[{"url": u_a, "title": "A"}, {"url": u_b, "title": "B"}])
        out = to_openai.convert(doc)
        a = [m for m in out["messages"] if m["role"] == "assistant"][0]
        cite = a["citations"][0]
        self.assertEqual([s["url"] for s in cite["sources"]], [u_b],
                         "精确来源必须是 API 声明的那几篇")
        cand_urls = {c["url"] for c in cite.get("candidates") or []}
        self.assertIn(u_a, cand_urls, "其余检索结果应进 candidates，不进 sources")

    def test_inline_marker_kind(self):
        doc = self._product(
            refs=[{"id": 3, "type": "TOOL_SEARCH", "urls": []}],
            content="【合成样例】引用[reference:0]。",
            search_results=[{"url": "https://example.invalid/a.html", "title": "A"}])
        out = to_openai.convert(doc)
        a = [m for m in out["messages"] if m["role"] == "assistant"][0]
        self.assertEqual(a["citations"][0]["marker_kind"], "inline")

    def test_no_marker_in_text_does_not_fabricate_inline_marker(self):
        """
        正文里没有行内标记时，**不能**拼一个 `[reference:N]` 出来——
        那会造出正文里根本不存在的标记（还可能与真的 `[reference:6]` 撞名）。
        """
        doc = self._product(
            refs=[{"id": 3, "type": "TOOL_SEARCH", "urls": []}],
            content="【合成样例】正文里没有任何行内标记。",
            search_results=[{"url": "https://example.invalid/a.html", "title": "A"}])
        out = to_openai.convert(doc)
        a = [m for m in out["messages"] if m["role"] == "assistant"][0]
        for c in a.get("citations") or []:
            self.assertEqual(c["marker_kind"], "fragment")
            self.assertTrue(c["marker"].startswith("[fragment:"),
                            "没有正文标记时只能用 fragment 形态，不能冒充 [reference:N]")

    def test_marker_not_duplicated_across_messages(self):
        """不同消息里的同名 fragment 不得产生同名 [reference:N] 标记。"""
        doc = self._product(
            refs=[{"id": 3, "type": "TOOL_SEARCH", "urls": []}],
            content="【合成样例】没有标记。",
            search_results=[{"url": "https://example.invalid/a.html", "title": "A"}])
        out = to_openai.convert(doc)
        markers = [c["marker"] for m in out["messages"] for c in (m.get("citations") or [])]
        self.assertEqual(len(markers), len(set(markers)), "标记不得重名")


class TestStrictOutput(unittest.TestCase):
    """守住坑 9：富产物**不能**直接 POST 给严格的 chat API；--strict 才是。"""

    def test_strict_is_role_content_only(self):
        import subprocess
        import sys as _sys
        import tempfile
        doc = synthetic_product()
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "p.json")
            o = os.path.join(td, "m.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False)
            r = subprocess.run(
                [_sys.executable, "-I",
                 os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "scripts", "to_openai.py"),
                 p, "--strict", "--out", o],
                capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            with open(o, encoding="utf-8") as f:
                msgs = json.load(f)
            self.assertIsInstance(msgs, list)
            for m in msgs:
                self.assertEqual(sorted(m.keys()), ["content", "role"])
                self.assertIn(m["role"], ("user", "assistant"))


class TestIdempotentReconvert(unittest.TestCase):
    """
    守住坑 10：把**已经转换过**的产物再喂回来，不能静默产出 0 条 message。

    静默丢数据比报错危险得多——用户会拿到一个空文件还以为「这段会话就这么长」。
    """

    def test_reconvert_keeps_messages(self):
        once = to_openai.convert(synthetic_product())
        twice = to_openai.convert(once)
        self.assertEqual(len(twice["messages"]), len(once["messages"]))
        self.assertTrue(twice["messages"])
        for a, b in zip(once["messages"], twice["messages"]):
            self.assertEqual(a["role"], b["role"])
            self.assertEqual(a["content"], b["content"])

    def test_reconvert_keeps_references(self):
        once = to_openai.convert(synthetic_product())
        twice = to_openai.convert(once)
        self.assertEqual(len(twice["references"]), len(once["references"]))

    def test_reconvert_keeps_reasoning(self):
        once = to_openai.convert(synthetic_product())
        twice = to_openai.convert(once)
        self.assertEqual(twice["messages"][1].get("reasoning"),
                         once["messages"][1].get("reasoning"))


# ============================================================
# 平台探测：只看第一行会漏掉 41.5% 的真实 Claude Code 会话
# ============================================================

class TestPlatformDetection(unittest.TestCase):
    """
    守住坑 11：真实 Claude Code transcript 的**第一行常常不是消息**。

    实测 142 个真实会话里 59 个（41.5%）首行是 last-prompt / mode /
    ai-title / file-history-snapshot 这类旁路记录；某些 fork/rewind 旁路
    transcript 里，第一条真正的 user/assistant 出现在**第 43 行甚至第 174 行**。
    只看第一行 → 判成 unknown → 静默按 codex 解析 → 输出一份全 N/A 的
    **假报告且 exit 0**。比崩溃更危险。
    """

    def _write(self, tmpdir, name, lines):
        path = os.path.join(tmpdir, name)
        with open(path, "w", encoding="utf-8") as f:
            for l in lines:
                f.write(json.dumps(l, ensure_ascii=False) + "\n")
        return path

    def test_detects_claude_when_first_line_is_side_record(self):
        import tempfile
        import session_extract
        with tempfile.TemporaryDirectory() as td:
            lines = [{"type": "last-prompt", "sessionId": "s1", "leafUuid": "u1"},
                     {"type": "mode", "sessionId": "s1", "mode": "default"},
                     {"type": "permission-mode", "sessionId": "s1"}]
            # 真正的消息出现在第 43 行
            for i in range(40):
                lines.append({"type": "attachment", "sessionId": "s1"})
            lines.append({"type": "user", "sessionId": "s1", "uuid": "u2",
                          "parentUuid": None, "message": {"role": "user"}})
            p = self._write(td, "session.jsonl", lines)
            self.assertEqual(session_extract.detect_platform(p), "claude")

    def test_detects_claude_when_message_is_very_late(self):
        import tempfile
        import session_extract
        with tempfile.TemporaryDirectory() as td:
            lines = [{"type": "ai-title", "sessionId": "s1"}]
            for i in range(300):
                lines.append({"type": "queue-operation", "sessionId": "s1"})
            lines.append({"type": "user", "sessionId": "s1", "uuid": "u2",
                          "message": {"role": "user"}})
            p = self._write(td, "late.jsonl", lines)
            self.assertEqual(session_extract.detect_platform(p), "claude")

    def test_detects_codex(self):
        import tempfile
        import session_extract
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, "c.jsonl",
                            [{"type": "session_meta", "id": "x"},
                             {"type": "event_msg", "message": "hi"}])
            self.assertEqual(session_extract.detect_platform(p), "codex")

    def test_unknown_when_nothing_recognizable(self):
        import tempfile
        import session_extract
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, "x.jsonl", [{"type": "whatever"}])
            self.assertEqual(session_extract.detect_platform(p), "unknown")

    def test_no_silent_fallback_to_codex(self):
        """
        认不出平台时**必须报错**，不能静默按 codex 解析出一份空报告。
        """
        import tempfile
        import session_extract
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, "x.jsonl", [{"type": "whatever"}])
            with self.assertRaises(ValueError):
                session_extract.extract(p, platform="unknown")


class TestEncodingTolerance(unittest.TestCase):
    """
    守住坑 12：Windows 上产物可能带 BOM 或被写成 UTF-16。

    - PowerShell 5.1 的 `>` 重定向写出 **UTF-16LE**（首字节 FF FE）
    - PowerShell 管道（两个原生命令之间）注入 **UTF-8 BOM**（EF BB BF）
    两种都必须能读回来。
    """

    def test_decode_plain_utf8(self):
        self.assertEqual(to_openai._decode_auto("你好".encode("utf-8")), "你好")

    def test_decode_utf8_bom(self):
        self.assertEqual(to_openai._decode_auto("﻿你好".encode("utf-8")),
                         "你好")

    def test_decode_utf16(self):
        self.assertEqual(to_openai._decode_auto("你好".encode("utf-16")),
                         "你好")

    def test_reads_bom_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "bom.json")
            with open(p, "wb") as f:
                f.write("﻿".encode("utf-8") + "{}".encode("utf-8"))
            with open(p, "rb") as f:
                self.assertEqual(to_openai._decode_auto(f.read()), "{}")


class TestDesensitizeScanner(unittest.TestCase):
    """
    守住坑 13：脱敏不能只靠字面量扫描。

    前两轮只按字面量扫（路径 / 分享 id / 标题），**漏掉了「照抄真实对象
    当示例」这一类**——真实会话的用户提问原文、真实附件的 file_id 与字节数
    都曾留在文档里。字面量扫描抓不到，只有 n-gram 比对能抓到。
    """

    def setUp(self):
        import desensitize_scan
        self.scan = desensitize_scan

    def _corpus_with(self, sentence):
        """构造一份合成的「导出包」语料。"""
        import tempfile
        td = tempfile.mkdtemp(prefix="ds_scan_test_")
        doc = [{
            "id": "c1", "title": "t",
            "mapping": {"root": {"id": "root", "message": {"fragments": [
                {"type": "REQUEST", "content": sentence},
            ]}}},
        }]
        p = os.path.join(td, "corpus.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
        return p

    def test_extracts_only_conversation_content(self):
        """只取 REQUEST/RESPONSE 正文，不把元数据当语料。"""
        p = self._corpus_with("【合成样例】这是一句足够长的自然语言提问内容用于测试。")
        texts = self.scan.load_corpus_texts([p])
        self.assertEqual(len(texts), 1)
        self.assertIn("自然语言提问", texts[0])

    def test_prose_filter_drops_code(self):
        """代码片段不当探针——它在仓库源码里必然命中，会淹没真泄漏。"""
        code = "def foo(x):\n    try:\n        return x + 1\n    except Exception as e:\n        raise ValueError(e)\n"
        p = self._corpus_with(code)
        texts = self.scan.load_corpus_texts([p])
        self.assertEqual(texts, [], "纯代码不该被当成散文探针")

    def test_ngram_finds_planted_leak(self):
        """把一段真实语料「抄」进 blob，n-gram 扫描必须抓到。"""
        sentence = "【合成样例】这句话必须被扫描器发现因为它像一句真话"
        p = self._corpus_with(sentence)
        texts = self.scan.load_corpus_texts([p])
        blobs = [("deadbeef", "前言\n这里抄了：" + sentence + "\n后记")]
        hits, probes = self.scan.ngram_scan(blobs, texts, 20, 10)
        self.assertGreater(probes, 0)
        self.assertTrue(hits, "抄进去的真实语料必须被抓到")

    def test_ngram_clean_when_absent(self):
        p = self._corpus_with("【合成样例】这段话不会出现在任何 blob 里所以不该命中。")
        texts = self.scan.load_corpus_texts([p])
        blobs = [("deadbeef", "完全无关的内容")]
        hits, _probes = self.scan.ngram_scan(blobs, texts, 20, 10)
        self.assertFalse(hits)

    def test_share_link_pattern_only_matches_real_ids(self):
        """格式说明 `/share/<id>` 合法；`/share/<真实id>` 才该报。"""
        self.assertIsNone(self.scan.SHARE_LINK_RE.search("https://chat.deepseek.com/share/<18位ID>"))
        self.assertIsNone(self.scan.SHARE_LINK_RE.search("https://chat.deepseek.com/share/<id>"))
        # ⚠️ 必须用**合成 id**：真实 share id 指向一个匿名可读的私人会话，
        # 把它写进公开仓库等于主动公开。测试素材一律合成。
        self.assertIsNotNone(self.scan.SHARE_LINK_RE.search(
            "https://chat.deepseek.com/share/synthetic0share0id0abc"))

    def test_this_file_contains_no_real_share_id(self):
        """
        自检：本文件里不得出现任何**真实**分享 id（合成 id 除外）。
        写测试时顺手抄一个真 id 进断言，是最容易犯也最致命的脱敏事故。
        """
        here = os.path.abspath(__file__)
        with open(here, encoding="utf-8") as f:
            text = f.read()
        import re
        for m in re.finditer(r'chat\.deepseek\.com/share/([A-Za-z0-9_-]{16,})', text):
            self.assertIn("synthetic", m.group(1),
                          "测试文件里出现了疑似真实的分享 id: {}".format(m.group(1)))



if __name__ == "__main__":
    unittest.main(verbosity=2)


# ============================================================
# Claude Code 会话：元信息必须从「任意事件」补齐
# ============================================================

class TestClaudeSessionMetadata(unittest.TestCase):
    """
    守住坑 14：首行不是消息的真实会话，元信息不能丢。

    实测 143 个真实 Claude Code 会话里 **59 个（41%）的首行不是消息**
    （是 mode / last-prompt / ai-title 这类旁路记录），而 cwd / gitBranch /
    version 往往就挂在那些旁路事件上。
    只从「首条 user 消息」取元信息 → 一半会话的 SessionID/CWD 全是 N/A。
    （与平台探测是同一个根因：首行不是消息。）
    """

    def _write(self, tmpdir, name, lines):
        path = os.path.join(tmpdir, name)
        with open(path, "w", encoding="utf-8") as f:
            for l in lines:
                f.write(json.dumps(l, ensure_ascii=False) + "\n")
        return path

    def test_metadata_from_side_records(self):
        import tempfile
        import extract_claude
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, "s.jsonl", [
                {"type": "mode", "sessionId": "sid-1", "mode": "default"},
                {"type": "last-prompt", "sessionId": "sid-1"},
                {"type": "attachment", "sessionId": "sid-1",
                 "cwd": "/tmp/proj", "gitBranch": "main", "version": "1.2.3"},
                {"type": "user", "sessionId": "sid-1",
                 "message": {"role": "user", "content": "【合成样例】你好"},
                 "timestamp": "2026-01-01T00:00:00Z"},
            ])
            r = extract_claude.extract(p)
            si = r["session_info"]
            self.assertEqual(si.get("sessionId"), "sid-1")
            self.assertEqual(si.get("cwd"), "/tmp/proj")
            self.assertEqual(si.get("branch"), "main")
            self.assertEqual(si.get("version"), "1.2.3")

    def test_metadata_merge_does_not_overwrite(self):
        import tempfile
        import extract_claude
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, "s2.jsonl", [
                {"type": "attachment", "sessionId": "sid-2", "cwd": "/tmp/a"},
                {"type": "attachment", "sessionId": "sid-2", "cwd": "/tmp/b",
                 "gitBranch": "dev"},
            ])
            r = extract_claude.extract(p)
            si = r["session_info"]
            self.assertEqual(si.get("cwd"), "/tmp/a", "先到的值不该被覆盖")
            self.assertEqual(si.get("branch"), "dev", "后来的字段仍要补上")

    def test_bom_file_is_readable(self):
        """带 UTF-8 BOM 的 jsonl 不该报 'Unexpected UTF-8 BOM'。"""
        import tempfile
        import extract_claude
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "bom.jsonl")
            with open(path, "w", encoding="utf-8-sig") as f:
                f.write(json.dumps({"type": "user", "sessionId": "s3",
                                    "message": {"role": "user", "content": "x"}},
                                   ensure_ascii=False) + "\n")
            r = extract_claude.extract(path)
            self.assertEqual(r["session_info"].get("sessionId"), "s3")


class TestScannerTrustworthiness(unittest.TestCase):
    """
    守住坑 15：**门禁本身必须可信**。

    一个专门为「不泄露」而写的工具，如果自己的门禁会假绿，
    那比没有门禁更危险——它给出「我检查过了」的假信号。

    三个曾经的假绿来源：
    - 按 **cwd** 定位仓库 → 在别的 git 仓库里跑只扫 1 个 blob 就报「干净」
    - **不扫提交信息** → 泄漏藏在 commit message 里照样绿灯
    - 扫描器**扫自己** → 黑名单模式必然命中自己，门禁永远红
    """

    def setUp(self):
        import desensitize_scan
        self.scan = desensitize_scan

    def test_repo_root_does_not_follow_cwd(self):
        """仓库定位必须跟着**脚本自身**，不跟 cwd。"""
        import os
        import tempfile
        root = self.scan.repo_root()
        self.assertTrue(os.path.isdir(os.path.join(root, ".git")),
                        "应定位到脚本所在的仓库，而不是进程 cwd")
        with tempfile.TemporaryDirectory() as td:
            cwd = os.getcwd()
            try:
                os.chdir(td)
                self.assertEqual(self.scan.repo_root(), root)
            finally:
                os.chdir(cwd)

    def test_bad_repo_path_is_error_not_clean(self):
        with self.assertRaises(RuntimeError):
            self.scan.repo_root("C:/definitely-not-a-real-dir-xyz")

    def test_scan_detects_planted_path(self):
        # ⚠️ 字面量**拼接构造**：测试夹具里也不能出现完整的真实路径形态，
        # 否则扫描器会把夹具当成泄露（门禁连自己都过不了）。
        planted = "C:" + chr(92) + "Users" + chr(92) + "someone" + chr(92) + "Documents"
        blobs = [("worktree:x.md", "见 " + planted + chr(92) + "secret.txt")]
        hits = self.scan.literal_scan(blobs)
        self.assertTrue(hits, "真实的个人路径必须被抓到")
        self.assertEqual(hits[0][0], "个人机器路径")

    def test_scan_detects_planted_uuid(self):
        # ⚠️ UUID **运行时随机生成**：不写任何固定字面量。
        # 固定字面量（哪怕用拼接藏起来）本身就是泄露——拼接只是让门禁看不见，
        # 那是**拿门禁当掩护**。要测「随机 UUID 会被抓到」就用真随机。
        import uuid
        synthetic = str(uuid.uuid4())
        self.assertFalse(self.scan._looks_synthetic_uuid(synthetic))
        blobs = [("worktree:x.md", "会话 " + synthetic + " 在这")]
        self.assertTrue([h for h in self.scan.literal_scan(blobs)
                         if h[0] == "疑似真实 UUID"], "真实 UUID 必须被抓到")

    def test_synthetic_uuid_not_flagged(self):
        """明显是凑出来的 UUID 不该报（否则夹具会把门禁弄红）。"""
        self.assertTrue(self.scan._looks_synthetic_uuid(
            "11111111-2222-3333-4444-555555555555"))
        blobs = [("worktree:x.md", "会话 11111111-2222-3333-4444-555555555555 在这")]
        self.assertFalse([h for h in self.scan.literal_scan(blobs)
                          if h[0] == "疑似真实 UUID"],
                         "合成 UUID 不该报（否则夹具会把门禁弄红）")

    def test_deobfuscation_sees_concatenated_literal(self):
        """
        守住坑 16：真实 id 写成 `"ab" + "cd"` 时，字面量扫描**看不见**——
        而这恰恰是作者最可能用的藏法。扫描器必须能**还原拼接**后再比对。
        """
        import uuid
        real = str(uuid.uuid4())
        parts = [real[i:i + 8] for i in range(0, len(real), 8)]
        hidden = " + ".join('"%s"' % p for p in parts)
        blobs = [("worktree:x.md", "会话 " + hidden + " 在这")]
        hits = [h for h in self.scan.literal_scan(blobs) if h[0] == "疑似真实 UUID"]
        self.assertTrue(hits, "拼接藏起来的真实 UUID 必须被抓到")

    def test_placeholder_path_not_flagged(self):
        """格式说明（`C:\\Users\\<用户名>\\`）不是泄露，不该报。"""
        blobs = [("worktree:x.md", r'路径形如 "<用户目录>"')]
        hits = self.scan.literal_scan(blobs)
        self.assertFalse([h for h in hits if h[0] == "个人机器路径"],
                         "占位形式不该报")

    def test_scanner_excludes_itself(self):
        """扫描器自身文件不得被自己的黑名单扫出来（按**路径**排除）。"""
        import random
        name = "WORKSTATION-LAPTOP-" + str(random.randint(10000000, 99999999))
        blobs = [("worktree:" + self.scan.SELF_FILE, "机器名形态 " + name),
                  ("worktree:other.py", "正常内容")]
        hits = self.scan.literal_scan(blobs)
        self.assertFalse([h for h in hits if h[0] == "疑似机器名"],
                         "扫描器自身必须被排除")

    def test_machine_shape_detected(self):
        """通用机器名形态（不含具体人名）应当被抓到。"""
        import random
        # ⚠️ 机器名**运行时生成**：写死字面量等于在仓库里留一个机器名形态
        name = "WORKSTATION-LAPTOP-" + str(random.randint(10000000, 99999999))
        blobs = [("worktree:x.md", "机器 " + name)]
        hits = self.scan.literal_scan(blobs)
        self.assertTrue([h for h in hits if h[0] == "疑似机器名"],
                        "机器名形态必须被抓到")

    def test_commit_messages_are_scanned(self):
        """提交信息也是泄漏面——必须纳入扫描对象。"""
        blobs = self.scan.collect_blobs(history=False, root=self.scan.repo_root())
        self.assertTrue(any("commit-messages" in str(t) for t, _ in blobs),
                        "提交信息必须在扫描对象里")

    def test_low_quality_probes_rejected(self):
        """纯空白/符号窗口不得成为探针（否则命中全是噪声）。"""
        self.assertFalse(self.scan._is_good_probe("          o"))
        self.assertFalse(self.scan._is_good_probe("   }  \n  "))
        self.assertFalse(self.scan._is_good_probe("a b c"))
        self.assertTrue(self.scan._is_good_probe("这是一句足够长的中文句子内容"))
        self.assertTrue(self.scan._is_good_probe("this is an english phrase here"))


class TestConsoleEncoding(unittest.TestCase):
    """
    守住坑 17：Windows 上「跟随控制台码页」曾经是**假的**。

    旧实现把 "utf-8" 放在候选列表第一位，而它必然可编码 → 函数恒返回 utf-8，
    算出来的码页从未被使用 → 在标准中文 Windows（cp936）上照样静默乱码。
    而所有测试都在 cp65001 下跑，**测不出来**。
    """

    def test_utf8_console(self):
        import ctypes
        import deepseek_common
        if os.name != "nt":
            self.skipTest("非 Windows")
        old = ctypes.windll.kernel32.GetConsoleOutputCP()
        try:
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            self.assertEqual(deepseek_common.console_encoding(), "utf-8")
        finally:
            ctypes.windll.kernel32.SetConsoleOutputCP(old)

    def test_legacy_console_page(self):
        import ctypes
        import deepseek_common
        if os.name != "nt":
            self.skipTest("非 Windows")
        old = ctypes.windll.kernel32.GetConsoleOutputCP()
        try:
            ctypes.windll.kernel32.SetConsoleOutputCP(936)
            enc = deepseek_common.console_encoding()
            self.assertNotEqual(enc, "utf-8",
                                "cp936 控制台下不能返回 utf-8（会被按 GBK 解码成乱码）")
            "中文".encode(enc)      # 必须是真实可用的编码
        finally:
            ctypes.windll.kernel32.SetConsoleOutputCP(old)


class TestOfflineRebuild(unittest.TestCase):
    """
    守住坑 18：分享链接随时可能失效/被删。

    实测那条会话的链接已经返回「share is deleted or banned」——
    没有 `--from-file` 离线重建入口，那条路径**再也回归不了**，
    「产物可复现」这条原则对分享侧就不成立。
    """

    def test_from_file_rebuilds_product(self):
        import tempfile
        import extract_deepseek_share as eds
        doc = {"code": 0, "data": {"biz_data": synthetic_biz()}}
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "orig.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False)
            payload = eds.build_payload(
                doc["data"]["biz_data"], doc["data"]["biz_data"]["messages"], [],
                share_id="offline")
            self.assertTrue(payload["transcript"])
            # extract() 的离线分支：from_file 存在时不联网
            result = eds.extract(None, from_file=p)
            self.assertTrue(result["conversations"][0]["transcript"])
            self.assertTrue(result["_meta"]["verification"]["ok"])

    def test_cli_requires_share_or_from_file(self):
        import subprocess
        import sys as _sys
        scripts = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "scripts")
        r = subprocess.run([_sys.executable, "-I",
                            os.path.join(scripts, "extract_deepseek_share.py"),
                            "--format", "json"],
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("share_id", (r.stderr or "") + (r.stdout or ""))


class TestCorpusProbes(unittest.TestCase):
    """
    守住坑 19：门禁对两类真实语料曾经**完全失明**——
    搜索结果的 snippet（发布说明原文、引文都在那里），以及英文语料。
    """

    def test_snippet_is_treated_as_corpus(self):
        import tempfile
        import desensitize_scan
        doc = {"data": {"biz_data": {"messages": [{
            "fragments": [{
                "type": "TOOL_SEARCH",
                "results": [{"url": "https://example.invalid/a",
                             "snippet": "这是一段只有搜索结果里才有的发布说明原文内容"}],
            }],
        }]}}}
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "c.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False)
            texts = desensitize_scan.load_corpus_texts([p])
        self.assertTrue(any("发布说明原文" in t for t in texts),
                        "搜索结果的 snippet 必须被当作语料")

    def test_english_probe_allowed(self):
        import desensitize_scan
        # ⚠️ 用**自己编的**英文句子：别把真实语料里的短语抄进测试——
        # 那等于把语料原文写进仓库（我犯过一次，扫描器当场抓出来）。
        self.assertTrue(desensitize_scan._is_good_probe(
            "upload silently failed after restart"))
        self.assertFalse(desensitize_scan._is_good_probe("   }  "))


class TestExplicitPlatformGuardrail(unittest.TestCase):
    """
    守住坑 20：显式 `--platform` 也必须有护栏。

    以前只有**自动探测**路径有「认不出就报错」的护栏；用户显式指定时
    护栏**永不触发** → 拿 Claude 会话按 codex 解析 → 得到一份
    **全是 N/A 的假报告**而且 exit 0。
    （实测三平台 × 三格式矩阵：只有 deepseek 因为解析器抛异常才偶然 exit 1。）
    """

    def _write(self, td, name, lines):
        p = os.path.join(td, name)
        with open(p, "w", encoding="utf-8") as f:
            for l in lines:
                f.write(json.dumps(l, ensure_ascii=False) + "\n")
        return p

    def _run(self, args):
        import subprocess
        import sys as _sys
        scripts = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "scripts")
        return subprocess.run([_sys.executable, "-I",
                               os.path.join(scripts, "session_extract.py")] + args,
                              capture_output=True, text=True)

    def test_wrong_platform_is_error_not_empty_report(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, "s.jsonl", [
                {"type": "mode", "sessionId": "s1", "mode": "default"},
                {"type": "user", "sessionId": "s1", "uuid": "u1",
                 "message": {"role": "user", "content": "【合成样例】你好"},
                 "timestamp": "2026-01-01T00:00:00Z"},
            ])
            r = self._run(["--platform", "codex", p])
            self.assertNotEqual(r.returncode, 0, "平台不符必须报错退出")
            self.assertIn("不一致", (r.stdout or "") + (r.stderr or ""))
            self.assertNotIn("N/A", r.stdout, "不能输出全 N/A 的假报告")

    def test_correct_platform_still_works(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = self._write(td, "s2.jsonl", [
                {"type": "mode", "sessionId": "s2", "mode": "default"},
                {"type": "user", "sessionId": "s2", "uuid": "u1",
                 "message": {"role": "user", "content": "【合成样例】你好"},
                 "timestamp": "2026-01-01T00:00:00Z"},
            ])
            r = self._run(["--platform", "claude", p])
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("CLAUDE", r.stdout)


class TestBareShareIdDetection(unittest.TestCase):
    """
    守住坑 21：门禁不能对**裸分享 id** 失明。

    本 skill 的产物里 `share_id` 就是一串**裸 token**
    （`_meta.source.share_id` / `payload.id`），不带域名——
    只匹配完整 URL 的规则对它完全失明，而「真实分享 id 不得出现」
    恰恰是最重要的那条红线。
    """

    def test_bare_share_id_detected(self):
        import random
        import string
        import desensitize_scan
        # ⚠️ 分享 id **运行时随机生成**：写死字面量等于在仓库里留一个
        # 「长得像真分享 id」的字符串（扫描器会当场把它抓出来）。
        ident = "".join(random.choice(string.ascii_lowercase + string.digits)
                        for _ in range(19))
        blobs = [("worktree:x.json", '{"share_id": "' + ident + '"}')]
        hits = [h for h in desensitize_scan.literal_scan(blobs)
                if "分享 id" in h[0]]
        self.assertTrue(hits, "裸 share id 必须被抓到")

    def test_placeholder_share_id_not_flagged(self):
        import desensitize_scan
        blobs = [("worktree:x.json", '{"share_id": "<18位ID>"}')]
        hits = [h for h in desensitize_scan.literal_scan(blobs)
                if "分享 id" in h[0]]
        self.assertFalse(hits, "占位形式不该报")

    def test_decoded_candidates_are_checked(self):
        """chr() / base64 藏起来的真实 id 也必须被抓到。"""
        import base64
        import random
        import string
        import desensitize_scan
        # 真实分享 id 约 19 位；运行时随机生成，既贴近真实输入形态，
        # 又不会在仓库里留下一个“长得像真 id”的字面量。
        ident = "".join(random.choice(string.ascii_lowercase + string.digits)
                        for _ in range(19))
        b64 = base64.b64encode(ident.encode()).decode()
        blobs = [("worktree:x.py", 'x = "' + b64 + '"')]
        cands = desensitize_scan._obfuscated_candidates(blobs[0][1])
        self.assertTrue(any(ident in c for c in cands),
                        "base64 藏起来的 id 必须被还原出来")
