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
    - 正文里的 `[!reference:N]` 里，N 是**该消息搜索结果列表的下标**
      （把该消息所有 TOOL_SEARCH/SEARCH 的 results 按顺序拍平后取第 N 个）
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
