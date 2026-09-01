#!/usr/bin/env python3
"""RT-032 只读发现画像、每日执行合同、试跑门禁与调度交接（确定性计算层）。

四件事，全部由代码从**既有只读回执与实际配置**算出来，不接受模型猜测：

1. `build_discovery_report`：只读既有运行回执与派生索引，产出发现报告。
   报告严格区分「实体名称/别名数」「候选实体族数」「用户已确认实体数」三个数，
   并标注它只代表当前授权可见范围与发现日期范围。
2. `build_execution_contract` / `compute_contract_sha256`：从真实配置渲染
   每日执行合同，并给出稳定摘要。配置一变，摘要就变，已有确认随之失效。
3. `evaluate_pilot`：只读 nightly manifest、验收回执与采集回执判定试跑。
   任何一项不达标——包括**根本没给采集回执**——都只能进 DEGRADED，
   进不了 PILOT_PASSED。
4. `build_scheduler_handoff` / `validate_schedule_receipt`：产出机器可读的调度
   交接，并校验宿主回填的外部任务标识。

**边界**：本模块不创建、不修改、不删除任何计划任务，不调用 OpenClaw / Gateway /
cron，也不假设任何 OpenClaw 调度 API 存在——仓库里没有这样的本地文档依据。
它只产出「要建一个什么样的任务」的交接单，并在宿主回填任务标识后做校验。

Refs: RT-032
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Optional

_PROJECT = Path(__file__).resolve().parents[1]
if str(_PROJECT / "scripts") not in sys.path:
    sys.path.insert(0, str(_PROJECT / "scripts"))

from cwk_activation_state import read_regular_path  # noqa: E402
from cwk_atomic_file import AtomicFileError, ContainmentError  # noqa: E402
from cwk_pr001_contracts import canonical_json_bytes  # noqa: E402

# 上游 `cwk_nightly_pipeline` 在**模块体**里就会执行
# `load_local_env(PROJECT / ".env")`，其中 `PROJECT = Path(__file__).resolve().parents[1]`。
# 本模块与它同住 `scripts/`，所以这里的 `_PROJECT` 与它的 `PROJECT` 是同一个算式、
# 同一个目录——不是 cwd，也不是配置文件所在的目录。
#
# `_PROJECT_ENV_ROOT` 单独起一个名字，是为了让「该读哪个项目根的 .env」这件事有一个
# **单一**的落点：既不受 `project_dir`（那是渲染时相对化路径用的）影响，也不受任何
# 环境变量或命令行开关影响。可被外部改写的「.env 在哪」本身就是一个漏洞面——
# 指错地方就等于给用户看一份描述别处配置的合同。
_PROJECT_ENV_ROOT = _PROJECT
PROJECT_ENV_FILE = ".env"

DISCOVERY_REPORT_SCHEMA = "cwk.activation_discovery_report.v1"
EXECUTION_CONTRACT_SCHEMA = "cwk.activation_execution_contract.v1"
PILOT_GATE_RECEIPT_SCHEMA = "cwk.activation_pilot_gate.v1"
SCHEDULER_HANDOFF_SCHEMA = "cwk.activation_scheduler_handoff.v1"

_CONTRACT_DOMAIN = b"cwk-activation-contract-v1\x00"
_PROFILE_DOMAIN = b"cwk-activation-profile-v1\x00"
_SCOPE_DOMAIN = b"cwk-activation-scope-v1\x00"

# 采集来源。与 scripts/cwk_collect_live.py 的 lane 划分一致。
DAILY_LANES = ("inbox", "pending", "outbox", "unread", "todo_pending")
BACKFILL_LANES = (
    "history_inbox",
    "history_outbox",
    "history_pending_report",
    "history_todo_pending",
    "history_todo_completed",
)

# 上游默认值的镜像。**不是**凭记忆写死的文案：
# tests/test_activation_contract.py 用 ast 静态解析上游源码，逐个断言这些数字
# 与 scripts/cwk_collect_live.py / cwk_nightly_pipeline.py 里的真实默认值相等。
# 上游一改而这里没跟，测试立刻红。
DEFAULT_DETAIL_CAP = 60
DEFAULT_CONTINUATION_CAP = 15
DEFAULT_BACKFILL_CAP = 20
DEFAULT_BACKFILL_PAGE_SIZE = 20
DEFAULT_LOOKBACK_DAYS = 2

DETAIL_READ_ACTIONS = ("report_body", "basic_info", "node_and_opinion_chain")

FORBIDDEN_ACTIONS = (
    "mark_cwork_items_as_read",
    "reply_or_comment_in_cwork",
    "approve_reject_or_complete_cwork_tasks",
    "delete_cwork_content",
    "send_cwork_messages",
    "write_back_to_raw",
    "upload_raw_to_docdb",
    "mix_other_users_raw_into_this_mirror",
)

OUTPUTS = (
    "daily_digest_markdown",
    "daily_digest_html",
    "action_center",
    "wiki_topic_and_entity_pages",
    "entity_and_relation_index",
    "coverage_and_acceptance_receipts",
)


# ── 小工具 ──────────────────────────────────────────────────────────────────


def load_json(path: Path | str) -> Optional[dict]:
    """读 JSON；文件不存在返回 None（缺料是要如实记录的事实，不是异常）。"""

    p = Path(path)
    if not p.is_file():
        return None
    try:
        with p.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (ValueError, OSError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def sha256_of_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_of_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ── nightly 运行时取值：与 cwk_nightly_pipeline 逐条对齐 ────────────────────
#
# 合同必须描述**那条被排期的命令实际会做什么**，不是「大致会做什么」。交接单里
# 的 argv 固定为 `--config <cfg> --run-name <n> --date <d>`，没有别的开关，所以
# 这里只需要复刻 nightly 在**没有其它命令行参数**时的取值规则。
#
# 为什么不把上游 nightly 模块导进来复用它的函数：那个模块在导入时就会执行
# `load_local_env(PROJECT/'.env')`，把 .env 里的东西塞进本进程的环境变量。
# 激活向导绝不能因为「渲染一份合同」而顺手把凭据读进自己的进程。所以这里重写一
# 份**等价实现**，并由 tests/test_rt032_contract_fidelity.py 直接调用上游真函数
# 做等价性对拍——上游一改，那组测试就红。
#
# ── nightly 设置登记表 ──────────────────────────────────────────────────────
#
# 之前这里只有八个键。八个键不是「先支持一部分」，而是一个**会说假话的模型**：
# `wiki_sync` 不在里面，于是配置 `{"sync_docdb": false, "wiki_sync": true}` 会渲染
# 出一份写着「派生内容发布到 DocDB：关」的合同，哈希还与 `wiki_sync: false` 完全
# 相同——而那一晚 nightly 会真的把 wiki/ 推上 DocDB。用户按那份合同点的「是」，
# 覆盖不了实际发生的事。
#
# 所以登记表必须是**完整的**：nightly 认的每一个配置键都在这里，各自带上它真实的
# 优先级、默认值和影响面。完整性不靠人记，靠三层：
#
#   1. 运行期：配置里出现登记表不认识的键 → 直接拒绝出合同（见 `_reject_unknown`）。
#      用户永远不会在「有一个没人建模的开关正在生效」的情况下做确认。
#   2. CI：tests/test_rt032_contract_fidelity.py 用 AST 从
#      `scripts/cwk_nightly_pipeline.py` 里把 `config.get(...)` /
#      `config_value(args, config, ...)` 的键名和 `os.environ.get("CWK_*")` 的变量名
#      抠出来，与本表对拍。上游加一个键，那组测试就红——它不读本表来生成期望值。
#   3. CI：同一组测试还把 `skill/templates/CONFIG.example.json` 的键与本表对拍，
#      因为那是我们真正发给用户的模板。
#
# 优先级有**五种**，不是一种。这不是笔误，是上游 `config_value` 与 `env_bool` 混用
# 的既有结果，合同的价值恰恰在于把它如实摊开：
#
#   config_env_default      config > env > 字面默认   （argparse 默认是 None 的标量）
#   env_config_default      env > config > 默认       （BooleanOptionalAction 开关）
#   sync_docdb              config 真值 > env > False （只有这一个键）
#   env_first_scalar        env > config > 默认       （argparse 默认**本身读环境**）
#   config_only_derived     没有环境层                （只有 wiki_mirror_root）
#
# `env_first_scalar` 最容易漏：`--ai-max-parallel` 的 argparse 默认是
# `int(os.environ["CWK_AI_MAX_PARALLEL"]) if ... else None`，环境一设，`config_value`
# 拿到的 args 值就不是 None，配置文件根本轮不上。把它按整数那一类算成
# 「config 优先」会得到相反的答案。
#
# 这五个名字不是本模块的自述：`test_rt032_contract_fidelity` 里的分类器**只看上游
# 语句的形状**，独立判出同一组名字再与本表对拍，对不上就红。所以这段注释即便有人
# 改错，也不会变成一份没人发现的错误说明。
#
# 上面所有「env」都不是单指 shell。上游的模块体里有一行
# `load_local_env(PROJECT / ".env")`，它在任何设置被解析**之前**执行，用
# `os.environ.setdefault` 把项目根那个 gitignore 掉的 `.env` 填进进程环境。于是环境
# 层其实是两层叠出来的：**shell > 项目 `.env`**（同名时 shell 赢，因为
# setdefault 不覆盖）。这一层过去完全不在合同里，`.env` 因此成了一条无人看管的通
# 道——改一行就能打开对外发布，而合同哈希、漂移检查、调度等价性判断全都不动。
# 见 `read_project_env` / `merge_runtime_env`：合并只发生在环境层内部，各键原本
# 排第几不变（config 优先的键，依旧在合并后的有效环境之前）。

NIGHTLY_ENV_TRUE = ("1", "true", "yes", "y", "on")

# 被排期的任务只拿得到交接单 env_allowlist 里的变量。这既是「合同 vs 现实」比较的
# 基准，也是 app_key 不会被误判成「依赖 shell」的原因——它本来就在白名单里。
SCHEDULED_ENV_ALLOWLIST = ("CWORK_APP_KEY",)

# 交接单 argv 恒为 `--config/--run-name/--date`，所以这些只走命令行的开关在被排期
# 的那次运行里**恒为下面的值**。它们同样决定发布行为，必须写进合同：
# `--sync-dry-run` 恒假，正是 `wiki_sync` 会真的上传而不是空跑的原因。
NIGHTLY_CLI_ONLY_FIXED = {
    "--source-dir": "unset (nightly collects live from CWork)",
    "--no-publish-mirror": False,
    "--sync-dry-run": False,
    "--experimental-cloud-first": False,
    "--experimental-cloud-query-catalog": False,
}

_UPSTREAM_MIRROR_ROOT = str(_PROJECT / "knowledge" / "工作协同镜像")
_UPSTREAM_COLLECTION_STATE = str(_PROJECT / "state" / "collection-state.json")


class NightlyConfigError(ValueError):
    """配置里的取值会让 nightly 直接崩在启动阶段，或本模块无法如实建模。

    上游对整数是 `int(...)` 硬转、对 lookback 有 0..31 的范围检查，转不动就
    `ValueError`/`SystemExit`——那条被排期的命令根本跑不起来。合同不能替它编一个
    默认值糊过去，否则用户确认的是一份永远不会发生的运行。

    登记表不认识的键也走这里：认不出就描述不了，描述不了就不能让用户确认。
    """


class UnschedulableNightlySetting(ValueError):
    """配置开了一条**被排期的命令走不通**的路径。

    目前只有两个：`cloud_first` 与 `publish_cloud_query_catalog`。上游
    `enforce_cloud_pause()` 要求各自再加一个 `--experimental-*` 命令行开关才放行，
    而交接单的 argv 只有 `--config/--run-name/--date`，给不出那个开关。于是那条被
    排期的命令每晚都会在启动时 `SystemExit`。

    不渲染合同、不出交接单：与其让用户确认一份「每晚定时失败」的自动化，不如当场
    说清这条路现在是暂停的。
    """


class ScheduledEnvironmentMismatch(ValueError):
    """合同取值依赖当前 shell 的 CWK_* 变量，而被排期的任务拿不到它们。

    交接单的 env_allowlist 只有 CWORK_APP_KEY。若某个设置是靠 shell 环境变量
    才成立的，宿主那条任务会解析出**另一个值**，于是用户确认的合同与实际夜跑
    不是同一件事。fail closed：不出交接单，请用户把值写进配置文件。
    """


class ProjectEnvironmentError(ValueError):
    """项目根下有一个 `.env`，但本模块没法**如实**把它算进今晚的行为里。

    上游在 import 阶段就 `load_local_env(PROJECT / ".env")`，那一步的失败模式全都
    发生在 nightly 做任何事情之前：非 UTF-8 直接 `UnicodeDecodeError`、目录
    `IsADirectoryError`、FIFO 则停在 `read_text` 的 `open` 上**永不返回**。这些情况
    下「今晚会发生什么」的正确答案不是某组设置，而是「今晚什么都不会发生」或者
    「今晚会挂在启动阶段」——合同不能替它编一份看起来正常的描述。

    读不动（权限、超出激活读取上限）也走这里：读不动就建模不了，建模不了就不能
    让用户确认。**文件不存在不算错误**——上游对缺失是直接 return，那是正常状态。
    """


class _Setting:
    """登记表的一行。"""

    __slots__ = ("key", "kind", "precedence", "env_keys", "default", "impact")

    def __init__(self, key, kind, precedence, env_keys, default, impact):
        self.key = key
        self.kind = kind
        self.precedence = precedence
        self.env_keys = env_keys
        self.default = default
        self.impact = impact


def _d(value):
    """把字面默认值包成与派生默认值同形的可调用对象。"""

    return lambda _settings: value


# 顺序有意义：派生默认值只能引用**排在自己前面**的键。
# sync_docdb 必须早于 wiki_sync，sync_wiki 早于 wiki_compile 早于
# wiki_topics_entities 早于 wiki_sync，mirror_root 早于 wiki_mirror_root。
NIGHTLY_SETTINGS: tuple[_Setting, ...] = (
    # —— 启动与身份 ——
    _Setting("app_key", "secret", "env_first_scalar", ("CWORK_APP_KEY", "XG_BIZ_API_KEY"), _d(""), "startup"),
    _Setting("history_run_name", "ident", "config_env_default", ("CWK_HISTORY_RUN_NAME",), _d(""), "sources"),
    _Setting("owner_emp_id", "ident", "config_env_default", ("CWK_OWNER_EMP_ID",), _d(""), "processing"),
    _Setting("owner_name", "opaque", "config_env_default", ("CWK_OWNER_NAME",), _d(""), "processing"),
    # —— 来源读取 ——
    _Setting("detail_cap", "int", "config_env_default", ("CWK_DETAIL_CAP",), _d(DEFAULT_DETAIL_CAP), "sources"),
    _Setting("continuation_cap", "int", "config_env_default", ("CWK_CONTINUATION_CAP",), _d(DEFAULT_CONTINUATION_CAP), "sources"),
    _Setting("backfill_enabled", "bool", "env_config_default", ("CWK_BACKFILL_ENABLED",), _d(True), "sources"),
    _Setting("backfill_cap", "int", "config_env_default", ("CWK_BACKFILL_CAP",), _d(DEFAULT_BACKFILL_CAP), "sources"),
    _Setting("backfill_page_size", "int", "config_env_default", ("CWK_BACKFILL_PAGE_SIZE",), _d(DEFAULT_BACKFILL_PAGE_SIZE), "sources"),
    _Setting("collection_state_file", "path", "config_env_default", ("CWK_COLLECTION_STATE_FILE",), _d(_UPSTREAM_COLLECTION_STATE), "sources"),
    _Setting("source_completeness", "bool", "env_config_default", ("CWK_SOURCE_COMPLETENESS",), _d(True), "sources"),
    _Setting("source_completeness_lookback_days", "int", "config_env_default", ("CWK_SOURCE_COMPLETENESS_LOOKBACK_DAYS",), _d(DEFAULT_LOOKBACK_DAYS), "sources"),
    _Setting("source_backfill_max_parallel", "int", "config_env_default", ("CWK_SOURCE_BACKFILL_MAX_PARALLEL",), _d(6), "sources"),
    _Setting("relation_api_base_url", "url", "config_env_default", ("CWK_RELATION_API_BASE_URL",), _d("https://sg-al-cwork-web.mediportal.com.cn"), "sources"),
    _Setting("relation_api_path", "ident", "config_env_default", ("CWK_RELATION_API_PATH",), _d(""), "sources"),
    _Setting("relation_api_timeout_seconds", "int", "config_env_default", ("CWK_RELATION_API_TIMEOUT_SECONDS",), _d(30), "sources"),
    # —— 产出位置 ——
    _Setting("mirror_root", "path", "config_env_default", ("CWK_MIRROR_ROOT",), _d(_UPSTREAM_MIRROR_ROOT), "outputs"),
    # —— AI 处理（会把记录内容发给外部模型服务）——
    _Setting("ai_enabled", "bool", "env_config_default", ("CWK_AI_ENABLED",), _d(False), "processing"),
    _Setting("ai_dry_run", "bool", "env_config_default", ("CWK_AI_DRY_RUN",), _d(False), "processing"),
    _Setting("ai_record_model", "ident", "env_first_scalar", ("CWK_AI_RECORD_MODEL",), _d("newapi/BD-MiniMax"), "processing"),
    _Setting("ai_cluster_model", "ident", "env_first_scalar", ("CWK_AI_CLUSTER_MODEL",), _d("newapi/BD-glm"), "processing"),
    _Setting("ai_quality_model", "ident", "env_first_scalar", ("CWK_AI_QUALITY_MODEL",), _d("newapi/BD-glm"), "processing"),
    _Setting("ai_max_parallel", "int", "env_first_scalar", ("CWK_AI_MAX_PARALLEL",), _d(4), "processing"),
    _Setting("ai_timeout_seconds", "int", "env_first_scalar", ("CWK_AI_TIMEOUT_SECONDS",), _d(120), "processing"),
    # —— 对外发布 ——
    _Setting("sync_docdb", "bool", "sync_docdb", ("CWK_SYNC_DOCDB",), _d(False), "publication"),
    _Setting("docdb_project_id", "ident", "config_env_default", ("CWK_DOCDB_PROJECT_ID",), _d(""), "publication"),
    _Setting("docdb_root_file_id", "ident", "config_env_default", ("CWK_DOCDB_ROOT_FILE_ID",), _d(""), "publication"),
    # —— Wiki 流水线：编译在本地，sync 是**第二条对外发布通道** ——
    _Setting("sync_wiki", "bool", "env_config_default", ("CWK_SYNC_WIKI",), _d(False), "processing"),
    _Setting("wiki_compile", "bool", "env_config_default", ("CWK_WIKI_COMPILE",), lambda s: bool(s["sync_wiki"]), "processing"),
    _Setting("wiki_topics_entities", "bool", "env_config_default", ("CWK_WIKI_TOPICS_ENTITIES",), lambda s: bool(s["sync_wiki"] or s["wiki_compile"]), "processing"),
    _Setting("wiki_sync", "bool", "env_config_default", ("CWK_WIKI_SYNC",), lambda s: bool(s["sync_docdb"] and (s["wiki_compile"] or s["wiki_topics_entities"])), "publication"),
    _Setting("wiki_mirror_root", "path", "config_only_derived", (), lambda s: s["mirror_root"], "outputs"),
    _Setting("wiki_model", "ident", "config_env_default", ("CWK_CLOUD_WIKI_MODEL",), _d("evan-openai/glm-5.3-flash"), "processing"),
    _Setting("wiki_repair_model", "ident", "config_env_default", ("CWK_CLOUD_WIKI_REPAIR_MODEL",), _d("deepseek/deepseek-v4-flash"), "processing"),
    _Setting("wiki_limit", "int", "config_env_default", ("CWK_WIKI_LIMIT",), _d(80), "processing"),
    _Setting("wiki_max_parallel", "int", "config_env_default", ("CWK_WIKI_MAX_PARALLEL",), _d(1), "processing"),
    _Setting("wiki_refine_fallbacks", "bool", "env_config_default", ("CWK_WIKI_REFINE_FALLBACKS",), _d(False), "processing"),
    _Setting("wiki_timeout_seconds", "int", "config_env_default", ("CWK_WIKI_TIMEOUT_SECONDS",), _d(180), "processing"),
    _Setting("wiki_best_effort", "bool", "env_config_default", ("CWK_WIKI_BEST_EFFORT",), _d(False), "processing"),
    # —— 暂停中的实验路径：解析得出真值就拒绝 ——
    _Setting("cloud_first", "bool", "env_config_default", ("CWK_CLOUD_FIRST",), _d(False), "publication"),
    _Setting("publish_cloud_query_catalog", "bool", "env_config_default", ("CWK_PUBLISH_CLOUD_QUERY_CATALOG",), _d(False), "publication"),
)

NIGHTLY_SETTING_KEYS: tuple[str, ...] = tuple(s.key for s in NIGHTLY_SETTINGS)

_SETTING_BY_KEY = {s.key: s for s in NIGHTLY_SETTINGS}

# 交接单 argv 给不出解锁开关，这两条路径每晚都会在 `enforce_cloud_pause` 里退出。
PAUSED_NIGHTLY_PATHS = ("cloud_first", "publish_cloud_query_catalog")

# 同一份配置文件同时供 nightly 和激活向导读。这几个键只有向导认，nightly 的
# `read_config` 会原样忽略它们，因此它们不改变夜跑行为，允许出现。
#
# `_comment` 在这里放行，而 `scope.json` 里同样的字段被拒——两者不矛盾：
# scope 对象会被逐字写进发现报告、再被念给用户当作「你授权的范围」，自由文本进
# 到那里就是一条直通用户同意语句的通道；配置文件里的注释既不进合同、也不进哈希、
# 更不会被念出来，它只是留给写配置的人自己看的。
ACTIVATION_CONFIG_KEYS = ("schedule_run_at_local", "schedule_timezone", "_comment")

# 字符串取值的封闭词表。渲染出来的合同会被 Agent 读给用户听，所以任何要**原样**
# 出现在里面的字符串都得先过一道白名单；过不了就拒绝，而不是「截断后照读」。
_IDENT_VALUE = re.compile(r"\A[A-Za-z0-9._:@/+-]{0,128}\Z")
_URL_VALUE = re.compile(
    r"\Ahttps?://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?(/[A-Za-z0-9._~%/-]{0,256})?\Z"
)
_MAX_PATH_LEN = 256


def nightly_env_bool(env: Mapping[str, str], name: str) -> Optional[bool]:
    """`cwk_nightly_pipeline.env_bool` 的等价实现（未设置返回 None）。

    与上游逐字一致：只有 :data:`NIGHTLY_ENV_TRUE` 这五个字面量算真，**其它任何
    取值都算假**，不存在「看不懂就回落到默认值」。少写一个 "y" 就会让合同在
    ``CWK_SYNC_DOCDB=y`` 时说「不发布」，而实际那晚会发布。
    """

    value = env.get(name)
    if value is None:
        return None
    return value.strip().lower() in NIGHTLY_ENV_TRUE


def _nightly_int(value: Any, *, where: str) -> int:
    """复刻上游的 `int(...)` 硬转，包括它会抛的那些异常。"""

    if isinstance(value, bool):
        # 上游 `int(True)` 会得到 1。合同里出现一个由 true 变出来的上限，
        # 用户读到的是数字、写下的是布尔，两边对不上。当作配置错误。
        raise NightlyConfigError(f"{where} is a boolean where an integer is required")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise NightlyConfigError(
            f"{where} is not an integer; the scheduled nightly command would abort "
            "before it started, so no contract can describe it"
        ) from exc


def _check_text(value: Any, *, kind: str, where: str) -> str:
    """字符串取值的封闭校验。不合格就拒，绝不「清洗后照用」。"""

    if value is None:
        return ""
    if not isinstance(value, str):
        raise NightlyConfigError(f"{where} must be a string")
    if len(value) > _MAX_PATH_LEN:
        raise NightlyConfigError(f"{where} is longer than {_MAX_PATH_LEN} characters")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        # 控制字符进不了合同：合同的 Markdown 会被念给用户听，换行和转义序列
        # 足以伪造出「合同里另有一句话」的观感。
        raise NightlyConfigError(f"{where} contains control characters")
    if kind == "ident" and not _IDENT_VALUE.match(value):
        raise NightlyConfigError(
            f"{where} is not a plain identifier "
            "(allowed: A-Z a-z 0-9 and . _ : @ / + -, at most 128 characters)"
        )
    if kind == "url" and value and not _URL_VALUE.match(value):
        raise NightlyConfigError(f"{where} is not a plain http(s) URL")
    return value


def _resolve_one(
    setting: _Setting,
    config: Mapping[str, Any],
    env: Mapping[str, str],
    settings: Mapping[str, Any],
    env_origin: Optional[Mapping[str, str]] = None,
) -> tuple[Any, str]:
    """按这一行声明的优先级取值，返回 ``(值, 来源)``。

    ``env`` 是 shell 与项目根 `.env` 合并后的**有效环境**，``env_origin`` 说明其中
    每个名字来自哪一层。环境层胜出时，来源报的是那一层的名字（``shell`` /
    ``project_env``）而不是笼统的 “env”——用户读合同时需要知道该去改哪儿，而
    「值在 `.env` 里」与「值在我这个 shell 里」对定时任务是完全相反的两件事。
    """

    key = setting.key
    default = setting.default(settings)
    origin = env_origin if env_origin is not None else {}

    def _from(env_key: str) -> str:
        # 没有 origin 信息时一律当成 shell：这是**更保守**的一侧，会让交接单在
        # 拿不准的时候拒绝，而不是放行。
        return origin.get(env_key, "shell")

    if setting.precedence == "env_config_default":
        # `env_bool(E) if 已设置 else bool(config.get(key, default))`
        # 注意上游对配置值用的是 Python 真值而非解析：`{"wiki_sync": "false"}`
        # 在上游是**真**。照抄，因为合同的价值在于把这种反直觉的配置如实摊开。
        from_env = nightly_env_bool(env, setting.env_keys[0])
        if from_env is not None:
            return from_env, _from(setting.env_keys[0])
        if key in config:
            return bool(config[key]), "config"
        return bool(default), "default"

    if setting.precedence == "sync_docdb":
        # `bool(config.get("sync_docdb", env_bool("CWK_SYNC_DOCDB") or False))`
        # 这一条是 config > env > False，与上面的布尔开关**方向相反**。
        if key in config:
            return bool(config[key]), "config"
        from_env = nightly_env_bool(env, "CWK_SYNC_DOCDB")
        if from_env is not None:
            return bool(from_env), _from("CWK_SYNC_DOCDB")
        return False, "default"

    if setting.precedence == "config_only_derived":
        # `config_value(args, config, key, <已解析的另一个键>)`，没有环境变量层。
        if key in config:
            return _coerce(setting, config[key], where=f"config.{key}"), "config"
        return default, "default"

    if setting.precedence == "env_first_scalar":
        # argparse 的 default **本身读环境**，于是 `config_value` 拿到的 args 值
        # 不是 None，配置文件根本轮不上。env > config > 默认。
        for env_key in setting.env_keys:
            raw = env.get(env_key)
            if raw:
                return _coerce(setting, raw, where=f"environment {env_key}"), _from(env_key)
        if key in config:
            return _coerce(setting, config[key], where=f"config.{key}"), "config"
        return default, "default"

    if setting.precedence == "config_env_default":
        # `config_value(args, config, key, os.environ.get(E, <字面量>))`，
        # args 默认是 None，所以 config > env > 字面量。
        if key in config:
            return _coerce(setting, config[key], where=f"config.{key}"), "config"
        env_key = setting.env_keys[0]
        if env_key in env:
            return _coerce(setting, env[env_key], where=f"environment {env_key}"), _from(env_key)
        return default, "default"

    raise AssertionError(f"unknown precedence {setting.precedence!r}")


def _coerce(setting: _Setting, value: Any, *, where: str) -> Any:
    if setting.kind == "int":
        return _nightly_int(value, where=where)
    if setting.kind == "bool":
        return bool(value)
    if setting.kind == "secret":
        if not isinstance(value, str):
            raise NightlyConfigError(f"{where} must be a string")
        return value
    return _check_text(value, kind=setting.kind, where=where)


def _reject_unknown(config: Mapping[str, Any]) -> None:
    """配置里出现登记表不认识的键 → 拒绝出合同。

    这是完整性的**运行期**那一层。上游哪天新增一个开关而本表没跟上，用户一旦在
    配置里用了它，这里就当场停住；不会出现「合同说不发布、那个没人建模的键让它
    发布了」的情况。

    只报个数，不回显键名：键名是配置文件里的任意字符串，抄进错误消息就等于把它
    转发给读这条消息的 Agent。用户对照 `skill/templates/CONFIG.example.json`
    就能知道自己写了什么。
    """

    unknown = [k for k in config if k not in _SETTING_BY_KEY and k not in ACTIVATION_CONFIG_KEYS]
    if unknown:
        raise NightlyConfigError(
            f"config contains {len(unknown)} key(s) the activation contract does not "
            "model; refusing to describe a nightly run whose behaviour it cannot "
            "account for. Compare against skill/templates/CONFIG.example.json"
        )


# ── 项目根 .env：上游在 import 阶段就会读的那一层 ──────────────────────────

# 上游 `load_local_env` 的键名规则，逐字照抄：`re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*")`。
# 编译一次是为了避免在每一行上重建，语义与上游的 `re.fullmatch(pattern, key)` 相同。
_PROJECT_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# 登记表认识的全部环境变量名。用来**只统计**、绝不回显 `.env` 里那些与 nightly
# 行为无关的名字（它们可能是别的工具的凭据名，抄进合同就等于替用户转发出去）。
NIGHTLY_ENV_KEYS: frozenset[str] = frozenset(
    key for setting in NIGHTLY_SETTINGS for key in setting.env_keys
)


class ProjectEnv(NamedTuple):
    """项目根 `.env` 这一层的建模结果。

    ``present`` 与 ``values`` 必须分开：文件存在但一行有效内容都没有，和文件根本
    不存在，对 nightly 是同一种行为、对用户却是两件事。
    """

    present: bool
    values: dict[str, str]


EMPTY_PROJECT_ENV = ProjectEnv(False, {})


def parse_project_env(text: str) -> dict[str, str]:
    """逐字复刻 `cwk_nightly_pipeline.load_local_env` 的解析，一条都不「改良」。

    照抄的清单（每一条都在 `test_rt032_project_env` 里对着上游函数本体验过）：

    - 切行用 `str.splitlines()`——所以 `\\x0b`、`\\x0c`、`\\u2028`、`\\u2085` 这些
      也算换行，一行里能塞进两条赋值；
    - 整行先 `strip()`，空行、`#` 开头、不含 `=` 的行跳过；
    - `export K=v` **不被接受**：键会变成 `"export K"`，过不了键名正则；
    - 键 `strip()` 后必须完全匹配 `[A-Za-z_][A-Za-z0-9_]*`，否则整行丢弃，
      而且**不影响后面的行**——一行写坏不会让其余的失效；
    - 值 `strip()` 后先 `strip('"')` 再 `strip("'")`，所以 `'"x"'` 得到 `"x"`
      而 `"'x'"` 得到 `x`，`""x""` 得到 `x`；
    - `K=` 得到**空字符串**，不是「没设」；
    - `K=a=b` 得到 `a=b`（`split("=", 1)`）；
    - 同名重复时**第一次出现的赢**（上游是 `os.environ.setdefault`，第二次调用时
      名字已经在了）。

    改良任何一条都会让合同描述的行为与今晚真实发生的行为分叉，所以这里的目标是
    「一样错」，不是「更对」。
    """

    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not _PROJECT_ENV_NAME.fullmatch(key):
            continue
        # setdefault：与上游写进 os.environ 的先后顺序一致，先到先得。
        values.setdefault(key, value.strip().strip('"').strip("'"))
    return values


def read_project_env(root: Optional[Path] = None) -> ProjectEnv:
    """把项目根的 `.env` 读成一层可建模的取值；读不准就当场失败。

    这一层过去完全不在合同里，于是一个 gitignore 掉的 `.env` 可以在没人看见的
    情况下改掉读取范围、打开对外发布、甚至决定 nightly 能不能启动，而合同哈希、
    漂移检查、调度等价性判断全都毫无反应。

    读法与「用户给的文件」一致，走 `read_regular_path`：**只 open 一次**并在同一个
    描述符上 `fstat`，带 `O_NONBLOCK`。`.env` 是一个可以被随时替换的名字，而它最坏
    的替换结果不是读到脏数据，是 `open` 永不返回——那会让向导整个挂住，而挂住的
    向导和「还在想」是分不出来的。

    映射到上游的失败模式：

    - 不存在 / 断链：上游 `path.exists()` 为假、直接 return。正常状态，返回
      :data:`EMPTY_PROJECT_ENV`，**不是**错误；
    - 目录、FIFO、设备、套接字：上游会 `IsADirectoryError` 或永久阻塞——今晚不会
      有一次成功的运行，拒绝出合同；
    - 非 UTF-8：上游 `read_text(encoding="utf-8")` 抛 `UnicodeDecodeError`，nightly
      在 import 阶段就死，拒绝；
    - 超过激活读取上限或读不动（权限等）：本模块建模不了，拒绝。

    错误消息里只有固定字面量 `.env`，没有路径、没有 errno、没有文件正文——
    正文是任意的攻击者可控文本，回显它就等于把它转发给读这条消息的 Agent。
    """

    base = Path(root) if root is not None else _PROJECT_ENV_ROOT
    try:
        raw = read_regular_path(base / PROJECT_ENV_FILE)
    except FileNotFoundError:
        # 上游对「没有」的反应就是什么都不做。这不是异常路径，是常态。
        return EMPTY_PROJECT_ENV
    except (IsADirectoryError, NotADirectoryError, ContainmentError):
        raise ProjectEnvironmentError(
            "the project root has a .env that is not a readable regular file; the "
            "nightly process loads it before it does anything else and would hang or "
            "abort at startup, so no contract was produced"
        ) from None
    except (AtomicFileError, OSError):
        raise ProjectEnvironmentError(
            "the project root has a .env that could not be read; the nightly process "
            "loads it at startup, so a contract written without it would describe a "
            "run that may not happen"
        ) from None

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ProjectEnvironmentError(
            "the project root has a .env that is not valid UTF-8 text; the nightly "
            "process decodes it at import time and would fail before starting, so "
            "there is no nightly run to describe"
        ) from None

    return ProjectEnv(True, parse_project_env(text))


def merge_runtime_env(
    shell: Mapping[str, str], project_env: Optional[Mapping[str, str]] = None
) -> tuple[dict[str, str], dict[str, str]]:
    """算出 nightly 解析设置时**真正**看到的那个环境，以及每个名字来自哪一层。

    上游是 `os.environ.setdefault(key, value)`：进程里已经有的名字原样保留，
    没有的才由 `.env` 补上。于是优先级是 shell > `.env`，而不是反过来。

    返回 ``(merged, origin)``，`origin[name]` 是 ``"shell"`` 或 ``"project_env"``。
    """

    merged: dict[str, str] = {}
    origin: dict[str, str] = {}
    for name, value in shell.items():
        merged[name] = value
        origin[name] = "shell"
    for name, value in (project_env or {}).items():
        if name not in merged:
            merged[name] = value
            origin[name] = "project_env"
    return merged, origin


def resolve_nightly_runtime(
    config: Mapping[str, Any],
    env: Optional[Mapping[str, str]] = None,
    project_env: Optional[Mapping[str, str]] = None,
) -> dict:
    """算出被排期的那条 nightly 命令实际会用的取值。

    返回 ``{"settings": {...}, "sources": {setting: config|shell|project_env|default}}``，
    覆盖登记表里的**每一个**键。

    ``env`` 是 shell/进程环境，``project_env`` 是项目根 `.env` 解析出来的那一层
    （见 :func:`read_project_env`）。两者先按上游的 `setdefault` 语义合并成一个
    「有效环境」，**再**送进各键原本的优先级：`config_env_default` 依旧是配置优先，
    合并只改变「环境层里那个值是谁给的」，不改变环境层排第几。

    取值无效或本模块无法建模时抛 :class:`NightlyConfigError`；配置开了一条被排期
    的命令走不通的路径时抛 :class:`UnschedulableNightlySetting`。两者都是 fail
    closed：宁可不出合同，也不出一份与当晚实际行为不符的合同。
    """

    if not isinstance(config, Mapping):
        raise NightlyConfigError("config must be a JSON object")
    _reject_unknown(config)

    env = env if env is not None else os.environ
    effective, origin = merge_runtime_env(env, project_env)
    settings: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for setting in NIGHTLY_SETTINGS:
        settings[setting.key], sources[setting.key] = _resolve_one(
            setting, config, effective, settings, origin
        )

    # 凭据不进配置文件。它会被写进合同、进哈希、并被念给用户听；而交接单本来就
    # 声明 CWORK_APP_KEY 由宿主环境提供，配置里再放一份只是多一个泄漏点。
    if isinstance(config.get("app_key"), str) and config["app_key"]:
        raise NightlyConfigError(
            "config.app_key must be empty; the scheduled task receives the key "
            "through the CWORK_APP_KEY environment variable instead, so a copy in "
            "the config file only adds a place for it to leak"
        )

    lookback = settings["source_completeness_lookback_days"]
    if lookback < 0 or lookback > 31:
        # 上游 `raise SystemExit(...)`：命令启动即失败。
        raise NightlyConfigError(
            "source_completeness_lookback_days must be between 0 and 31; the scheduled "
            "nightly command would refuse to start"
        )

    for key in PAUSED_NIGHTLY_PATHS:
        if settings[key]:
            raise UnschedulableNightlySetting(
                f"{key} is enabled, but the scheduled command is only given "
                "--config/--run-name/--date and therefore cannot pass the matching "
                "--experimental-* unlock that cwk_nightly_pipeline.enforce_cloud_pause "
                "requires; every scheduled run would exit at startup. Turn it off "
                "before asking anyone to confirm nightly automation"
            )

    return {"settings": settings, "sources": sources}


def render_nightly_settings(
    settings: Mapping[str, Any], *, project_dir: Optional[Path] = None
) -> dict:
    """把解析结果变成**可以安全写进合同、可以念出来**的形状。

    三条规则：

    - 布尔和整数原样；
    - `ident` / `url` / `path` 已经过封闭校验，原样（路径先相对化到项目目录，
      不落宿主绝对路径）；
    - `opaque`（人名）与 `secret`（凭据）**不回显取值**：只说「设了/没设」，
      opaque 另附取值指纹，好让「值变了」依然会改变合同哈希。
    """

    root = Path(project_dir) if project_dir is not None else _PROJECT
    rendered: dict[str, Any] = {}
    for setting in NIGHTLY_SETTINGS:
        value = settings[setting.key]
        if setting.kind in ("int", "bool"):
            rendered[setting.key] = value
        elif setting.kind == "secret":
            rendered[setting.key] = {"state": "set" if value else "unset"}
        elif setting.kind == "opaque":
            rendered[setting.key] = {
                "state": "set" if value else "unset",
                "fingerprint": (
                    hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]
                    if value
                    else None
                ),
            }
        elif setting.kind == "path":
            rendered[setting.key] = _render_path(str(value), root)
        else:
            rendered[setting.key] = value
    return rendered


def _render_path(value: str, root: Path) -> str:
    """路径相对化。项目外的路径只留指纹——合同不带宿主绝对路径。"""

    if not value:
        return ""
    try:
        resolved = Path(value).expanduser().resolve()
        return str(resolved.relative_to(root))
    except (ValueError, OSError, RuntimeError):
        return "<outside-project>:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def upstream_collect_defaults(source: Path | str | None = None) -> dict[str, int]:
    """静态解析 cwk_collect_live.py 里的 argparse 默认值。

    上游的 parser 建在 ``main()`` 内部，直接 import 取不到，而执行 ``main()``
    会真的去采集。所以这里用 ast 只读源码。仅供测试与审计对账使用。
    """

    path = Path(source) if source else _PROJECT / "scripts" / "cwk_collect_live.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    wanted = {"--detail-cap", "--continuation-cap", "--backfill-cap", "--backfill-page-size"}
    found: dict[str, int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_argument"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        flag = node.args[0].value
        if flag not in wanted:
            continue
        for keyword in node.keywords:
            if keyword.arg == "default" and isinstance(keyword.value, ast.Constant):
                if isinstance(keyword.value.value, int):
                    found[flag.lstrip("-").replace("-", "_")] = keyword.value.value
    return found


def upstream_lookback_default(source: Path | str | None = None) -> Optional[int]:
    """静态解析 nightly pipeline 里 source_completeness_lookback_days 的默认值。"""

    path = Path(source) if source else _PROJECT / "scripts" / "cwk_nightly_pipeline.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "get"):
            continue
        if len(node.args) != 2:
            continue
        first, second = node.args
        if (
            isinstance(first, ast.Constant)
            and first.value == "CWK_SOURCE_COMPLETENESS_LOOKBACK_DAYS"
            and isinstance(second, ast.Constant)
            and isinstance(second.value, int)
        ):
            return second.value
    return None


# ── 1. 只读发现与业务画像 ───────────────────────────────────────────────────


SCOPE_MIRROR_KINDS = ("personal", "team")
SCOPE_LANES = DAILY_LANES + BACKFILL_LANES
SCOPE_KEYS = ("mirror_kind", "subject_ref", "authorized_lanes", "read_only")
# 主体标识是个**标识符**，不是一句话：不留空格，也就不留自由文本的位置。
_SCOPE_SUBJECT_REF = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:@-]{0,63}\Z")


class ScopeSchemaError(ValueError):
    """范围文件不符合闭合 schema。"""


def normalize_scope(scope: Any) -> dict:
    """把第一道门要确认的「授权可见范围」收敛成闭合 schema。

    为什么必须闭合：这个对象会被原样写进 `discovery-report.json`，再由 AI 念给
    用户听。任何自由文本字段都是一条直通 Agent 上下文的注入通道，而它偏偏又是
    「用户到底授权了什么」的权威表述。所以只认四个字段、四种类型，多一个键就
    拒绝——宁可让调用方改文件，也不把调用方给的任意对象转发出去。

    lane 顺序会被归一到固定次序，因此同一份授权不会因为书写顺序不同而算出两个
    哈希、把第一道确认无端作废。错误消息只说键名与允许值，不回显调用方内容。
    """

    if not isinstance(scope, Mapping):
        raise ScopeSchemaError("scope must be a JSON object")
    keys = set(scope)
    missing = sorted(set(SCOPE_KEYS) - keys)
    extra = len(keys - set(SCOPE_KEYS))
    if missing or extra:
        # 缺的键名可以直说，那是 schema 自己的词表；多出来的**只报个数**。键名
        # 是调用方写的字符串，把它抄进错误消息就等于把任意文本转发给读这条消息
        # 的 AI——而这个函数存在的理由正是不做这种转发。用户想知道自己多写了
        # 什么，对照后半句列出的四个允许键即可。
        raise ScopeSchemaError(
            f"scope keys mismatch missing={missing} unexpected_key_count={extra}; "
            f"the only allowed keys are {list(SCOPE_KEYS)}"
        )

    mirror_kind = scope["mirror_kind"]
    if mirror_kind not in SCOPE_MIRROR_KINDS:
        raise ScopeSchemaError(f"scope.mirror_kind must be one of {list(SCOPE_MIRROR_KINDS)}")

    subject_ref = scope["subject_ref"]
    if not isinstance(subject_ref, str) or not _SCOPE_SUBJECT_REF.match(subject_ref):
        raise ScopeSchemaError(
            "scope.subject_ref must be a short identifier "
            "([A-Za-z0-9][A-Za-z0-9._:@-]{0,63}); it is not a free-text field"
        )

    lanes = scope["authorized_lanes"]
    if not isinstance(lanes, list) or not lanes:
        raise ScopeSchemaError("scope.authorized_lanes must be a non-empty list")
    if len(lanes) > len(SCOPE_LANES):
        raise ScopeSchemaError("scope.authorized_lanes has more entries than there are lanes")
    for lane in lanes:
        if not isinstance(lane, str) or lane not in SCOPE_LANES:
            raise ScopeSchemaError(f"scope.authorized_lanes may only contain {list(SCOPE_LANES)}")
    if len(set(lanes)) != len(lanes):
        raise ScopeSchemaError("scope.authorized_lanes contains a duplicate lane")

    if scope["read_only"] is not True:
        # 只读不是一个可以谈判的选项：范围文件声称别的，就不是这个产品的范围。
        raise ScopeSchemaError("scope.read_only must be literally true")

    return {
        "mirror_kind": mirror_kind,
        "subject_ref": subject_ref,
        "authorized_lanes": [lane for lane in SCOPE_LANES if lane in set(lanes)],
        "read_only": True,
    }


def compute_scope_sha256(scope: Mapping[str, Any]) -> str:
    """授权可见范围的稳定摘要（第一道确认绑定的对象）。

    先归一再哈希：确认绑定的必须是校验过的闭合对象，而不是调用方递进来的原物。
    """

    return hashlib.sha256(
        _SCOPE_DOMAIN + canonical_json_bytes(normalize_scope(scope))
    ).hexdigest()


def compute_profile_sha256(profile: Mapping[str, Any]) -> str:
    """用户确认后的业务画像摘要（第二道确认原像的一部分）。"""

    return hashlib.sha256(_PROFILE_DOMAIN + canonical_json_bytes(dict(profile))).hexdigest()


def _entity_counts(
    entity_catalog: Optional[dict], entity_registry: Optional[dict]
) -> dict[str, Any]:
    """三个必须互相区分的实体口径。

    - ``entity_surface_count``：原始名称 + 别名去重后的「写法」数；
    - ``candidate_entity_family_count``：算法聚出来的候选实体族数；
    - ``confirmed_entity_count``：**只**数注册表里带真实决策记录的条目。

    三者分别来自不同来源，任何一个都不由另一个推导，这样报告才不会把
    「机器猜的」说成「人确认的」。
    """

    surfaces: set[str] = set()
    families: set[str] = set()
    if isinstance(entity_catalog, dict):
        entries = entity_catalog.get("entities")
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("normalized") or entry.get("canonical_display") or entry.get("name")
                if isinstance(name, str) and name:
                    surfaces.add(name)
                for alias in entry.get("aliases") or []:
                    if isinstance(alias, str) and alias:
                        surfaces.add(alias)
                family = entry.get("family_id")
                if isinstance(family, str) and family:
                    families.add(family)

    confirmed = 0
    if isinstance(entity_registry, dict):
        for entry in entity_registry.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("decided_by") and entry.get("decided_at"):
                confirmed += 1

    return {
        "entity_surface_count": len(surfaces),
        "candidate_entity_family_count": len(families),
        "confirmed_entity_count": confirmed,
        "counts_are_independent": True,
    }


def build_discovery_report(
    *,
    scope: Mapping[str, Any],
    collect_manifest: Optional[dict],
    nightly_manifest: Optional[dict],
    acceptance: Optional[dict],
    entity_catalog: Optional[dict] = None,
    entity_registry: Optional[dict] = None,
    generated_at: str,
) -> dict:
    """从既有只读回执生成发现报告。缺料如实记录成 gaps，不猜、不补。"""

    gaps: list[str] = []
    if collect_manifest is None:
        gaps.append("collect_manifest_missing")
    if nightly_manifest is None:
        gaps.append("nightly_manifest_missing")
    if acceptance is None:
        gaps.append("acceptance_missing")
    if entity_catalog is None:
        gaps.append("entity_catalog_missing")

    collect = collect_manifest or {}
    nightly = nightly_manifest or {}
    accept = acceptance or {}

    lookback = _as_int(nightly.get("source_completeness_lookback_days"), DEFAULT_LOOKBACK_DAYS)

    record_counts = {
        "candidate_count": _as_int(collect.get("candidate_count"), 0),
        "selected_daily_count": _as_int(collect.get("selected_daily_count"), 0),
        "selected_backfill_count": _as_int(collect.get("selected_backfill_count"), 0),
        "written_count": _as_int(collect.get("written_count"), 0),
        "pending_count": _as_int(collect.get("pending_count"), 0),
        "processed_count": _as_int(nightly.get("processed_count"), 0),
        "raw_count": _as_int(accept.get("raw_count"), 0),
    }

    lane_counts = accept.get("lane_counts")
    lane_counts = lane_counts if isinstance(lane_counts, dict) else {}

    relations = {
        "relation_items": _as_int(accept.get("relation_items"), 0),
        "unique_relation_pairs": _as_int(accept.get("unique_relation_pairs"), 0),
        "strong_relations": _as_int(accept.get("strong_relations"), 0),
        "suspected_relations": _as_int(accept.get("suspected_relations"), 0),
        # 「未知」= 有配对但证据不足以判强弱的部分。
        "unknown_relation_count": max(
            _as_int(accept.get("unique_relation_pairs"), 0)
            - _as_int(accept.get("strong_relations"), 0)
            - _as_int(accept.get("suspected_relations"), 0),
            0,
        ),
    }

    report = {
        "schema": DISCOVERY_REPORT_SCHEMA,
        "generated_at": generated_at,
        "coverage_caveat": (
            "counts reflect only the currently authorized visible scope and the stated "
            "discovery date range; they are not a claim about the whole organisation"
        ),
        # 归一后的闭合对象，不是调用方原物：报告要被 AI 念给用户，不能替任意
        # 输入当传声筒。
        "authorized_visible_scope": normalize_scope(scope),
        "business_date_range": {
            "end": nightly.get("date"),
            "late_data_lookback_days": lookback,
        },
        "source_lanes": {
            "daily": list(DAILY_LANES),
            "backfill": list(BACKFILL_LANES),
            "daily_lane_count": len(DAILY_LANES),
            "backfill_lane_count": len(BACKFILL_LANES),
            "backfill_rotation": "round_robin",
        },
        "record_counts": record_counts,
        "lane_record_counts": lane_counts,
        "topic_count": _as_int(accept.get("recurring_topic_proposals"), 0),
        "entities": _entity_counts(entity_catalog, entity_registry),
        "relations": relations,
        "completeness": {
            "daily_source_complete": bool(collect.get("daily_source_complete", False)),
            "daily_source_failure_count": _as_int(collect.get("daily_source_failure_count"), 0),
            "source_completeness_failures": list(
                nightly.get("source_completeness_failures") or []
            ),
        },
        "gaps": gaps,
    }
    report["report_sha256"] = sha256_of_json(
        {k: v for k, v in report.items() if k != "generated_at"}
    )
    return report


# ── 2. 每日执行合同 ─────────────────────────────────────────────────────────

# 不进哈希的字段：时间戳与本地路径。它们一变不代表合同语义变了。
_CONTRACT_VOLATILE_KEYS = ("generated_at", "contract_sha256")


def build_execution_contract(
    *,
    config: Mapping[str, Any],
    env: Optional[Mapping[str, str]] = None,
    project_env_root: Optional[Path] = None,
    profile_sha256: str,
    run_at_local: str,
    timezone: str,
    generated_at: str,
    project_dir: Optional[Path] = None,
) -> dict:
    """从**实际配置 + 实际环境 + 项目根 `.env`** 渲染每日执行合同。

    取值规则不是本模块自己发明的，而是 `cwk_nightly_pipeline` 在
    `--config/--run-name/--date` 这条固定 argv 下的真实行为（见
    :func:`resolve_nightly_runtime`）：整数是 config > env > 默认，布尔开关里
    `backfill_enabled` / `source_completeness` 是 env > config > 默认，而
    `sync_docdb` 又回到 config > env > False。合同照抄这份不对称，因为它描述的
    是现实，不是理想。

    这里的 “env” 有**两层**。上游在 import 阶段就执行
    `load_local_env(PROJECT / ".env")`，把项目根那个 gitignore 掉的 `.env` 用
    `setdefault` 填进进程环境；于是真实优先级是
    **shell > 项目 `.env` > 配置 > 默认**（各键原本的优先级类别不变，合并只发生在
    环境层内部）。合同以前只看 shell，`.env` 因此成了一条无人看管的通道：改一行就
    能打开对外发布，而哈希、漂移、调度等价性全都不动。

    额外记录两件事：每个取值**来自哪一层**（config / shell / project_env /
    default），以及**换成被排期的那次运行真实会有的环境后结果是否相同**。后者决定
    交接单能不能出。关键在于「被排期的环境」= 允许清单里的 shell 变量
    （只有 CWORK_APP_KEY）**加上同一份 `.env`**——那个文件今晚会被 nightly 自己重新
    读一遍，把它一起去掉会把「值写在 `.env` 里」误判成「依赖当前 shell」而白白拒绝；
    而只有 shell 里的 `CWK_*` 才是定时任务真的拿不到、必须拒绝的那一类。
    """

    env = env if env is not None else os.environ
    layer = read_project_env(project_env_root)

    resolved = resolve_nightly_runtime(config, env, layer.values)
    settings = resolved["settings"]
    sources = resolved["sources"]
    # 被排期的任务只拿得到 env_allowlist 里的变量（CWORK_APP_KEY），但它**会**自己
    # 再读一次项目根的 `.env`。用「允许清单 ∩ 当前 shell + 同一份 .env」再算一次，
    # 就是它今晚真实的取值。把整个环境清空是不对的，那会把「凭据来自环境变量」
    # 误判成「依赖 shell」；把 `.env` 一起清掉同样不对，理由见上。
    scheduled_env = {k: v for k, v in env.items() if k in SCHEDULED_ENV_ALLOWLIST}
    scheduled = resolve_nightly_runtime(config, scheduled_env, layer.values)["settings"]
    # 差异有两种成因，都必须拒绝：值只在 shell 里（定时任务拿不到），或者 shell 正
    # 遮住一个 `.env` 值（今晚没有那个 shell，`.env` 会翻上来生效）。后者尤其阴险：
    # 交互式解析说「不发布」，定时运行却会发布。
    env_only = [key for key in NIGHTLY_SETTING_KEYS if settings[key] != scheduled[key]]
    from_project_env = [k for k in NIGHTLY_SETTING_KEYS if sources[k] == "project_env"]

    caps = {
        "detail_cap": settings["detail_cap"],
        "continuation_cap": settings["continuation_cap"],
        "backfill_cap": settings["backfill_cap"],
        "backfill_page_size": settings["backfill_page_size"],
    }
    backfill_enabled = settings["backfill_enabled"]
    source_completeness = settings["source_completeness"]
    sync_docdb = settings["sync_docdb"]
    wiki_sync = settings["wiki_sync"]
    lookback = settings["source_completeness_lookback_days"]

    # 发布通道有**两条**，不是一条。`sync_docdb` 推 daily/ 与 runs/，
    # `wiki_sync` 推 wiki/，后者由 `if args.wiki_sync:` 独立触发，不需要
    # `sync_docdb` 为真。合同以前只写前一条，于是 `{"sync_docdb": false,
    # "wiki_sync": true}` 会被描述成「不发布」。
    publication_targets = []
    if sync_docdb:
        publication_targets.append("docdb:daily_and_runs")
    if wiki_sync:
        publication_targets.append("docdb:wiki")
    # 模型调用同样是「内容离开这台机器」。dry-run 时不会真的调。
    ai_sends_content = bool(settings["ai_enabled"] and not settings["ai_dry_run"])

    contract = {
        "schema": EXECUTION_CONTRACT_SCHEMA,
        "generated_at": generated_at,
        "profile_sha256": profile_sha256,
        "sources": {
            "daily_lanes": list(DAILY_LANES),
            "backfill_lanes": list(BACKFILL_LANES) if backfill_enabled else [],
            "backfill_enabled": backfill_enabled,
            "backfill_rotation": "round_robin",
            # 补采关掉时 late_data_lookback_days 那一趟根本不会跑，所以这个开关
            # 必须和天数一起出现，否则合同会声称一个不存在的回溯范围。
            "source_completeness_enabled": source_completeness,
        },
        "caps": caps,
        # 登记表里每一个键的解析结果。进哈希，所以任何一项变化都会作废旧确认。
        "settings": render_nightly_settings(settings, project_dir=project_dir),
        "runtime_resolution": {
            "sources": dict(sources),
            # 交接单 argv 只有 --config/--run-name/--date，命令行那一层不参与取值。
            "resolved_for_argv": "config_run_name_date_only",
            "scheduled_environment_equivalent": not env_only,
            "settings_requiring_shell_environment": env_only,
            "scheduled_environment_allowlist": list(SCHEDULED_ENV_ALLOWLIST),
            # 项目根 `.env` 这一层。全部是封闭词表：固定字面量 `.env`、布尔、计数，
            # 以及登记表里的键名。**不出现** `.env` 里的任意名字、任意取值、任意
            # 正文或任何路径——那个文件的内容是攻击者可控文本，也常常是别的工具的
            # 凭据，抄进合同就等于替用户把它转发出去。
            "project_env": {
                "file": PROJECT_ENV_FILE,
                "present": layer.present,
                "loaded_by": "cwk_nightly_pipeline.load_local_env",
                # 上游是 setdefault：进程里已有的名字不会被覆盖。
                "shell_overrides_file": True,
                # 定时任务也会自己读同一个文件，所以它**不是** shell 依赖。
                "reloaded_by_scheduled_run": True,
                "modelled_variables_present": sum(
                    1 for name in layer.values if name in NIGHTLY_ENV_KEYS
                ),
                "settings_sourced_from_file": from_project_env,
            },
        },
        "scheduled_invocation": {
            "argv_options": ["--config", "--run-name", "--date"],
            # 这些开关只走命令行，被排期的那次运行里恒为下面的值。
            # `--sync-dry-run` 恒假正是「wiki_sync 会真的上传」的原因。
            "cli_only_flags_fixed": dict(NIGHTLY_CLI_ONLY_FIXED),
            "requires_environment": list(SCHEDULED_ENV_ALLOWLIST),
        },
        "detail_read_actions": list(DETAIL_READ_ACTIONS),
        "current_business_day_full_pagination": True,
        "late_data_lookback_days": lookback,
        "outputs": list(OUTPUTS),
        "publishing": {
            "sync_docdb": sync_docdb,
            "wiki_sync": wiki_sync,
            "any_external_publication": bool(publication_targets),
            "targets": publication_targets,
            "publishes_derived_only": True,
            "uploads_raw": False,
            # 交接单 argv 不带 --sync-dry-run，所以发布是真的发布。
            "dry_run": False,
            # cloud_first / publish_cloud_query_catalog 在解析阶段就被拒了，
            # 所以这两条恒假；写出来是为了让合同自己说清它为什么敢这么讲。
            "cloud_query_catalog": False,
            "cloud_first": False,
        },
        "wiki_pipeline": {
            "sync_wiki": settings["sync_wiki"],
            "compile": settings["wiki_compile"],
            "topics_entities": settings["wiki_topics_entities"],
            "sync_to_docdb": wiki_sync,
        },
        "ai_processing": {
            "enabled": settings["ai_enabled"],
            "dry_run": settings["ai_dry_run"],
            "sends_content_to_model_service": ai_sends_content,
        },
        "raw_boundary": {
            "raw_is_local_and_authoritative": True,
            "raw_never_written_back": True,
            # raw 上云只发生在 cloud_first 下，而 cloud_first 已被拒绝。
            "raw_never_uploaded": True,
        },
        "forbidden_actions": list(FORBIDDEN_ACTIONS),
        "schedule_intent": {
            "cadence": "daily",
            "run_at_local": run_at_local,
            "timezone": timezone,
        },
        "read_only": True,
    }
    contract["contract_sha256"] = compute_contract_sha256(contract)
    return contract


def compute_contract_sha256(contract: Mapping[str, Any]) -> str:
    """合同的稳定摘要。

    只对语义字段求哈希（去掉时间戳与摘要字段本身），并用 JCS 规范化，
    因此同一份配置反复生成得到同一个摘要；任何一项来源、上限、回看天数、
    发布开关或运行时间变化都会改变它。
    """

    body = {k: v for k, v in contract.items() if k not in _CONTRACT_VOLATILE_KEYS}
    return hashlib.sha256(_CONTRACT_DOMAIN + canonical_json_bytes(body)).hexdigest()


def contract_drift(contract: Mapping[str, Any], recorded_sha256: Optional[str]) -> dict:
    """比对当前合同与状态里记录的摘要。"""

    current = compute_contract_sha256(contract)
    return {
        "current_contract_sha256": current,
        "recorded_contract_sha256": recorded_sha256,
        "drifted": recorded_sha256 is not None and recorded_sha256 != current,
    }


_SOURCE_LABELS = {
    "config": "配置文件",
    "shell": "当前 shell 的环境变量",
    "project_env": "项目根的 .env 文件",
    "default": "上游默认值",
    # 旧词表。`.env` 这一层进合同之前，环境来源统一记成 "env"。留着只是为了让一份
    # 早于本次改动写下的合同仍然能被念成人话——它的哈希在新词表下必然已经变了，
    # 所以走到这里的路径只有「渲染一份历史文件」。
    "env": "当前 shell 的环境变量",
}


def render_contract_markdown(contract: Mapping[str, Any]) -> str:
    """给人读的合同复述。内容全部来自合同对象，不另写文案。"""

    caps = contract["caps"]
    sources = contract["sources"]
    schedule = contract["schedule_intent"]
    publishing = contract["publishing"]
    wiki = contract.get("wiki_pipeline") or {}
    ai = contract.get("ai_processing") or {}
    lines = [
        "# CWK 每日执行合同",
        "",
        f"- 合同摘要：`{contract.get('contract_sha256')}`",
        f"- 运行时间：每天 {schedule['run_at_local']}（{schedule['timezone']}）",
        "",
        "## 每天读取哪些来源",
        f"- 日常 lane（{len(sources['daily_lanes'])} 条）：{', '.join(sources['daily_lanes'])}",
        f"- 历史回填 lane（{len(sources['backfill_lanes'])} 条，轮转）："
        + (", ".join(sources["backfill_lanes"]) or "本次未启用"),
        f"- 详情读取动作：{', '.join(contract['detail_read_actions'])}",
        "",
        "## 每轮处理上限",
        f"- 新增/更新/顺延：{caps['detail_cap']}",
        f"- 持续事项：{caps['continuation_cap']}",
        f"- 历史回填：{caps['backfill_cap']}（每页 {caps['backfill_page_size']}）",
        "",
        "## 完整性",
        "- 当前业务日：完整分页",
        f"- 来源完整性补采：{'开' if sources.get('source_completeness_enabled') else '关'}",
        f"- 迟到数据回看：前 {contract['late_data_lookback_days']} 个业务日"
        + ("" if sources.get("source_completeness_enabled") else "（补采已关，本项不生效）"),
        "",
        "## 产出与发布",
        f"- 产物：{', '.join(contract['outputs'])}",
        # 两条发布通道分开说。合在一句「发布：开/关」里，
        # `sync_docdb: false, wiki_sync: true` 就会被念成「不发布」。
        f"- daily/ 与 runs/ 发布到 DocDB：{'开' if publishing['sync_docdb'] else '关'}",
        f"- wiki/ 发布到 DocDB：{'开' if publishing.get('wiki_sync') else '关'}"
        + ("（由 wiki_sync 独立控制，不需要 sync_docdb 为真）" if publishing.get("wiki_sync") else ""),
        f"- 本次是否有内容离开这台机器：{'是' if publishing.get('any_external_publication') else '否'}"
        + (f"（目标：{', '.join(publishing.get('targets') or [])}）" if publishing.get("targets") else ""),
        "- 发布是真发布，不是空跑（被排期的 argv 不带 --sync-dry-run）",
        f"- AI 处理：{'开' if ai.get('enabled') else '关'}"
        + ("（dry-run，不外发）" if ai.get("enabled") and ai.get("dry_run") else ""),
        f"- 记录内容发送给外部模型服务：{'是' if ai.get('sends_content_to_model_service') else '否'}",
        "- raw 原文：只留在本地，不回写、不上传",
        "",
        "## Wiki 流水线",
        f"- 编译摘要：{'开' if wiki.get('compile') else '关'}",
        f"- 重建 topics/entities：{'开' if wiki.get('topics_entities') else '关'}",
        f"- 同步 wiki/ 到 DocDB：{'开' if wiki.get('sync_to_docdb') else '关'}",
        "",
        "## 这些取值从哪里来",
    ]
    resolution = contract.get("runtime_resolution") or {}
    rendered = contract.get("settings") or {}
    origins = resolution.get("sources") or {}
    # 逐条列出登记表里的**每一个**键，而不是挑几个。这一节存在的意义就是让
    # 「这个数字/开关是哪来的」有一个不用猜的答案；漏掉一行，那一行就成了
    # 合同里没人核对过的部分。
    for name in NIGHTLY_SETTING_KEYS:
        origin = origins.get(name)
        if not origin:
            continue
        shown = rendered.get(name)
        if isinstance(shown, dict):
            shown = shown.get("state", "unset")
        lines.append(f"- {name} = `{shown}` ← {_SOURCE_LABELS.get(origin, origin)}")
    # 项目根的 `.env` 会在 nightly 启动的第一步被读进环境。它被 gitignore、不会出现
    # 在配置文件里、也不会出现在命令行上——用户要是不知道它在参与，就等于在对一份
    # 自己看不见来源的行为签字。所以只要它存在就明说，哪怕当前一个取值都没被它决定。
    project_env = resolution.get("project_env") or {}
    if project_env.get("present"):
        from_file = list(project_env.get("settings_sourced_from_file") or [])
        note = (
            f"- 项目根存在 `{project_env.get('file', PROJECT_ENV_FILE)}`，"
            "nightly 启动时会先把它读进环境（同名时当前 shell 的值优先）。"
        )
        if from_file:
            note += "本合同中由它决定的取值：" + "、".join(from_file) + "。"
        else:
            note += "本合同中没有取值由它决定。"
        lines.extend(["", note])
    needs_shell = list(resolution.get("settings_requiring_shell_environment") or [])
    if needs_shell:
        lines.extend(
            [
                "",
                "> 警告：以下取值在被排期的那次运行里会解析成**另一个值**——它们要么"
                "来自当前 shell 的 CWK_* 环境变量（定时任务只会拿到 CWORK_APP_KEY），"
                "要么正被当前 shell 遮住、届时会由 .env 里的值翻上来生效："
                + "、".join(needs_shell)
                + "。把它们写进配置文件后重新渲染，否则不会出交接单。",
            ]
        )
    lines.extend(["", "## 绝不执行的动作"])
    lines.extend(f"- {action}" for action in contract["forbidden_actions"])
    return "\n".join(lines) + "\n"


# ── 3. 试跑门禁 ─────────────────────────────────────────────────────────────

# 采集回执必须真的长这样才算「有回执」。字段取自 scripts/cwk_collect_live.py
# 实际写出的 collect-manifest.json；缺字段或类型不对 = 这不是采集器的回执，
# 按缺证处理，不按「大概可以」处理。
COLLECT_RECEIPT_SHAPE: tuple[tuple[str, str], ...] = (
    ("daily_source_complete", "bool"),
    ("daily_source_failure_count", "int"),
    ("written_count", "int"),
    ("errors", "list"),
    ("mutating_commands_called", "list"),
)


def _shape_ok(value: Any, kind: str) -> bool:
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "int":
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0
    if kind == "list":
        return isinstance(value, list)
    return False


def verify_collect_receipt(collect_manifest: Optional[Mapping[str, Any]]) -> dict:
    """核验采集回执，并抽出要进哈希的既核事实。

    「有没有给」「形状对不对」「本身成没成功」三件事分开判，任何一件不成立
    都不给 PASS。**省略参数不是中立的**：没有采集回执就无法声称当天来源完整，
    所以省略等价于缺证，而不是「这一项不适用」。

    返回值整体会被绑进试跑回执的哈希，其中 ``receipt_sha256`` 是采集回执
    文档自身的摘要——换一份采集证据，试跑回执的哈希必然改变，因而当时
    基于旧证据做出的第二道确认自动作废。
    """

    if collect_manifest is None:
        return {
            "present": False,
            "shape_valid": False,
            "success": False,
            "problems": ["collect_receipt_omitted"],
            "receipt_sha256": None,
            "verified": None,
        }
    if not isinstance(collect_manifest, Mapping):
        return {
            "present": True,
            "shape_valid": False,
            "success": False,
            "problems": ["collect_receipt_not_an_object"],
            "receipt_sha256": None,
            "verified": None,
        }

    problems: list[str] = []
    for name, kind in COLLECT_RECEIPT_SHAPE:
        if name not in collect_manifest:
            problems.append(f"collect_receipt_missing_{name}")
        elif not _shape_ok(collect_manifest[name], kind):
            problems.append(f"collect_receipt_bad_{name}")
    shape_valid = not problems

    receipt_sha256 = sha256_of_json(dict(collect_manifest))

    if not shape_valid:
        return {
            "present": True,
            "shape_valid": False,
            "success": False,
            "problems": problems,
            "receipt_sha256": receipt_sha256,
            "verified": None,
        }

    verified = {
        "daily_source_complete": collect_manifest["daily_source_complete"],
        "daily_source_failure_count": collect_manifest["daily_source_failure_count"],
        "written_count": collect_manifest["written_count"],
        "error_count": len(collect_manifest["errors"]),
        "mutating_command_count": len(collect_manifest["mutating_commands_called"]),
    }
    if verified["daily_source_complete"] is not True:
        problems.append("collect_receipt_daily_source_incomplete")
    if verified["daily_source_failure_count"] != 0:
        problems.append("collect_receipt_has_source_failures")
    if verified["error_count"] != 0:
        problems.append("collect_receipt_has_errors")
    if verified["mutating_command_count"] != 0:
        problems.append("collect_receipt_called_mutating_commands")

    return {
        "present": True,
        "shape_valid": True,
        "success": not problems,
        "problems": problems,
        "receipt_sha256": receipt_sha256,
        "verified": verified,
    }


def _evidence_sha256(document: Optional[Mapping[str, Any]]) -> Optional[str]:
    """证据文档自身的摘要；没给就是 None（缺证也是一个要进哈希的事实）。"""

    if document is None:
        return None
    return sha256_of_json(dict(document))


def _mutating_calls(document: Any) -> list:
    """回执自称调用过的写操作。非对象、缺字段当空；非列表的真值当一次调用。"""

    if not isinstance(document, Mapping):
        return []
    value = document.get("mutating_commands_called")
    if isinstance(value, (list, tuple)):
        return list(value)
    return [] if not value else [value]


def evaluate_pilot(
    *,
    nightly_manifest: Optional[dict],
    acceptance: Optional[dict],
    collect_manifest: Optional[dict] = None,
    bound_contract_sha256: str,
    generated_at: str,
) -> dict:
    """判定一次只读试跑是否够格放行定时任务。

    全部谓词必须为真才 PASS。任何一项不达标，结果就是 FAIL，
    调用方只能把状态推进到 DEGRADED——`PILOT_PASSED` 在迁移表里
    根本无法从 FAIL 回执到达。

    三份证据（nightly manifest、验收回执、采集回执）**都是必需的**，
    并且它们各自的文档摘要都进回执哈希：换证据必然换回执，绑在旧回执上的
    第二道确认随之失效。
    """

    nightly = nightly_manifest if isinstance(nightly_manifest, Mapping) else {}
    accept = acceptance if isinstance(acceptance, Mapping) else {}

    checks = accept.get("checks")
    checks = checks if isinstance(checks, dict) else {}

    collect_check = verify_collect_receipt(collect_manifest)
    collect_verified = collect_check["verified"] or {}

    predicates: dict[str, bool] = {
        "nightly_manifest_present": nightly_manifest is not None,
        "acceptance_present": acceptance is not None,
        "collect_receipt_present": collect_check["present"],
        "collect_receipt_shape_valid": collect_check["shape_valid"],
        "collect_receipt_success": collect_check["success"],
        "nightly_overall_pass": nightly.get("overall_pass") is True,
        "nightly_content_quality_pass": nightly.get("content_quality_pass") is True,
        "nightly_not_degraded": nightly.get("degraded") is not True,
        "no_sync_failures": not (nightly.get("sync_failures") or []),
        "no_source_completeness_failures": not (
            nightly.get("source_completeness_failures") or []
        ),
        "acceptance_overall_pass": accept.get("overall_pass") is True,
        "acceptance_no_failures": not (accept.get("failures") or []),
        "acceptance_all_checks_pass": bool(checks) and all(bool(v) for v in checks.values()),
        "acceptance_a4_ok": accept.get("A4_status") in {"PASS", "PASS_LOW_VOLUME"},
        # 缺回执时这一条必然为假：没有证据就不能声称当天来源完整。
        "daily_source_complete": collect_verified.get("daily_source_complete") is True,
        "no_mutating_commands": not _mutating_calls(nightly_manifest)
        and not _mutating_calls(acceptance)
        and not _mutating_calls(collect_manifest),
    }

    failed = sorted(name for name, ok in predicates.items() if not ok)
    receipt = {
        "schema": PILOT_GATE_RECEIPT_SCHEMA,
        "generated_at": generated_at,
        "bound_contract_sha256": bound_contract_sha256,
        "result": "PASS" if not failed else "FAIL",
        "predicates": predicates,
        "failed_predicates": failed,
        "collection_receipt": collect_check,
        "evidence": {
            "nightly_manifest_sha256": _evidence_sha256(nightly_manifest),
            "acceptance_sha256": _evidence_sha256(acceptance),
            "collect_manifest_sha256": collect_check["receipt_sha256"],
        },
        "run_name": nightly.get("run_name"),
        "business_date": nightly.get("date"),
        "processed_count": _as_int(nightly.get("processed_count"), 0),
    }
    receipt["receipt_sha256"] = sha256_of_json(
        {k: v for k, v in receipt.items() if k != "generated_at"}
    )
    return receipt


# ── 4. 调度交接与回执 ───────────────────────────────────────────────────────


class ConfigLocatorError(ValueError):
    """配置文件无法表示成项目相对定位符。

    这是 fail-closed 的落点：宁可当场拒绝出交接单，也不把宿主的绝对路径写进
    Agent 可见的成功负载与 `handoff_sha256`。
    """


# 交接单里唯一被允许的「项目根」表述：只说宿主该**怎么找**，不说它**在哪**。
# 绝对路径同时是主机布局与用户身份的泄露面（/Users/<name>/… 、/home/<name>/…），
# 而宿主本来就知道自己把仓库放在哪——把它回写进交接单没有任何信息增益。
_PROJECT_ROOT_LOCATOR = {
    "kind": "cwk_project_root",
    "resolution_order": [
        "environment variable CWK_PROJECT_DIR",
        "the directory the host already uses to run this repository's scripts",
    ],
    "verify_marker": "scripts/cwk_doctor.py",
    "absolute_path_intentionally_omitted": True,
}

CONFIG_LOCATOR_SCHEMA = "cwk.activation_config_locator.v1"

# 段内一律不接受控制字符；首段不接受以 - 开头（会被下游当成选项解析）。
_UNSAFE_SEGMENT = re.compile(r"[\x00-\x1f\x7f]")


def project_root_locator() -> dict:
    """返回项目根定位契约的独立副本，避免调用方改到模块级常量。"""

    locator = dict(_PROJECT_ROOT_LOCATOR)
    locator["resolution_order"] = list(_PROJECT_ROOT_LOCATOR["resolution_order"])
    return locator


def build_config_locator(*, config_path: Path | str, project_root: Path | str) -> dict:
    """把配置路径压成**项目相对**定位符；压不下去就当场失败。

    宿主 Agent 需要的信息是「在项目根下跑哪一个配置」，不是「这台机器上的绝对
    路径」。因此这里只输出相对段，并附上项目根的解析契约让宿主自己解析。
    配置落在项目之外时不做任何降级——降级的唯一形式就是把绝对路径写回去，
    那正是要防的东西。
    """

    root = Path(project_root).resolve()
    target = Path(config_path)
    if not target.is_absolute():
        target = Path.cwd() / target
    target = target.resolve()

    try:
        relative = target.relative_to(root)
    except ValueError:
        raise ConfigLocatorError(
            "the config file is outside the CWK project directory; describing it would "
            "require an absolute host path, so no scheduler handoff was produced. "
            "Move the config inside the project directory and re-run."
        ) from None

    parts = relative.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ConfigLocatorError(
            "the config path cannot be reduced to a stable project-relative locator"
        )
    if any(_UNSAFE_SEGMENT.search(part) for part in parts):
        raise ConfigLocatorError(
            "the config file name contains control characters and cannot be handed off safely"
        )
    if parts[0].startswith("-"):
        raise ConfigLocatorError(
            "the config path would be parsed as a command-line option and cannot be handed off safely"
        )

    return {
        "schema": CONFIG_LOCATOR_SCHEMA,
        "kind": "project_relative",
        "path": "/".join(parts),
        "resolved_against": "cwk_project_root",
        "project_root_locator": project_root_locator(),
        "absolute_path_omitted": True,
    }


def build_scheduler_handoff(
    *,
    contract: Mapping[str, Any],
    contract_sha256: str,
    profile_sha256: str,
    pilot_receipt_sha256: str,
    config_path: Path | str,
    project_root: Path | str,
    generated_at: str,
) -> dict:
    """产出「请宿主建一个这样的任务」的交接单。

    本仓库不建任务。交接单只描述要跑什么命令、需要哪些环境变量名（**只有名字，
    没有值**）、以及前置条件。真正的创建动作由用户在宿主/OpenClaw 侧完成，
    完成后把外部任务标识回填给 `validate_schedule_receipt`。

    配置位置以项目相对定位符表述，绝对路径既不进负载也不进 `handoff_sha256`；
    无法安全表述时抛 `ConfigLocatorError`，不出交接单。

    合同若依赖当前 shell 的 CWK_* 变量，同样拒绝出单（`ScheduledEnvironmentMismatch`）：
    env_allowlist 只有 CWORK_APP_KEY，宿主那条任务复现不出这份合同。
    """

    resolution = contract.get("runtime_resolution") or {}
    needs_shell = list(resolution.get("settings_requiring_shell_environment") or [])
    if needs_shell:
        raise ScheduledEnvironmentMismatch(
            "the contract depends on shell environment variables the scheduled task will "
            f"not receive ({', '.join(sorted(needs_shell))}); move those values into the "
            "config file, re-render the contract and re-run the pilot"
        )
    locator = build_config_locator(config_path=config_path, project_root=project_root)
    schedule = contract["schedule_intent"]
    handoff = {
        "schema": SCHEDULER_HANDOFF_SCHEMA,
        "generated_at": generated_at,
        "contract_sha256": contract_sha256,
        "profile_sha256": profile_sha256,
        "pilot_receipt_sha256": pilot_receipt_sha256,
        "cadence": schedule["cadence"],
        "run_at_local": schedule["run_at_local"],
        "timezone": schedule["timezone"],
        "config_locator": locator,
        "command_spec": {
            "argv": [
                "python3",
                "scripts/cwk_nightly_pipeline.py",
                "--config",
                locator["path"],
                "--run-name",
                "nightly-{{YYYYMMDD-HHMM}}",
                "--date",
                "{{YYYY-MM-DD}}",
            ],
            "cwd_relative_to_project_root": ".",
            "project_root_locator": project_root_locator(),
            "env_allowlist": ["CWORK_APP_KEY"],
            "secrets_included": False,
            "absolute_paths_included": False,
        },
        "preconditions": [
            "activation state is PILOT_PASSED",
            "second human confirmation is bound to this contract_sha256",
            "pilot receipt result is PASS for this contract_sha256",
        ],
        "host_responsibilities": [
            "resolve the CWK project root locally using project_root_locator",
            "create the scheduled task using the host's own mechanism",
            "return the external task id to `record-schedule`",
            "never embed credentials in the task definition",
        ],
        "repository_does_not": [
            "create, modify or delete scheduled tasks",
            "call OpenClaw, Gateway or cron APIs",
            "assume any specific scheduler API exists",
        ],
        "requires_second_confirmation": True,
    }
    handoff["handoff_sha256"] = sha256_of_json(
        {k: v for k, v in handoff.items() if k != "generated_at"}
    )
    return handoff


def validate_schedule_receipt(
    *,
    handoff: Mapping[str, Any],
    contract_sha256: str,
    external_task_id: str,
) -> dict:
    """校验宿主回填的调度回执是否与本次交接单和当前合同一致。"""

    problems: list[str] = []
    if handoff.get("schema") != SCHEDULER_HANDOFF_SCHEMA:
        problems.append("handoff_schema_unknown")
    if handoff.get("contract_sha256") != contract_sha256:
        problems.append("handoff_contract_mismatch")
    if not isinstance(external_task_id, str) or not external_task_id.strip():
        problems.append("external_task_id_missing")
    return {"ok": not problems, "problems": problems}


def detect_schedule_drift(
    *,
    state_schedule: Optional[Mapping[str, Any]],
    observed_task_id: Optional[str],
    contract_sha256: str,
) -> dict:
    """检查外部调度是否与记录一致。

    发现未知任务只如实报告，**绝不**自动删除或覆盖——那可能是别人的任务。
    """

    findings: list[str] = []
    if state_schedule is None:
        if observed_task_id:
            findings.append("unknown_external_task")
        return {"drifted": bool(findings), "findings": findings, "destructive_action_taken": False}

    if state_schedule.get("bound_contract_sha256") != contract_sha256:
        findings.append("contract_drift")
    if observed_task_id is not None and observed_task_id != state_schedule.get("external_task_id"):
        findings.append("schedule_id_unknown")
    return {"drifted": bool(findings), "findings": findings, "destructive_action_taken": False}
