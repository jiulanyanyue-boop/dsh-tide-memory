# 没有 Python 会怎样？

一句话：**记忆能记下来，但不会自己整理。**

下面说清楚哪些能用、哪些不能，以及怎么补上。

---

## 一、能力对照表

| 功能 | 没有 Python | 说明 |
|---|---|---|
| **采集你说过的话** | ✅ 正常 | 插件直接把消息写进 `inbox.jsonl`，纯 JS |
| **把相关记忆带回上下文** | ✅ 正常 | 插件读 `memories.json` 快照，纯 JS |
| **设置页浏览记忆** | ✅ 正常 | 读快照，纯 JS |
| **设置页新增 / 编辑 / 删除** | ❌ 不可用 | 这些操作要走巩固状态机（Python） |
| **从对话里自动抽取新记忆** | ❌ 不可用 | 需要调 LLM + 读会话日志 |
| **冲突判定**（同一条事实变了怎么办） | ❌ 不可用 | 需要状态机 |
| **自动过期**（项目删了记忆该失效） | ❌ 不可用 | 需要维护任务 |
| **一句话自动归类**（生成 `summary`） | ❌ 不可用 | 需要语义层 |

---

## 二、实际会发生什么

### 短期看：一切正常

你说话，插件记下来；下次相关话题，插件把记忆带回上下文。**这部分完全不依赖 Python。**

### 几天后：开始感觉到问题

`inbox.jsonl` 会一直变长，但**没有东西消费它**：

```
inbox.jsonl    ← 原料越堆越多，没人加工
memory.db      ← 库里的记忆还是那几条，没增加
```

具体表现：

- **新偏好不会生效**——你说「以后回复都用简体中文」，但下次还是老样子，因为这句话躺在 inbox 里没被抽取
- **矛盾不会解决**——你说「现在改用 vim 了」，但库里的「用 neovim」还在，两条都召回
- **过期的还在**——项目删了，它相关的记忆还坚挺地出现在上下文里
- **设置页只能看不能改**——点「新增记忆」会报错

**不是崩了，是卡住了。** 而且因为没有提示，你会以为「这插件没什么用」。

---

## 三、为什么会这样

这是历史原因，不是设计初衷：

```
最初 memory-consolidation 是个独立 Python 项目
        ↓
后来加了 DSH 插件，为了复用巩固逻辑 → 直接 execFile 调 Python
        ↓
结果：插件一半 JS 一半 Python
```

**技术上 Python 已经不是必需品了。** 我们实测验证过：

- `node:sqlite` 是 Node 22.5+ 内置的，DSH 自带 Node v24，一定有
- 它能直接读写 Python 建的库（中文正常）
- 它支持 FTS5 全文索引
- DSH 自己就在用它（`dsh-session-query-sqlite`）

所以把引擎移植到 JS 是可行的，只是工作量大（约 2000 行）。见 `research/python-dependency-analysis.md`。

---

## 四、怎么补上

### 方法 A · 一键安装（推荐）

在 DSH 设置页里点一下就会弹窗，弹窗上有 **「一键安装 Python」** 按钮：

- 从 python.org 下载官方安装包（3.12 x64）
- **只装给当前用户**，装到默认位置（`%LOCALAPPDATA%\Programs\Python\Python312`）
- 自动加进用户 PATH
- **不需要管理员权限**，不改系统环境变量，不删任何东西

装完回弹窗点 **「重新检测」** 即可。

### 方法 B · 手动装

从 [python.org/downloads](https://www.python.org/downloads/) 下载安装：

> ⚠️ 安装时**一定要勾选** `Add python.exe to PATH`，否则 DSH 找不到它。

装完重启 DSH。

### 方法 C · DSH 桌面端通常自带

DSH 桌面版把 Python 放在运行时目录里：

```
$DSH_HOME/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe
```

**如果你用的是桌面端，很可能本来就有一份，插件会自动找到。** 只有从 npm 装的 CLI 版本才通常没有。

### 方法 D · 指定已有 Python

如果你机器上已经有 Python，只是插件没找到，设个环境变量就行：

```powershell
$env:DSH_PYTHON = "C:\path\to\python.exe"
```

或者写进 `$DSH_HOME/dsh-tide-memory.json`：

```json
{ "python": "C:\\path\\to\\python.exe" }
```

---

## 五、插件怎么找 Python

按这个顺序，找到第一个能用的就停：

1. `DSH_PYTHON` 环境变量
2. 配置文件里的 `python` 字段
3. `$DSH_HOME/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe`（**DSH 桌面端自带**）
4. `~/.dsh/dsh-runtimes/.../python.exe`
5. `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`
6. `%LOCALAPPDATA%\Programs\Python\Python311\python.exe`
7. PATH 里的 `python`

要求：**Python 3.10 或更高**。3.9 及以下不行（缺一些语法特性）。

---

## 六、常见问题

**Q：装了 Python 但还是说找不到？**
A：PATH 没刷新。关掉 DSH 重开，或者在新终端里 `python --version` 确认能用。装了但版本是 3.9 或更老也不行。

**Q：一键安装卡住了？**
A：安装程序可能弹了 UAC 或界面窗口被其他窗口挡住。看看任务栏。也可以手动跑安装包。

**Q：能不能完全不要 Python？**
A：现在不行。计划是把引擎移植到 JS（Node 内置 sqlite），做完就零依赖了。这需要时间。

**Q：没有 Python 期间采集的数据会丢吗？**
A：**不会。** `inbox.jsonl` 会一直攒着，等你装好 Python 后跑一次 `flush-inbox` 就能全部补处理。
