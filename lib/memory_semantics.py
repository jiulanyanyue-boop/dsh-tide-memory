"""记忆语义层：归属、生命周期、可读性、入库闸门。

这一层解决四件「记忆本身是错的」的问题（跟检索无关）：

  ① 归属    —— 这条记忆属于谁？全局 / 工作区 / 某个项目
  ② 生命周期 —— 它会过期吗？依赖的东西没了怎么办？
  ③ 可读性  —— 主人看得懂吗？（summary 一句人话）
  ④ 闸门    —— 这条值得记吗？（三问：主语清楚 / 脱离上下文能读 / 不过时）

设计原则：全部零依赖、纯函数优先，便于单测。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------- #
# ① 归属：scope / scope_ref
# --------------------------------------------------------------------------- #

SCOPE_GLOBAL = "global"
SCOPE_WORKSPACE = "workspace"
SCOPE_PROJECT = "project"

#: 状态类事实必须使用的稳定键。只有拿稳定键，新状态才能覆盖旧状态。
STATUS_PREDICATES = {
    "security_status",
    "build_status",
    "test_status",
    "deploy_status",
    "dev_phase",
    "open_issues",
    "current_blocker",
    "next_action",
    "publish_status",
}

#: 用户画像类键 -> 这些天然属于 global
USER_PREDICATES = {
    "user_name", "nickname", "user_job", "responds_in", "prefers_report_language",
    "prefers_editor", "prefers_theme", "prefers_explanation_style",
    "knowledge_level", "prefers_ui_theme_consistency", "prefers_hover_reveal_actions",
    "prefers_cleanup_debug_artifacts", "prefers_github_link_install",
}

#: 环境类键 -> 属于 workspace（跟这台机器/这个工作区绑定）
ENV_PREDICATES = {
    "dsh_install_path", "dsh_user_data_path", "dsh_host_loads_from",
    "plugin_install_source", "primitives_bundle_location", "slot_contract_source",
}


def normalize_path(path: str) -> str:
    """规范化路径：统一分隔符、去尾斜杠、小写盘符（Windows 大小写不敏感）。

    顺序很重要：先换分隔符再 strip，否则 'e:\\x\\\\' 会残留反斜杠。
    """
    p = str(path).strip().replace("/", "\\")
    p = p.rstrip("\\") or p
    if len(p) >= 2 and p[1] == ":":
        p = p[0].lower() + p[1:]
    return p.lower() if _is_windows() else p


def _is_windows() -> bool:
    import os

    return os.name == "nt"


def workspace_ref(cwd: str) -> str:
    """工作区身份：cwd 规范化后的短哈希（路径变则身份变，可接受）。"""
    return "ws:" + hashlib.sha1(normalize_path(cwd).encode("utf-8")).hexdigest()[:12]


def project_ref(path: str) -> str:
    """项目身份：优先 git remote（跨机器稳定），否则路径哈希。

    同时把真实路径记进 var/scope-roots.json，供 scope_ref_label() 反查名字。
    """
    root = find_project_root(path)
    remote = try_git_remote(root)
    _remember_scope_root(root)
    if remote:
        return "git:" + normalize_git_url(remote)
    return "path:" + hashlib.sha1(normalize_path(root).encode("utf-8")).hexdigest()[:12]


def _data_dir() -> Path:
    """数据目录：$DSH_HOME/dsh-tide-memory/。

    **绝不写进插件包内部**——插件目录在升级/重装时会被替换，
    而且安装位置可能是只读的（node_modules）。所有运行时数据都放 DSH_HOME。
    """
    import os

    dsh_home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    return Path(dsh_home) / "dsh-tide-memory"


def _remember_scope_root(root: str) -> None:
    """把项目根记进登记表（幂等，失败静默）。"""
    import json

    reg = _data_dir() / "scope-roots.json"
    try:
        reg.parent.mkdir(parents=True, exist_ok=True)
        existing: list[str] = []
        if reg.exists():
            existing = [
                p for p in json.loads(reg.read_text(encoding="utf-8")) if isinstance(p, str)
            ]
        norm = normalize_path(root)
        if norm not in existing:
            existing.append(norm)
            reg.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass


def find_project_root(path: str) -> str:
    """向上找项目根：有 .git 或 package.json 的那一层，最多上溯 6 层。"""
    p = Path(normalize_path(path))
    cur = p if p.is_dir() else p.parent
    for _ in range(6):
        if (cur / ".git").exists() or (cur / "package.json").is_file():
            return str(cur)
        if cur.parent == cur:
            break
        cur = cur.parent
    return str(p if p.is_dir() else p.parent)


def try_git_remote(root: str) -> str | None:
    """读 .git/config 里的 origin url（不调 git 命令，零依赖）。"""
    cfg = Path(root) / ".git" / "config"
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', text)
    return m.group(1) if m else None


def normalize_git_url(url: str) -> str:
    """git@github.com:a/b.git -> github.com-a-b（与 OpenViking 同一思路）。"""
    u = url.strip()
    u = re.sub(r"^git@([^:]+):", r"\1/", u)
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"\.git$", "", u)
    return re.sub(r"[^A-Za-z0-9]+", "-", u).strip("-").lower()


@dataclass
class Scope:
    scope: str
    scope_ref: str

    def describe(self) -> str:
        if self.scope == SCOPE_GLOBAL:
            return "全局"
        if self.scope == SCOPE_WORKSPACE:
            return "本工作区"
        return f"项目·{scope_ref_label(self.scope_ref)}"

    def to_json(self) -> dict[str, str]:
        return {"scope": self.scope, "scope_ref": self.scope_ref}


#: 缓存 scope_ref -> 人话名字，避免每次读盘
_SCOPE_REF_LABELS: dict[str, str] = {}


def scope_ref_label(ref: str) -> str:
    """把 'path:5b2c3d1ed9fb' / 'git:github-com-a-b' 变成主人看得懂的名字。"""
    if not ref:
        return ""
    cached = _SCOPE_REF_LABELS.get(ref)
    if cached:
        return cached

    label = ref
    m = re.match(r"^(path|git):(.*)$", ref)
    if m:
        kind, value = m.group(1), m.group(2)
        if kind == "git":
            # github-com-volcengine-openviking -> 末两段
            parts = [p for p in value.split("-") if p]
            label = "-".join(parts[-2:]) if len(parts) >= 2 else value
        else:
            # path:<hash> 是人算不出含义的哈希；尝试反查已知项目目录
            label = _resolve_hashed_path(value) or "本地项目"
    label = label or ref
    _SCOPE_REF_LABELS[ref] = label
    return label


def known_project_refs(cwd: str) -> list[str]:
    """给定会话 cwd，返回它可能对应的**全部**项目身份标识。

    为什么需要「全部」：同一个项目可能被登记成不同形式——
      · `path:<hash>`（没有 git remote 的本地目录）
      · `git:github-com-a-b`（有 git remote，跨机器稳定）
    宿主 JS 侧只算 path:，Python 侧可能算 git:。两边不一致时，
    自己项目的记忆反而召回不了（这是实测发现的坑）。

    所以召回时**两种形式都接受**，比「统一算法」更宽容、也更不容易漏。
    """
    if not cwd:
        return []
    norm = normalize_path(cwd)
    refs: list[str] = []

    # 候选根：cwd 本身 + 已登记的所有项目根里能覆盖 cwd 的那些
    roots = [norm]
    for reg in _load_scope_roots():
        nr = normalize_path(reg)
        if norm == nr or norm.startswith(nr + "\\"):
            roots.append(nr)

    for root in dict.fromkeys(roots):
        refs.append("path:" + hashlib.sha1(root.encode("utf-8")).hexdigest()[:12])
        remote = try_git_remote(root)
        if remote:
            refs.append("git:" + normalize_git_url(remote))

    return list(dict.fromkeys(refs))


def _load_scope_roots() -> list[str]:
    """读登记表里记录过的项目根。"""
    import json

    reg = _data_dir() / "scope-roots.json"
    try:
        if reg.exists():
            return [p for p in json.loads(reg.read_text(encoding="utf-8")) if isinstance(p, str)]
    except (OSError, json.JSONDecodeError):
        pass
    return []


def _resolve_hashed_path(hashed: str) -> str | None:
    """把路径哈希反查成目录名。

    两级策略（便宜的先做）：
      1. 直接读登记表里记录过的路径，算哈希比对 —— 不碰磁盘遍历
      2. 扫候选目录 —— 兜底，兼容登记表缺失的老库
    """
    # 1. 登记表直查
    reg = _data_dir() / "scope-roots.json"
    try:
        import json

        if reg.exists():
            for raw in json.loads(reg.read_text(encoding="utf-8")):
                if not isinstance(raw, str):
                    continue
                h = hashlib.sha1(normalize_path(raw).encode("utf-8")).hexdigest()[:12]
                if h == hashed:
                    return Path(normalize_path(raw)).name
    except (OSError, json.JSONDecodeError):
        pass

    # 2. 兜底：扫候选目录
    for root in _project_scan_roots():
        try:
            entries = [p for p in Path(root).iterdir() if p.is_dir()]
        except OSError:
            continue
        for p in entries:
            h = hashlib.sha1(normalize_path(str(p)).encode("utf-8")).hexdigest()[:12]
            if h == hashed:
                return p.name
    return None


def _project_scan_roots() -> list[str]:
    """候选扫描目录：DSH 插件目录 + 当前工作区 + 记录过的项目目录。"""
    import json
    import os

    roots: list[str] = []
    home = os.environ.get("DSH_HOME") or str(Path.home() / ".dsh")
    roots.append(str(Path(home) / "plugins"))
    roots.append(os.getcwd())
    # 迁移/写入时记录过的真实项目路径（比哈希可读，且能反查名字）
    reg = _data_dir() / "scope-roots.json"
    try:
        if reg.exists():
            for p in json.loads(reg.read_text(encoding="utf-8")):
                if isinstance(p, str):
                    roots.append(p)
    except (OSError, json.JSONDecodeError):
        pass
    return roots


def resolve_scope_from_cwd(
    *,
    predicate: str,
    subject: str,
    cwd: str,
) -> Scope:
    """从会话 cwd 判定归属。采集路径（inbox）专用。

    优先级：
      1. 用户画像类谓词 → global（跟项目无关，跨项目都要带上）
      2. 环境类谓词     → workspace（跟这台机器绑定）
      3. 有 cwd 且能定位到项目 → project（绑那个项目）
      4. 拿不到 cwd     → global（不能瞎猜成某个项目）

    这样「项目 A 说的话」就不会因为 subject=user 而被归到 global。
    """
    if predicate in USER_PREDICATES:
        return Scope(SCOPE_GLOBAL, "")
    if predicate in ENV_PREDICATES:
        # 没有 cwd 时不产出「空 scope_ref 的 workspace」——
        # 那种记忆在召回侧永远匹配不上任何 ref，等于写进去就消失。
        ref = workspace_ref(cwd) if cwd else ""
        return Scope(SCOPE_WORKSPACE, ref) if ref else Scope(SCOPE_GLOBAL, "")
    if cwd:
        return Scope(SCOPE_PROJECT, project_ref(cwd))
    # 没有任何归属信息：只能是全局。宁可范围大，也不能错绑到别的项目。
    return Scope(SCOPE_GLOBAL, "")


def resolve_scope(
    *,
    predicate: str,
    subject: str,
    cwd: str = "",
    mentioned_path: str = "",
) -> Scope:
    """判定一条记忆该归属到哪。规则优先，不让模型猜。

    ⚠️ 关键修正（本次）：**不能靠 `subject == 'user'` 判 global**。
    抽取器给绝大多数候选填的 subject 都是 `user`（「用户提到……）」），
    于是「这个项目的更新检查必须多源回退」这类**项目级结论**全被误判成 global，
    结果项目 A 的结论漏进了项目 B 的会话——这正是要消灭的跨工作区污染。

    新的优先级：
      1. 明确是用户画像键 → global（跨项目通用的偏好/身份）
      2. 环境类键           → workspace
      3. 状态类键           → project（有路径）/ workspace
      4. 有 cwd             → project（项目级结论就该跟项目走）
      5. 什么都没            → global（无法判定时范围最小地保守处理）
    """
    if predicate in USER_PREDICATES:
        return Scope(SCOPE_GLOBAL, "")
    if predicate in ENV_PREDICATES:
        ref = workspace_ref(cwd) if cwd else ""
        return Scope(SCOPE_WORKSPACE, ref)
    if predicate in STATUS_PREDICATES:
        # 状态类默认跟项目走；没有路径信息就退到 workspace
        if mentioned_path:
            return Scope(SCOPE_PROJECT, project_ref(mentioned_path))
        ref = workspace_ref(cwd) if cwd else ""
        return Scope(SCOPE_WORKSPACE, ref)
    # 有路径线索：项目级
    if mentioned_path:
        return Scope(SCOPE_PROJECT, project_ref(mentioned_path))
    # 有 cwd：这条结论是在某个工作区里得出的，就该留在这个工作区，
    # 不能再因为 subject=user 而无差别升级成 global。
    if cwd:
        return Scope(SCOPE_PROJECT, project_ref(cwd))
    # 真的没有任何归属线索：范围最小地保守处理
    return Scope(SCOPE_GLOBAL, "")


# --------------------------------------------------------------------------- #
# ② 生命周期：depends_on / 过期
# --------------------------------------------------------------------------- #

EXPIRED_SCOPE_GONE = "scope-gone"
EXPIRED_TTL = "ttl"
EXPIRED_SUPERSEDED = "superseded"
EXPIRED_MANUAL = "manual"

#: 按 kind 的默认 TTL（天）。None = 永不过期。
DEFAULT_TTL_DAYS: dict[str, int | None] = {
    "preference": None,
    "reflection": None,
    "fact": None,      # 项目级 fact 会被 scope-gone 清掉，不靠 TTL
    "episode": 90,
}

#: 项目级 fact 更短：项目细节会变
PROJECT_FACT_TTL_DAYS = 180


def ttl_days_for(kind: str, scope: str) -> int | None:
    if scope == SCOPE_PROJECT and kind == "fact":
        return PROJECT_FACT_TTL_DAYS
    return DEFAULT_TTL_DAYS.get(kind)


@dataclass
class DependsOn:
    """这条记忆依赖什么才成立。依赖没了 -> 自动失效。"""

    type: str      # "path" | "git-remote" | ""
    value: str = ""

    def to_json(self) -> dict[str, str]:
        return {"type": self.type, "value": self.value}

    @staticmethod
    def parse(raw: str | None) -> "DependsOn":
        if not raw:
            return DependsOn("", "")
        m = re.match(r"^(path|git-remote):(.*)$", raw, re.S)
        if not m:
            return DependsOn("", "")
        return DependsOn(m.group(1), m.group(2).strip())

    def serialize(self) -> str:
        return f"{self.type}:{self.value}" if self.type and self.value else ""


def dependency_alive(dep: DependsOn) -> tuple[bool, str]:
    """检查依赖是否还在。返回 (是否存活, 说明)。"""
    if not dep.type or not dep.value:
        return True, "无依赖"
    if dep.type == "path":
        p = Path(dep.value)
        return (p.exists(), "路径存在" if p.exists() else f"路径已不存在：{dep.value}")
    if dep.type == "git-remote":
        # 不联网，只验证本地仓库还在
        return True, "git-remote（离线不校验）"
    return True, "未知依赖类型"


# --------------------------------------------------------------------------- #
# ④ 闸门：这条值得记吗（三问）
# --------------------------------------------------------------------------- #

#: 看不出主语的词
VAGUE_SUBJECTS = {"当前插件", "这个插件", "该项目", "这个项目", "它", "那个", "此项", "上述"}

#: 口语化开头，说明这是对话不是事实
CHATTY_PREFIXES = (
    "你看看", "你看", "我看看", "也可以", "顺便", "另外", "还有", "对了",
    "其实", "不过", "但是", "而且", "所以", "然后", "就是说", "我想",
)

#: 明显是「指令」而不是「事实」的句式
INSTRUCTION_MARKERS = (
    "你自己", "帮我", "告诉我", "你去", "需要修复", "改完之后", "自检一遍",
    "要不要", "能不能", "可以不可以",
    # ── 祈使/排除句式（v2.3.1 补）──────────────────────────────────
    # 历史漏网：「你别管真实服务器，只需要回答我问题」抽出了
    # reflection/must/"回答我问题" —— 这是一句**当下的请求**，
    # 不是可复用的规则。旧表里没有任何一个词能命中它。
    "回答我", "别管", "只需要", "不用管", "别问", "只回答", "别废话",
    "听我说", "先别说", "不要解释", "直接给",
)


@dataclass
class GateResult:
    ok: bool
    reason: str
    code: str = "ok"


#: 过程性句式（正则）。**只认真正描述「进行中/未完成」的句式**，
#: 而不是简单包含「先跑」「现在」这类词——「必须先跑 lint」是规律，不是过程。
_PROCESS_PATTERNS = tuple(
    re.compile(p) for p in (
        r"^(?:刚才|刚刚|现在|目前|正在|暂时|临时)(?!.*(?:用|是|总是|习惯))",
        r"(?:下一步|接下来)(?:要|再|去|看|做|改|处理)?",
        r"(?:等会儿|稍后|待会|回头)(?:再|要|去|看|做|改|处理)",
        # 「打算/准备/计划」只有在**句首**才是计划叙述；
        # 「附英文版，同时准备中文简介」里的「准备」是动作，不是计划。
        r"^(?:打算|准备|计划|想要|想先)",
        r"(?:，|。|,|^)\s*(?:打算|准备|计划)(?:把|去|要|在|将)",
        r"(?:还没|尚未|未)(?:有|做|改|写|跑|看|处理|完成|确认|验证|定|测)",
        r"(?:还没|尚未|未)(?=[^\s]{1,12})",
        r"(?:跑|改|写|做|测|看)(?:完|完了|好了|到第)",
        r"已经(?:.{0,8}?)(?:了)$",
        r"(?:先|再)(?:试|跑|看|改)(?:一下|一遍|看看)",
        r"(?:正在|在)(?:排查|调试|尝试|试|写|改|跑)",
        # ── 裸「…了」完成态（v2.3.1 补）─────────────────────────────
        # 历史漏网：「服务器我重装了」。它不是「正在做」，但更不是结论——
        # 它陈述的是**这次**发生过的一个动作，下次会话毫无复用价值。
        # 判据：主语性短语 + 动作动词 + 了的完成态，且全程没有结论词。
        # 之所以不笼统地见「了」就拦：像「链路未清理导致了残留」是结论，
        # 由 _CONCLUSION_MARKERS（「导致」「原因」）先行放行。
        r"(?:我|我们|他|她|它|你)?(?:重装|重启|删除|安装|卸载|升级|回滚|迁移|"
        r"部署|配置|修改|更改|替换|切换|更新|刷新|清理|恢复|导入|导出|备份|"
        r"拉取|推送|提交|合并|回退|格式化|初始化|重建|编译|打包|发布|上线|"
        r"跑通|修好|搞定|处理|整理|生成|下载|上传|打开|关闭|启动|停止|登录|登出)"
        r"(?:了|过了|完了|掉了|好了)",
    )
)

#: 结论性回指：句子里出现这些词，说明它讲的是**已经发生过的事的结论**，
#: 而不是「现在正在做」——即使含过程词也应放行。
#: 例：「归档→恢复→删除的操作序列曾导致残留，原因是链路未清理」
#:     「曾导致」是已完成因果，属于可复用经验。
#:
#: ⚠️ 这组词只对 **content** 生效，且对 root_cause 谓词永久失效。
#:    历史 bug：旧版把 obj+content 拼起来查、且无条件放行，于是
#:    「因为服务器我重装了」里的那个「因为」本身就命中了 root_cause 规则，
#:    又给自己发了免检通行证——过程叙述的判定整条被跳过，
#:    「服务器我重装」就这么进了库。免检必须窄到「只免过程判定，
#:    且只认 content 里真正的结论词」。
_CONCLUSION_MARKERS = (
    "曾导致", "曾经", "导致过", "原因", "根因", "根本原因", "教训", "经验",
    "因此", "所以必须", "必须", "需要保证", "结论", "规律", "修复后", "已修复",
    "曾经导致", "引发", "造成了",
)

#: 这些谓词抽出来的值天生容易是**一次性事件的起因**，需要额外警惕。
#: 注意：这**不是**免检开关——「链路未清理（导致残留）」是真正可复用的
#: 因果结论，必须放行。真正的一次性事件靠下面的形态判定拦。
_NO_EXEMPT_PREDICATES = frozenset()  # 保留位：按「谓词」一刀切会误伤真结论

#: 句子片段判据：值里出现人称代词 = 这是半句话，不是一个「值」。
#:
#: 这是拦住「服务器我重装」「回答我问题」最稳的一条。
#: 好的记忆 object 永远是一个**值**（root / 192.168.120.52:8003 / 简体中文 /
#: IntelliJ IDEA），而「服务器我重装」是「因为服务器我重装了」被截下的半句，
#: 「回答我问题」是「你别管真实服务器，只需要回答我问题」被截下的半句。
#: 它们共同点：带着第一/第二人称代词，脱离原句就没意义。
#: 而「链路未清理」（真正的因果结论）不含代词，不受影响。
_FRAGMENT_PRONOUNS = ("我", "你", "咱")

#: 环境事实类谓词：它们的值天生短、精确、信息密度高。
#: 给这类开「短值豁免」——账号 root、主机名 db1 都该记。
ENV_FACT_PREDICATES = frozenset({
    "ssh_endpoint", "host_ip", "server_ip", "hostname", "login_account",
    "login_user", "ssh_port", "service_port", "db_host", "db_port",
    "db_name", "db_user", "conn_string", "endpoint", "base_url",
    "deploy_target", "deploy_path", "environment", "server_role",
    "db_engine", "api_key", "login_credential",
})

#: 身份类谓词：值是姓名/昵称/语言这类**短但完整**的原子值。
#: 「刘润」「中文」「简体中文」都只有 2~4 字，却是最稳的长期事实，
#: 绝不能被「值太短」拦掉——这是把闸门修过头的典型误伤。
IDENTITY_PREDICATES = frozenset({
    "user_name", "nickname", "user_job", "responds_in", "prefers_language",
    "prefers_report_language", "prefers_theme", "prefers_editor",
    "user_city", "user_company", "user_role", "prefers_os",
    "prefers_shell", "prefers_db", "prefers_ide",
})

#: 时间锚：带这些词意味着「只在此刻成立」，脱离上下文就没意义。
#: 注意「今天开始用 neovim」这类能归一化成一辈子的偏好，靠 CONTEXTUAL_TENSE
#: 排除：只有「今天/这次」而没有「以后/长期/一直」才判为带锚。
TIME_ANCHORS = (
    "今天", "昨天", "今晚", "今早", "这次", "本次", "这回", "上次", "上回",
    "这一轮", "本轮", "刚才那条", "上面那条", "前面提到",
)

#: 长期性标记：和 TIME_ANCHORS 同时出现时视为「由时间点引入的长期结论」，放行
LONGTERM_MARKERS = (
    "以后", "长期", "一直", "今后", "从现在", "永久", "习惯", "每次", "总是",
)

#: 对话碎片：整句就是这些词，没有任何信息
CHAT_FRAGMENTS = {
    "好的", "好", "嗯", "哦", "ok", "okay", "继续", "收到", "明白", "了解",
    "开始吧", "来吧", "可以", "不行", "对的", "是的", "不对", "没错",
    "谢谢", "多谢", "辛苦了", "麻烦了", "你好", "在吗",
}

#: 临时路径特征：从 Temp 目录里学到的东西不具复用价值
_TEMP_PATH_RE = re.compile(
    r"(?:\\|/)Temp(?:\\|/)|(?:\\|/)tmp[0-9a-z_]{4,}|AppData\\Local\\Temp",
    re.IGNORECASE,
)


def _is_chat_fragment(text: str) -> bool:
    t = re.sub(r"[\s。，！？、,.!?~～…]+", "", (text or "").lower())
    return bool(t) and t in CHAT_FRAGMENTS


def _is_process_narrative(text: str) -> bool:
    """过程叙述：正在做 / 还没做 / 下一步做什么。这类不可复用。

    但「曾导致 / 根因 / 必须 / 已修复」这类**结论回指**要放行：
    它们讲的是已发生事件的因果结论，恰恰是最该记的那种经验。
    """
    t = (text or "").strip()
    if not t:
        return False
    if any(m in t for m in _CONCLUSION_MARKERS):
        return False
    return any(p.search(t) for p in _PROCESS_PATTERNS)


def _has_time_anchor(text: str) -> bool:
    """带时间锚 = 只在此刻成立。

    「今天开始用 neovim，以后都这样」不算——它有时间词，但结论是长期的：
    句子里同时出现长期性标记时放行，否则一律按「只在此刻成立」丢弃。
    """
    t = (text or "").strip()
    if not t:
        return False
    if not any(a in t for a in TIME_ANCHORS):
        return False
    return not any(m in t for m in LONGTERM_MARKERS)


def _is_temp_noise(text: str) -> bool:
    return bool(_TEMP_PATH_RE.search(text or ""))


#: 动作指令首词：值以这些动词开头 = 在描述「要做的一件事」，不是稳定规则。
#: 对 20 条真实漏网样本 + 10 条真实好记忆校准：19 拦 1 放 / 零误伤。
ACTION_HEADS = (
    "定位", "核实", "登录", "截图", "支持", "获取", "触发", "参考", "修复", "排查",
    "测试", "试试", "验收", "实现", "配置", "安装", "部署", "上传", "下载", "打开",
    "点击", "检查", "确认", "填写", "启动", "运行", "执行", "处理", "整理", "生成",
)

#: 一次性语境词：值里出现 = 这件事做完就失效
ONE_SHOT_HINTS = (
    "这个问题", "这个 bug", "这个bug", "该问题", "本次", "这之后",
    "体检报告", "看图", "如下图", "截图",
)

#: 裸产品名：must/requires/root_cause 谓词下值只是个产品名，无语义
_BARE_PRODUCT_NAMES = {
    "claude", "google", "claude desktop", "chatgpt", "微信", "qq", "gpt", "gemini",
}


#: 名词性成分：值以动作词开头、但后面跟的是这些名词时，它是**名词短语**
#: 而不是动作指令。「下载文件夹」= 一个目录，不是「去下载」这个动作。
_NOUN_SUFFIXES = (
    "文件夹", "目录", "文件", "路径", "列表", "按钮", "页面", "接口", "记录",
    "面板", "设置", "选项", "字段", "清单", "位置", "地址", "名称", "名字",
    "文档", "配置", "开关", "主题", "风格", "格式", "版本", "日志",
)


def _is_noun_phrase(t: str) -> bool:
    """值是不是一个名词短语（哪怕它以动作词开头）。"""
    return any(t.endswith(suffix) or suffix in t[-4:] for suffix in _NOUN_SUFFIXES)


def _is_one_shot_instruction(obj: str) -> str | None:
    """一次性动作/诉求检测：返回拒绝理由；None 表示不是。

    判别的是「形态合法但语义空洞」的漏网形态——购物需求清单里的
    「必须支持热插拔」、闲聊里的「登录才能看」、任务描述里的
    「定位存储逻辑的问题根因」。它们不是句子结构问题，是**用途**问题：
    做完/聊完就失效，不属于跨会话记忆。
    """
    t = (obj or "").strip()
    if not t:
        return None
    # 以动作词开头，但整体是名词短语 -> 不是动作指令
    # （「下载文件夹」是用户的交付习惯，「下载」只是词头）
    if any(t.startswith(v) for v in ACTION_HEADS) and not _is_noun_phrase(t):
        return "动作指令：描述要做的一件事，不是稳定规则"
    if any(h in t for h in ONE_SHOT_HINTS):
        return "一次性语境：问题解决后这条就失效了"
    # 需求/协商语气：当下的诉求，不是长期成立的结论
    # 「还要用，够安全就行」「应该可以测试连通性」「实际手机上试试」
    if re.search(r"就行$|就可以了?$|应该可以|到时候|再(说|看)|先这样|试试$|要用的?$", t):
        return "协商语气：当下的诉求，不是长期成立的结论"
    # 裸产品名：must/requires/root_cause 谓词下只写个产品名，无复用语义
    if t.lower() in _BARE_PRODUCT_NAMES:
        return "裸产品名：不含可复用的语义"
    # 指代性短语：『这些工具』『那个方案』——脱离会话不知道指什么
    if re.match(r"^(这些|那些|这个|那个)", t):
        return "指代性短语：脱离会话不可读"
    # 条件依赖：「额外设置才能实现自动调用」「根据APP引导填信息就能启动」
    if re.search(r"才能|就能|才可以", t):
        return "条件依赖：描述当下达成目标的手段，不是稳定结论"
    # 当下定性：「本来就是聊天」——离开会话无主语
    if re.search(r"本来就", t):
        return "当下定性：离开会话无主语"
    # 一次性操作安排：「另一个电脑上push」——设备相关的一次性动作
    if re.search(r"(电脑|手机|机器|虚拟机|环境)上", t) and len(t) <= 16:
        return "一次性操作安排：设备相关的一次性动作"
    return None


def worth_remembering(*,
    kind: str,
    predicate: str,
    obj: str,
    content: str,
    quote: str = "",
) -> GateResult:
    """五问闸门。任何一问不过就不该进库。

    比旧版多两问（可复用 / 无时间锚），因为旧版放行了大量
    「刚才改到第 3 行」这类过程叙述，把库塞成了流水账。
    """
    text = f"{obj} {content}".strip()

    # 问 1：主语清楚吗？
    if _has_vague_subject(obj) or _has_vague_subject(content):
        return GateResult(False, "主语不明：不知道说的是谁/哪个项目", "no-subject")

    # 问 2：脱离上下文还成立吗？
    if _requires_context(obj) or _requires_context(content):
        return GateResult(False, "前没头后没尾：脱离上下文不可读", "no-context")

    # 问 3：这是事实还是指令？
    if _looks_like_instruction(text, quote):
        return GateResult(False, "这是对话/指令，不是可复述的事实", "instruction")

    # 问 4：可复用吗？——过程叙述与对话碎片一律丢
    if _is_chat_fragment(obj) or _is_chat_fragment(content):
        return GateResult(False, "对话碎片：没有可复用的信息", "fragment")

    # 问 4.5：这个 object 是「一个值」还是「半句话」？
    #
    # 这是拦住「服务器我重装」「回答我问题」最稳的一条——
    # 它俩形态合法（有主语、无时间锚、够长），但都是半句。
    # 判据两条：
    #   ① 值里带人称代词 → 「因为服务器我重装**了**」被截下的半句
    #   ② 值是「主谓宾」结构而非名词短语 → 后半段出现动作动词
    # 好记忆的 object 永远是名词性的值（root / 简体中文 / IntelliJ IDEA /
    # 192.168.120.52:8003 / 链路未清理），碰不到这两条。
    if _is_sentence_fragment(obj):
        return GateResult(False, "值不是名词短语：是句子被截下的半截", "fragment-sentence")

    # 结论免检：**只看 content**。
    #
    # 历史 bug：旧版把 obj+content 拼起来查结论词、且无条件跳过过程判定。
    # 「服务器我重装」的 content 是「用户提到：服务器我重装」，
    # 而它的 predicate 恰恰是 root_cause（由「因为」触发）——
    # 「因为」既是抽取规则的触发器，又被当成了结论词，
    # 于是这条过程叙述一路免检通关。
    # 现在：只认 content 里真正的结论词，且该词必须**真的在陈述结论**
    # （「导致/根因/教训」），而不是抽取规则的触发词本身。
    exempt = any(m in content for m in _CONCLUSION_MARKERS)
    if not exempt:
        if _is_process_narrative(content) or _is_process_narrative(obj):
            return GateResult(False, "过程叙述：正在做/还没做，不是结论", "process")

    # 问 5：无时间锚吗？
    if _has_time_anchor(obj) or _has_time_anchor(content):
        return GateResult(False, "带时间锚：只在此刻成立，下次用不上", "time-anchored")

    # 附加：键名必须是合法谓词
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", predicate or ""):
        return GateResult(False, f"谓词不合法（不是英文小写键）：{predicate!r}", "bad-predicate")

    # 附加：值不能太短——「核实」「截图」「些什么」「L1 + L2」这类超短语
    # 即使形态合法也几乎没有复用价值（v2.2.0 实测漏网 25 条的共性）。
    # 真正的结论值（SQLite / IntelliJ IDEA / 简体中文）都 ≥ 4 个有效字符。
    #
    # ⚠️ 例外：环境事实类谓词的「值」本来就短且精确。
    #    账号 root、主机名 db1、端口 8003 —— 4 字门槛会把**最该记的**拦掉。
    #    这不是放宽标准，是承认「短」有两种：碎片式短（无信息）与
    #    标识符式短（信息密度极高）。判据是「含 ASCII 标识符」而不是长度。
    obj_stripped = re.sub(r"[\s（）()【】\[\]……]+", "", obj or "")
    if len(obj_stripped) < 4:
        # 短值的两种合法形态：
        #   ① 环境事实：标识符式短值（root / d1 / 192.168.120.52:8003）
        #   ② 身份事实：姓名/昵称/语言这类原子值（刘润 / 中文）
        # 两种都不是「碎片」，是无歧义、无冗余的完整值。
        exempt = (
            (predicate in ENV_FACT_PREDICATES and _is_identifier_value(obj))
            or (predicate in IDENTITY_PREDICATES and _is_atomic_value(obj))
        )
        if not exempt:
            return GateResult(False, f"值太短（{len(obj_stripped)} 字）：更像片段不像结论", "short-object")

    # 附加：值不能是整段话
    if len((obj or "").strip()) > 60:
        return GateResult(False, f"object 过长（{len(obj)}字），不像是一个值", "long-object")

    # 附加：不能是临时目录噪音
    if _is_temp_noise(text):
        return GateResult(False, "临时路径噪音：不具复用价值", "temp-noise")

    # 附加：不能是空话
    if _is_vapid(obj) or _is_vapid(content):
        return GateResult(False, "空话：只有态度没有事实", "vapid")
    # 附加：值本身就是纯态度词（「必须小心」抽出的那个「小心」）
    if (obj or "").strip() in VAPID_WORDS:
        return GateResult(False, "空话：值只有态度没有事实", "vapid")

    # 附加：一次性动作/诉求检测（v2.2.0 上线后实测漏网 25 条的针对性补丁）。
    # 这些值形态合法（主语清楚、非碎片、无时间锚），但语义上是「当下要做的一件事
    # 或当下的诉求」——做完/聊完就失效。判别式对 20 条真实漏网样本 19 拦 1 放，
    # 对 10 条真实好记忆零误伤（多源回退 / 先跑 lint / 避免被识别为脚本 等均放行）。
    action_reason = _is_one_shot_instruction(obj or "")
    if action_reason:
        return GateResult(False, action_reason, "one-shot")

    return GateResult(True, "ok")


def _is_sentence_fragment(obj: str) -> bool:
    """判断 object 是「一个值」还是「半句话」。

    只看 object，不看 content——content 本来就该是一句完整陈述，
    而 object 必须是名词性的值。

    两条判据：
      ① 含第一/第二人称代词：「服务器我重装」「回答我问题」
         好的值不会带「我」「你」。
      ② 名词短语里塞进了动作动词并且像主谓结构。
         这条更保守：只在「前半是名词、后半是动词+了/过」时触发，
         避免误伤「先跑 lint」「避免被识别为脚本」这类规则型结论。
    """
    t = (obj or "").strip()
    if not t:
        return False
    if any(p in t for p in _FRAGMENT_PRONOUNS):
        return True
    # 「服务器我重装」的加强判据：名词 + 动词完成态，中间没有任何连接词
    if _PREDICATE_TAIL_RE.search(t):
        return True
    return False


#: 名词短语尾部挂着「动作+了/过」= 这是叙述句不是值。
#: 例：「服务器我重装」「密码改过」「配置换了」
_PREDICATE_TAIL_RE = re.compile(
    r"^[^\s]{1,10}"
    r"(?:重装|重启|删除|安装|卸载|升级|回滚|迁移|部署|配置|修改|更改|替换|"
    r"切换|更新|刷新|清理|恢复|导入|导出|备份|拉取|推送|提交|合并|回退|"
    r"格式化|初始化|重建|编译|打包|发布|上线|跑通|修好|搞定|处理|整理|"
    r"生成|下载|上传|打开|关闭|启动|停止|登录|登出)"
    r"(?:了|过|完|掉|好)$"
)


def _has_vague_subject(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    # 以泛指词开头且后面没有具体名字
    for v in VAGUE_SUBJECTS:
        if t.startswith(v):
            # 「当前插件 archive-manager」这种有具体名字的放过
            rest = t[len(v):].strip(" ：:是")
            if len(rest) < 4:
                return True
    # 泛指词出现在句首附近（前 6 字内）且全句没有具体标识 -> 主语不明
    head = t[:6]
    if any(v in head for v in VAGUE_SUBJECTS):
        if not _has_concrete_identifier(t):
            return True
    return False


_CONCRETE_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9_.\-/\\]{2,}"      # 英文标识/路径
    r"|[A-Z]:\\"                            # Windows 路径
    r"|[\u4e00-\u9fff]{2,}(插件|项目|脚本|工具)"  # 带名字的中文名词
)

#: 空话尾巴：这些词结尾说明没有实际信息
VAPID_TAILS = (
    "要小心", "注意点", "得注意", "很重要", "别搞错", "当心", "要留意",
    "要记住", "别忘了", "小心点", "多注意",
)

#: 纯态度词：整个值就是这些，等于什么都没说。
#: 闸门里兜底——不管规则还是 LLM 抽出这些词都该拦。
VAPID_WORDS = {
    "小心", "注意", "当心", "留意", "谨慎", "认真", "仔细", "严谨", "用心",
    "重视", "关注", "在意", "上心", "留神", "警惕",
}


#: 标识符式短值：纯 ASCII 标识符 / IP / 端口 / 路径。
#: 用于给环境事实类谓词开短值豁免——「root」「db1」「192.168.120.52」
#: 都很短，但信息密度极高，绝不该被 4 字门槛拦掉。
_IDENTIFIER_SHAPE_RE = re.compile(
    r"^(?:"
    r"\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?"   # IPv4[:port]
    r"|[A-Za-z0-9_][A-Za-z0-9_.\-:/\\]{1,}$"   # 标识符 / 主机名 / 路径 / URL
    r")$"
)


def _is_identifier_value(text: str) -> bool:
    """值是不是一个「标识符式」的短值（而不是无信息的碎片）。

    「核实」「截图」这种碎片是中文纯动词，永远不匹配；
    「root」「db1」「127.0.0.1」「192.168.120.52:8003」全部匹配。
    """
    t = (text or "").strip()
    if not t or len(t) > 120:
        return False
    return bool(_IDENTIFIER_SHAPE_RE.match(t))


def _is_atomic_value(text: str) -> bool:
    """短但完整的原子值：纯中文的姓名、昵称、语言名等。

    与 _is_identifier_value 互补——那条管 ASCII 标识符，
    这条管「刘润」「中文」「简体中文」这类纯中文原子值。

    判据刻意保守，避免把碎片放进来：
      · 2~12 个汉字，无标点、无空白
      · 不是纯动作词（「核实」「截图」这类已在 ACTION_HEADS / VAPID 里）
    """
    t = (text or "").strip()
    if not (2 <= len(t) <= 12):
        return False
    if not re.fullmatch(r"[\u4e00-\u9fff]+", t):
        return False
    if t in VAPID_WORDS or t in _NOT_ATOMIC_WORDS:
        return False
    return True


#: 中文里「看着像值、其实是动作/虚词」的词。原子值豁免要排除它们。
_NOT_ATOMIC_WORDS = {
    "核实", "截图", "些什么", "处理", "整理", "生成", "配置", "检查", "确认",
    "测试", "部署", "安装", "打开", "关闭", "启动", "停止", "下载", "上传",
    "参考", "支持", "获取", "触发", "修复", "排查", "验收", "实现", "运行",
    "执行", "点击", "填写", "定位", "登录", "试试",
}


def _has_concrete_identifier(text: str) -> bool:
    return bool(_CONCRETE_RE.search(text))


def _is_vapid(text: str) -> bool:
    """整句是空话：只有态度没有事实。"""
    t = (text or "").strip()
    for tail in VAPID_TAILS:
        if t.endswith(tail):
            # 去掉尾巴后剩下不到 6 字 -> 纯空话
            core = t[: -len(tail)].strip(" ：:，,")
            if len(core) < 6:
                return True
    return False


def _requires_context(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    # 以口语连接词开头 -> 必然是对话碎片
    for p in CHATTY_PREFIXES:
        if t.startswith(p):
            return True
    # 以标点/省略号结尾 -> 被截断
    if t.endswith(("…", "...", "：", ":")):
        return True
    return False


def _looks_like_instruction(text: str, quote: str) -> bool:
    t = text or ""
    markers = sum(1 for m in INSTRUCTION_MARKERS if m in t)
    if markers >= 2:
        return True
    # 引用里是指令句式，且抽出的内容几乎就是引用原文的一部分
    if quote and len(t) > 15:
        norm_q = re.sub(r"\s+", "", quote)
        norm_t = re.sub(r"\s+", "", t)
        if norm_t and norm_t[:15] in norm_q and markers >= 1:
            return True
    return False


# --------------------------------------------------------------------------- #
# ③ 可读性：summary
# --------------------------------------------------------------------------- #

KIND_LABELS = {
    "preference": "偏好",
    "fact": "事实",
    "episode": "经历",
    "reflection": "反思",
}

#: 谓词 -> 中文短语（用来说明「关于什么」）
PREDICATE_LABELS = {
    "responds_in": "回复语言",
    "user_name": "名字",
    "nickname": "昵称",
    "user_job": "职业",
    "knowledge_level": "知识背景",
    "prefers_editor": "常用编辑器",
    "prefers_report_language": "报告语言",
    "prefers_theme": "界面主题",
    "prefers_ui_theme_consistency": "界面风格要求",
    "prefers_hover_reveal_actions": "按钮显示方式",
    "prefers_cleanup_debug_artifacts": "调试代码清理",
    "prefers_github_link_install": "插件发布方式",
    "prefers_explanation_style": "解释风格",
    "uses_dsh_plugin": "已装插件",
    "dsh_install_path": "DSH 安装位置",
    "dsh_user_data_path": "DSH 数据目录",
    "dsh_host_loads_from": "DSH 加载方式",
    "plugin_install_source": "插件安装来源",
    "plugin_name": "项目名称",
    "plugin_features": "插件功能",
    "plugin_react_usage": "客户端写法",
    "plugin_client_entry_pattern": "插件入口写法",
    "primitives_bundle_location": "前端 bundle 位置",
    "settings_section_register_pattern": "设置页注册方式",
    "slot_contract_source": "slot 契约位置",
    "client_service_sources": "客户端服务位置",
    "host_switch_unusable": "宿主组件限制",
    "delete_bug_root_cause": "删除 bug 根因",
    "security_review_findings": "安全检测结论",
    "security_status": "安全状态",
    "build_status": "构建状态",
    "test_status": "测试状态",
    "deploy_status": "部署状态",
    "dev_phase": "开发阶段",
    "open_issues": "未解决问题",
    "current_blocker": "当前阻塞",
    "next_action": "下一步",
    "publish_status": "发布状态",
    "requires_path_boundary_check": "路径边界要求",
    "fail_closed_on_activity_check": "失败处理原则",
    "dsh_memory_capture_needs_subagent_filter": "记忆采集过滤",
    "dsh_memory_inject_mechanism": "记忆注入机制",
    "dsh_memory_market_baseline": "市场基线数据",
    "dsh_plugin_registries": "插件目录站",
    "dsh_memento_maintenance": "memento 维护状态",
    "memory_recall_long_text_penalty": "检索打分缺陷",
    "memory_needs_scope_and_lifecycle": "记忆结构缺陷",
    # ── 环境事实（v2.3.1 新增：服务器地址与账号属于必记内容）──────
    "ssh_endpoint": "服务器地址",
    "host_ip": "主机地址",
    "server_ip": "服务器 IP",
    "hostname": "主机名",
    "ssh_port": "SSH 端口",
    "service_port": "服务端口",
    "login_account": "登录账号",
    "login_user": "登录用户",
    "login_credential": "登录凭据",
    "api_key": "接口密钥",
    "db_host": "数据库地址",
    "db_port": "数据库端口",
    "db_name": "数据库名",
    "db_user": "数据库账号",
    "conn_string": "连接串",
    "endpoint": "服务端点",
    "base_url": "接口地址",
    "deploy_path": "部署目录",
    "deploy_target": "部署目标",
    "environment": "环境类型",
    "server_role": "服务器用途",
    "db_engine": "数据库类型",
    "test_prerequisite": "测试前置条件",
}


def predicate_label(predicate: str) -> str:
    """未知谓词不报错，退回原键名并标注。"""
    return PREDICATE_LABELS.get(predicate, f"{predicate}（原始键名）")


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind)


def make_summary(
    *,
    kind: str,
    predicate: str,
    obj: str,
    content: str,
    scope: Scope | None = None,
) -> str:
    """生成一句给主人看的人话。优先用 content，缺了就用谓词标签拼。"""
    import credential_store  # 局部导入：只在生成摘要时需要

    body = (content or "").strip()
    # content 只是 object 的复读 -> 不算有效摘要
    if not body or body.strip() == (obj or "").strip():
        body = f"{predicate_label(predicate)}：{(obj or '').strip()}"
    # 去掉「用户提到：」这类无用前缀
    body = re.sub(r"^(用户|我)(提到|说|表示|想要|希望)[:：]\s*", "", body)
    # 凭据指针在面板上显示成掩码，别把 cred://xxx 这种内部 id 露给主人看
    body = re.sub(
        r"cred://[A-Za-z0-9_]+",
        lambda m: credential_store.masked_of(m.group(0)),
        body,
    )
    if scope is not None and scope.scope != SCOPE_GLOBAL:
        body = f"［{scope.describe()}］{body}"
    return body.strip()[:200]
