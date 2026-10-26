# dsh-tide-memory

> **Tide Memory** — long-term memory for DeepSeek Harness that actually remembers you —
> and knows what to forget.

[![DSH](https://img.shields.io/badge/DSH-0.2.1%2B-blue)](https://github.com/deepseek-ai/deepseek-harness)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Node](https://img.shields.io/badge/Node-%3E%3D22.19-339933?logo=node.js)](https://nodejs.org)
[![Tests](https://img.shields.io/badge/tests-63%20passed-brightgreen)](lib/)
[![Dependencies](https://img.shields.io/badge/npm%20deps-0-brightgreen)](package.json)

English · [简体中文](README.md)

---

## Why this exists

AI memory systems share one flaw: **things go in, but they don't get understood,
don't get updated, and don't get out.**

This isn't a guess — we went through all 147 memory plugins in the DSH ecosystem
to confirm it. Here's what actually happens:

```
Three months ago you and the AI built a plugin together. It wrote down:
  "The current plugin has a path traversal vulnerability that needs fixing."

Three months later the plugin is fixed, shipped, and you've half-forgotten
the project existed. But that sentence is still in the store, resurfacing
in every loosely-related conversation.
```

Four concrete symptoms:

| Symptom | What it looks like |
|---|---|
| **No context** | "The current plugin has a vulnerability" — which plugin? After you switch projects, nobody knows |
| **Never updates** | The bug is fixed, but "has a vulnerability" is still there; both get recalled |
| **Never expires** | The project is deleted, but its 15 memories keep showing up in context |
| **Stores the wrong things** | A work instruction gets extracted as "user preference: also self-check security issues" |

dsh-tide-memory is built against those four.

---

## Four design decisions

### ① A three-question gate — ask whether it deserves to be remembered

Every candidate passes three questions before it can enter the store.
Fail any one and it's rejected (audited, not stored):

| Question | What it filters out |
|---|---|
| **Is the subject clear?** | "The current plugin needs care" → which plugin? No name, rejected |
| **Does it stand alone?** | "You could also self-check... overall conclusion: the current plugin..." → conversational fragment, rejected |
| **Is this fact or instruction?** | "After you finish changing it" → that's a task, not a fact, rejected |

Plus three hard checks: the predicate must match `[a-z][a-z0-9_]{1,40}`,
the value must be ≤ 60 chars, and it can't be empty talk ("be careful").

**Measured**: a real junk memory was caught by `no-context`; normal memories in the
same batch went through.

### ② Ownership — every memory knows which project it belongs to

| Content | Scope |
|---|---|
| User profile (name, job, preferences) | `global` |
| This machine's environment (install paths) | `workspace` |
| A specific project (features, bugs, decisions) | `project` → bound to a git remote or path |

The panel shows **`[project · archive-manager]`**, not a hash.

**More importantly, recall is actually isolated** (fail-closed):

```
Ask inside project A → only A + global     ✅ A's credentials never enter B's session
Ask inside project B → only B + global
Ask elsewhere        → only global
```

### ③ Lifecycle — things expire, and things update

**Dependencies invalidate automatically**

A memory records what it depends on. One cheap check (milliseconds) before recall:

```
depends_on = path:C:\...\plugins\archive-manager
  ├─ directory exists  → active
  └─ directory deleted → status=expired, expired_by=scope-gone
```

**Status facts get superseded** (this one matters)

Status must use **stable keys**, or "fixed" can never overwrite "vulnerable":

```
old: security_status = has_vulnerabilities → status=superseded, expired_by=superseded
new: security_status = fixed               → status=active
```

> We measured this: rename the key even slightly (`security_review_findings` vs
> `security_status`) and the two memories **never find each other — the new one
> just inserts, and the old one sits there untouched.** That's why status keys
> must be fixed.

Plus TTL: `episode` 90 days, project-level `fact` 180 days;
`preference` / `reflection` never expire.

### ④ Readability — the panel shows human language

The store keeps a `(kind, predicate, object)` triple — **for the program**.
The `summary` field — **for the human**.

```
stored:   preference / responds_in / 简体中文
shown:    - [preference] Replies should be in Simplified Chinese
```

The raw key folds into a collapsible "raw key" detail. **You never have to see `responds_in`.**

---

## How it works

```
                   ┌─────────────────────────────────┐
   you speak ─────►│  agent/pre-step                 │
                   │  ├─ three-question gate         │
                   │  ├─ scope resolution            │
                   │  ├─ recall (coverage + scope)   │
                   │  └─ inject <system-reminder>    │
                   └──────────────┬──────────────────┘
                                  │
                   ┌──────────────▼──────────────────┐
                   │  SQLite (bi-temporal + evidence)│
                   │  ├─ valid_from / valid_to       │
                   │  ├─ supersedes / superseded_by  │
                   │  ├─ memory_evidence (verbatim)  │
                   │  └─ audit_log (every decision)  │
                   └──────────────┬──────────────────┘
                                  │
                   ┌──────────────▼──────────────────┐
                   │  consolidation state machine    │
                   │  insert / merge / invalidate /  │
                   │  skip / reject / expire         │
                   └─────────────────────────────────┘
```

**Nothing is ever hard-deleted**: conflicts become `superseded`, expiry becomes
`expired`, failed extraction becomes `rejected`. Every decision lands in
`audit_log` — replayable.

### Why `agent/pre-step` and not the system prompt

This is the key finding from surveying the whole DSH plugin ecosystem:

> A preset whose persona declares `complete: true` restores that persona as the
> sole prompt section after assembly, **silently discarding every other
> contribution — with no error.**

So a memory plugin that writes to the system prompt is **completely inert** under
such presets. (One popular plugin in the ecosystem fails exactly this way.)
User messages injected at `pre-step` aren't swallowed, and each injection is a
replayable session event visible to compaction.

### Subagents are not you

A subagent's delegation prompt **is not something you said**. It's neither
captured nor injected by default:

```json
{"origin":"subagent","delegationDepth":1,"parentSession":"session-..."}
```

The signal comes from the session header, not from guessing.

---

## Install

```powershell
dsh plugin --profile web add github:jiulanyanyue-boop/dsh-tide-memory
```

Pin a version tag (safer):

```powershell
dsh plugin --profile web add github:jiulanyanyue-boop/dsh-tide-memory#v2.0.0
```

Then **restart `dsh web`** once.

> ⚠️ **Use the GitHub URL, not the bare package name.**
> `dsh plugin add` resolves a bare name against the npm registry. If someone
> already owns that name you'll install their package, which may be incompatible
> with your DSH version.

### Requirements

| Dependency | Version | Notes |
|---|---|---|
| Node.js | `>=22.19.0` | Required by DSH itself |
| Python | `3.10+` | Engine uses stdlib only, **no pip install needed** |

**It works without Python** — capture and injection still run; only automatic
organizing is unavailable. On first start, if no Python is found, a dialog
appears with a **one-click installer** (official package, current user only, no
admin rights). See [WITHOUT-PYTHON.md](WITHOUT-PYTHON.md).

### Where data lives

`$DSH_HOME/dsh-tide-memory/` (usually `C:\Users\<you>\.dsh\dsh-tide-memory\`):

```
memory.db          # the store (SQLite)
memories.json      # recall snapshot (the plugin reads this)
inbox.jsonl        # captured raw material
scope-roots.json   # project path registry
llm.json           # LLM config (if configured)
```

**No path is hardcoded** — everything derives from `DSH_HOME` or `~/.dsh`,
so it works on another machine or another username.

---

## Usage

### Day to day: do nothing

The plugin hooks host events, so **capture and injection are fully automatic**:

- What you say → appended to `inbox.jsonl`
- Related topics → memory returns to context automatically
- Session start → your profile (name, job, preferences) comes along

### Settings page

**Settings → Memory** lets you browse every memory (including candidates and
expired ones), add / edit / delete (audited), and see engine status.

### CLI

```powershell
# Consume inbox: extract + consolidate (rule-based, milliseconds)
lib\mem.cmd flush-inbox

# With an LLM configured, for better accuracy
lib\mem.cmd flush-inbox --llm

# Recall by query
lib\mem.cmd context --query "editor preference"

# Browse by scope
lib\mem.cmd by-scope project

# Lifecycle maintenance (dependencies gone / TTL)
lib\mem.cmd maintain
lib\mem.cmd maintain --dry-run

# Stats
lib\mem.cmd stats
```

### Configure an LLM (optional, recommended)

It works without one (rule-based, offline). With one, extraction and conflict
resolution get noticeably better:

```powershell
lib\mem.cmd llm set --base-url https://api.deepseek.com/v1 --model deepseek-chat
# Enter the key at the hidden prompt; it is not echoed or stored in shell history.
lib\mem.cmd llm test
```

For automation, send the key through standard input instead of a command-line argument:

```powershell
$env:MEMORY_KEY = Read-Host 'API key' -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($env:MEMORY_KEY)
try { $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr); $plain | lib\mem.cmd llm set --api-key-stdin }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr); Remove-Item Env:MEMORY_KEY }
```

Local Ollama works too:

```powershell
lib\mem.cmd llm set --base-url http://127.0.0.1:11434/v1 --model qwen2.5:7b
```

Remote LLM endpoints must use HTTPS. Cleartext HTTP is accepted only for `localhost` or a loopback IP address (for example, local Ollama).
The key config file is restricted to the current user on Unix and to the current user, SYSTEM, and Administrators on Windows; saving fails and removes the config if permissions cannot be applied.

**A failing model never corrupts the store**: timeout, disconnect, invalid JSON,
out-of-range target → all fall back to rule-based decisions.

---

## Data model

| Field | Purpose |
|---|---|
| `valid_from` / `valid_to` | **Bi-temporal**: when the fact was true. Conflicts only write `valid_to`; nothing is deleted |
| `recorded_at` | When it was recorded (separate from fact time) |
| `confidence` | Below 0.5 goes to the candidate pool; promoted only by stronger evidence |
| `supersedes` / `superseded_by` | Version chain — the whole evolution is traceable |
| `source_session` / `source_seq` | Provenance, paired with `memory_evidence` back to the original words |
| `scope` / `scope_ref` | Who this memory belongs to (global / workspace / project) |
| `summary` | One human-readable sentence |
| `depends_on` | What must exist for this to hold; auto-invalidated when it's gone |
| `expired_by` | Why it expired (superseded / scope-gone / ttl / manual) |

---

## Security & privacy

What we actually did:

| Area | Approach |
|---|---|
| **Data location** | Everything under `$DSH_HOME`; nothing in the plugin dir or system dirs |
| **HTTP endpoint** | Three gates: loopback Host allowlist (anti DNS-rebinding) + Origin check + CSRF token |
| **Installer** | Authenticode signature check that **aborts on failure**; current user only, no system dirs |
| **Secrets** | `$DSH_HOME/dsh-tide-memory/llm.json` (0600), gitignored; env vars supported for no-disk mode |
| **Export** | Defaults to `$DSH_HOME`, never the plugin dir; gitignored |
| **Network** | Zero network without an LLM; with one, only extraction/conflict resolution sends fragments to your endpoint |

**Stated limits** (no sugar-coating):

- **Delete is not erasure.** The memory stops being recalled, but its content
  stays in `audit_log`. To truly erase, delete `memory.db` manually.
- **Not multi-user isolation.** The API gates stop browsers and cross-site
  attacks; they don't stop a malicious local process (which could read your disk anyway).
- **Zero telemetry, zero third-party deps.** The plugin imports only `node:`
  builtins; the engine uses only the Python stdlib.

---

## Known limitations

- **Chinese recall uses 2-gram + coverage scoring**, no vector search.
  Synonyms ("write code with neovim" vs "editor is nvim") aren't recognized yet.
- **Single-process SQLite (WAL)**: fine for personal use; concurrent multi-process
  writes would need a serialized queue.
- **Rule-based extraction is deliberately conservative**: task-oriented sessions
  often yield nothing. Use LLM extraction for production quality.
- **Scope resolution is rule-based**: complex cross-project cases may need an
  explicit `--scope` / `--path`.

---

## Under the hood

| | |
|---|---|
| Size | ~5,100 lines (3,600 Python + 1,500 JS) |
| Tests | 63 unit tests, all passing |
| Runtime deps | **0** npm packages, **0** pip packages |
| Storage | SQLite + FTS5 (Node 22.5+ builtin `node:sqlite`) |
| DSH | `0.2.1+` |

### Measured results

| Scenario | Before | After |
|---|---|---|
| Recall for "what should we improve about memory" | 0 hits (long memory diluted to 0.0104 by Jaccard) | **3 hits** (coverage scoring) |
| Cross-project leakage | Project A's memory entered project B's session | **Fully isolated**; unrelated dirs get global only |
| Junk memory entering the store | Written as-is | **Blocked by the gate**, marked `rejected` |
| Status update | Old and new coexist | **Old superseded, new active** |
| After project deletion | Memory lives forever | **Auto `expired_by=scope-gone`** |

---

## Uninstall

```powershell
dsh plugin --profile web remove dsh-tide-memory
```

Data stays in `$DSH_HOME/dsh-tide-memory/`. Delete that directory to remove it too
(**back up `memory.db` first**).

---

## License

[MIT](LICENSE)
