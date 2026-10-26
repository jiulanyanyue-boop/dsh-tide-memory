# dsh-tide-memory

> **潮汐记忆** — 让 DeepSeek Harness 真正记住你，而且记得住、看得懂、会过期、能更新。

[![DSH](https://img.shields.io/badge/DSH-0.2.1%2B-blue)](https://github.com/deepseek-ai/deepseek-harness)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Node](https://img.shields.io/badge/Node-%3E%3D22.19-339933?logo=node.js)](https://nodejs.org)
[![Tests](https://img.shields.io/badge/tests-63%20passed-brightgreen)](lib/)
[![Dependencies](https://img.shields.io/badge/npm%20deps-0-brightgreen)](package.json)

[English](README.en.md) · 简体中文

---

## 为什么需要它

AI 的记忆系统有个通病：**存得进去，但读不懂、不会更新、删不掉。**

这不是猜测，是我们把市面上 147 个 DSH 记忆插件全部扒了一遍之后确认的。真实场景长这样：

```
三个月前，你和 AI 合作开发一个插件。它记下了：
  「当前插件存在路径穿越漏洞，需要修复」

三个月后，插件早就修好了、也发布了、甚至你自己都忘了做过这个项目。
但那句话还躺在记忆库里，每次相关对话都被翻出来。
```

具体有四个病：

| 病症 | 表现 |
|---|---|
| **没头没尾** | 「当前插件有漏洞」——哪个插件？换了项目之后谁还记得 |
| **不会更新** | 漏洞修好了，那条「有漏洞」还在，新旧两条一起召回 |
| **不会过期** | 项目删了，它的 15 条记忆还坚挺地出现在上下文里 |
| **记了不该记的** | 把一句工作指令抽成「用户偏好：也可以自检一遍安全相关问题」 |

dsh-tide-memory 就是针对这四条做的。

---

## 四个设计

### ① 三问闸门：先问值不值得记

任何记忆进库前必过三问，**答不上来就不记**：

| 追问 | 拦掉的例子 |
|---|---|
| **主语清楚吗？** | 「当前插件要小心」→ 哪个插件？没有名字，拦掉 |
| **脱离上下文能读吗？** | 「也可以自检一遍…：综合结论：当前插件不建议…」→ 口语碎片，拦掉 |
| **这是事实还是指令？** | 「你自己改完之后」→ 是给你的活，不是事实，拦掉 |

再加三条硬检查：谓词必须是 `[a-z][a-z0-9_]{1,40}`、值不超过 60 字、不能是空话（「要小心」）。

**实测**：一条真实的垃圾记忆被 `no-context` 挡住；同批正常记忆全部放行。

### ② 归属：每条记忆知道自己是哪个项目的

| 内容 | 归属 |
|---|---|
| 用户画像（名字、职业、偏好） | `global` |
| 本机环境（DSH 装哪、数据目录） | `workspace` |
| 具体项目（功能、bug、技术选型） | `project` → 绑 git remote 或路径 |

面板上显示成 **`［项目·archive-manager］`**，不是一串哈希。

**更重要的是召回时真的隔离**（fail-closed）：

```
在项目 A 提问 → 只召回 A + 全局       ✅ A 的密码不会进 B 的会话
在项目 B 提问 → 只召回 B + 全局
在无关目录   → 只召回全局
```

### ③ 生命周期：会过期，也会更新

**依赖消失自动失效**

记忆自己记下「我依赖这个路径」。检索前跑一次廉价检查（毫秒级）：

```
depends_on = path:C:\...\plugins\archive-manager
  ├─ 目录存在 → active
  └─ 目录删了 → status=expired, expired_by=scope-gone
```

**状态类事实会被覆盖**（这是关键）

状态必须用**稳定键**，否则「修好了」永远覆盖不掉「有漏洞」：

```
旧: security_status = has_vulnerabilities → status=superseded, expired_by=superseded
新: security_status = fixed               → status=active
```

> 我们实测过：键名只要变一下（`security_review_findings` vs `security_status`），
> 两条记忆**永远找不到对方，直接 insert，旧的纹丝不动**。这就是为什么状态键必须固定。

还有 TTL：`episode` 90 天、项目级 `fact` 180 天；`preference` / `reflection` 永不过期。

### ④ 可读性：面板只显示人话

存储层是 `(kind, predicate, object)` 三元组——**程序看的**。
显示层是 `summary`——**人看的**。

```
存储:  preference / responds_in / 简体中文
显示:  - [偏好] 用户希望回复用简体中文
```

原始键名折叠进「原始键名」小三角，想看才点开。**主人永远不用看见 `responds_in`。**

---

## 这东西怎么工作

```
                    ┌─────────────────────────────────┐
   你说话  ────────► │  agent/pre-step                 │
                    │  ├─ 三问闸门（值得记吗）          │
                    │  ├─ 归属判定（谁的记忆）          │
                    │  ├─ 检索（覆盖率打分 + 归属过滤）  │
                    │  └─ 注入 <system-reminder>       │
                    └──────────────┬──────────────────┘
                                   │
                    ┌──────────────▼──────────────────┐
                    │  SQLite（双时间 + 证据链）        │
                    │  ├─ valid_from / valid_to        │
                    │  ├─ supersedes / superseded_by   │
                    │  ├─ memory_evidence（原话）       │
                    │  └─ audit_log（每次决策）         │
                    └──────────────┬──────────────────┘
                                   │
                    ┌──────────────▼──────────────────┐
                    │  巩固状态机                       │
                    │  insert / merge / invalidate /   │
                    │  skip / reject / expire          │
                    └─────────────────────────────────┘
```

**记忆永不物理删除**：冲突标 `superseded`，过期标 `expired`，抽取失败标 `rejected`。
每次决策都进 `audit_log`，可复查、可回放。

### 为什么走 `agent/pre-step` 而不是 system prompt

这是我们调研整个 DSH 插件生态后确认的关键点：

> persona 声明 `complete: true` 的 preset，组装后会把 persona 恢复为唯一 prompt 段，
> **静默丢弃其它所有贡献，而且不报错**。

所以写 system prompt 的记忆插件在这类 preset 下会**彻底失效**（市面上一款流行插件就栽在这）。
`pre-step` 插的 user 消息不会被吞，而且每次注入都是可重放、对 compaction 可见的 session event。

### 子 agent 不是主人

子 agent 的委派 prompt **不是你说的话**，默认不采集也不注入：

```json
{"origin":"subagent","delegationDepth":1,"parentSession":"session-..."}
```

判据来自会话头，不是靠猜。

---

## 安装

```powershell
dsh plugin --profile web add github:jiulanyanyue-boop/dsh-tide-memory
```

装带版本 tag 的（更稳）：

```powershell
dsh plugin --profile web add github:jiulanyanyue-boop/dsh-tide-memory#v2.0.0
```

装完**重启一次 `dsh web`**。

> ⚠️ **一定要填 GitHub 地址，不要只填包名。**
> `dsh plugin add` 会把裸包名当 npm registry 名解析。npm 上有人占了同名包，
> 你就会装到别人的东西，还可能和当前 DSH 版本不兼容。

### 前置条件

| 依赖 | 要求 | 说明 |
|---|---|---|
| Node.js | `>=22.19.0` | DSH 本身要求，装 DSH 就有了 |
| Python | `3.10+` | 引擎用纯标准库，**不需要 pip install** |

**没有 Python 也能用**——采集和注入照常工作，只是不会自动整理。
第一次启动如果检测不到 Python，会弹窗提示并给**一键安装**按钮（官方安装包、仅当前用户、无需管理员）。
详见 [WITHOUT-PYTHON.md](WITHOUT-PYTHON.md)。

### 数据放在哪

`$DSH_HOME/dsh-tide-memory/`（通常是 `C:\Users\<你>\.dsh\dsh-tide-memory\`）：

```
memory.db          # 记忆库（SQLite）
memories.json      # 检索快照（插件读这个）
inbox.jsonl        # 采集的原料
scope-roots.json   # 项目路径登记表
llm.json           # LLM 配置（如果配了）
```

**没有任何路径写死在代码里**——全部基于 `DSH_HOME` 或 `~/.dsh` 推导，换电脑换用户名都能跑。

---

## 怎么用

### 日常：什么都不用做

插件挂在宿主事件上，**采集和注入全自动**，模型完全不感知：

- 你说的话 → 自动进 `inbox.jsonl`
- 相关话题 → 自动把记忆带回上下文
- 会话开场 → 自动带上用户画像（名字、职业、偏好）

### 设置页

**设置 → 记忆**，可以：

- 浏览全部记忆（含候选池、已失效）
- 新增 / 编辑 / 删除（走审计日志）
- 看引擎状态、配 LLM

### 命令行

```powershell
# 消费 inbox：抽取 + 巩固（规则版，毫秒级）
lib\mem.cmd flush-inbox

# 配了 LLM 想更准
lib\mem.cmd flush-inbox --llm

# 按查询调取记忆
lib\mem.cmd context --query "编辑器偏好"

# 按归属看
lib\mem.cmd by-scope project

# 生命周期维护（依赖消失 / TTL 过期）
lib\mem.cmd maintain
lib\mem.cmd maintain --dry-run

# 统计
lib\mem.cmd stats
```

### 配 LLM（可选，但建议）

不配也能用（规则判定、离线）。配了之后抽取和冲突判定会准很多：

```powershell
lib\mem.cmd llm set --base-url https://api.deepseek.com/v1 --model deepseek-chat
# 按提示输入密钥（不会回显，也不会进入命令历史）
lib\mem.cmd llm test
```

自动化环境可将密钥通过标准输入传入，而不要放进命令行参数：

```powershell
$env:MEMORY_KEY = Read-Host 'API key' -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($env:MEMORY_KEY)
try { $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr); $plain | lib\mem.cmd llm set --api-key-stdin }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr); Remove-Item Env:MEMORY_KEY }
```

本地 Ollama 也行：

```powershell
lib\mem.cmd llm set --base-url http://127.0.0.1:11434/v1 --model qwen2.5:7b
```

为保护 API key，远程 LLM 地址必须使用 HTTPS；明文 HTTP 只允许 `localhost` 或 loopback IP（例如本机 Ollama）。
密钥配置文件在 Unix 上设为仅当前用户可读写；Windows 上 DACL 限定为当前用户、SYSTEM 和管理员，权限设置失败会中止并删除该配置文件。

**模型挂了不会写坏库**：LLM 超时 / 断连 / 非法 JSON / 越界 target → 全部回退规则判定。

---

## 数据模型

| 字段 | 作用 |
|---|---|
| `valid_from` / `valid_to` | **双时间**：事实何时为真。冲突只写 `valid_to`，永不删除 |
| `recorded_at` | 何时被记下（与事实时间分开） |
| `confidence` | 置信度；低于 0.5 进候选池，被更强证据支持才晋升 |
| `supersedes` / `superseded_by` | 新旧链，可回溯整条演变 |
| `source_session` / `source_seq` | 来源，配合 `memory_evidence` 回到原话 |
| `scope` / `scope_ref` | 这条记忆属于谁（global / workspace / project） |
| `summary` | 给主人看的一句人话 |
| `depends_on` | 依赖什么才成立；依赖没了自动失效 |
| `expired_by` | 失效原因（superseded / scope-gone / ttl / manual） |

---

## 安全与隐私

我们认真做过的：

| 项 | 做法 |
|---|---|
| **数据位置** | 全部在 `$DSH_HOME` 下，不写插件目录、不写系统目录 |
| **HTTP 端点** | 三道闸：Host 回环白名单（防 DNS rebinding）+ Origin 同源校验 + CSRF token |
| **安装脚本** | Authenticode 签名校验，**验证失败直接中止**；只装当前用户、不碰系统目录 |
| **密钥** | 存 `$DSH_HOME/dsh-tide-memory/llm.json`（0600），`.gitignore` 挡住；支持环境变量不落盘 |
| **导出** | 默认导到 `$DSH_HOME`，不进插件目录；`.gitignore` 覆盖 |
| **网络** | 不配 LLM 时零网络；配了之后只有抽取/冲突判定会发片段给你指定的端点 |

**明确的边界**（不粉饰）：

- **删除不是彻底清除。** 记忆不再参与召回，但内容仍留在 `audit_log`。想彻底抹除需手动删 `memory.db`。
- **不是多用户隔离。** API 的三道闸挡得住浏览器和跨站攻击，挡不住本机恶意进程（它本来就能读你的磁盘）。
- **零遥测、零第三方依赖。** 插件只 import `node:` 内建模块；引擎只用 Python 标准库。

---

## 已知限制

- **中文检索用 2-gram + 覆盖率打分**，不做向量召回。
  同义不同词（「写代码用 neovim」vs「编辑器是 nvim」）目前认不出。
- **单进程 SQLite（WAL）**：个人使用没问题，多进程并发写需要串行化队列。
- **规则抽取很保守**：任务型会话经常一条都抽不出来（有意为之，生产请用 LLM 抽取）。
- **归属靠规则判定**：跨项目的复杂场景可能需要手动指定 `--scope` / `--path`。

---

## 技术细节

| 项 | 值 |
|---|---|
| 代码量 | 约 5,100 行（Python 3,600 + JS 1,500） |
| 测试 | 63 个单元测试，全过 |
| 运行时依赖 | **0** 个 npm 包、**0** 个 pip 包 |
| 存储 | SQLite + FTS5（Node 22.5+ 内置 `node:sqlite`） |
| DSH | `0.2.1+` |

### 实测数据

| 场景 | 改造前 | 改造后 |
|---|---|---|
| 「记忆有什么优化空间」召回 | 0 条（长记忆被 Jaccard 稀释到 0.0104） | **3 条**（覆盖率打分） |
| 跨项目泄漏 | 项目 A 记忆进入项目 B 会话 | **完全隔离**，无关目录只放行全局 |
| 垃圾记忆进库 | 直接写入 | **闸门拦下**，标 `rejected` |
| 状态更新 | 新旧两条并存 | **旧的 superseded，新的 active** |
| 项目删除后 | 记忆永远存活 | **自动 `expired_by=scope-gone`** |

---

## 卸载

```powershell
dsh plugin --profile web remove dsh-tide-memory
```

数据留在 `$DSH_HOME/dsh-tide-memory/`。要一起删就手动删那个目录（**建议先备份 `memory.db`**）。

---

## License

[MIT](LICENSE)
