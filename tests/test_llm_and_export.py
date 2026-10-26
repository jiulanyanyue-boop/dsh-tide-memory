"""LLM 抽取解析、候选池、导出的测试。"""

from __future__ import annotations

import json
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from consolidate import Candidate, RuleDecider, consolidate  # noqa: E402
from export import export, slugify  # noqa: E402
from extract_llm import (  # noqa: E402
    extract,
    messages_from_events,
    normalize_predicate,
    parse_candidates,
)
from llm import (  # noqa: E402
    LlmConfig, LlmClient, LlmError, _NoRedirectHandler, extract_json, validate_base_url,
)
from consolidate import LlmDecider  # noqa: E402
import mem as mem_cli  # noqa: E402
from store import Store, connect  # noqa: E402


class JsonExtractionTest(unittest.TestCase):
    def test_plain_json(self) -> None:
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_fenced_json(self) -> None:
        text = '好的，结果如下：\n```json\n[{"kind": "fact"}]\n```\n以上。'
        self.assertEqual(extract_json(text), [{"kind": "fact"}])

    def test_noisy_prefix(self) -> None:
        text = 'think... 输出 [{"a": "带]括号"}] 结束'
        self.assertEqual(extract_json(text), [{"a": "带]括号"}])

    def test_empty_raises(self) -> None:
        with self.assertRaises(LlmError):
            extract_json("   ")


class LlmSecurityBoundaryTest(unittest.TestCase):
    def test_remote_https_endpoint_is_allowed(self) -> None:
        self.assertEqual(validate_base_url("https://api.example.test/v1/"),
                         "https://api.example.test/v1")
        self.assertEqual(LlmClient(LlmConfig("https://api.example.test/v1", "m")).base_url,
                         "https://api.example.test/v1")

    def test_loopback_http_endpoints_are_allowed(self) -> None:
        for url in ("http://127.0.0.1:11434/v1", "http://[::1]:11434/v1",
                    "http://localhost:11434/v1"):
            self.assertEqual(validate_base_url(url), url)
            self.assertEqual(LlmDecider(url, "m").base_url, url)

    def test_remote_http_and_non_http_schemes_are_rejected(self) -> None:
        for url in ("http://llm.example.test/v1", "http://192.0.2.8/v1",
                    "file:///tmp/llm", "ftp://llm.example.test/v1"):
            with self.subTest(url=url), self.assertRaises(LlmError):
                LlmClient(LlmConfig(url, "m", api_key="secret"))
            with self.subTest(decider_url=url), self.assertRaises(LlmError):
                LlmDecider(url, "m", api_key="secret")

    def test_endpoint_userinfo_and_query_are_rejected(self) -> None:
        for url in ("https://user:pass@llm.example.test/v1",
                    "https://llm.example.test/v1?redirect=http://evil"):
            with self.subTest(url=url), self.assertRaises(LlmError):
                validate_base_url(url)

    def test_redirects_are_never_followed_for_authenticated_requests(self) -> None:
        request = __import__("urllib.request", fromlist=["Request"]).Request(
            "https://llm.example.test/v1/chat/completions",
            headers={"Authorization": "Bearer test-secret"},
        )
        handler = _NoRedirectHandler()
        self.assertIsNone(handler.redirect_request(
            request, None, 302, "Found", {}, "http://attacker.example/collect"
        ))

    def test_cli_rejects_legacy_secret_argument(self) -> None:
        self.assertEqual(mem_cli.main(["llm", "set", "--api-key", "secret"]), 2)

    def test_cli_accepts_key_over_stdin(self) -> None:
        import llm as llm_module

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "llm.json"
            original_path = llm_module.CONFIG_PATH
            original_stdin = sys.stdin
            original_env = {key: os.environ.get(key) for key in (
                "MEM_LLM_BASE_URL", "MEM_LLM_MODEL", "MEM_LLM_API_KEY"
            )}
            try:
                llm_module.CONFIG_PATH = target
                for key in original_env:
                    os.environ.pop(key, None)
                sys.stdin = io.StringIO("stdin-secret\n")
                self.assertEqual(mem_cli.main([
                    "llm", "set", "--base-url", "https://api.example.test/v1",
                    "--model", "m", "--api-key-stdin",
                ]), 0)
                self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["api_key"],
                                 "stdin-secret")
            finally:
                llm_module.CONFIG_PATH = original_path
                sys.stdin = original_stdin
                for key, value in original_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


class ParseCandidatesTest(unittest.TestCase):
    def test_valid_and_invalid(self) -> None:
        payload = [
            {"kind": "preference", "predicate": "prefers_editor", "object": "neovim",
             "content": "用户偏好 neovim", "confidence": 0.9, "source_seq": 9},
            {"kind": "unknown", "predicate": "x", "object": "y"},          # kind 非法
            {"kind": "fact", "predicate": "Bad Predicate", "object": "y"},  # 谓词非法
            {"kind": "fact", "predicate": "uses_db", "object": "x"},        # object 太短
            {"kind": "fact", "predicate": "uses_db", "object": "sqlite",
             "source_seq": 999},                                            # seq 编的
        ]
        got, dropped = parse_candidates(
            payload, session_id="s1", seq_texts={9: "我喜欢 neovim", 12: "用 sqlite"}
        )
        self.assertEqual(dropped, 3)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0].confidence, 0.9)
        self.assertEqual(got[0].source_seq, 9)
        # 越界 seq 被丢弃，但靠 content 反查到了真正来源
        self.assertEqual(got[1].source_seq, 12)
        self.assertEqual(got[1].kind, "fact")

    def test_single_object_payload(self) -> None:
        payload = {"kind": "fact", "predicate": "uses_db", "object": "sqlite",
                   "content": "项目用 sqlite"}
        got, dropped = parse_candidates(payload, session_id="s1", seq_texts={})
        self.assertEqual(dropped, 0)
        self.assertEqual(len(got), 1)

    def test_predicate_value_stripped(self) -> None:
        self.assertEqual(normalize_predicate("prefers_neovim", "neovim"), "prefers")
        self.assertEqual(normalize_predicate("db_engine_sqlite", "sqlite"), "db_engine")
        self.assertEqual(normalize_predicate("prefers_editor", "neovim"), "prefers_editor")
        # 中文对象不参与剥离，避免误伤
        self.assertEqual(normalize_predicate("responds_in", "简体中文"), "responds_in")

    def test_seq_inferred_from_quote(self) -> None:
        payload = {"kind": "preference", "predicate": "prefers_editor", "object": "neovim",
                   "content": "用户偏好 neovim", "quote": "我比较喜欢用 neovim 写代码",
                   "source_seq": 999}
        got, _ = parse_candidates(
            payload, session_id="s1",
            seq_texts={10: "我比较喜欢用 neovim 写代码，vim 也想保留。"},
        )
        self.assertEqual(got[0].source_seq, 10)


class FakeClient:
    def __init__(self, payloads: list) -> None:
        self.payloads = payloads
        self.calls = 0

    def chat_json(self, system: str, user) -> object:
        payload = self.payloads[min(self.calls, len(self.payloads) - 1)]
        self.calls += 1
        return payload


class ExtractTest(unittest.TestCase):
    def test_batching_and_dedupe(self) -> None:
        msgs = [
            {"seq": 1, "time": "2026-10-01T00:00:00+00:00", "text": "我喜欢 vim"},
            {"seq": 2, "time": "2026-10-02T00:00:00+00:00", "text": "还是 vim 好"},
        ]
        item = {"kind": "preference", "predicate": "prefers_editor", "object": "vim",
                "content": "用户偏好 vim", "confidence": 0.8, "source_seq": 1}
        client = FakeClient([[item], [dict(item, source_seq=2, confidence=0.7)]])
        report = extract(msgs, session_id="s1", client=client, batch_size=1)
        self.assertTrue(report.used_llm)
        self.assertEqual(report.batches, 2)
        self.assertEqual(len(report.candidates), 1)  # 同指纹去重
        self.assertEqual(report.candidates[0].confidence, 0.8)  # 保留高置信度

    def test_fallback_on_error(self) -> None:
        class Broken:
            def chat_json(self, system, user):
                raise LlmError("boom")

        msgs = [{"seq": 1, "time": None, "text": "我比较喜欢用 vim"}]
        report = extract(msgs, session_id="s1", client=Broken(), batch_size=1)
        self.assertTrue(report.candidates)  # 规则兜底救回来
        self.assertEqual(report.candidates[0].subject, "user")

    def test_messages_from_events_filters_runtime(self) -> None:
        events = [
            {"type": "user/message", "seq": 1, "time": 1,
             "data": {"source": {"kind": "user"}, "content": [{"type": "text", "text": "hi"}]}},
            {"type": "user/message", "seq": 2, "time": 2,
             "data": {"source": {"kind": "runtime-context"},
                      "content": [{"type": "text", "text": "runtime"}]}},
            {"type": "assistant/message", "seq": 3,
             "data": {"message": {"content": [{"type": "text", "text": "yo"}]}}},
        ]
        msgs = messages_from_events(events)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["seq"], 1)


class CandidatePoolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        self.store = Store(self.conn, run_id="pool-run")
        self.store.start_run()
        self.decider = RuleDecider()

    def test_low_confidence_goes_to_pool(self) -> None:
        cand = Candidate("preference", "user", "prefers_editor", "helix",
                         "用户可能喜欢 helix", confidence=0.3)
        out = consolidate(self.store, cand, self.decider)
        mem = self.store.get(out.memory_id)
        assert mem is not None
        self.assertEqual(mem.status, "candidate")
        # 候选池不进 active 统计
        self.assertEqual(self.store.count("active"), 0)
        self.assertEqual(self.store.count("candidate"), 1)

    def test_later_evidence_promotes(self) -> None:
        low = Candidate("preference", "user", "prefers_editor", "helix",
                        "用户可能喜欢 helix", confidence=0.3)
        first = consolidate(self.store, low, self.decider)
        strong = Candidate("preference", "user", "prefers_editor", "helix",
                           "用户确认偏好 helix", confidence=0.9)
        second = consolidate(self.store, strong, self.decider)
        self.assertEqual(second.memory_id, first.memory_id)
        mem = self.store.get(first.memory_id)
        assert mem is not None
        self.assertEqual(mem.status, "active")

    def test_low_confidence_conflict_overwrites_pool_entry(self) -> None:
        low = Candidate("preference", "user", "prefers_editor", "helix",
                        "用户可能喜欢 helix", confidence=0.3)
        consolidate(self.store, low, self.decider)
        other = Candidate("preference", "user", "prefers_editor", "zed",
                          "用户可能喜欢 zed", confidence=0.3)
        out = consolidate(self.store, other, self.decider)
        self.assertEqual(out.action, "merge")
        self.assertEqual(self.store.count("candidate"), 1)
        self.assertEqual(self.store.count("active"), 0)
        mem = self.store.get(out.memory_id)
        assert mem is not None
        self.assertEqual(mem.object, "zed")


class ExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="dsh-tide-memory-export-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_export_idempotent(self) -> None:
        conn = connect(":memory:")
        store = Store(conn, run_id="exp-run")
        store.start_run()
        decider = RuleDecider()
        consolidate(store, Candidate("preference", "user", "prefers_editor", "vim",
                                     "用户偏好 vim", confidence=0.8), decider)
        consolidate(store, Candidate("preference", "user", "prefers_editor", "neovim",
                                     "用户改用 neovim", confidence=0.9), decider)

        tmp = str(self.tmp)
        first = export(conn, tmp)
        self.assertEqual(first["total"], 2)
        self.assertGreaterEqual(first["written"], 3)  # 2 条 + INDEX

        second = export(conn, tmp)
        self.assertEqual(second["written"], 0)  # 内容没变，不重写

        files = list(Path(tmp).rglob("*.md"))
        self.assertTrue(any(p.name == "INDEX.md" for p in files))
        pref_files = [p for p in files if p.parent.name == "preference"]
        self.assertEqual(len(pref_files), 2)
        body = pref_files[0].read_text(encoding="utf-8")
        self.assertIn("valid_from:", body)
        self.assertIn("## 证据", body)

    def test_slugify_keeps_chinese(self) -> None:
        self.assertEqual(slugify("用户偏好：vim/neovim"), "用户偏好-vim-neovim")


if __name__ == "__main__":
    unittest.main()
