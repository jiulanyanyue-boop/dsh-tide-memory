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

### ① 闸门：先问值不值得记

任何记忆进库前必过**五问**，**答不上来就不记**：

| 追问 | 拦掉的例子 |
|---|---|
| **主语清楚吗？** | 「当前插件要小心」→ 哪个插件？没有名字，拦掉 |
| **脱离上下文能读吗？** | 「也可以自检一遍…：综合结论：当前插件不建议…」→ 口语碎片，拦掉 |
| **这是事实还是指令？** | 「你自己改完之后」→ 是给你的活，不是事实，拦掉 |
| **可复用吗？** | 「刚才改到第 3 行」「下一步看 client.js」「已经跑完了」→ 过程与待办，拦掉 |
| **无时间锚吗？** | 「今天用 neovim」→ 只在此刻成立；除非带「以后/一直/习惯」 |

再加四条硬检查：谓词必须是 `[a-z][a-z0-9_]{1,40}`、值不超过 60 字、不能是空话（「要小心」）、不能是临时路径噪音（`…\Temp\tmpXXXX`）。

**关键取舍**：带「曾导致 / 根因 / 必须 / 已修复」这类**结论回指**的句子要放行——
它们讲的是已发生事件的因果结论，恰恰是最该记的经验，不能因为句子里有过程词就误杀。

**实测**：一条真实的垃圾记忆被 `no-context` 挡住；同批正常记忆全部放行。
存量 52 条按新标准体检，只有 2 条被拦。

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
dsh plugin --profile web add github:jiulanyanyue-boop/dsh-tide-memory#v2.2.0
```

装完**重启一次 `dsh web`**。

> ⚠️ **一定要填 GitHub 地址，不要只填包名。**
> `dsh plugin add` 会把裸包名当 npm registry 名解析。npm 上有人占了同名包，
> 你就会装到别人的东西，还可能和当前 DSH 版本不兼容。

### 手动安装（离线包 / 换电脑）

拿到的是一个 `dsh-tide-memory` 目录（无 `node_modules`，纯源码）。放到**任意位置**都行，
插件不依赖安装位置——所有数据路径都由 `DSH_HOME` 推导。步骤：

**1. 放插件本体**（位置随你，下面用 `<插件目录>` 代指）

```powershell
# 例：放到 harness home 下的 plugins 目录
Move-Item <解压出来的 dsh-tide-memory> "$env:USERPROFILE\.dsh\plugins\dsh-tide-memory"
```

**2. 让 profile 认到这个插件**

编辑 `<DSH_HOME>\profiles\<你的profile>\package.json`（通常是 `profiles\desktop\package.json`）：

```jsonc
{
  "dependencies": {
    // 加这一行：link: 后面写你上一步的实际绝对路径
    "dsh-tide-memory": "link:<插件目录>"
  },
  "dsh": {
    "profile": {
      "bundles": [
        // 数组末尾加这一行
        "dsh-tide-memory"
      ]
    }
  }
}
```

**3. 同步依赖树**（在 profile 目录下执行，`pnpm` 用 DSH 自带的即可）

```powershell
pnpm install
```

**4. 重启 DSH**，然后到 **设置 → 记忆** 确认面板出现。

**5. 没装 Python 也不影响**：采集和注入照常工作，只是不会自动整理。
第一次启动会弹窗提示，给「一键安装 Python」按钮（官方安装包、仅当前用户、无需管理员）。

### 装完就自动工作，不需要再配什么

这三件事做完，**记忆就开始自动采集与整理**：

| # | 做什么 | 为什么 |
|---|---|---|
| 1 | 装插件（见上面两种装法之一） | 宿主才会加载 host.js 并挂上事件钩子 |
| 2 | 有 Python 3.10+ | 采集和注入是纯 JS，但**整理记忆**要跑 Python；没有则首次启动会弹窗+一键安装 |
| 3 | 重启 DSH | host.js / client.js 都是启动时加载的 |

之后**每一个新会话都会自动**：

- 你说的话 → 自动进收件箱（带来源工作区）
- 相关话题 → 自动把记忆带回上下文（插一条 user 消息，不是 system prompt）
- 会话开场 → 自动带上用户画像
- 收件箱攒够 → **自动抽取整理**，并刷新检索快照（`autoConsolidate`，默认开，零配置）

> **为什么不是 MCP**：本插件是 DSH **宿主插件**，走宿主的事件钩子
> （`agent/pre-step` 注入、`session/event` 采集），**模型完全不感知**，
> 所以不需要任何 MCP 配置、也不需要模型主动调用。

**唯一需要手动的一件事**：如果你要用 LLM 抽取（推荐），得在设置页**重新填一次 API Key**——
密钥密文用 DPAPI 绑定了当前 Windows 用户，换机器解不开（这是刻意的安全设计）。

### 自动整理的节奏（可调，但不必调）

| 配置项 | 默认 | 含义 |
|---|---|---|
| `autoConsolidate` | `true` | 总开关；设 `false` 回到「完全靠手动/外部定时任务」 |
| `autoConsolidateDelayMs` | `60000` | 启动后多久跑第一次 |
| `autoConsolidateIntervalMs` | `1800000` | 之后每隔多久检查一次（默认 30 分钟） |
| `autoConsolidateMinEntries` | `1` | 收件箱至少几条才值得起一次 Python 进程 |
| `autoConsolidateMinIntervalMs` | `300000` | 两次整理之间的最小间隔（防反复起进程） |

写进 `$DSH_HOME/dsh-tide-memory.json` 即可，不写就用默认值。

### 想要更精细的节奏？可选配一个定时任务

插件自带的自动整理已经够用。如果你想让它在**固定时间**跑（比如每天凌晨，
顺便做生命周期维护），再配一个 DSH 定时任务：

```text
执行每日长期记忆巩固任务：

1. 运行 <插件目录>\lib\mem.cmd flush-inbox --llm
   （消费 inbox.jsonl，抽取并巩固；这一步是主流程）
2. 然后运行 <插件目录>\lib\mem.cmd maintain
   （检查依赖与 TTL，让该失效的记忆失效）
3. 汇报：候选数、动作统计（insert/merge/invalidate/skip）、失效条数。

约束：不要手工修改记忆库，不要直接编辑 memory.db。
```

不配 LLM 时把第 1 步的 `--llm` 去掉（走规则抽取，离线可用）。
**两者并存不冲突**：Python 侧有指纹去重，重复消费不会产生重复记忆。

> 💡 **这条任务现在可以直接在面板里开**：设置 → 记忆 → 设置页签 →
> 「每日定时整理（LLM 巩固）」开关 + 时刻输入框，保存立即生效，不用再配外部任务。
> 开关写进 `$DSH_HOME/dsh-tide-memory.json`（`dailyConsolidate` / `dailyConsolidateTime`）。

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
llm.json           # LLM 配置（base_url / model，**不含密钥**）
llm.secret         # 密钥密文（Windows 用 DPAPI 绑定当前用户）
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

- 按工作区分组浏览记忆（当前工作区 / 其它工作区 / 用户画像）
- 新增 / 编辑 / 删除（走审计日志，跨工作区的改删会被拒绝）
- **看引擎状态**：一眼分清当前走的是「LLM 生成模式」还是「规则引擎模式」，附判定依据
- **测试生成**：点一下就跑一次完整抽取+巩固流程，**不留下任何真实记忆**，用来验证引擎是否真的在工作
- 配 LLM 与 API Key（密钥加密存储，面板只回显掩码）

### 工作区隔离

每条记忆都绑定它诞生的那个工作区。在某个工作区里，你只会看到：

| 可见 | 说明 |
|---|---|
| **当前工作区**的记忆 | 在这个工作区里得出的结论 |
| **用户画像（global）** | 名字、职业、通用偏好——跨工作区都要带 |
| ❌ 其它工作区的记忆 | 面板只显示「别处还有 N 条」，**不给内容** |

拿不到当前工作区时（例如无头环境）**fail-closed**：只放行用户画像，绝不把别的项目内容漏过来。

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

不配也能用（规则判定、离线）。配了之后抽取和冲突判定会准很多。

**推荐在设置页里配**（设置 → 记忆 → 巩固引擎）：

1. 填 `base_url` 与 `model`
2. 在明文输入框里粘贴 API Key，点「保存密钥」
3. 输入框会立刻清空，界面只回显末 4 位掩码；旁边的徽标会显示 `DPAPI 已加密`

想用命令行也行：

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

#### 密钥怎么存的

| 平台 | 做法 |
|---|---|
| Windows | **DPAPI 加密**（`CryptProtectData`），密文绑定**当前 Windows 用户**；换机器/换用户就解不开 |
| 其他 | 随机 32 字节主密钥文件（0600）+ HMAC-SHA256 流式加密 + encrypt-then-MAC |

- `llm.json` 里**只有** `base_url` / `model` / `timeout` / `retries`，**永不写密钥**
- 密文文件 `llm.secret` 的 ACL 收紧到「当前用户 + SYSTEM + 管理员」，权限设置失败会中止并删除文件
- **任何 API 响应都不含明文**，面板只回掩码（末 4 位）
- 老版本升级上来时，若 `llm.json` 里还有明文 `api_key`，会自动迁进密文并抹掉原字段
- 解不开时会如实报 `decryptable: false`，而不是假装已经配好

**模型挂了不会写坏库**：LLM 超时 / 断连 / 非法 JSON / 越界 target → 全部回退规则判定。
想知道当前**实际**用的哪个引擎，看设置页的引擎徽标，或点「测试生成」跑一次。

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
| **工作区隔离** | 每条记忆绑定来源工作区；查询只放行「当前工作区 + 用户画像」，拿不到工作区时 fail-closed |
| **HTTP 端点** | 三道闸：Host 回环白名单（防 DNS rebinding）+ Origin 同源校验 + CSRF token |
| **安装脚本** | Authenticode 签名校验，**验证失败直接中止**；只装当前用户、不碰系统目录 |
| **密钥** | Windows 用 **DPAPI** 加密存 `llm.secret`（绑定当前用户）；`llm.json` 不含密钥；API 响应只回末 4 位掩码 |
| **导出** | 默认导到 `$DSH_HOME`，不进插件目录；`.gitignore` 覆盖 |
| **网络** | 不配 LLM 时零网络；配了之后只有抽取/冲突判定会发片段给你指定的端点 |

**明确的边界**（不粉饰）：

- **删除不是彻底清除。** 记忆不再参与召回，但内容仍留在 `audit_log`。想彻底抹除需手动删 `memory.db`。
- **不是多用户隔离。** API 的三道闸挡得住浏览器和跨站攻击，挡不住本机恶意进程（它本来就能读你的磁盘）。
- **零遥测、零第三方依赖。** 插件只 import `node:` 内建模块；引擎只用 Python 标准库。

---

## 自动更新与 Star 引导

插件自带升级能力，设置页里有「插件升级」卡片：

- **三个版本**：运行版本（本进程启动时加载的）、磁盘版本（当前文件）、最新版本
- **检查更新**：多源回退（依次尝试，任一成功即用）

  | 顺序 | 源 | 说明 |
  |---|---|---|
  | 1 | `api.github.com/.../releases/latest` | 最新 release |
  | 2 | `api.github.com/.../tags` | 最高 semver tag |
  | 3 | `raw.githubusercontent.com/.../package.json` | 静态兜底，不耗配额 |
  | 4 | `cdn.jsdelivr.net/.../package.json` | 静态兜底 |

  1、2 会**合并取较高者**——因为 `releases/latest` 常常落后于 tags，只看它会永远收不到升级提示。
  配额耗尽（HTTP 403 + `x-ratelimit-remaining: 0`）时自动跳过 API 源，走静态源。
  每个源独立 12 秒超时、ETag 条件请求、只对白名单域名跟随跳转。
- **失败不写缓存**：成功才记 6 小时 TTL；失败只记 `lastErrorAt` 并做 60 秒退避，
  避免连点按钮把匿名配额烧光（GitHub 匿名额度只有 60 次/小时）。
- **诊断可查**：卡片上有「查看诊断」，逐源显示状态码与耗时；`update_diag` 只回放内存快照，不触网。
- **立即升级**：按安装形态选路径 ——
  - 目录里有 `.git`：`git fetch --tags` + `git reset --hard` + `git checkout --detach <tag>`
  - 没有 `.git`：下载 tag 源码包解压覆盖（保留 `.git` / `node_modules` / `var` 等用户数据）；
    `codeload.github.com` 失败会回退 `github.com/.../archive/refs/tags/<tag>.tar.gz`
- **重启提示**：升级只改磁盘，运行中的代码不会变，所以卡片会显示「代码已更新，重启 DSH 后生效」
- **去 GitHub 点 Star**：调系统默认浏览器打开仓库页；只接受 `https://github.com/...`，不接受任意 URL

要有更新源，`package.json` 里必须写真实仓库地址（`repository`）；地址的**所有者**是占位符（如 `YOUR_GITHUB_USER`）时会被识别为「无更新源」，
卡片会如实显示而不是假装检查过。

> 两个插件各自独立实现这套能力（各有一份 `update/status`、`update/apply`、`open-repo`），
> 互不 import、不共享文件或状态，只共用「行为约定」这一层。
## 更新日志

### v2.2.0（2026-10-27）

**面板：页签化 + 可视化设置**

- 学习 our-free-model 的 `ofm_tabs` 交互，把原来一条竖直瀑布
  （升级卡片 → 引擎卡片 → 记忆列表，要看记忆得滚到底）
  改成四个页签：**记忆（默认）· 巩固引擎 · 设置 · 插件升级**
- 「记忆」页签带条数角标；「新增记忆」按钮只在记忆页显示
- 「设置」页新增**自动整理开关**：一点就切换，走 `settings_get` / `settings_set`
  持久化到 `$DSH_HOME/dsh-tide-memory.json`，host 下一次整理即生效，**无需重启**
- 白名单外的设置键（如 db 路径）后端拒绝并在响应里回传 `rejected`——
  路径、密钥这类敏感项**不可能**从面板改
- 后端没升级时开关禁用并注明原因，不崩、不误报保存成功

**自动整理：零配置**

- 插件加载后**自己**在启动延迟后跑一次整理，之后每 30 分钟检查一次；
  收件箱攒够才起 Python 进程，同一时刻只跑一个
- 修掉一个致命缺口：原来采集与注入是自动的，但**整理完全靠外部触发**——
  新机器装完插件后话被采集了、检索也在跑，可**快照永远是空的，一条记忆都注入不出来**
- `autoConsolidate: false` 可回到「完全手动」，六个 `autoConsolidate*` 参数都能调
- 与外部定时任务并存不冲突（Python 侧有指纹去重）

**规则抽取：25% → 83%**

- 旧规则只有 8 条且几乎全是第一人称句式（「我喜欢…」「我希望…」），
  对 12 条典型结论只识出 3 条
- 补上陈述句：`这个项目用 X` / `X 是 Y` / `必须 X` / `零依赖` / `因为 X` / `已修复` / `我不用 X`
- 修 object 质量：`neovim 写代码` → `neovim`；`回复用简体中文` → `简体中文`
- 加宽规则的代价被控住：`must` 加负向断言排除纯态度词，闸门新增纯态度词兜底
- 实测 13 条样例全部符合预期（该入库的入库、该拦的拦住）

**闸门：三问 → 五问**

- 新增「可复用吗」：过程叙述与待办（「刚才改到第 3 行」「下一步看 client.js」）一律不记
- 新增「无时间锚吗」：只在此刻成立的句子不记；带「以后/一直/习惯」的长期结论放行
- **结论回指要放行**：「曾导致…根因是…」这类已发生事件的因果结论是最该记的经验，不因含过程词而误杀
- 附加硬检查：对话碎片、临时路径噪音（`…\Temp\tmpXXXX`）

**密钥：明文文件 → 加密存储**

- Windows 用 **DPAPI** 绑定当前用户加密，密文存 `llm.secret`；`llm.json` 里永不含密钥
- 面板新增明文输入框：保存后立刻清空，只回显末 4 位掩码
- 老装的明文 `api_key` 会自动迁进密文并抹掉原字段
- 新增 `decryptable` 状态：换机器后解不开时如实上报，不假装已配好
- 掩码只留末 4 位（不再露前缀）

**工作区隔离：更严格**

- 新增 `workspaces` 登记表，工作区显示成**人话**（`default-workspace`，不是哈希）
- 列表改成按工作区分组：当前工作区 → 其它工作区 → 用户画像
- 其它工作区只显示「别处还有 N 条」，**不给内容**；跨工作区的改删请求会被拒绝
- 修掉一个真 bug：`subject='user'` 的候选曾被无差别升级成 global，
  导致项目结论漏进别的项目

**引擎可观测性（新增）**

- 设置页显示当前实际生效的引擎：**LLM 生成模式** / **规则引擎模式**，附判定依据
- 新增「测试生成」按钮：跑完整抽取+巩固流程，**零副作用**（临时库，跑完即删）

**更新检查：修掉三个真问题**

- 多源回退（releases → tags → raw → jsDelivr），配额耗尽自动跳静态源
- **失败不再写缓存**（原来连点按钮会反复烧匿名配额）
- **版本匹配修正**：`releases/latest` 常落后于 tags，现在两源合并取较高者——
  否则仓库发了新版却没打 release 时，你永远收不到升级提示

### v2.1.0

- 记忆归属（global / workspace / project）与生命周期（依赖消失自动失效、TTL）
- 三问闸门、面板只显示人话 summary
- 自动更新与 Star 引导

---

## 已知限制

- **中文检索用 2-gram + 覆盖率打分**，不做向量召回。
  同义不同词（「写代码用 neovim」vs「编辑器是 nvim」）目前认不出。
- **单进程 SQLite（WAL）**：个人使用没问题，多进程并发写需要串行化队列。
- **规则抽取覆盖常见结论句式**（不配 LLM 时用）：第一人称偏好、`X 用 Y`、`X 是 Y`、
  必须/需要、零依赖约束、因果结论、状态变更等 —— 实测 12 条典型结论识出 10 条。
  它**不做语义判断**：像「用户日常主力 IDE 是…」这类需要判断属性归属（是职业还是偏好）
  的句子**有意不覆盖**，因为规则层硬猜会产生错误的属性合并。要更准就配 LLM。
- **归属靠规则判定**：项目身份优先用 git remote / 项目根路径识别；
  独立目录（如 `~/.dsh/plugins/xxx`）会被当成一个独立项目，不归属到某个工作区——
  这是有意的 fail-closed，宁可少召回也不串项目。复杂场景可手动指定 `--scope` / `--path`。
- **DPAPI 不跨机**：Windows 下密钥密文绑定当前用户，换机器/换用户需重新保存密钥。
  这是刻意的安全取舍，不是 bug。

---

## 技术细节

| 项 | 值 |
|---|---|
| 代码量 | 约 9,900 行（Python 6,200 + JS 3,700） |
| 运行时依赖 | **0** 个 npm 包、**0** 个 pip 包 |
| 存储 | SQLite + FTS5（Node 22.5+ 内置 `node:sqlite`）；宿主无 `node:sqlite` 时读 JSON 快照 |
| 验证 | 5 套验证脚本（宿主隔离 20 / 面板渲染 24 / 引擎与密钥 29 / 零副作用 8 / **自动整理 11**），合计 92 项；**脚本不随包发布** |
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
