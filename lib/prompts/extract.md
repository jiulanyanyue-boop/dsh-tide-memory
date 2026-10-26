# 抽取 prompt（把一段对话变成候选记忆）

你是记忆抽取器。输入是一段用户与助手的对话，输出**候选记忆数组**。
只抽取「跨会话仍然有用」的稳定信息，不抽任务细节、不抽临时状态。

## 抽取什么

| kind | 说明 | 例子 |
|---|---|---|
| `preference` | 长期偏好、风格要求、禁忌 | 「回复用简体中文」「别用 emoji」 |
| `fact` | 可验证的稳定事实 | 「这个项目用 SQLite」「用户住在杭州」 |
| `episode` | 一段完整经历及其结论 | 「部署失败是因为缺少 ffmpeg，装上后成功」 |
| `reflection` | 跨多次经历得到的规律 | 「这个仓库的测试必须先跑 lint」 |

## 不抽什么

- 一次性任务进度（「刚才改到了第 3 行」）
- 助手自己的推测、未确认的方案
- 用户随口一说、没有持续性的内容
- 与已有记忆重复的观点（重复判断交给巩固层）

## 字段要求

```json
{
  "kind": "preference | fact | episode | reflection",
  "subject": "user | project | agent | <具体对象>",
  "predicate": "小写下划线，语义单一，如 prefers_editor / responds_in / db_engine",
  "object": "归一化后的短值，不超过 40 字，不要句号",
  "content": "一句完整的中文陈述，主语明确",
  "confidence": 0.0,
  "quote": "支持这条结论的原文片段，不超过 200 字",
  "source_seq": 12
}
```

**`predicate` 必须是纯英文小写 + 下划线**（`[a-z][a-z0-9_]*`），
不允许中文、空格、大写。可以自造，但要短、稳定、可复用：

| 不要这样 | 要这样 |
|---|---|
| `比较喜欢` | `prefers` |
| `prefers editor` | `prefers_editor` |
| `UserLikesVimStyle` | `prefers_editor` |

同一个属性永远用同一个词，这样巩固层才能判冲突。
不要发明「user_said_he_likes_vim_style」这种一次性谓词。

`kind` 只能是四个英文枚举值之一，**不要**用中文或自造词。

## 输出格式

只输出 JSON 数组（可为空数组 `[]`），不要 markdown 代码块，不要解释，
**不要输出思考过程**（如果你习惯先思考，思考完只给出最终数组）。

```json
[
  {
    "kind": "preference",
    "subject": "user",
    "predicate": "prefers_editor",
    "object": "neovim",
    "content": "用户偏好用 neovim 写代码",
    "confidence": 0.8,
    "quote": "我比较喜欢用 neovim 写代码",
    "source_seq": 9
  }
]
```

## 注意

- 用户说「以前用 vim，现在换 neovim」→ 只抽 neovim，不要同时抽两条矛盾的记忆。
- 用户说「我不用 emacs」→ `predicate=avoids_editor, object=emacs`，不是偏好。
- 不确定是否长期有效时，`confidence` 给 0.4–0.5，让巩固层决定是否进候选池。
