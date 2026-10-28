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
  /**
   * 自动巩固（零配置）。
   * 采集与注入本来就是插件自动做的；巩固原来要靠手动命令或自建定时任务，
   * 结果新机器装完永远没有记忆可注入。这几个默认值把它变成开箱即用。
   * 想回到「完全手动」就设 autoConsolidate:false。
   */
  autoConsolidate: true,
  /** 启动后多久跑第一次（毫秒）：给宿主与数据目录一点时间 */
  autoConsolidateDelayMs: 60000,
  /** 之后每隔多久检查一次（毫秒）：默认 30 分钟 */
  autoConsolidateIntervalMs: 1800000,
  /** inbox 至少攒够几条才值得起一次 Python 进程 */
  autoConsolidateMinEntries: 1,
  /** 两次巩固之间的最小间隔（毫秒）：防止反复起进程 */
  autoConsolidateMinIntervalMs: 300000,
  /** 单次巩固的超时（毫秒）：LLM 抽取可能慢，给足 5 分钟 */
  autoConsolidateTimeoutMs: 300000,
  /**
   * 每日整理（对应旧「LLM 每日巩固」定时任务）：在固定时刻跑一次
   * flush-inbox + maintain。默认关——常规整理已由上面的周期检查覆盖，
   * 这里给的是「每天固定时间整理一次 + 顺带做生命周期维护」的选项。
   */
  dailyConsolidate: false,
  /** 每日整理的时刻，HH:MM 本地时间（对照旧任务习惯是凌晨 03:00） */
  dailyConsolidateTime: '03:00',
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
// 自动巩固调度（零配置）
//
// 设计取舍：
//   · 为什么不挂 DSH 的调度服务：宿主调度是会话级的，且留给主人自己表达意图；
//     插件只保证「装完就能用」。自带定时器让新机器无需任何配置即可工作。
//   · 为什么先延迟再跑：宿主刚启动时数据目录可能还没建好，Python 也在冷启动。
//   · 为什么按 inbox 条数判阈值：巩固要起 Python 进程，空跑纯属浪费；
//     攒到一定量再跑，既省资源又不丢内容。
//   · 为什么用 execFile 而不是 spawn detached：我们需要拿到退出码与输出，
//     好在日志里如实报告；异步执行不会阻塞会话。
// --------------------------------------------------------------------------- //

/** 巩固是否正在跑（进程内单飞，避免并发写同一个库）。 */
let consolidateChild = null
/** 上次巩固的时间戳（毫秒），用于「至少间隔多久才再跑」。 */
let lastConsolidateAt = 0

/** 数一下 inbox 里有多少条待处理的采集记录。 */
function countInboxEntries(inboxPath) {
  try {
    if (!existsSync(inboxPath)) return 0
    const text = readFileSync(inboxPath, 'utf8')
    let n = 0
    for (const line of text.split('\n')) {
      if (line.trim().length > 0) n += 1
    }
    return n
  } catch {
    return 0
  }
}

/** 挑一个能用的 Python（与探针/安装共用同一套候选顺序）。 */
function pickRuntimePython(config) {
  for (const candidate of candidatePythons(config)) {
    const path = candidate.path
    if (!path) continue
    if (path === 'python' || path === 'python3') return path
    if (existsSync(path)) return path
  }
  return null
}

/**
 * 跑一次巩固。返回 { ok, ran, reason, entries, stdout?, stderr? }。
 *
 * **绝不抛异常、绝不阻塞调用方**：巩固失败不该影响主人说话。
 */
function runConsolidateOnce(config, reason) {
  return new Promise((resolvePromise) => {
    if (consolidateChild !== null) {
      resolvePromise({ ok: false, ran: false, reason: 'busy' })
      return
    }

    const entries = countInboxEntries(config.inbox)
    const idleMs = Date.now() - lastConsolidateAt
    const minIdle = config.autoConsolidateMinIntervalMs ?? 0
    if (entries < (config.autoConsolidateMinEntries ?? 1)) {
      resolvePromise({ ok: true, ran: false, reason: 'inbox-empty', entries })
      return
    }
    // 距上次太近就别再跑（防止反复起进程）
    if (lastConsolidateAt > 0 && idleMs < minIdle) {
      resolvePromise({ ok: true, ran: false, reason: 'too-soon', entries, idleMs })
      return
    }

    const python = pickRuntimePython(config)
    if (!python) {
      ctxLog(`自动巩固跳过（${reason}）：找不到可用的 Python 3.10+；采集与注入不受影响`)
      resolvePromise({ ok: false, ran: false, reason: 'no-python', entries })
      return
    }

    // 配了 LLM 就带上 --llm（抽取更准）；没配就走规则，毫秒级
    const args = [MEM_PY, 'flush-inbox']
    if (hasUsableLlm()) args.push('--llm')

    lastConsolidateAt = Date.now()
    consolidateChild = execFile(
      python,
      args,
      { cwd: MEM_DIR, timeout: config.autoConsolidateTimeoutMs ?? 300000, maxBuffer: 8 * 1024 * 1024, windowsHide: true },
      (error, stdout, stderr) => {
        consolidateChild = null
        const out = (stdout || '').trim()
        const err = (stderr || '').trim()
        if (error) {
          ctxLog(`自动巩固失败（${reason}）：${err || error.message}`)
          resolvePromise({ ok: false, ran: true, reason, entries, stdout: out, stderr: err, error: error.message })
          return
        }
        ctxLog(`自动巩固完成（${reason}）：${entries} 条采集 → ${out || '(无输出)'}`)
        resolvePromise({ ok: true, ran: true, reason, entries, stdout: out, stderr: err })
      },
    )
  })
}

/** 插件日志出口。ctx 在各处不方便传递时用一个模块级别的引用。 */
let activeCtx = null
function ctxLog(message) {
  try {
    activeCtx?.logger?.info?.(`dsh-tide-memory: ${message}`)
  } catch {
    /* 日志失败无所谓 */
  }
}

/** 有可用的 LLM 配置（配了 base_url/model 且密钥能取到）。 */
function hasUsableLlm() {
  try {
    const configFile = join(DSH_HOME, 'dsh-tide-memory', 'llm.json')
    if (!existsSync(configFile)) return false
    const parsed = JSON.parse(readFileSync(configFile, 'utf8'))
    if (typeof parsed?.base_url !== 'string' || typeof parsed?.model !== 'string') return false
    if (!parsed.base_url || !parsed.model) return false
    // 密钥存在与否交给 Python 判断（它才知道密文能否解开）；
    // 这里只要配置文件像样就带上 --llm，密钥不usable 时 Python 会自己降级。
    return true
  } catch {
    return false
  }
}

/**
 * 装上自动巩固。默认行为：
 *   · 启动后 `autoConsolidateDelayMs`（默认 60 秒）跑第一次
 *   · 之后每 `autoConsolidateIntervalMs`（默认 30 分钟）检查一次
 *   · inbox 少于 `autoConsolidateMinEntries`（默认 1 条）就跳过
 * 全部可用 `enabled:false` / 三个数值项在 config.json 里调节；
 * `autoConsolidate:false` 可整体关掉（回到「完全手动」的旧行为）。
 */
function startAutoConsolidate(ctx, config) {
  activeCtx = ctx
  if (config.autoConsolidate === false) {
    ctx.logger?.info?.('dsh-tide-memory: 自动巩固已关闭（config.autoConsolidate=false）')
    return
  }

  const delayMs = Math.max(0, Number(config.autoConsolidateDelayMs ?? 60000))
  const intervalMs = Math.max(60000, Number(config.autoConsolidateIntervalMs ?? 1800000))
  let firstTimer = null
  let loopTimer = null

  // 启动第一次：给宿主和数据目录一点时间，别和启动流程抢资源
  firstTimer = setTimeout(() => {
    firstTimer = null
    void runConsolidateOnce(config, 'startup')
  }, delayMs)
  firstTimer.unref?.()

  loopTimer = setInterval(() => {
    void runConsolidateOnce(config, 'interval')
  }, intervalMs)
  loopTimer.unref?.()

  ctx.logger?.info?.(
    `dsh-tide-memory: 自动巩固已启用（启动后 ${Math.round(delayMs / 1000)}s 首次，` +
    `之后每 ${Math.round(intervalMs / 60000)} 分钟检查一次；可设 autoConsolidate=false 关闭）`,
  )

  // 卸载时停掉定时器并清掉引用——不留悬挂回调
  ctx.effect?.(() => () => {
    if (firstTimer !== null) clearTimeout(firstTimer)
    if (loopTimer !== null) clearInterval(loopTimer)
    firstTimer = null
    loopTimer = null
    if (dailyTimer !== null) clearTimeout(dailyTimer)
    dailyTimer = null
    activeCtx = null
  }, 'dsh-tide-memory: auto consolidate timers')

  // 每日整理：固定时刻跑 flush-inbox + maintain（对应旧「LLM 每日巩固」任务）
  startDailyConsolidate(ctx, config)
}

// --------------------------------------------------------------------------- //
// 每日整理（对应旧「LLM 每日巩固」定时任务）
//
// 与周期整理的区别：
//   · 周期整理（autoConsolidate）：inbox 攒够就跑，**只做 flush-inbox**
//   · 每日整理（dailyConsolidate）：固定时刻跑一次，**flush-inbox + maintain**，
//     maintain 会做依赖检查与 TTL 失效——生命周期维护原来只能靠手动或外部任务
//
// 实现是「自算下一次触发点」的单发 setTimeout：到点执行 → 算下一个明天同一时刻
// → 再排。比 setInterval 每分钟轮询省事，也不依赖宿主进程常驻精度（错过就等
// 下一个周期检查顺带处理 inbox，maintain 在下次启动后的每日触发补上）。
// --------------------------------------------------------------------------- //

let dailyTimer = null

/** 解析 HH:MM 为 {hour, minute}；非法格式返回 null（fail-closed，回落默认 03:00）。 */
function parseDailyTime(raw) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(raw ?? '').trim())
  if (!m) return { hour: 3, minute: 0 }
  const hour = Number(m[1])
  const minute = Number(m[2])
  if (hour < 0 || hour > 23 || minute < 0 || minute > 59) return { hour: 3, minute: 0 }
  return { hour, minute }
}

/** 距下一个「今天的 HH:MM（已过则取明天）」还有多少毫秒。 */
function msUntilNextDaily(hour, minute) {
  const now = new Date()
  const next = new Date(now.getFullYear(), now.getMonth(), now.getDate(), hour, minute, 0, 0)
  if (next.getTime() <= now.getTime()) next.setDate(next.getDate() + 1)
  return next.getTime() - now.getTime()
}

/** 每日整理的执行体：flush-inbox（带 LLM 如可用）+ maintain，串行执行。 */
function runDailyConsolidate(config) {
  return new Promise((resolvePromise) => {
    if (consolidateChild !== null) {
      resolvePromise({ ok: false, ran: false, reason: 'busy' })
      return
    }
    const python = pickRuntimePython(config)
    if (!python) {
      ctxLog('每日整理跳过：找不到可用的 Python 3.10+')
      resolvePromise({ ok: false, ran: false, reason: 'no-python' })
      return
    }

    const useLlm = hasUsableLlm()
    const flushArgs = [MEM_PY, 'flush-inbox']
    if (useLlm) flushArgs.push('--llm')

    lastConsolidateAt = Date.now()
    consolidateChild = execFile(
      python,
      flushArgs,
      { cwd: MEM_DIR, timeout: config.autoConsolidateTimeoutMs ?? 300000, maxBuffer: 8 * 1024 * 1024, windowsHide: true },
      (flushError, flushStdout, flushStderr) => {
        consolidateChild = null
        const flushOut = (flushStdout || '').trim()
        if (flushError) {
          ctxLog(`每日整理的抽取步骤失败：${(flushStderr || flushError.message).trim()}`)
          resolvePromise({ ok: false, ran: true, step: 'flush', error: flushError.message })
          return
        }
        ctxLog(`每日整理·抽取完成：${flushOut || '(无输出)'}`)

        // 抽取成功 → 顺带做生命周期维护（依赖消失 / TTL）
        consolidateChild = execFile(
          python,
          [MEM_PY, 'maintain'],
          { cwd: MEM_DIR, timeout: config.autoConsolidateTimeoutMs ?? 300000, maxBuffer: 8 * 1024 * 1024, windowsHide: true },
          (maintainError, maintainStdout) => {
            consolidateChild = null
            const maintainOut = (maintainStdout || '').trim()
            if (maintainError) {
              ctxLog(`每日整理的生命周期维护失败：${maintainError.message}`)
              resolvePromise({ ok: false, ran: true, step: 'maintain', error: maintainError.message })
              return
            }
            ctxLog(`每日整理·维护完成：${maintainOut || '(无需处理)'}`)
            resolvePromise({ ok: true, ran: true, flush: flushOut, maintain: maintainOut })
          },
        )
      },
    )
  })
}

/** 装上每日整理调度。dailyConsolidate:false 或缺省时不装。 */
function startDailyConsolidate(ctx, config) {
  if (config.dailyConsolidate !== true) return
  const { hour, minute } = parseDailyTime(config.dailyConsolidateTime)

  const schedule = () => {
    const delay = msUntilNextDaily(hour, minute)
    dailyTimer = setTimeout(() => {
      dailyTimer = null
      void runDailyConsolidate(config).then(() => {
        // 跑完排下一次（明天同一时刻），而不是递归立即——防漂移也防连跑
        schedule()
      })
    }, delay)
    dailyTimer.unref?.()
    ctx.logger?.info?.(
      `dsh-tide-memory: 每日整理已排定（每天 ${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}，` +
      `约 ${Math.round(delay / 60000)} 分钟后首次执行）`,
    )
  }
  schedule()
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
/** 每个更新源独立 12 秒超时：慢源只拖死自己，不拖垮整条解析链。 */
const UPDATE_SOURCE_TIMEOUT_MS = 12000;
/** 检查失败后的退避窗口：60 秒内不再打远程，免得连点按钮把匿名配额烧光。 */
const UPDATE_FAIL_BACKOFF_MS = 60 * 1000;
/** 源码包下载允许落地的域名（github.com 会 302 到 codeload / objects，得放行）。 */
const UPDATE_ARCHIVE_HOSTS = new Set([
  'github.com',
  'www.github.com',
  'codeload.github.com',
  'objects.githubusercontent.com',
]);
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
  /** 上次检查失败的时间戳：只用于退避，不参与 6 小时缓存判定（旧代码在这里写 checkedAt 才会烧配额）。 */
  lastErrorAt: 0,
  /**
   * 成功解析时用的源标识：github-releases | github-tags | raw-package | jsdelivr-package。
   * API 两源合并时写成 'github-releases+github-tags'。
   */
  source: null,
  /** 每个源的尝试明细，给面板「查看诊断」用 */
  diagnostics: [],
  /** 各源各自报出的版本：{ source, version }[]，让面板一眼看出谁落后 */
  candidates: [],
  /** 匿名 API 配额快照：{ remaining, reset, retryAfter }；拿不到就是 null */
  quota: null,
};

/**
 * 每个源的 ETag 内存表：url -> { etag, tag, version }。
 * 304 时直接复用上次解析结果，同时不消耗带宽；进程重启后自然失效。
 */
const updateEtagCache = new Map();

/** 解析链：前两个是 API（有 60 次/小时匿名配额），后两个是静态文件（几乎不限流）。 */
const UPDATE_SOURCES = [
  { id: 'github-releases', kind: 'api', url: (repo) => 'https://api.github.com/repos/' + repo.slug + '/releases/latest' },
  { id: 'github-tags', kind: 'api', url: (repo) => 'https://api.github.com/repos/' + repo.slug + '/tags?per_page=100' },
  { id: 'raw-package', kind: 'file', url: (repo) => 'https://raw.githubusercontent.com/' + repo.slug + '/HEAD/package.json' },
  { id: 'jsdelivr-package', kind: 'file', url: (repo) => 'https://cdn.jsdelivr.net/gh/' + repo.slug + '@HEAD/package.json' },
];

/** 按 id 取源声明；写死 id 而不是靠数组下标，改顺序也不会错。 */
function updateSourceById(id) {
  return UPDATE_SOURCES.find((source) => source.id === id) ?? null;
}

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

/** 从 tag 数组里挑出最高的 semver tag；没有合法的就返回 null。 */
function pickHighestTag(entries) {
  let best = null;
  for (const entry of entries) {
    const tag = typeof entry?.name === 'string' ? entry.name : '';
    if (parseVersion(tag) === null) continue;
    if (best === null || compareVersions(tag, best.tag) > 0) best = { tag, version: tag.replace(/^v/i, '') };
  }
  return best;
}

/** 从响应头里读出匿名 API 配额信息；一个都没有就算没拿到。 */
function parseQuota(headers) {
  if (!headers || typeof headers.get !== 'function') return null;
  const remaining = headers.get('x-ratelimit-remaining');
  const reset = headers.get('x-ratelimit-reset');
  const retryAfter = headers.get('retry-after');
  if (remaining === null && reset === null && retryAfter === null) return null;
  const toNumber = (value) => {
    if (value === null) return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : value;
  };
  return { remaining: toNumber(remaining), reset: toNumber(reset), retryAfter: toNumber(retryAfter) };
}

/** 是不是限流：429，或 403 且 x-ratelimit-remaining 已经被打空。 */
function isRateLimited(status, quota) {
  if (status === 429) return true;
  return status === 403 && quota !== null && String(quota.remaining) === '0';
}

/**
 * 试一个版本源。
 *
 * 单源独立 12s 超时 + redirect:'error'（防凭据/内容被跳转劫持）；
 * 命中 If-None-Match 的 304 直接复用上次解析结果，不重复消耗带宽。
 *
 * @returns {{source:string,url:string,status:number|null,error:string|null,ms:number,tag?:string|null,version?:string|null,quota:object|null,rateLimited:boolean}}
 */
async function fetchUpdateSource(source, repo) {
  const url = source.url(repo);
  const startedAt = Date.now();
  const attempt = { source: source.id, url, status: null, error: null, ms: 0, quota: null, rateLimited: false };
  const cached = updateEtagCache.get(url);

  const headers = {
    'user-agent': name + '/update-check',
    accept: source.kind === 'api' ? 'application/vnd.github+json' : 'application/json',
  };
  if (cached !== undefined && typeof cached.etag === 'string') headers['if-none-match'] = cached.etag;

  let response;
  try {
    response = await fetch(url, {
      headers,
      redirect: 'error',
      signal: AbortSignal.timeout(UPDATE_SOURCE_TIMEOUT_MS),
    });
  } catch (error) {
    attempt.ms = Date.now() - startedAt;
    const reason = error?.name === 'TimeoutError' || error?.name === 'AbortError'
      ? 'timeout'
      : String(error?.message ?? error);
    attempt.error = reason;
    return attempt;
  }

  attempt.status = response.status;
  attempt.ms = Date.now() - startedAt;
  attempt.quota = parseQuota(response.headers);

  // 304：内容没变，直接复用上次解析出来的版本（缓存里没有就说明这条 304 没意义）
  if (response.status === 304) {
    if (cached !== undefined && typeof cached.tag === 'string') {
      attempt.tag = cached.tag;
      attempt.version = cached.version ?? cached.tag.replace(/^v/i, '');
      return attempt;
    }
    attempt.error = 'not-modified-without-cache';
    return attempt;
  }

  if (isRateLimited(response.status, attempt.quota)) {
    attempt.error = 'rate-limited';
    attempt.rateLimited = true;
    return attempt;
  }
  if (!response.ok) {
    attempt.error = 'HTTP ' + response.status;
    return attempt;
  }

  try {
    const payload = await response.json();
    const best = source.kind === 'api' && source.id === 'github-releases'
      ? (() => {
        const tag = typeof payload?.tag_name === 'string' ? payload.tag_name.trim() : '';
        if (tag === '' || parseVersion(tag) === null) return null;
        return { tag, version: tag.replace(/^v/i, '') };
      })()
      : source.id === 'github-tags'
        ? (Array.isArray(payload) ? pickHighestTag(payload) : null)
        : (typeof payload?.version === 'string' && parseVersion(payload.version) !== null
          ? { tag: 'v' + payload.version.replace(/^v/i, ''), version: payload.version.replace(/^v/i, '') }
          : null);
    if (best === null) {
      attempt.error = 'no-semver-version';
      return attempt;
    }
    attempt.tag = best.tag;
    attempt.version = best.version;
    const etag = response.headers.get('etag');
    if (typeof etag === 'string' && etag !== '') rememberEtag(url, { etag, tag: best.tag, version: best.version });
    return attempt;
  } catch (error) {
    attempt.error = 'bad-json: ' + String(error?.message ?? error);
    return attempt;
  }
}

/** 把每个源的失败拼成一句人话（面板直接显示）。 */
function describeUpdateFailure(diagnostics, quota) {
  const parts = [];
  for (const item of diagnostics) {
    const reason = item.error !== null ? item.error : item.status !== null ? 'HTTP ' + item.status : 'unknown';
    parts.push(item.source + ': ' + reason);
  }
  let text = '全部更新源均失败（' + parts.join('；') + '）';
  if (quota !== null && quota !== undefined && String(quota.remaining) === '0') {
    text += '。GitHub 匿名配额已耗尽，已自动改走静态源，稍后会恢复';
  }
  return text.slice(0, 320);
}

/** ETag 表加个上限：源只有 4 个，正常不会到 32；用户换仓库时顺手丢掉最旧的。 */
const UPDATE_ETAG_LIMIT = 32;
function rememberEtag(url, entry) {
  if (updateEtagCache.size >= UPDATE_ETAG_LIMIT) {
    const oldest = updateEtagCache.keys().next();
    if (oldest.done !== true) updateEtagCache.delete(oldest.value);
  }
  updateEtagCache.set(url, entry);
}

/** 单源执行 + 诊断登记 + 配额汇总：多源链里每个源都走这一条路，保证口径一致。 */
async function probeSource(source, repo, diagnostics, ctx) {
  const attempt = await fetchUpdateSource(source, repo);
  const record = {
    source: attempt.source,
    url: attempt.url,
    status: attempt.status,
    error: attempt.error,
    ms: attempt.ms,
    ...(attempt.tag !== undefined && attempt.tag !== null ? { tag: attempt.tag } : {}),
  };
  diagnostics.push(record);
  if (attempt.quota !== null) {
    ctx.quota = attempt.quota;
    if (String(attempt.quota.remaining) === '0') ctx.quotaExhausted = true;
  }
  if (attempt.rateLimited) ctx.quotaExhausted = true;
  return attempt;
}

/**
 * API 两源合并：releases 与 tags 各报一个版本，取**较高者**，而不是「先到先得」。
 *
 * 为什么必须合并：仓库可能出现「打了 tag v2.1.0、但 release 还停在 v2.0.0」的状态。
 * 若 releases 一命中就返回，latestVersion 会停在 v2.0.0；等仓库发了 v2.2.0 却没建 release，
 * 面板就会显示「已是最新」——用户永远收不到升级提示，这正是「更新检查失败」的另一副面孔。
 *
 * 省配额策略：只有 releases 版本 **不高于** 磁盘版本时，才追加去查 tags。
 * releases 已经严格高于磁盘版本时，它必然是「有新版本」，无需再花一次配额。
 *
 * @returns {{result:{tag,version,source}|null, candidates:Array, tagsChecked:boolean, tagsSkippedReason:string|null}}
 */
async function resolveApiLatest(repo, disk, diagnostics, ctx) {
  const candidates = [];
  const releases = updateSourceById('github-releases');
  const tags = updateSourceById('github-tags');
  let fromReleases = null;

  // ① releases/latest
  if (releases !== null && !ctx.quotaExhausted) {
    const attempt = await probeSource(releases, repo, diagnostics, ctx);
    if (attempt.tag !== undefined && attempt.tag !== null) {
      fromReleases = { tag: attempt.tag, version: attempt.version, source: releases.id };
      candidates.push({ source: attempt.source, version: attempt.version });
    }
  } else if (releases !== null) {
    diagnostics.push({ source: releases.id, url: releases.url(repo), status: null, error: 'skipped: quota exhausted', ms: 0 });
  }

  // ② 决定要不要再查 tags
  //    只在「releases 没给出严格高于磁盘的版本」时查 —— 那才是它可能落后于 tags 的情形
  const releasesIsNewer = fromReleases !== null && compareVersions(fromReleases.version, disk) > 0;
  let tagsSkippedReason = null;
  if (tags === null) tagsSkippedReason = 'no-source';
  else if (releasesIsNewer) tagsSkippedReason = 'releases-newer';
  else if (ctx.quotaExhausted) tagsSkippedReason = 'quota-exhausted';

  if (tagsSkippedReason === null) {
    const attempt = await probeSource(tags, repo, diagnostics, ctx);
    if (attempt.tag !== undefined && attempt.tag !== null) {
      candidates.push({ source: attempt.source, version: attempt.version });
      const fromTags = { tag: attempt.tag, version: attempt.version, source: tags.id };
      if (fromReleases === null) {
        // releases 没拿到：tags 单源胜出
        return { result: fromTags, candidates, tagsChecked: true, tagsSkippedReason: null };
      }
      // 两源都有：取较高者。相同时保留较高者那条 tag，但 source 标成合并，表达「两源都确认过」
      const cmp = compareVersions(fromTags.version, fromReleases.version);
      if (cmp > 0) {
        return {
          result: { tag: fromTags.tag, version: fromTags.version, source: releases.id + '+' + tags.id },
          candidates,
          tagsChecked: true,
          tagsSkippedReason: null,
        };
      }
      return {
        result: {
          tag: fromReleases.tag,
          version: fromReleases.version,
          source: releases.id + '+' + tags.id,
        },
        candidates,
        tagsChecked: true,
        tagsSkippedReason: null,
      };
    }
    // tags 查了但没结果：退回 releases（若有）
    return { result: fromReleases, candidates, tagsChecked: true, tagsSkippedReason: null };
  }

  // 跳过了 tags：补一条诊断，面板能看清「为什么只有一个源报数」。
  // 配额耗尽与 releases-newer 都要记，只是措辞不同（前者是被限流，后者是主动省配额）。
  if (tags !== null) {
    diagnostics.push({
      source: tags.id,
      url: tags.url(repo),
      status: null,
      error: tagsSkippedReason === 'quota-exhausted' ? 'skipped: quota exhausted' : 'skipped: ' + tagsSkippedReason,
      ms: 0,
    });
  }
  return { result: fromReleases, candidates, tagsChecked: false, tagsSkippedReason };
}

/**
 * 问出「仓库最新版本」：API 两源合并优先，静态文件源作为 API 全挂时的兜底。
 *
 * 全部源都失败才算失败（source=null）。
 */
async function fetchLatestTag(repo) {
  const diagnostics = [];
  const ctx = { quota: null, quotaExhausted: false };
  const disk = diskVersion();

  // 第一梯队：API 两源合并（取较高者，避免 releases 落后导致漏报升级）
  const api = await resolveApiLatest(repo, disk, diagnostics, ctx);
  if (api.result !== null) {
    return {
      tag: api.result.tag,
      version: api.result.version,
      source: api.result.source,
      diagnostics,
      candidates: api.candidates,
      quota: ctx.quota,
      tagsChecked: api.tagsChecked,
      tagsSkippedReason: api.tagsSkippedReason,
    };
  }

  // 第二梯队：静态文件源兜底（几乎不受匿名配额影响）
  const candidates = [...api.candidates];
  for (const source of UPDATE_SOURCES) {
    if (source.kind !== 'file') continue;
    const attempt = await probeSource(source, repo, diagnostics, ctx);
    if (attempt.tag !== undefined && attempt.tag !== null) {
      candidates.push({ source: attempt.source, version: attempt.version });
      return {
        tag: attempt.tag,
        version: attempt.version,
        source: attempt.source,
        diagnostics,
        candidates,
        quota: ctx.quota,
        tagsChecked: api.tagsChecked,
        tagsSkippedReason: api.tagsSkippedReason,
      };
    }
  }

  return {
    tag: null,
    version: null,
    source: null,
    diagnostics,
    candidates,
    quota: ctx.quota,
    tagsChecked: api.tagsChecked,
    tagsSkippedReason: api.tagsSkippedReason,
  };
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
    // 失败时刻只用于退避判定，和 checkedAt 分开（旧的烧配额 bug 就是把失败写进了 checkedAt）
    lastErrorAt: updateState.lastErrorAt > 0 ? new Date(updateState.lastErrorAt).toISOString() : null,
    ttlMs: UPDATE_TTL_MS,
    repo: repo?.slug ?? null,
    htmlUrl: repo?.htmlUrl ?? null,
    canSelfUpdate: repo !== null,
    updateAvailable: latest !== null && compareVersions(latest, baseline) > 0,
    restartRequired: compareVersions(disk, RUNTIME_VERSION) > 0,
    error: updateState.error,
    message: updateState.message,
    // ── 以下为本次新增（只增不减，旧前端忽略即可）
    source: updateState.source,
    diagnostics: updateState.diagnostics,
    quota: updateState.quota,
    // 各源分别报了什么版本：面板据此一眼看出「releases 落后于 tags」
    candidates: updateState.candidates,
  };
}

/**
 * 诊断快照：给面板「查看诊断」用。
 * 只回放本进程内存里的最后一次结果，不主动打远程 —— 看一眼诊断不该消耗配额。
 */
function updateDiagnostics() {
  const repo = resolveUpdateRepo();
  return {
    ok: true,
    packageName: name,
    runtimeVersion: RUNTIME_VERSION,
    diskVersion: diskVersion(),
    repo: repo?.slug ?? null,
    htmlUrl: repo?.htmlUrl ?? null,
    canSelfUpdate: repo !== null,
    latestTag: updateState.latestTag,
    latestVersion: updateState.latestVersion,
    source: updateState.source,
    checkedAt: updateState.checkedAt > 0 ? new Date(updateState.checkedAt).toISOString() : null,
    lastErrorAt: updateState.lastErrorAt > 0 ? new Date(updateState.lastErrorAt).toISOString() : null,
    error: updateState.error,
    message: updateState.message,
    quota: updateState.quota,
    diagnostics: updateState.diagnostics,
    candidates: updateState.candidates,
    sources: UPDATE_SOURCES.map((source) => ({
      id: source.id,
      kind: source.kind,
      url: repo === null ? null : source.url(repo),
    })),
    timeoutMs: UPDATE_SOURCE_TIMEOUT_MS,
    ttlMs: UPDATE_TTL_MS,
    failBackoffMs: UPDATE_FAIL_BACKOFF_MS,
    etagEntries: updateEtagCache.size,
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

  // 失败退避：60 秒内连点不再打远程。
  // 权威来源是 lastErrorAt —— 成功一次就会把 checkedAt 推到现在，那时退避自然失效。
  const backingOff = updateState.lastErrorAt > 0
    && updateState.repoSlug === repo.slug
    && Date.now() - updateState.lastErrorAt < UPDATE_FAIL_BACKOFF_MS
    && updateState.error !== null;
  if (backingOff) {
    return {
      ...updateStatus(repo),
      message: (updateState.message ?? '上次检查失败，稍后再试。') + '（60 秒内不重复请求）',
    };
  }

  try {
    const result = await fetchLatestTag(repo);
    updateState.repoSlug = repo.slug;
    updateState.diagnostics = result.diagnostics;
    updateState.quota = result.quota;
    updateState.source = result.source;
    updateState.candidates = result.candidates ?? [];
    if (result.source !== null) {
      // 只有成功才写 checkedAt：失败也写会让「6 小时缓存」变成「失败缓存」，连点就烧配额
      updateState.checkedAt = Date.now();
      updateState.lastErrorAt = 0;
      updateState.latestTag = result.tag;
      updateState.latestVersion = result.version;
      updateState.error = null;
      updateState.message = null;
    } else {
      // 全部源失败：不碰 checkedAt，只记 lastErrorAt 供退避
      updateState.lastErrorAt = Date.now();
      updateState.error = describeUpdateFailure(result.diagnostics, result.quota);
      updateState.message = null;
    }
  } catch (error) {
    // 兜底：解析链自己的意外异常同样只记录失败时刻，绝不污染 checkedAt
    updateState.repoSlug = repo.slug;
    updateState.lastErrorAt = Date.now();
    updateState.error = String(error?.message ?? error);
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

/** 从指定 URL 拉源码包；失败返回带明细的结果，不抛。 */
async function downloadArchive(url, archivePath) {
  const startedAt = Date.now();
  try {
    const response = await fetch(url, {
      headers: { 'user-agent': name + '/update' },
      signal: AbortSignal.timeout(60000),
      // 这个源要跟随 302（github.com → codeload / objects），但只允许落到白名单域名，
      // 免得凭据或内容被任意跳转劫持。
      redirect: 'follow',
    });
    if (!response.ok) return { ok: false, status: response.status, ms: Date.now() - startedAt, error: 'HTTP ' + response.status };
    const finalHost = (() => {
      try {
        return new URL(response.url).hostname.toLowerCase();
      } catch {
        return '';
      }
    })();
    if (finalHost !== '' && !UPDATE_ARCHIVE_HOSTS.has(finalHost)) {
      return { ok: false, status: response.status, ms: Date.now() - startedAt, error: 'unexpected-host: ' + finalHost };
    }
    writeFileSync(archivePath, Buffer.from(await response.arrayBuffer()));
    return { ok: true, status: response.status, ms: Date.now() - startedAt, finalUrl: response.url };
  } catch (error) {
    return {
      ok: false,
      status: null,
      ms: Date.now() - startedAt,
      error: error?.name === 'TimeoutError' || error?.name === 'AbortError' ? 'timeout' : String(error?.message ?? error),
    };
  }
}

/** 没有 .git 时：下载 tag 源码包覆盖插件文件（保留 .git / node_modules / var / tests）。 */
async function applyViaTarball(directory, repo, tag) {
  const staging = mkdtempSync(join(tmpdir(), 'dsh-tide-memory-update-'));
  try {
    // 主源 codeload；失败再退 github.com 的 archive 地址（契约 §4「升级源回退」）
    const candidates = [
      { source: 'codeload', url: 'https://codeload.github.com/' + repo.slug + '/tar.gz/refs/tags/' + encodeURIComponent(tag) },
      { source: 'github-archive', url: 'https://github.com/' + repo.slug + '/archive/refs/tags/' + encodeURIComponent(tag) + '.tar.gz' },
    ];
    const attempts = [];
    let archivePath = null;
    let usedSource = null;
    for (const candidate of candidates) {
      const target = join(staging, 'source-' + candidate.source + '.tar.gz');
      const attempt = await downloadArchive(candidate.url, target);
      attempts.push({ source: candidate.source, url: candidate.url, status: attempt.status, error: attempt.error ?? null, ms: attempt.ms });
      if (attempt.ok === true) {
        archivePath = target;
        usedSource = candidate.source;
        break;
      }
    }
    if (archivePath === null) {
      // 两个源都不行才报错，错误里带上每步明细，面板能直接看出卡在哪
      return {
        ok: false,
        error: 'download-failed',
        detail: attempts.map((item) => item.source + ': ' + (item.error ?? 'unknown')).join('；').slice(0, 400),
        attempts,
      };
    }

    const extracted = runTool('tar', ['-xzf', archivePath, '-C', staging], staging);
    if (!extracted.ok) {
      return {
        ok: false,
        error: 'extract-failed',
        detail: extracted.stderr.trim().slice(0, 400),
        attempts,
        downloadSource: usedSource,
      };
    }

    const root = readdirSync(staging, { withFileTypes: true }).find((entry) => entry.isDirectory());
    if (root === undefined) return { ok: false, error: 'empty-archive', attempts, downloadSource: usedSource };

    const files = copyTree(join(staging, root.name), directory, UPDATE_SKIP);
    return { ok: true, method: 'tarball', files, downloadSource: usedSource, attempts };
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
              if (payload && payload.op === 'update_diag') {
                // 只回放内存里的诊断快照，不触网 —— 看诊断不该消耗匿名配额
                const reply = updateDiagnostics()
                res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' })
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

  // ── 自动巩固调度 ────────────────────────────────────────────────────── //
  //
  // 为什么必须有：采集（session/event → inbox.jsonl）和注入（读快照）都是
  // 插件自己完成的，但**巩固**（inbox → 抽取 → 记忆库 → 快照）原来完全依赖
  // 外部触发（手动命令或用户自建的定时任务）。结果是新机器装完插件后：
  // 话被采集了、检索也在跑，但快照永远是空的 → 一条记忆都注入不出来。
  //
  // 这里把它变成**零配置**：插件加载后自己在合适的时机跑巩固。
  //   ① 启动后延迟一小会儿先跑一次（等宿主把数据目录写完）
  //   ② 之后按固定间隔再检查，只有 inbox 攒够了才真的跑（避免空转）
  //   ③ 同一时刻只允许一个巩固进程
  // 手动命令与外部定时任务仍然可用，两者不冲突（Python 侧有指纹去重）。
  startAutoConsolidate(ctx, config)
}
