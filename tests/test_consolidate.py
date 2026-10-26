"""巩固状态机的单元测试。

运行：
  python -m unittest discover -s tests -v
（在 memory-consolidation 目录下）
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from consolidate import (  # noqa: E402
    Candidate,
    RuleDecider,
    consolidate,
    similarity,
)
from extract_dsh import extract_from_text  # noqa: E402
from search import search  # noqa: E402
from store import Store, connect, utc_now  # noqa: E402


def cand(predicate: str, obj: str, *, kind: str = "preference",
         content: str | None = None, seq: int = 1) -> Candidate:
    return Candidate(
        kind=kind,
        subject="user",
        predicate=predicate,
        object=obj,
        content=content or f"user {predicate} {obj}",
        confidence=0.7,
        quote=content or obj,
        source_session="test-session",
        source_seq=seq,
    )


class ConsolidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        self.store = Store(self.conn, run_id="test-run")
        self.store.start_run()
        self.decider = RuleDecider()

    def run_one(self, c: Candidate):
        return consolidate(self.store, c, self.decider)

    # -- 基本状态迁移 --------------------------------------------------- #

    def test_first_is_insert(self) -> None:
        out = self.run_one(cand("prefers_editor", "vim"))
        self.assertEqual(out.action, "insert")
        self.assertEqual(self.store.count("active"), 1)
        mem = self.store.get(out.memory_id)
        self.assertIsNotNone(mem)
        assert mem is not None
        self.assertIsNone(mem.valid_to)
        self.assertEqual(len(self.store.evidence(mem.id)), 1)

    def test_duplicate_is_skip(self) -> None:
        first = self.run_one(cand("prefers_editor", "vim"))
        second = self.run_one(cand("prefers_editor", "vim", seq=2))
        self.assertEqual(second.action, "skip")
        self.assertEqual(second.memory_id, first.memory_id)
        self.assertEqual(self.store.count("active"), 1)
        # 证据累积，但条目没变多
        self.assertEqual(len(self.store.evidence(first.memory_id)), 2)

    def test_conflict_invalidates_old(self) -> None:
        old = self.run_one(cand("prefers_editor", "vim"))
        new = self.run_one(cand("prefers_editor", "neovim", seq=2))

        self.assertEqual(new.action, "invalidate")
        self.assertEqual(new.target_id, old.memory_id)

        old_mem = self.store.get(old.memory_id)
        new_mem = self.store.get(new.memory_id)
        assert old_mem is not None and new_mem is not None

        self.assertEqual(old_mem.status, "superseded")
        self.assertIsNotNone(old_mem.valid_to)
        self.assertEqual(old_mem.superseded_by, new_mem.id)
        self.assertEqual(new_mem.supersedes, old.memory_id)
        self.assertEqual(new_mem.status, "active")
        self.assertIsNone(new_mem.valid_to)

        # 只保留一条有效记忆
        self.assertEqual(self.store.count("active"), 1)
        self.assertEqual(self.store.count("superseded"), 1)

    def test_refinement_merges(self) -> None:
        first = self.run_one(cand("responds_in", "中文"))
        second = self.run_one(cand("responds_in", "中文简体", seq=2))
        self.assertEqual(second.action, "merge")
        self.assertEqual(second.memory_id, first.memory_id)
        mem = self.store.get(first.memory_id)
        assert mem is not None
        self.assertEqual(mem.object, "中文简体")
        self.assertEqual(self.store.count("active"), 1)

    def test_latin_substring_is_not_refinement(self) -> None:
        # 'vim' 是 'neovim' 的子串，但语义不同，必须判冲突
        self.run_one(cand("prefers_editor", "vim"))
        out = self.run_one(cand("prefers_editor", "neovim", seq=2))
        self.assertEqual(out.action, "invalidate")

    def test_different_predicate_is_independent(self) -> None:
        self.run_one(cand("prefers_editor", "vim"))
        out = self.run_one(cand("responds_in", "中文"))
        self.assertEqual(out.action, "insert")
        self.assertEqual(self.store.count("active"), 2)

    # -- 审计 ----------------------------------------------------------- #

    def test_audit_records_every_decision(self) -> None:
        self.run_one(cand("prefers_editor", "vim"))
        self.run_one(cand("prefers_editor", "vim", seq=2))
        self.run_one(cand("prefers_editor", "neovim", seq=3))
        rows = self.store.audit("test-run")
        self.assertEqual([r["action"] for r in rows], ["insert", "skip", "invalidate"])
        for row in rows:
            self.assertEqual(row["run_id"], "test-run")
            self.assertTrue(row["candidate_json"])
            self.assertTrue(row["decision_json"])

    # -- 检索 ----------------------------------------------------------- #

    def test_search_hits_and_excludes_superseded(self) -> None:
        self.run_one(cand("prefers_editor", "vim"))
        self.run_one(cand("prefers_editor", "neovim", seq=2))

        hits = search(self.conn, "neovim")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["object"], "neovim")

        # 已失效的 vim 不应出现在结果里
        self.assertEqual(search(self.conn, "vim"), [])

    def test_search_prefers_recent(self) -> None:
        old = self.run_one(cand("likes_topic", "海豚", seq=1))
        new = self.run_one(cand("likes_topic", "鲸鱼", seq=2))

        # 手动把旧条目的 last_seen_at 调到 200 天前
        past = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        self.conn.execute(
            "UPDATE memories SET last_seen_at = ? WHERE id = ?", (past, old.memory_id)
        )
        self.conn.commit()

        # 两条同 key 不同值本会冲突，这里人为恢复两条有效，只为验证排序权重
        self.conn.execute(
            "UPDATE memories SET status='active', valid_to=NULL WHERE id = ?",
            (old.memory_id,),
        )
        self.conn.commit()

        hits = search(self.conn, "user", limit=5)
        ids = [h["id"] for h in hits]
        self.assertIn(new.memory_id, ids)
        if old.memory_id in ids:
            self.assertLess(ids.index(new.memory_id), ids.index(old.memory_id))

    # -- 抽取 ----------------------------------------------------------- #

    def test_extract_rules(self) -> None:
        text = "我比较喜欢用 vim。以后都用中文回复。记住：周五不要安排会议。"
        got = extract_from_text(text)
        kinds = {k for k, _, _ in got}
        predicates = {p for _, p, _ in got}
        self.assertIn("preference", kinds)
        self.assertIn("prefers", predicates)
        self.assertIn("requires", predicates)
        self.assertIn("must_remember", predicates)

    def test_similarity_basic(self) -> None:
        self.assertGreater(similarity("中文简体", "中文"), 0.0)
        self.assertEqual(similarity("vim", "neovim"), 0.0)


if __name__ == "__main__":
    unittest.main()
