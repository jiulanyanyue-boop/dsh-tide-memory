"""迁移 002 的单元测试：闸门 / 归属 / 生命周期 / 可读性 / 检索打分。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from consolidate import Candidate, RuleDecider, consolidate  # noqa: E402
from memory_semantics import (  # noqa: E402
    SCOPE_GLOBAL,
    SCOPE_PROJECT,
    SCOPE_WORKSPACE,
    DependsOn,
    Scope,
    dependency_alive,
    make_summary,
    project_ref,
    resolve_scope,
    ttl_days_for,
    worth_remembering,
    workspace_ref,
)
from search import coverage, coverage_core, search  # noqa: E402
from store import Store, connect  # noqa: E402


class GateTest(unittest.TestCase):
    """三问闸门：值得记吗。"""

    def test_junk_memory_blocked(self):
        """那条真实的垃圾记忆必须被挡住。"""
        g = worth_remembering(
            kind="preference",
            predicate="requires",
            obj="也可以自检一遍安全相关问题：综合结论：当前插件不建议直接发布到 GitHub",
            content="用户提到：也可以自检一遍安全相关问题",
            quote="你看看，这是codex安全检测后的问题…",
        )
        self.assertFalse(g.ok)

    def test_vague_subject_blocked(self):
        g = worth_remembering(
            kind="fact", predicate="misc", obj="它应该改成那样", content="它应该改成那样"
        )
        self.assertFalse(g.ok)
        self.assertEqual(g.code, "no-subject")

    def test_vapid_statement_blocked(self):
        g = worth_remembering(
            kind="fact", predicate="misc", obj="这个项目中要小心", content="这个项目中要小心"
        )
        self.assertFalse(g.ok)

    def test_non_english_predicate_blocked(self):
        g = worth_remembering(kind="fact", predicate="中文键", obj="值", content="值")
        self.assertFalse(g.ok)
        self.assertEqual(g.code, "bad-predicate")

    def test_long_object_blocked(self):
        long_text = "这是一段非常长的内容用来测试长度的上限是否生效" * 4
        g = worth_remembering(
            kind="fact", predicate="huge", obj=long_text, content=long_text
        )
        self.assertFalse(g.ok)
        self.assertEqual(g.code, "long-object")

    def test_good_memories_pass(self):
        good = [
            ("preference", "user_name", "刘润", "用户叫刘润"),
            ("preference", "prefers_editor", "IntelliJ IDEA", "用户日常主力 IDE 是 IntelliJ IDEA"),
            ("fact", "security_status", "fixed", "归档插件删除接口已修复路径穿越问题"),
            ("fact", "plugin_name", "archive-manager", "归档会话管理插件 archive-manager 按工作区分组"),
        ]
        for kind, pred, obj, content in good:
            g = worth_remembering(kind=kind, predicate=pred, obj=obj, content=content)
            self.assertTrue(g.ok, f"误伤了好记忆：{pred} -> {g.reason}")


class ScopeTest(unittest.TestCase):
    """归属判定：这条记忆属于谁。"""

    def test_user_predicate_is_global(self):
        s = resolve_scope(predicate="user_name", subject="user", cwd=r"E:\work")
        self.assertEqual(s.scope, SCOPE_GLOBAL)
        self.assertEqual(s.scope_ref, "")

    def test_env_predicate_is_workspace(self):
        s = resolve_scope(predicate="dsh_install_path", subject="project", cwd=r"E:\work")
        self.assertEqual(s.scope, SCOPE_WORKSPACE)
        self.assertTrue(s.scope_ref.startswith("ws:"))

    def test_mentioned_path_binds_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "package.json").write_text("{}", encoding="utf-8")
            s = resolve_scope(
                predicate="plugin_features", subject="project", cwd=r"E:\work",
                mentioned_path=tmp,
            )
            self.assertEqual(s.scope, SCOPE_PROJECT)
            self.assertTrue(s.scope_ref.startswith(("path:", "git:")))

    def test_workspace_ref_stable(self):
        """同一路径不同写法应得到同一身份（大小写、斜杠、尾斜杠都不敏感）。"""
        import os

        if os.name == "nt":
            a = workspace_ref(r"C:\Work\MyProject")
            b = workspace_ref(r"c:/WORK/myproject//")
        else:
            a = workspace_ref("/work/myproject")
            b = workspace_ref("/work/myproject/")
        self.assertEqual(a, b, "同一路径不同写法应得到同一身份")

    def test_project_ref_without_git_uses_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "package.json").write_text("{}", encoding="utf-8")
            ref = project_ref(tmp)
            self.assertTrue(ref.startswith("path:"))


class LifecycleTest(unittest.TestCase):
    """生命周期：依赖消失自动失效。"""

    def test_dependency_alive_when_path_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            dep = DependsOn("path", tmp)
            alive, _ = dependency_alive(dep)
            self.assertTrue(alive)

    def test_dependency_dead_when_path_missing(self):
        dep = DependsOn("path", r"C:\__definitely_not_exist__\xyz")
        alive, why = dependency_alive(dep)
        self.assertFalse(alive)
        self.assertIn("不存在", why)

    def test_no_dependency_always_alive(self):
        alive, _ = dependency_alive(DependsOn("", ""))
        self.assertTrue(alive)

    def test_depends_on_parse_roundtrip(self):
        d = DependsOn.parse(r"path:C:\a\b")
        self.assertEqual(d.type, "path")
        self.assertEqual(d.serialize(), r"path:C:\a\b")

    def test_ttl_by_kind(self):
        self.assertIsNone(ttl_days_for("preference", SCOPE_GLOBAL))
        self.assertIsNone(ttl_days_for("reflection", SCOPE_GLOBAL))
        self.assertEqual(ttl_days_for("episode", SCOPE_GLOBAL), 90)
        self.assertIsNotNone(ttl_days_for("fact", SCOPE_PROJECT))


class SummaryTest(unittest.TestCase):
    """可读性：summary 必须是给人看的话。"""

    def test_summary_uses_content(self):
        s = make_summary(
            kind="preference", predicate="responds_in", obj="简体中文",
            content="用户希望回复用简体中文",
        )
        self.assertIn("简体中文", s)

    def test_summary_falls_back_to_label(self):
        s = make_summary(
            kind="preference", predicate="prefers_editor", obj="IDEA", content="IDEA",
        )
        self.assertIn("常用编辑器", s, "object 与 content 相同时应使用中文谓词标签")

    def test_summary_marks_scope(self):
        s = make_summary(
            kind="fact", predicate="plugin_name", obj="x", content="插件 x",
            scope=Scope(SCOPE_PROJECT, "path:abc"),
        )
        self.assertIn("项目", s)

    def test_summary_strips_useless_prefix(self):
        s = make_summary(
            kind="preference", predicate="user_name", obj="刘润",
            content="用户提到：刘润",
        )
        self.assertFalse(s.startswith("用户提到"))


class RecallScoreTest(unittest.TestCase):
    """检索打分：长记忆不该被稀释。"""

    QUERY = "编辑器偏好"
    SHORT = "用户偏好 neovim"
    LONG = (
        "用户偏好 neovim 编辑器，因为它启动快、插件生态好、终端内可用、"
        "配置用 Lua、迁移成本低、支持 LSP 和 tree-sitter"
    )

    def test_coverage_not_penalized_by_length(self):
        """长记忆的覆盖率应 >= 短记忆（与 Jaccard 相反）。"""
        c_short = coverage(self.QUERY, self.SHORT)
        c_long = coverage(self.QUERY, self.LONG)
        self.assertGreaterEqual(
            c_long, c_short,
            "覆盖率不应惩罚长记忆（这正是要修的 Jaccard 缺陷）",
        )

    def test_long_memory_still_recalled(self):
        """关键回归：长记忆不能因为长度被过滤掉。"""
        self.assertGreater(coverage(self.QUERY, self.LONG), 0.0)

    def test_recall_recent_memory_by_intent(self):
        """主人问「记忆有什么优化空间」时，该能召回记忆系统自己的结论。"""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "m.db"
            conn = connect(db)
            try:
                store = Store(conn)
                store.start_run()
                decider = RuleDecider()
                for pred, content in [
                    ("memory_recall_long_text_penalty",
                     "记忆检索用 Jaccard 型相似度会稀释长记忆，需要换成覆盖率公式"),
                    ("memory_needs_scope_and_lifecycle",
                     "记忆库缺归属与生命周期两个维度，需要加 scope 和 depends_on"),
                    ("user_name", "用户叫刘润"),
                ]:
                    cand = Candidate(
                        kind="reflection" if pred.startswith("memory_") else "preference",
                        subject="user", predicate=pred,
                        object=content[:40], content=content, confidence=0.8,
                    )
                    consolidate(store, cand, decider)
                hits = search(conn, "记忆有什么优化空间", limit=5)
                preds = {h["predicate"] for h in hits}
                self.assertIn(
                    "memory_needs_scope_and_lifecycle", preds,
                    f"应召回记忆结构相关的结论，实际：{preds}",
                )
            finally:
                conn.close()


class ScopeStorageTest(unittest.TestCase):
    """端到端：闸门 + 归属 + 状态更新 + 依赖过期。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "m.db"
        self.conn = connect(self.db)
        self.store = Store(self.conn)
        self.store.start_run()
        self.decider = RuleDecider()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_junk_rejected_not_inserted(self):
        before = self.store.count()
        cand = Candidate(
            kind="preference", subject="user", predicate="requires",
            object="也可以自检一遍安全相关问题：当前插件不建议直接发布",
            content="用户提到：也可以自检一遍安全相关问题",
            confidence=0.55,
        )
        outcome = consolidate(self.store, cand, self.decider)
        self.assertEqual(outcome.action, "reject")
        self.assertEqual(self.store.count(), before, "被否决的候选不该进库")

    def test_status_update_invalidates_old(self):
        """状态类事实：新值必须让旧值失效。"""
        c1 = Candidate(kind="fact", subject="project", predicate="security_status",
                       object="has_vulnerabilities", content="插件存在路径穿越漏洞",
                       confidence=0.8)
        consolidate(self.store, c1, self.decider)
        c2 = Candidate(kind="fact", subject="project", predicate="security_status",
                       object="fixed", content="插件已修复路径穿越漏洞", confidence=0.85)
        outcome = consolidate(self.store, c2, self.decider)
        self.assertEqual(outcome.action, "invalidate")

        rows = self.conn.execute(
            "SELECT status, expired_by FROM memories WHERE predicate='security_status'"
        ).fetchall()
        statuses = {r["status"] for r in rows}
        self.assertIn("superseded", statuses)
        self.assertIn("active", statuses)
        expired = [r for r in rows if r["status"] == "superseded"]
        self.assertEqual(expired[0]["expired_by"], "superseded")

    def test_dependency_expiry(self):
        """项目目录被删 -> 记忆自动失效。"""
        from mem import maintain_lifecycle

        plugin_dir = Path(self.tmp.name) / "plugin"
        plugin_dir.mkdir()
        (plugin_dir / "package.json").write_text("{}", encoding="utf-8")

        cand = Candidate(
            kind="fact", subject="project", predicate="deploy_status",
            object="已部署", content="插件已完成部署",
            confidence=0.8, mentioned_path=str(plugin_dir),
        )
        consolidate(self.store, cand, self.decider)

        mem = self.store.active_key("fact", "project", "deploy_status")
        self.assertIsNotNone(mem)
        self.assertTrue(mem.depends_on.startswith("path:"))
        self.assertEqual(mem.scope, SCOPE_PROJECT)

        # 目录还在 -> 不失效
        stats = maintain_lifecycle(self.conn)
        self.assertEqual(stats["scope-gone"], 0)

        # 删掉目录 -> 失效
        import shutil

        shutil.rmtree(plugin_dir)
        stats = maintain_lifecycle(self.conn)
        self.assertEqual(stats["scope-gone"], 1)

        mem2 = self.store.get(mem.id)
        self.assertEqual(mem2.expired_by, "scope-gone")

    def test_summary_generated_on_insert(self):
        cand = Candidate(
            kind="preference", subject="user", predicate="responds_in",
            object="简体中文", content="用户希望回复用简体中文", confidence=0.8,
        )
        consolidate(self.store, cand, self.decider)
        mem = self.store.active_key("preference", "user", "responds_in")
        self.assertTrue(mem.summary, "入库时应自动生成 summary")
        self.assertIn("简体中文", mem.summary)


if __name__ == "__main__":
    unittest.main()
