-- =============================================================================
-- memory-consolidation / schema.sql
-- 巩固层的持久化结构：事实条目 + 证据 + 审计 + 运行记录 + 全文索引
--
-- 设计要点
--   1. 记忆永不删除：冲突时写 valid_to，状态标 superseded。
--   2. 每条记忆都有来源（session_id + seq）与置信度，可回溯。
--   3. 每一次巩固决策都进 audit_log，人可复查、可重放。
--   4. FTS5 只索引有效条目（validation_to IS NULL），避免旧事实污染召回。
-- =============================================================================

PRAGMA foreign_keys = ON;

-- 供 DSH / 多进程场景使用；旧版 SQLite 会忽略
PRAGMA application_id = 0x4D454D4F;  -- 'MEMO'
PRAGMA user_version = 1;

-- 记忆条目
CREATE TABLE IF NOT EXISTS memories (
  id            TEXT PRIMARY KEY,            -- mem_<12位hex>
  kind          TEXT NOT NULL
                 CHECK (kind IN ('preference', 'fact', 'episode', 'reflection')),
  subject       TEXT NOT NULL,               -- 谁：user / project / agent
  predicate     TEXT NOT NULL,               -- 关系：prefers_editor / lives_in / ...
  object        TEXT NOT NULL,               -- 值（归一化后的短文本）
  content       TEXT NOT NULL,               -- 人可读的一句话
  status        TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active', 'superseded', 'candidate', 'rejected', 'expired')),
  valid_from    TEXT NOT NULL,               -- 事实何时开始为真（ISO8601 UTC）
  valid_to      TEXT,                        -- 事实何时失效；NULL 表示仍有效
  recorded_at   TEXT NOT NULL,               -- 何时被记录
  last_seen_at  TEXT NOT NULL,               -- 最近一次被证据支持
  confidence    REAL NOT NULL DEFAULT 0.5 CHECK (confidence BETWEEN 0 AND 1),
  supersedes    TEXT REFERENCES memories(id), -- 本条替代了谁
  superseded_by TEXT,                        -- 本条被谁替代（冗余，便于查询）
  source_session TEXT,
  source_seq    INTEGER,
  fingerprint   TEXT NOT NULL,               -- sha256(kind|subject|predicate|norm(object))
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,

  -- 迁移 002：归属（这条记忆属于谁）
  scope         TEXT NOT NULL DEFAULT 'global',   -- global | workspace | project
  scope_ref     TEXT NOT NULL DEFAULT '',         -- '' | ws:<hash> | git:<url> | path:<hash>

  -- 迁移 002：可读性（给主人看的一句人话；面板只显示这个）
  summary       TEXT NOT NULL DEFAULT '',

  -- 迁移 002：生命周期（依赖什么才成立）
  depends_on    TEXT NOT NULL DEFAULT '',         -- 'path:...' | 'git-remote:...' | ''
  expired_by    TEXT                             -- superseded | scope-gone | ttl | manual
                  CHECK (expired_by IS NULL OR
                         expired_by IN ('superseded', 'scope-gone', 'ttl', 'manual')),

  -- 迁移 002：状态类事实的稳定键（新状态覆盖旧状态时用）
  is_status     INTEGER NOT NULL DEFAULT 0        -- 1 = 状态类，可被同键新值 invalidate
);

CREATE INDEX IF NOT EXISTS idx_mem_key    ON memories(kind, subject, predicate, status);
CREATE INDEX IF NOT EXISTS idx_mem_active ON memories(status, valid_to);
CREATE INDEX IF NOT EXISTS idx_mem_fp     ON memories(fingerprint);
CREATE INDEX IF NOT EXISTS idx_mem_seen   ON memories(last_seen_at);

-- =============================================================================
-- 迁移 002：归属 / 生命周期 / 可读性（见 memory_semantics.py）
--
--   scope       'global' | 'workspace' | 'project' —— 这条记忆属于谁
--   scope_ref   具体身份：'' / 'ws:<hash>' / 'git:<url>' / 'path:<hash>'
--   summary     一句人话，给主人看的（面板只显示这个）
--   depends_on  依赖：'path:C:\...' / 'git-remote:...'，依赖没了自动失效
--   expired_by  谁让它失效：superseded | scope-gone | ttl | manual
--
-- 注意：这些列的索引在 store.ensure_columns() 里建，不放在本文件——
-- 因为老库先跑本文件时列还不存在，建索引会报 "no such column"。
-- =============================================================================

-- 证据：同一条记忆可以被多次对话支持
CREATE TABLE IF NOT EXISTS memory_evidence (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_id   TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  session_id  TEXT,
  seq         INTEGER,
  quote       TEXT NOT NULL,
  recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ev_mem ON memory_evidence(memory_id);

-- 审计：每次决策一行，用于复现「当时为什么这么合并」
CREATE TABLE IF NOT EXISTS audit_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id        TEXT NOT NULL,
  ts            TEXT NOT NULL,
  action        TEXT NOT NULL,   -- insert | merge | invalidate | skip | promote | reject
  memory_id     TEXT,            -- 新条目 / 被更新的条目
  target_id     TEXT,            -- 被合并或失效的旧条目
  candidate_json TEXT NOT NULL,
  decision_json  TEXT NOT NULL,
  note          TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_log(run_id);

-- 运行记录
CREATE TABLE IF NOT EXISTS runs (
  run_id      TEXT PRIMARY KEY,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  stats_json  TEXT
);

-- 会话游标：避免每天重复抽取同一个没变化的会话（省 LLM 调用）
CREATE TABLE IF NOT EXISTS session_cursor (
  session_id   TEXT PRIMARY KEY,
  source_mtime REAL NOT NULL,
  last_run_at  TEXT NOT NULL,
  candidates   INTEGER NOT NULL DEFAULT 0
);

-- 全文索引（external content，由触发器维护）
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  content,
  subject,
  predicate,
  object,
  content = 'memories',
  content_rowid = 'rowid',
  tokenize = 'unicode61'
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, content, subject, predicate, object)
  VALUES (new.rowid, new.content, new.subject, new.predicate, new.object);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content, subject, predicate, object)
  VALUES ('delete', old.rowid, old.content, old.subject, old.predicate, old.object);
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content, subject, predicate, object)
  VALUES ('delete', old.rowid, old.content, old.subject, old.predicate, old.object);
  INSERT INTO memories_fts(rowid, content, subject, predicate, object)
  VALUES (new.rowid, new.content, new.subject, new.predicate, new.object);
END;
