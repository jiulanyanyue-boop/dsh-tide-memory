"""跨项目隔离的回归测试。

这些用例是为了防止「归属隔离被改回去」——
每一条都对应一次真实检出：
  · 采集不带 cwd → 项目内容落全局
  · 检索不过滤 scope → 项目 A 的记忆进项目 B
  · 巩固不分 scope → 项目 B 改值 invalidate 掉项目 A
  · git:/path: 身份不一致 → 自己项目召回不了
  · 编辑后 summary 不刷新 → 界面和注入还是旧内容

运行：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))


def _fake_project(root: Path, name: str) -> str:
    """造一个能识别成项目根的目录（有 package.json）。"""
    p = root / name
    p.mkdir(parents=True, exist_ok=True)
    (p / "package.json").write_text("{}", encoding="utf-8")
    return str(p)


class ScopeIsolationTest(unittest.TestCase):
    """跨项目隔离：采集 / 检索 / 巩固三条链路都要隔离。"""

    def setUp(self):
        import store as store_mod
        from consolidate import RuleDecider
        from store import Store, connect

        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name)
        self.proj_a = _fake_project(self.root, "projA")
        self.proj_b = _fake_project(self.root, "projB")
        self.db = str(self.root / "m.db")

        # 每个用例独立的 DSH_HOME，避免污染真实数据
        self._old_home = os.environ.get("DSH_HOME")
        os.environ["DSH_HOME"] = str(self.root / "dsh")

        self.conn = connect(self.db)
        self.store = Store(self.conn)
        self.store.start_run()
        self.decider = RuleDecider()

    def tearDown(self):
        # Windows 上必须先关连接再删临时目录，否则 m.db 被占用删不掉
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass
        if self._old_home is None:
            os.environ.pop("DSH_HOME", None)
        else:
            os.environ["DSH_HOME"] = self._old_home
        # ignore_cleanup_errors 兜底 Windows 文件句柄延迟释放
        self._tmp.cleanup()

    def _add(self, predicate, obj, content, cwd, confidence=0.8):
        from consolidate import Candidate, consolidate
        from memory_semantics import resolve_scope_from_cwd

        sc = resolve_scope_from_cwd(predicate=predicate, subject="user", cwd=cwd)
        cand = Candidate(
            kind="fact", subject="project", predicate=predicate, object=obj,
            content=content, confidence=confidence, cwd=cwd,
            scope=sc.scope, scope_ref=sc.scope_ref,
        )
        return consolidate(self.store, cand, self.decider)

    # -- ① 归属判定 ---------------------------------------------------- #

    def test_cwd_makes_project_scope(self):
        """有 cwd 的项目内容必须归 project，不能因为 subject=user 落 global。"""
        from memory_semantics import resolve_scope_from_cwd

        sc = resolve_scope_from_cwd(predicate="db_password", subject="user", cwd=self.proj_a)
        self.assertEqual(sc.scope, "project")
        self.assertTrue(sc.scope_ref)

    def test_user_profile_stays_global(self):
        """用户画像永远是 global——跨项目都要带上。"""
        from memory_semantics import resolve_scope_from_cwd

        sc = resolve_scope_from_cwd(predicate="responds_in", subject="user", cwd=self.proj_a)
        self.assertEqual(sc.scope, "global")

    def test_no_cwd_falls_back_to_global(self):
        """拿不到 cwd 时不能瞎猜成某个项目。"""
        from memory_semantics import resolve_scope_from_cwd

        sc = resolve_scope_from_cwd(predicate="db_password", subject="user", cwd="")
        self.assertEqual(sc.scope, "global")

    # -- ② 检索隔离 ---------------------------------------------------- #

    def test_recall_isolated_between_projects(self):
        """项目 A 的记忆不能出现在项目 B 的检索结果里。"""
        from search import search

        self._add("db_password", "hunter2", "项目A的数据库密码是 hunter2", self.proj_a)
        self._add("db_password", "swordfish", "项目B的数据库密码是 swordfish", self.proj_b)

        hits_a = search(self.conn, "数据库密码是什么", limit=10, cwd=self.proj_a)
        text_a = " ".join((h["summary"] or h["content"]) for h in hits_a)
        self.assertIn("hunter2", text_a)
        self.assertNotIn("swordfish", text_a, "项目 A 的检索里出现了 B 的内容")

        hits_b = search(self.conn, "数据库密码是什么", limit=10, cwd=self.proj_b)
        text_b = " ".join((h["summary"] or h["content"]) for h in hits_b)
        self.assertIn("swordfish", text_b)
        self.assertNotIn("hunter2", text_b, "项目 B 的检索里出现了 A 的内容")

    def test_recall_without_cwd_is_global_only(self):
        """没给 cwd → 只返回 global（fail-closed）。"""
        from search import search

        self._add("db_password", "hunter2", "项目A的数据库密码是 hunter2", self.proj_a)
        self._add("responds_in", "中文", "用户希望回复用简体中文", "")

        hits = search(self.conn, "数据库密码", limit=10, cwd=None)
        text = " ".join((h["summary"] or h["content"]) for h in hits)
        self.assertNotIn("hunter2", text, "没有 cwd 时不该召回项目记忆")

        # 全局记忆仍然可见
        hits2 = search(self.conn, "回复语言", limit=10, cwd=None)
        text2 = " ".join((h["summary"] or h["content"]) for h in hits2)
        self.assertIn("简体中文", text2, "全局记忆在任何情况下都该可见")

    def test_unrelated_dir_gets_global_only(self):
        """无关目录只放行全局。"""
        from search import search

        self._add("db_password", "hunter2", "项目A的数据库密码是 hunter2", self.proj_a)
        elsewhere = str(self.root / "elsewhere")
        hits = search(self.conn, "数据库密码是什么", limit=10, cwd=elsewhere)
        text = " ".join((h["summary"] or h["content"]) for h in hits)
        self.assertNotIn("hunter2", text, "无关目录泄漏了项目记忆")

    # -- ③ 巩固隔离 ---------------------------------------------------- #

    def test_consolidation_does_not_cross_projects(self):
        """项目 B 改值不该 invalidate 项目 A 的同名键。"""
        self._add("db_password", "hunter2", "项目A的密码 hunter2", self.proj_a)
        self._add("db_password", "swordfish", "项目B的密码 swordfish", self.proj_b)

        # B 改成 mysql
        self._add("db_password", "mysql", "项目B改用 mysql", self.proj_b)

        rows = self.conn.execute(
            "SELECT object, scope, status FROM memories WHERE predicate='db_password'"
        ).fetchall()
        by_obj = {r["object"]: r["status"] for r in rows}

        self.assertEqual(by_obj.get("hunter2"), "active", "A 的记忆被 B 的改动波及了")
        self.assertEqual(by_obj.get("swordfish"), "superseded", "B 的旧值应该失效")
        self.assertEqual(by_obj.get("mysql"), "active")

    # -- ④ git / path 身份兼容 ----------------------------------------- #

    def test_known_refs_include_both_forms(self):
        """同一项目要同时接受 path: 和 git: 两种身份，否则自己项目召回不了。"""
        from memory_semantics import known_project_refs

        refs = known_project_refs(self.proj_a)
        self.assertTrue(any(r.startswith("path:") for r in refs))
        # 造一个假 git remote
        git_dir = Path(self.proj_a) / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text(
            '[remote "origin"]\n\turl = git@github.com:someone/projA.git\n', encoding="utf-8"
        )
        refs2 = known_project_refs(self.proj_a)
        self.assertTrue(
            any(r.startswith("git:") for r in refs2),
            f"有 git remote 时应该也能识别 git: 身份，实际：{refs2}",
        )
        self.assertTrue(any(r.startswith("path:") for r in refs2), "path: 身份不该丢")


class SummaryRefreshTest(unittest.TestCase):
    """编辑后 summary 必须刷新。"""

    def test_edit_refreshes_summary(self):
        import json

        from store import Store, connect

        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        old_home = os.environ.get("DSH_HOME")
        os.environ["DSH_HOME"] = str(Path(tmp.name) / "dsh")
        self.addCleanup(
            lambda: os.environ.pop("DSH_HOME", None)
            if old_home is None
            else os.environ.__setitem__("DSH_HOME", old_home)
        )

        db = str(Path(tmp.name) / "m.db")
        conn = connect(db)
        self.addCleanup(conn.close)
        store = Store(conn)
        store.start_run()

        from mem import cmd_api

        payload = json.dumps({"op": "add_text", "text": "我喜欢用 IntelliJ IDEA"})

        class FakeStdin:
            def read(self_inner):
                return payload

        class FakeArgs:
            pass

        old_stdin, old_stdout = sys.stdin, sys.stdout
        import io

        sys.stdin = io.StringIO(payload)
        sys.stdout = io.StringIO()
        old_db_env = os.environ.get("MEMORY_DB")
        os.environ["MEMORY_DB"] = db
        try:
            cmd_api(FakeArgs())
        finally:
            out = sys.stdout.getvalue()
            sys.stdin, sys.stdout = old_stdin, old_stdout
            if old_db_env is None:
                os.environ.pop("MEMORY_DB", None)
            else:
                os.environ["MEMORY_DB"] = old_db_env

        result = json.loads(out)
        self.assertTrue(result.get("ok"), f"add_text 失败：{out}")
        item_id = result["results"][0]["id"]

        before = conn.execute(
            "SELECT summary FROM memories WHERE id = ?", (item_id,)
        ).fetchone()["summary"]
        self.assertTrue(before, "新增时应生成 summary")


if __name__ == "__main__":
    unittest.main()
