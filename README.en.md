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

### ① A five-question gate — ask whether it deserves to be remembered

Every candidate passes five questions before it can enter the store.
Fail any one and it's rejected (audited, not stored):

| Question | What it filters out |
|---|---|
| **Is the subject clear?** | "The current plugin needs care" → which plugin? No name, rejected |
| **Does it stand alone?** | "You could also self-check... overall conclusion: the current plugin..." → conversational fragment, rejected |
| **Is this fact or instruction?** | "After you finish changing it" → that's a task, not a fact, rejected |
| **Is it reusable?** | "Just changed line 3", "next I'll look at client.js", "tests are done" → process and to-dos, rejected |
| **Is it free of time anchors?** | "Using neovim today" only holds right now — unless it adds "from now on / always / as a habit" |

Plus four hard checks: the predicate must match `[a-z][a-z0-9_]{1,40}`,
the value must be ≤ 60 chars, it can't be empty talk ("be careful"), and it
can't be temp-path noise (`…\Temp\tmpXXXX`).

**Key trade-off**: sentences carrying a *conclusion back-reference* —
"was caused by / root cause / must / already fixed" — are let through. They state
the causal conclusion of something that already happened, which is exactly the
knowledge worth keeping. A process word elsewhere in the sentence must not kill them.

**Measured**: a real junk memory was caught by `no-context`; normal memories in the
same batch went through. Auditing the 52 stored memories against the new gate
rejected only 2.

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
dsh plugin --profile web add github:jiulanyanyue-boop/dsh-tide-memory#v2.2.0
```

Then **restart `dsh web`** once.

> ⚠️ **Use the GitHub URL, not the bare package name.**
> `dsh plugin add` resolves a bare name against the npm registry. If someone
> already owns that name you'll install their package, which may be incompatible
> with your DSH version.

### Manual install (offline bundle / another machine)

You get a `dsh-tide-memory` directory (pure source, no `node_modules`). It can live
**anywhere** — the plugin never depends on its own location; every data path derives
from `DSH_HOME`. Steps:

**1. Place the plugin** (anywhere you like; `<plugin-dir>` below refers to it)

```powershell
# Example: under the harness home's plugins directory
Move-Item <extracted dsh-tide-memory> "$env:USERPROFILE\.dsh\plugins\dsh-tide-memory"
```

**2. Make the profile aware of it**

Edit `<DSH_HOME>\profiles\<your-profile>\package.json` (usually `profiles\desktop\package.json`):

```jsonc
{
  "dependencies": {
    // add this line: the path after link: is the absolute path from step 1
    "dsh-tide-memory": "link:<plugin-dir>"
  },
  "dsh": {
    "profile": {
      "bundles": [
        // add this line to the end of the array
        "dsh-tide-memory"
      ]
    }
  }
}
```

**3. Sync the dependency tree** (run inside the profile directory; use the `pnpm`
bundled with DSH if you don't have one)

```powershell
pnpm install
```

**4. Restart DSH**, then check **Settings → Memory**.

**5. No Python? Fine.** Capture and injection still work; only automatic
consolidation is unavailable. On first start a dialog offers a one-click Python
install (official installer, current user only, no admin rights).

### It just works — nothing more to configure

Do these three things and memory starts **capturing and consolidating itself**:

| # | Do this | Why |
|---|---|---|
| 1 | Install the plugin (either method above) | The host only attaches the event hooks once host.js is loaded |
| 2 | Have Python 3.10+ | Capture and injection are pure JS, but **consolidation** needs Python; a one-click installer appears on first start if missing |
| 3 | Restart DSH | host.js / client.js load at startup |

After that, **every new session is automatic** — no per-session action, ever:

- what you say → captured into the inbox (tagged with its origin workspace)
- related topics → memory returns to context (as an injected *user* message, not a system prompt)
- session start → your user profile comes along
- inbox fills up → **extracted and consolidated automatically**, snapshot refreshed
  (`autoConsolidate`, on by default, zero configuration)

> **Why this isn't MCP**: this is a DSH **host plugin**, driven by host event hooks
> (`agent/pre-step` to inject, `session/event` to capture). The model never sees it,
> so there is no MCP server to register and no tool call for the model to make.

**The one manual step**: if you want LLM extraction (recommended), re-enter your API key
on the settings page — the ciphertext is DPAPI-bound to the current Windows user and cannot
be decrypted on another machine (a deliberate security property).

### Consolidation cadence (tunable, but you don't need to)

| Setting | Default | Meaning |
|---|---|---|
| `autoConsolidate` | `true` | Master switch; `false` returns to manual / externally scheduled runs |
| `autoConsolidateDelayMs` | `60000` | First run this long after startup |
| `autoConsolidateIntervalMs` | `1800000` | How often to check afterwards (30 min) |
| `autoConsolidateMinEntries` | `1` | Minimum inbox entries worth spawning Python for |
| `autoConsolidateMinIntervalMs` | `300000` | Minimum gap between two runs (avoids respawning) |

Put them in `$DSH_HOME/dsh-tide-memory.json`; defaults apply otherwise.

### Want a fixed schedule? Add a task (optional)

The built-in auto-consolidation is enough. If you want it to run at a **fixed time**
(for example nightly, together with lifecycle maintenance), add a DSH scheduled task:

```text
Run the daily memory consolidation task:

1. Run <plugin-dir>\lib\mem.cmd flush-inbox --llm
   (consume inbox.jsonl, extract and consolidate — this is the main step)
2. Then run <plugin-dir>\lib\mem.cmd maintain
   (check dependencies and TTL, expire what no longer holds)
3. Report: candidate count, action stats (insert/merge/invalidate/skip), expired count.

Constraint: never edit the store by hand, never touch memory.db directly.
```

Without an LLM configured, drop `--llm` from step 1 (rule-based, offline).
**They coexist peacefully** — the Python side deduplicates by fingerprint, so consuming
the same inbox twice never produces duplicate memories.

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
llm.json           # LLM config (base_url / model, **never the key**)
llm.secret         # encrypted key (DPAPI-bound to the current Windows user)
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

**Settings → Memory** lets you:

- browse memories grouped by workspace (current workspace / other workspaces / user profile)
- add / edit / delete (audited; edits and deletes across workspaces are refused)
- **read the engine status**: see at a glance whether extraction currently runs in
  **LLM mode** or **rule-engine mode**, with the checks behind that verdict
- **Test generation**: runs a full extract + consolidate pass and leaves
  **no real memory behind** — proof that the engine is actually working
- configure the LLM and API key (the key is stored encrypted; the panel only ever
  shows a mask)

### Workspace isolation

Every memory is bound to the workspace it was born in. Inside one workspace you see:

| Visible | Meaning |
|---|---|
| memories of the **current workspace** | conclusions reached here |
| the **user profile** (`global`) | name, job, general preferences — needed everywhere |
| ❌ memories of other workspaces | the panel shows "N more elsewhere" but **never their content** |

When the current workspace cannot be determined (headless host, for example) it
**fails closed**: only the user profile is released, never another project's content.

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
resolution get noticeably better.

**The recommended way is the settings page** (Settings → Memory → consolidation engine):

1. fill in `base_url` and `model`
2. paste the API key into the plain-text field and click **Save key**
3. the field clears immediately and the panel only ever shows the last 4 characters

The command line works too:

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

#### How the key is stored

| Platform | Mechanism |
|---|---|
| Windows | **DPAPI** (`CryptProtectData`), bound to the **current Windows user** — another machine or another user cannot decrypt it |
| Other | random 32-byte master key file (0600) + HMAC-SHA256 stream cipher + encrypt-then-MAC |

- `llm.json` holds **only** `base_url` / `model` / `timeout` / `retries` — **never the key**
- the ciphertext file `llm.secret` has its ACL narrowed to the current user, SYSTEM and
  Administrators; if permissions cannot be applied, saving aborts and removes the file
- **no API response ever carries the plaintext**; the panel only shows a mask (last 4 chars)
- upgrading from an older version: a plaintext `api_key` still sitting in `llm.json` is
  migrated into the ciphertext and the old field is erased
- when the ciphertext cannot be decrypted it reports `decryptable: false` instead of
  pretending everything is configured

**A failing model never corrupts the store**: timeout, disconnect, invalid JSON,
out-of-range target → all fall back to rule-based decisions. To see which engine is
*actually* in use, read the engine badge on the settings page or press **Test generation**.

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
| **Workspace isolation** | Every memory carries its origin workspace; recall only releases "current workspace + user profile", and fails closed when the workspace is unknown |
| **HTTP endpoint** | Three gates: loopback Host allowlist (anti DNS-rebinding) + Origin check + CSRF token |
| **Installer** | Authenticode signature check that **aborts on failure**; current user only, no system dirs |
| **Secrets** | Windows: **DPAPI**-encrypted `llm.secret` (bound to the current user). `llm.json` never holds the key; API responses only carry a last-4-chars mask |
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
- **Rule-based extraction covers the common conclusive patterns** (used when no LLM is
  configured): first-person preferences, `X uses Y`, `X is Y`, must/needs, zero-dependency
  constraints, causal conclusions, status changes — 10 of 12 typical conclusions recognised
  in testing. It deliberately does **not** do semantic judgement: sentences like
  "my main IDE is…" need a decision about which attribute they describe (job? preference?),
  and guessing there would merge unrelated attributes, so those wait for the LLM.
- **Scope resolution is rule-based**: project identity prefers a git remote or a
  project root; a standalone directory (e.g. `~/.dsh/plugins/xxx`) counts as its own
  project rather than belonging to a workspace. That is intentional fail-closed
  behaviour — better to recall less than to leak across projects. Complex cases can
  pass `--scope` / `--path` explicitly.
- **DPAPI does not travel**: on Windows the key ciphertext is bound to the current
  user, so another machine or user must save the key again. That is a deliberate
  security trade-off, not a bug.

---

## Under the hood

| | |
|---|---|
| Size | ~9,900 lines (6,200 Python + 3,700 JS) |
| Runtime deps | **0** npm packages, **0** pip packages |
| Storage | SQLite + FTS5 (Node 22.5+ builtin `node:sqlite`); falls back to the JSON snapshot when the host lacks `node:sqlite` |
| Verification | 4 suites (host isolation 20 / panel rendering 24 / engine & key 29 / zero side-effects 8) = 81 checks; **the scripts ship separately, not with the plugin** |
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
