/**
 * dsh-tide-memory · DSH 宿主插件
 *
 * 做两件事，都是「无感」的：
 *   ① 每次进入模型步骤前（agent/pre-step），拿当前真人消息去记忆库检索，
 *      命中且内容变化时，注入一条 <system-reminder> 消息当背景。
 *   ② 收到真人消息时（session/event 的 user/message），把它追加进收件箱
 *      （inbox.jsonl），由每日任务抽取巩固。写文件是同步追加，微秒级。
 *
 * 不 import 任何 @deepseek-ai 包：消息对象自己构造，避免插件在 profile
 * node_modules 里解析不到依赖而整体加载失败。
 *
 * 数据库用 Node 24 内置的 node:sqlite，只读打开，不与 Python 侧写操作抢锁。
 */

import { execFile, spawn, spawnSync } from 'node:child_process'
import { createHash, randomUUID } from 'node:crypto'
import { appendFileSync, copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { homedir, tmpdir } from 'node:os'

export const name = 'dsh-tide-memory'
/** 事件本身不需要额外服务；webServer 在 apply 里按需 inject（设置页 API）。 */
export const inject = []

const HERE = dirname(fileURLToPath(import.meta.url))
/** 包根 = 插件根（host.js 所在目录）；Python 引擎与数据都在它下面。 */
const PKG_ROOT = HERE
const MEM_DIR = join(PKG_ROOT, 'lib')
const MEM_PY = join(MEM_DIR, 'mem.py')
const API_PATH = '/plugins/dsh-tide-memory/api'
const DSH_HOME = process.env.DSH_HOME?.trim()
  ? process.env.DSH_HOME.trim()
  : join(homedir(), '.dsh')

const PYTHON_FALLBACK = join(
  DSH_HOME,
  'dsh-runtimes',
  'dsh-primary-runtime',
  'dependencies',
  'python',
  'python.exe',
)

const DEFAULTS = {
  enabled: true,
  /** 数据放在 DSH_HOME 下，跟着用户的 DSH 数据走，不写死本机路径 */
  db: join(DSH_HOME, 'dsh-tide-memory', 'memory.db'),
  inbox: join(DSH_HOME, 'dsh-tide-memory', 'inbox.jsonl'),
  snapshot: join(DSH_HOME, 'dsh-tide-memory', 'memories.json'),
  python: PYTHON_FALLBACK,
  maxItems: 6,
  maxChars: 1400,
  minQueryChars: 4,
  /** 子 agent 的委派 prompt 不是「用户说的话」，默认不采集也不注入 */
  captureSubagentSessions: false,
  /** 开场无条件注入用户画像（不靠相似度） */
  profileInjection: true,
  profileMaxItems: 5,
}

/**
 * 判断这是不是子 agent 会话。
 * DSH 会话头里有 origin / delegationDepth / parentSession 三个字段：
 *   {"origin":"subagent","delegationDepth":1,"parentSession":"session-..."}
 * 官方包 dsh-workspace-changes 就是这么判断的。
 */
function isSubagentSession(session) {
  const header = session?.header ?? {}
  if (header.origin === 'subagent') return true
  if (Number(header.delegationDepth ?? 0) > 0) return true
  return false
}

const SYSTEM_REMINDER_OPEN = '<system-reminder>'
const SYSTEM_REMINDER_CLOSE = '</system-reminder>'

/**
 * 配置里的绝对路径可能来自另一台机器（换机、迁移），用不了就退回 DSH_HOME 下的默认位置。
 * 这样打包带走时即使 config.json 带着旧路径，也不会一上来就哑掉。
 */
function resolveConfig(raw) {
  const cfg = { ...DEFAULTS, ...raw }
  // 数据目录：DSH_HOME 下的 defaults 永远比旧机器路径可靠
  try {
    if (typeof cfg.db !== 'string' || !existsSync(dirname(cfg.db))) cfg.db = DEFAULTS.db
  } catch {
    cfg.db = DEFAULTS.db
  }
  try {
    if (typeof cfg.inbox !== 'string' || !existsSync(dirname(cfg.inbox))) cfg.inbox = DEFAULTS.inbox
  } catch {
    cfg.inbox = DEFAULTS.inbox
  }
  try {
    if (typeof cfg.snapshot !== 'string') cfg.snapshot = DEFAULTS.snapshot
  } catch {
    cfg.snapshot = DEFAULTS.snapshot
  }
  try {
    if (typeof cfg.python !== 'string' || (cfg.python !== 'python' && !existsSync(cfg.python))) {
      cfg.python = process.env.DSH_PYTHON || DEFAULTS.python
    }
  } catch {
    cfg.python = DEFAULTS.python
  }
  // 确保数据目录存在，否则首次启动 sqlite 会建不出来
  try {
    mkdirSync(dirname(cfg.db), { recursive: true })
    mkdirSync(dirname(cfg.inbox), { recursive: true })
  } catch {
    /* 建不出来就交给 Python 侧兜底 */
  }
  return cfg
}

function loadConfig(ctx) {
  const candidates = [
    process.env.DSH_TIDE_MEMORY_CONFIG,
    join(HERE, 'config.json'),
    join(DSH_HOME, 'dsh-tide-memory.json'),
  ].filter((p) => typeof p === 'string' && p.length > 0)

  for (const path of candidates) {
    try {
      if (!existsSync(path)) continue
      const parsed = JSON.parse(readFileSync(path, 'utf8'))
      return { ...resolveConfig(parsed), configPath: path }
    } catch (error) {
      ctx.logger?.warn?.(`dsh-tide-memory: 配置 ${path} 读取失败，忽略：${String(error?.message ?? error)}`)
    }
  }
  return { ...resolveConfig({}), configPath: '(defaults)' }
}

// --------------------------------------------------------------------------- //
// 检索：中文 2-gram + 时间衰减，与 Python 侧 search.py 保持同一口径
// --------------------------------------------------------------------------- //

const PUNCT = /[，。！？；：、,.!?;:"'“”‘’（）()[\]【】]/g

function normalize(text) {
  return String(text ?? '').replace(PUNCT, '').replace(/\s+/g, '').toLowerCase()
}

function tokens(text) {
  const norm = normalize(text)
  if (!norm) return new Set()
  if (/[\u4e00-\u9fff]/.test(norm)) {
    if (norm.length === 1) return new Set([norm])
    const out = new Set()
    for (let i = 0; i < norm.length - 1; i += 1) out.add(norm.slice(i, i + 2))
    return out
  }
  return new Set(norm.split(/[^a-z0-9]+/).filter(Boolean))
}

function similarity(a, b) {
  const ta = tokens(a)
  const tb = tokens(b)
  if (ta.size === 0 || tb.size === 0) return 0
  let inter = 0
  for (const t of ta) if (tb.has(t)) inter += 1
  return inter / (ta.size + tb.size - inter)
}

function ageDays(iso) {
  const ts = Date.parse(iso)
  if (!Number.isFinite(ts)) return 365
  return Math.max(0, (Date.now() - ts) / 86400000)
}

function openDbSync(sqlite, path) {
  if (!existsSync(path) || !sqlite?.DatabaseSync) return null
  try {
    return new sqlite.DatabaseSync(path, { readOnly: true })
  } catch {
    return null
  }
}

/**
 * 检索优先走 JSON 快照（Python 侧导出），因为宿主进程不一定是 Node 24，
 * 未必有 node:sqlite。快照不存在时才动态 import sqlite 兜底。
 */
let sqliteModule

async function queryMemories(config, query, limit, sessionCwd) {
  const snapshotPath = config.snapshot ?? join(dirname(config.db), 'memories.json')
  if (existsSync(snapshotPath)) {
    try {
      const snap = JSON.parse(readFileSync(snapshotPath, 'utf8'))
      if (Array.isArray(snap?.items)) {
        return scoreItems(snap.items, query, limit, sessionCwd, config)
      }
    } catch {
      /* 快照坏了就走 sqlite */
    }
  }

  if (sqliteModule === undefined) {
    try {
      sqliteModule = await import('node:sqlite')
    } catch {
      sqliteModule = null
    }
  }
  if (!sqliteModule) return []

  const db = openDbSync(sqliteModule, config.db)
  if (!db) return []
  try {
    const rows = db
      .prepare(
        `SELECT id, kind, object, content, confidence, last_seen_at,
                scope, scope_ref, summary
           FROM memories
          WHERE status = 'active' AND valid_to IS NULL`,
      )
      .all()
    return scoreItems(rows, query, limit, sessionCwd, config)
  } catch {
    return []
  } finally {
    try {
      db.close()
    } catch {
      /* ignore */
    }
  }
}

// --------------------------------------------------------------------------- //
// 归属隔离：项目 A 的记忆不能进项目 B 的会话
// --------------------------------------------------------------------------- //

/** 与 memory_semantics.normalize_path 同一口径，保证哈希算得出同样的值。 */
function normalizePathForHash(p) {
  let s = String(p || '').trim().replace(/\//g, '\\').replace(/\\+$/, '') || String(p || '')
  if (s.length >= 2 && s[1] === ':') s = s[0].toLowerCase() + s.slice(1)
  return s.toLowerCase()
}

/** 当前会话的工作区身份（ws:<12 hex>）。算法与 Python 侧一致。 */
function workspaceRefFor(cwd) {
  if (!cwd) return ''
  return 'ws:' + createHash('sha1').update(normalizePathForHash(cwd)).digest('hex').slice(0, 12)
}

/** 已登记的项目根（Python 侧写入），用于把 cwd 反查成项目身份。 */
function loadScopeRoots(config) {
  const p = join(dirname(config.db), 'scope-roots.json')
  try {
    if (!existsSync(p)) return []
    const arr = JSON.parse(readFileSync(p, 'utf8'))
    return Array.isArray(arr) ? arr.filter((x) => typeof x === 'string') : []
  } catch {
    return []
  }
}

function projectRefsFor(cwd, config) {
  if (!cwd) return []
  const norm = normalizePathForHash(cwd)
  const refs = []
  for (const root of loadScopeRoots(config)) {
    const nr = normalizePathForHash(root)
    // cwd 等于项目根，或在项目根之下
    if (norm === nr || norm.startsWith(nr + '\\')) {
      // path:<hash> —— 与 Python 侧同一算法
      refs.push('path:' + createHash('sha1').update(nr).digest('hex').slice(0, 12))
      // git:<normalized-remote> —— Python 侧有 git remote 时会用这种形式，
      // 不认它就会导致「自己项目的记忆反而召回不了」。
      const remote = readGitRemote(root)
      if (remote) refs.push('git:' + normalizeGitUrl(remote))
    }
  }
  return [...new Set(refs)]
}

/** 读 .git/config 里的 origin url（不调 git 命令，零依赖）。 */
function readGitRemote(root) {
  try {
    const cfg = join(root, '.git', 'config')
    if (!existsSync(cfg)) return ''
    const text = readFileSync(cfg, 'utf8')
    const m = /\[remote "origin"\][^[]*?url\s*=\s*(\S+)/.exec(text)
    return m ? m[1] : ''
  } catch {
    return ''
  }
}

/** 与 Python 的 normalize_git_url 同一口径。 */
function normalizeGitUrl(url) {
  let u = String(url || '').trim()
  u = u.replace(/^git@([^:]+):/, '$1/')
  u = u.replace(/^https?:\/\//, '')
  u = u.replace(/\.git$/, '')
  return u.replace(/[^A-Za-z0-9]+/g, '-').replace(/^-+|-+$/g, '').toLowerCase()
}

/**
 * 这条记忆能不能在当前会话里召回？
 *
 *   global    → 永远可见（用户画像、跨项目偏好）
 *   workspace → 只有 scope_ref 等于当前工作区才可见
 *   project   → 只有 scope_ref 命中当前项目根才可见
 *
 * 判定失败（拿不到 cwd/身份）时**只放行 global**——fail-closed，
 * 宁可少召回，也不能把 A 项目的东西漏给 B 项目。
 */
function isVisibleInSession(item, ctx) {
  const scope = item.scope || 'global'
  if (scope === 'global') return true
  if (scope === 'workspace') {
    const mine = ctx.workspaceRef
    return Boolean(mine) && item.scope_ref === mine
  }
  if (scope === 'project') {
    return ctx.projectRefs.includes(item.scope_ref)
  }
  // 未知 scope：不放行
  return false
}

function scoreItems(rows, query, limit, sessionCwd, config) {
  const ctx = {
    workspaceRef: sessionCwd ? workspaceRefFor(sessionCwd) : '',
    projectRefs: sessionCwd && config ? projectRefsFor(sessionCwd, config) : [],
  }
  const scored = []
  let hidden = 0
  for (const row of rows) {
    // 数据库行没有 scope 字段时（老库）当作 global，保持向后兼容
    if (!isVisibleInSession(row, ctx)) {
      hidden += 1
      continue
    }
    const hay = `${row.content || ''} ${row.object || ''} ${row.summary || ''}`
    const sim = Math.max(
      similarity(query, hay),
      similarity(query, row.object || ''),
      similarity(query, row.content || ''),
    )
    if (sim <= 0.02) continue
    const recency = Math.exp(-ageDays(row.last_seen_at) / 30)
    scored.push({ ...row, score: 0.75 * sim + 0.25 * recency })
  }
  scored.sort((a, b) => b.score - a.score || b.confidence - a.confidence)
  if (hidden > 0) {
    // 只写日志，不打扰用户：让他知道有多少条因归属被挡
    try {
      globalThis.__dshTideMemoryHidden = hidden
    } catch {
      /* ignore */
    }
  }
  return scored.slice(0, limit)
}

// --------------------------------------------------------------------------- //
// 消息工具
// --------------------------------------------------------------------------- //

function textOf(blocks) {
  if (!Array.isArray(blocks)) return ''
  return blocks
    .filter((b) => b && b.type === 'text' && typeof b.text === 'string')
    .map((b) => b.text)
    .join('\n')
}

function isHumanMessage(message) {
  const source = message?.source
  return message?.role === 'user' && source?.kind === 'user'
}

function renderMemory(hits, maxChars) {
  const lines = [
    SYSTEM_REMINDER_OPEN,
    '以下是与当前对话相关的长期记忆（来自本地记忆库，可当作背景参考；与用户当前说法冲突时以用户为准，不要向用户复述本段）：',
  ]
  let total = lines.join('\n').length
  for (const hit of hits) {
    // 优先用 summary（人话），退回 content
    const text = hit.summary || hit.content || ''
    const kindCn = KIND_CN[hit.kind] || hit.kind
    const line = `- [${kindCn}] ${text}`
    if (total + line.length > maxChars) break
    lines.push(line)
    total += line.length
  }
  lines.push(SYSTEM_REMINDER_CLOSE)
  return lines.join('\n')
}

const KIND_CN = {
  preference: '偏好',
  fact: '事实',
  episode: '经历',
  reflection: '反思',
}

/**
 * 开场画像：无条件带上用户是谁（不靠相似度）。
 * 抄的是 dsh-auto-memory 的 Tier-0 思路——偏好本来就该一直在。
 */
async function mergeProfileHits(config, hits) {
  const snapshotPath = config.snapshot ?? join(dirname(config.db), 'memories.json')
  let items = []
  try {
    if (existsSync(snapshotPath)) {
      const snap = JSON.parse(readFileSync(snapshotPath, 'utf8'))
      if (Array.isArray(snap?.items)) items = snap.items
    }
  } catch {
    return hits
  }
  const profile = items
    .filter((it) => (it.scope ?? 'global') === 'global')
    .filter((it) => ['preference', 'fact'].includes(it.kind))
    .filter((it) => PREFERRED_PROFILE_KEYS.has(it.predicate))
    .sort((a, b) => Number(b.confidence ?? 0) - Number(a.confidence ?? 0))
    .slice(0, config.profileMaxItems)

  const seen = new Set(hits.map((h) => h.id))
  const merged = [...hits]
  for (const p of profile) {
    if (!seen.has(p.id)) merged.push({ ...p, score: 0 })
  }
  return merged
}

/** 开场必带的用户画像键（按重要性排序） */
const PREFERRED_PROFILE_KEYS = new Set([
  'user_name',
  'nickname',
  'user_job',
  'responds_in',
  'prefers_report_language',
  'prefers_editor',
  'prefers_explanation_style',
  'knowledge_level',
  'prefers_theme',
  'prefers_ui_theme_consistency',
  'prefers_hover_reveal_actions',
])

function memoryMessage(text) {
  return Object.freeze({
    id: randomUUID(),
    role: 'user',
    content: [{ type: 'text', text }],
    source: { kind: 'dsh-tide-memory', form: 'reminder' },
  })
}

function appendInbox(config, entry) {
  try {
    mkdirSync(dirname(config.inbox), { recursive: true })
    appendFileSync(config.inbox, `${JSON.stringify(entry)}\n`, 'utf8')
  } catch {
    /* 收件箱写入失败不该影响对话 */
  }
}

/** 设置页的读写都转发给 Python（复用巩固状态机与审计），宿主不碰数据库。 */
function callPython(config, body) {
  return new Promise((resolvePromise, rejectPromise) => {
    const python = config.python && existsSync(config.python) ? config.python : 'python'
    const child = execFile(
      python,
      [MEM_PY, 'api'],
      { cwd: MEM_DIR, timeout: 60000, maxBuffer: 8 * 1024 * 1024, windowsHide: true },
      (error, stdout, stderr) => {
        if (error) {
          rejectPromise(new Error((stderr || '').trim() || error.message))
          return
        }
        resolvePromise((stdout || '').trim() || '{"ok":false,"error":"empty output"}')
      },
    )
    child.stdin.write(body)
    child.stdin.end()
  })
}

// --------------------------------------------------------------------------- //
// 环境检查：完全不依赖 Python，因为「没有 Python」正是它要报告的结论
// --------------------------------------------------------------------------- //

/** 与 setup-python.ps1 保持同一套查找顺序，避免两边结论不一致。 */
function candidatePythons(config) {
  const list = []
  if (process.env.DSH_PYTHON) list.push({ path: process.env.DSH_PYTHON, source: 'DSH_PYTHON' })
  if (config.python) list.push({ path: config.python, source: '配置' })
  list.push({
    path: join(DSH_HOME, 'dsh-runtimes', 'dsh-primary-runtime', 'dependencies', 'python', 'python.exe'),
    source: 'DSH 自带',
  })
  list.push({ path: 'python', source: 'PATH' })
  list.push({ path: 'python3', source: 'PATH' })
  return list
}

function runVersion(exe, timeoutMs = 8000) {
  return new Promise((resolvePromise) => {
    execFile(exe, ['--version'], { timeout: timeoutMs, windowsHide: true }, (error, stdout, stderr) => {
      const text = `${stdout || ''}${stderr || ''}`.trim()
      if (error && !text) {
        resolvePromise({ ok: false, version: '', raw: '' })
        return
      }
      const m = /Python (\d+)\.(\d+)\.(\d+)/.exec(text)
      if (!m) {
        resolvePromise({ ok: false, version: '', raw: text })
        return
      }
      const major = Number(m[1])
      const minor = Number(m[2])
      resolvePromise({
        ok: major === 3 && minor >= 10,
        version: `${m[1]}.${m[2]}.${m[3]}`,
        raw: text,
      })
    })
  })
}

/**
 * 环境探测。返回给设置页/弹窗的数据。
 * 注意：这里**绝不抛异常**，环境缺失本身就是正常返回值。
 */
async function probeEnvironment(config) {
  const tried = []
  for (const cand of candidatePythons(config)) {
    if (cand.path && cand.path !== 'python' && cand.path !== 'python3' && !existsSync(cand.path)) {
      tried.push({ path: cand.path, source: cand.source, result: 'not-found' })
      continue
    }
    const r = await runVersion(cand.path)
    tried.push({
      path: cand.path,
      source: cand.source,
      result: r.ok ? 'ok' : r.version ? 'too-old' : 'not-runnable',
      version: r.version || '',
    })
    if (r.ok) {
      return {
        ok: true,
        python: { path: cand.path, source: cand.source, version: r.version },
        tried,
        setupScript: join(PKG_ROOT, 'setup-python.ps1'),
      }
    }
  }

  // 记忆库/快照是否存在，决定「采集能不能工作」
  let hasSnapshot = false
  try {
    hasSnapshot = existsSync(config.snapshot)
  } catch {
    hasSnapshot = false
  }

  return {
    ok: false,
    python: null,
    tried,
    setupScript: join(PKG_ROOT, 'setup-python.ps1'),
    capabilities: {
      inject: hasSnapshot,
      capture: true,
      settingsWrite: false,
      distill: false,
    },
    reason: '没有找到 Python 3.10+',
  }
}

/** 已启动的安装进程（同一时间只允许一个） */
let installChild = null

/**
 * 触发一键安装：detached 启动 setup-python.ps1，立刻返回，不阻塞 HTTP。
 * 前端轮询 env_status 观察结果。
 */
function startPythonInstall(config) {
  return new Promise((resolvePromise) => {
    if (installChild) {
      resolvePromise({ ok: true, started: false, message: '安装已在进行中' })
      return
    }
    const script = join(PKG_ROOT, 'setup-python.ps1')
    if (!existsSync(script)) {
      resolvePromise({ ok: false, error: `找不到安装脚本：${script}` })
      return
    }
    if (process.platform !== 'win32') {
      resolvePromise({
        ok: false,
        error: '一键安装目前只支持 Windows。请手动安装 Python 3.10+ 后点「重新检测」。',
      })
      return
    }
    try {
      // -NoProfile 避免用户 profile 拖慢；-ExecutionPolicy Bypass 只作用于这一次
      installChild = spawn(
        'powershell.exe',
        ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script],
        { detached: true, stdio: 'ignore', windowsHide: false },
      )
      installChild.on('exit', () => {
        installChild = null
      })
      installChild.unref()
      resolvePromise({
        ok: true,
        started: true,
        message: '安装程序已启动。完成后点「重新检测」；如果没反应，可能弹出了安装界面。',
      })
    } catch (error) {
      installChild = null
      resolvePromise({ ok: false, error: String(error?.message ?? error) })
    }
  })
}

// --------------------------------------------------------------------------- //
// 自动更新 + Star 引导（自包含：只用 GitHub 公开 API 与本机 git/tar）
//
// 与 archive-manager 完全独立：不 import 对方，也不共用任何文件或状态。
// --------------------------------------------------------------------------- //

/** 更新检查缓存有效期：6 小时（与页面「自动检查：每 6 小时」一致）。 */
const UPDATE_TTL_MS = 6 * 60 * 60 * 1000;
/** Star 按钮只允许打开这些域名。 */
const EXTERNAL_HOSTS = new Set(['github.com', 'www.github.com']);
/** 覆盖升级时保留的东西：仓库元信息、依赖、用户数据、测试。 */
const UPDATE_SKIP = new Set(['.git', 'node_modules', 'var', 'tests', '.backup', '.DS_Store']);

/** 本进程启动时加载的版本（升级磁盘后不会变，用来判断「需不需要重启」）。 */
const RUNTIME_VERSION = (() => {
  try {
    const manifest = JSON.parse(readFileSync(join(PKG_ROOT, 'package.json'), 'utf8'));
    return typeof manifest.version === 'string' ? manifest.version : '0.0.0';
  } catch {
    return '0.0.0';
  }
})();

/** 更新检查的内存缓存（每个 Host 进程一份）。 */
const updateState = {
  checkedAt: 0,
  repoSlug: null,
  latestTag: null,
  latestVersion: null,
  error: null,
  message: null,
};

function readSelfManifest() {
  try {
    return JSON.parse(readFileSync(join(PKG_ROOT, 'package.json'), 'utf8'));
  } catch {
    return null;
  }
}

function diskVersion() {
  const manifest = readSelfManifest();
  return manifest !== null && typeof manifest.version === 'string' ? manifest.version : RUNTIME_VERSION;
}

/**
 * 从 package.json 的 repository 解析 GitHub 仓库；占位符视为「没有更新源」。
 */
function resolveUpdateRepo() {
  const manifest = readSelfManifest();
  if (manifest === null) return null;
  const raw = manifest.repository;
  const url = typeof raw === 'string' ? raw : typeof raw?.url === 'string' ? raw.url : null;
  if (url === null) return null;
  const match = /github\.com[/:]([^/\s]+)\/([^/#\s]+)/i.exec(url);
  if (match === null) return null;
  const owner = match[1];
  const repo = match[2].replace(/\.git$/i, '').replace(/[.,;]+$/, '');
  if (/^(your[_-]?github[_-]?user|your[_-]?name|owner|user|example)$/i.test(owner)) return null;
  return { owner, repo, slug: owner + '/' + repo, htmlUrl: 'https://github.com/' + owner + '/' + repo };
}

function parseVersion(value) {
  const match = /^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$/.exec(String(value ?? '').trim());
  if (match === null) return null;
  return { parts: [Number(match[1]), Number(match[2]), Number(match[3])], pre: match[4] ?? '' };
}

function compareVersions(left, right) {
  const a = parseVersion(left);
  const b = parseVersion(right);
  if (a === null || b === null) return 0;
  for (let index = 0; index < 3; index += 1) {
    if (a.parts[index] !== b.parts[index]) return a.parts[index] - b.parts[index];
  }
  if (a.pre === b.pre) return 0;
  if (a.pre === '') return 1;
  if (b.pre === '') return -1;
  return a.pre < b.pre ? -1 : 1;
}

/** 问 GitHub 要仓库里最新的 semver tag。 */
async function fetchLatestTag(repo) {
  const response = await fetch('https://api.github.com/repos/' + repo.slug + '/tags?per_page=100', {
    headers: { accept: 'application/vnd.github+json', 'user-agent': name + '/update-check' },
    signal: AbortSignal.timeout(12000),
  });
  if (!response.ok) throw new Error('GitHub API ' + response.status);
  const payload = await response.json();
  if (!Array.isArray(payload)) throw new Error('GitHub API：返回了非预期结构');
  let best = null;
  for (const entry of payload) {
    const tag = typeof entry?.name === 'string' ? entry.name : '';
    if (parseVersion(tag) === null) continue;
    if (best === null || compareVersions(tag, best.tag) > 0) best = { tag, version: tag.replace(/^v/i, '') };
  }
  return best;
}

/** 组装前端要的状态。 */
function updateStatus(repo) {
  const disk = diskVersion();
  const latest = updateState.latestVersion;
  const baseline = compareVersions(disk, RUNTIME_VERSION) >= 0 ? disk : RUNTIME_VERSION;
  return {
    ok: updateState.error === null,
    packageName: name,
    runtimeVersion: RUNTIME_VERSION,
    diskVersion: disk,
    latestVersion: latest,
    latestTag: updateState.latestTag,
    checkedAt: updateState.checkedAt > 0 ? new Date(updateState.checkedAt).toISOString() : null,
    ttlMs: UPDATE_TTL_MS,
    repo: repo?.slug ?? null,
    htmlUrl: repo?.htmlUrl ?? null,
    canSelfUpdate: repo !== null,
    updateAvailable: latest !== null && compareVersions(latest, baseline) > 0,
    restartRequired: compareVersions(disk, RUNTIME_VERSION) > 0,
    error: updateState.error,
    message: updateState.message,
  };
}

/** 检查更新（默认走 6 小时缓存）。 */
async function checkUpdate(force) {
  const repo = resolveUpdateRepo();
  if (repo === null) {
    return {
      ...updateStatus(null),
      ok: false,
      canSelfUpdate: false,
      message: 'package.json 里还没有配置真实的 repository 地址，无法检查更新。',
    };
  }
  const fresh = updateState.checkedAt > 0
    && updateState.repoSlug === repo.slug
    && Date.now() - updateState.checkedAt < UPDATE_TTL_MS;
  if (fresh && force !== true) return updateStatus(repo);

  try {
    const latest = await fetchLatestTag(repo);
    updateState.checkedAt = Date.now();
    updateState.repoSlug = repo.slug;
    updateState.latestTag = latest?.tag ?? null;
    updateState.latestVersion = latest?.version ?? null;
    updateState.error = null;
    updateState.message = latest === null ? '仓库里还没有可用的版本 tag。' : null;
  } catch (error) {
    updateState.checkedAt = Date.now();
    updateState.repoSlug = repo.slug;
    updateState.error = error instanceof Error ? error.message : String(error);
    updateState.message = null;
  }
  return updateStatus(repo);
}

/** 跑一个外部命令（git / tar），不走 shell。 */
function runTool(command, args, cwd) {
  const result = spawnSync(command, args, {
    cwd,
    encoding: 'utf8',
    windowsHide: true,
    timeout: 120000,
    maxBuffer: 8 * 1024 * 1024,
  });
  return {
    ok: result.status === 0 && result.error === undefined,
    code: result.status,
    stdout: result.stdout ?? '',
    stderr: result.stderr ?? (result.error !== undefined ? String(result.error.message) : ''),
  };
}

/** 递归复制目录树。 */
function copyTree(source, destination, skip) {
  let count = 0;
  const walk = (from, to) => {
    mkdirSync(to, { recursive: true });
    for (const entry of readdirSync(from, { withFileTypes: true })) {
      if (skip.has(entry.name)) continue;
      const sourcePath = join(from, entry.name);
      const targetPath = join(to, entry.name);
      if (entry.isDirectory()) walk(sourcePath, targetPath);
      else if (entry.isFile()) {
        copyFileSync(sourcePath, targetPath);
        count += 1;
      }
    }
  };
  walk(source, destination);
  return count;
}

/** 有 .git 时：fetch + checkout 到 tag。 */
function applyViaGit(directory, tag) {
  const fetched = runTool('git', ['fetch', '--tags', '--force', 'origin'], directory);
  if (!fetched.ok) return { ok: false, error: 'git-fetch-failed', detail: fetched.stderr.trim().slice(0, 400) };
  // 用户很可能改过 package.json（例如手填仓库地址）；先硬还原，否则 checkout 会因本地改动而中止。
  // 插件源码以仓库为准；用户数据在 var/ 与 .gitignore 之外，不受这次还原影响。
  const reset = runTool('git', ['reset', '--hard', 'HEAD'], directory);
  if (!reset.ok) return { ok: false, error: 'git-reset-failed', detail: reset.stderr.trim().slice(0, 400) };
  const checkout = runTool('git', ['checkout', '--detach', tag], directory);
  if (!checkout.ok) return { ok: false, error: 'git-checkout-failed', detail: checkout.stderr.trim().slice(0, 400) };
  return { ok: true, method: 'git' };
}

/** 没有 .git 时：下载 tag 源码包覆盖插件文件（保留 .git / node_modules / var / tests）。 */
async function applyViaTarball(directory, repo, tag) {
  const staging = mkdtempSync(join(tmpdir(), 'dsh-tide-memory-update-'));
  try {
    const archivePath = join(staging, 'source.tar.gz');
    const url = 'https://codeload.github.com/' + repo.slug + '/tar.gz/refs/tags/' + encodeURIComponent(tag);
    const response = await fetch(url, {
      headers: { 'user-agent': name + '/update' },
      signal: AbortSignal.timeout(60000),
    });
    if (!response.ok) return { ok: false, error: 'download-failed-' + response.status };
    writeFileSync(archivePath, Buffer.from(await response.arrayBuffer()));

    const extracted = runTool('tar', ['-xzf', archivePath, '-C', staging], staging);
    if (!extracted.ok) return { ok: false, error: 'extract-failed', detail: extracted.stderr.trim().slice(0, 400) };

    const root = readdirSync(staging, { withFileTypes: true }).find((entry) => entry.isDirectory());
    if (root === undefined) return { ok: false, error: 'empty-archive' };

    const files = copyTree(join(staging, root.name), directory, UPDATE_SKIP);
    return { ok: true, method: 'tarball', files };
  } finally {
    try {
      rmSync(staging, { recursive: true, force: true });
    } catch {
      /* 清不掉临时目录不影响结果 */
    }
  }
}

/** 执行升级。 */
async function applyUpdate() {
  const status = await checkUpdate(true);
  if (status.canSelfUpdate !== true) {
    return { ok: false, error: 'no-update-source', message: status.message ?? '没有可用的更新源。' };
  }
  if (status.latestTag === null) {
    return { ok: false, error: 'no-release', message: status.message ?? '仓库里还没有可用的版本 tag。' };
  }
  if (!status.updateAvailable) {
    return {
      ok: true,
      alreadyLatest: true,
      from: status.diskVersion,
      to: status.latestVersion,
      restartRequired: status.restartRequired,
      method: 'none',
      message: status.restartRequired ? '代码已更新，重启 DSH 即可生效。' : '已经是最新版本。',
    };
  }
  const repo = resolveUpdateRepo();
  const hasGit = existsSync(join(PKG_ROOT, '.git'));
  const result = hasGit ? applyViaGit(PKG_ROOT, status.latestTag) : await applyViaTarball(PKG_ROOT, repo, status.latestTag);
  return {
    ...result,
    from: status.diskVersion,
    to: status.latestVersion,
    restartRequired: result.ok === true,
  };
}

/** 用系统默认浏览器打开 github.com 链接（只允许 https + github.com）。 */
function openExternalUrl(rawUrl) {
  let parsed;
  try {
    parsed = new URL(String(rawUrl));
  } catch {
    return { ok: false, error: 'bad-url' };
  }
  if (parsed.protocol !== 'https:' || !EXTERNAL_HOSTS.has(parsed.hostname)) {
    return { ok: false, error: 'unsupported-url' };
  }
  const command = process.platform === 'win32' ? 'cmd' : process.platform === 'darwin' ? 'open' : 'xdg-open';
  const args = process.platform === 'win32' ? ['/c', 'start', '', parsed.href] : [parsed.href];
  try {
    const child = spawn(command, args, { detached: true, stdio: 'ignore', windowsHide: true });
    child.unref();
    return { ok: true, url: parsed.href };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : String(error) };
  }
}

// --------------------------------------------------------------------------- //
// API 防护
//
// 宿主的 webServer.register() 不做任何鉴权：它只按路径查表然后调 handler。
// 也就是说，任何人只要能让浏览器（或本机任意进程）向这个路径发 POST，就能读写记忆。
// 插件必须自己扛三道闸：
//   ① Host 白名单 —— 防 DNS rebinding（攻击者域名解析到 127.0.0.1）
//   ② Origin 校验 —— 防跨站请求（别的网页偷偷 POST 到本机）
//   ③ CSRF token —— 每次进程启动随机生成，只有同源页面能拿到
// --------------------------------------------------------------------------- //

/** 每次启动随机生成，只通过 GET 引导页发给同源前端 */
const API_TOKEN = randomUUID()

const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '[::1]', '::1'])

function hostnameOf(hostHeader) {
  const h = String(hostHeader || '').trim().toLowerCase()
  if (!h) return ''
  // 去端口：127.0.0.1:3080 / [::1]:3080
  if (h.startsWith('[')) {
    const end = h.indexOf(']')
    return end > 0 ? h.slice(0, end + 1) : h
  }
  const colon = h.lastIndexOf(':')
  return colon > 0 ? h.slice(0, colon) : h
}

function isLoopbackHost(hostHeader) {
  const name = hostnameOf(hostHeader)
  return LOOPBACK_HOSTS.has(name)
}

/** Origin 必须与本次请求的 Host 同源（或缺失——非浏览器客户端如 curl 不带 Origin）。 */
function isSameOrigin(origin, hostHeader) {
  if (!origin) return true // 非浏览器调用，交给 token 闸
  try {
    const u = new URL(origin)
    const oHost = u.host.toLowerCase()
    const rHost = String(hostHeader || '').toLowerCase()
    if (!rHost) return false
    return oHost === rHost
  } catch {
    return false
  }
}

function readToken(req) {
  const h = req.headers || {}
  const direct = h['x-dsh-tide-token'] || h['x-dsh-memory-token']
  if (typeof direct === 'string' && direct) return direct
  const auth = h['authorization']
  if (typeof auth === 'string' && auth.toLowerCase().startsWith('bearer ')) {
    return auth.slice(7).trim()
  }
  return ''
}

/**
 * 返回 null 表示放行，否则返回 { code, body } 让调用方直接响应。
 * 注意：这里刻意 **fail-closed**——判定不了一律拒绝。
 *
 * @param issueToken GET 且只为取 token 时放行 token 闸（但仍然要过 Host/Origin），
 *                   否则「取 token」这个动作自己就需要 token，成死锁。
 */
function guardRequest(req, opts = {}) {
  const headers = req.headers || {}
  const host = headers['host'] || headers[':authority'] || ''

  // ① Host 必须是本机回环名，否则可能是 DNS rebinding
  if (!isLoopbackHost(host)) {
    return { code: 403, body: { ok: false, error: 'forbidden: non-loopback host' } }
  }

  // ② Origin 存在时必须与 Host 同源
  if (!isSameOrigin(headers['origin'], host)) {
    return { code: 403, body: { ok: false, error: 'forbidden: cross-origin' } }
  }

  // ③ CSRF token 必须匹配（写操作尤其重要）
  if (opts.issueToken) {
    // 取 token 的请求只靠前两道闸：攻击者控制的网页仍拿不到（跨站被 ② 拦），
    // DNS rebinding 被 ① 拦；本机恶意进程本来就能读你的磁盘，token 拦不住它。
    return null
  }
  if (readToken(req) !== API_TOKEN) {
    return { code: 401, body: { ok: false, error: 'unauthorized: missing or invalid token' } }
  }

  return null
}

// --------------------------------------------------------------------------- //
// 插件入口
// --------------------------------------------------------------------------- //

export function apply(ctx) {
  const config = loadConfig(ctx)
  if (!config.enabled) {
    ctx.logger?.info?.('dsh-tide-memory: 已禁用（config.enabled=false）')
    return
  }
  ctx.logger?.info?.(
    `dsh-tide-memory: 就绪（db=${config.db}，inbox=${config.inbox}，config=${config.configPath}）`,
  )

  /** agent.id -> 上次注入的 digest，避免同一条记忆反复刷进上下文 */
  const injected = new Map()
  /** agent.id -> 是否已经做过开场画像注入 */
  const profiled = new Set()
  /** 已写进 inbox 的 (sessionId, seq)，防止事件重放导致重复 */
  const recorded = new Set()

  ctx.on('agent/pre-step', async ({ agent, messages }, next) => {
    const decision = await next()
    if (decision.kind !== 'enter') return decision
    if (!Array.isArray(decision.messages) || decision.messages.length === 0) return decision

    // 子 agent 是被派活的：不注入主会话的偏好记忆，免得误导 + 浪费 token
    if (!config.captureSubagentSessions && isSubagentSession(agent?.session)) return decision

    // 找本批里最后一条真人消息；没有就说明是工具循环，不重复注入
    const human = [...decision.messages].reverse().find(isHumanMessage)
    if (!human) return decision

    const query = textOf(human.content).trim()
    if (query.length < config.minQueryChars) return decision

    // 会话工作目录决定「哪些归属的记忆可见」：项目 A 的东西不能进项目 B
    const sessionCwd = agent?.session?.header?.cwd || ''
    const hits = await queryMemories(config, query, config.maxItems, sessionCwd)

    // digest 只算「检索命中」（不含画像），这样画像补充不会改变去重判定
    const digest = createHash('sha1').update(hits.map((h) => h.id).join(',')).digest('hex')
    if (injected.get(agent.id) === digest) return decision

    // 开场首次：无条件补上用户画像（不靠相似度命中）
    let finalHits = hits
    if (config.profileInjection && !profiled.has(agent.id)) {
      profiled.add(agent.id)
      finalHits = await mergeProfileHits(config, hits)
    }
    if (finalHits.length === 0) return decision

    injected.set(agent.id, digest)

    const anchorIndex = decision.messages.findLastIndex((m) => messages.includes(m))
    const message = memoryMessage(renderMemory(finalHits, config.maxChars))
    return {
      ...decision,
      messages: decision.messages.toSpliced(anchorIndex + 1, 0, message),
    }
  })

  ctx.on('session/event', (session, event) => {
    if (!event || event.type !== 'user/message') return
    // 子 agent 的委派 prompt 不是「用户说的话」，别采进 inbox
    if (!config.captureSubagentSessions && isSubagentSession(session)) return
    const data = event.data ?? {}
    if (data.source?.kind !== 'user') return
    const text = textOf(data.content).trim()
    if (text.length < 6) return

    const sessionId = String(session?.id ?? 'unknown')
    const key = `${sessionId}#${event.seq ?? ''}`
    if (recorded.has(key)) return
    if (recorded.size > 2000) recorded.clear()
    recorded.add(key)

    appendInbox(config, {
      session: sessionId,
      seq: event.seq ?? null,
      time: event.time ?? Date.now(),
      text,
      // 关键：记下这条消息来自哪个工作区/项目。
      // 没有它，引擎只能把内容归到 global，项目 A 的东西就会进项目 B 的上下文。
      cwd: session?.header?.cwd || '',
    })
  })

  // 设置页 API：转发给 Python（stdin JSON -> stdout JSON）
  try {
    ctx.inject(['webServer'], (scope) => {
      const server = scope.get ? scope.get('webServer') : scope.webServer
      if (!server || typeof server.register !== 'function') {
        ctx.logger?.warn?.('dsh-tide-memory: 拿不到 webServer，设置页改为不可用（功能不受影响）')
        return
      }
      ctx.effect(
        () =>
          server.register({
            kind: 'exact',
            path: API_PATH,
            handler: async (req, res) => {
              const isGet = req.method === 'GET'

              // ── 三道闸：Host 白名单 / Origin 同源 / CSRF token
              // GET 只用来取 token，所以对它放开第三道（否则自己需要自己，死锁）
              const blocked = guardRequest(req, { issueToken: isGet })
              if (blocked) {
                res.writeHead(blocked.code, { 'content-type': 'application/json; charset=utf-8' })
                res.end(JSON.stringify(blocked.body))
                return
              }

              // GET：把 token 发给同源前端（Host + Origin 两道闸已过）
              if (isGet) {
                res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' })
                res.end(JSON.stringify({ ok: true, token: API_TOKEN }))
                return
              }

              if (req.method !== 'POST') {
                res.writeHead(405, { 'content-type': 'application/json; charset=utf-8' })
                res.end('{"ok":false,"error":"POST only"}')
                return
              }
              const chunks = []
              for await (const chunk of req) chunks.push(chunk)
              const body = Buffer.concat(chunks).toString('utf8') || '{}'

              // ── 环境检查与安装：这两个 op 完全不依赖 Python，必须能在「没有 Python」时响应
              let payload = {}
              try {
                payload = JSON.parse(body)
              } catch {
                /* 交给下面按空对象处理 */
              }
              if (payload && payload.op === 'env_status') {
                const reply = await probeEnvironment(config)
                res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' })
                res.end(JSON.stringify(reply))
                return
              }
              if (payload && payload.op === 'python_install') {
                const reply = await startPythonInstall(config)
                res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' })
                res.end(JSON.stringify(reply))
                return
              }

              // ── 自动更新与 Star 引导：同样不依赖 Python，没有 Python 时也能响应
              if (payload && payload.op === 'update_status') {
                const reply = await checkUpdate(payload.force === true)
                res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' })
                res.end(JSON.stringify(reply))
                return
              }
              if (payload && payload.op === 'update_apply') {
                const reply = await applyUpdate()
                res.writeHead(reply.ok === true ? 200 : 500, { 'content-type': 'application/json; charset=utf-8' })
                res.end(JSON.stringify(reply))
                return
              }
              if (payload && payload.op === 'open_repo') {
                const repo = resolveUpdateRepo()
                const reply = repo === null
                  ? { ok: false, error: 'no-repository', message: '还没有配置仓库地址。' }
                  : openExternalUrl(repo.htmlUrl)
                res.writeHead(reply.ok === true ? 200 : 400, { 'content-type': 'application/json; charset=utf-8' })
                res.end(JSON.stringify(reply))
                return
              }

              try {
                const out = await callPython(config, body)
                res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' })
                res.end(out)
              } catch (error) {
                res.writeHead(500, { 'content-type': 'application/json; charset=utf-8' })
                res.end(JSON.stringify({ ok: false, error: String(error?.message ?? error) }))
              }
            },
          }),
        'dsh-tide-memory: settings api',
      )
      ctx.logger?.info?.(`dsh-tide-memory: 设置页 API 已注册 ${API_PATH}`)
    })
  } catch (error) {
    ctx.logger?.warn?.(`dsh-tide-memory: 设置页 API 注册失败：${String(error?.message ?? error)}`)
  }

  ctx.effect?.(() => () => {
    injected.clear()
    recorded.clear()
  }, 'dsh-tide-memory: clear caches')
}
