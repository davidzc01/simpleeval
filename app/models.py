"""simpleEval 数据模型（pydantic）"""

from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field


# ============== 枚举类型 ==============

class AuthType(str):
    NONE = "none"
    BEARER = "bearer"
    API_KEY = "api_key"
    COOKIE = "cookie"
    HEADERS = "headers"


class EvalType(str):
    EXACT = "exact"
    CONTAINS = "contains"
    NOT_CONTAINS = "not_contains"
    LENGTH = "length"
    LLM_JUDGE = "llm_judge"


class TaskShape(str):
    CODING = "coding"
    CUSTOMER_SERVICE = "customer_service"
    MULTI_TURN = "multi_turn"
    GENERAL = "general"
    CUSTOM = "custom"


class RunStatus(str):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


# ============== 配置模型 ==============

class AuthConfig(BaseModel):
    """认证配置"""
    type: Literal["none", "bearer", "api_key", "cookie", "headers"] = "none"
    bearer_token: Optional[str] = None
    api_key_header: Optional[str] = "X-API-Key"
    api_key_value: Optional[str] = None
    cookies: list[dict] = Field(default_factory=list)  # [{"name": "...", "value": "..."}]
    headers: list[dict] = Field(default_factory=list)  # [{"name": "...", "value": "..."}]


class ResponseMapping(BaseModel):
    """响应字段映射（旧设计，兼容保留；自动转为 output_paths）"""
    name: str
    jsonpath: str  # 如 "$.data.reply"


class ResponseParsing(BaseModel):
    """响应解析配置（四键模型 + content 二次解包，无 mode 概念）

    - output_paths: 输出候选路径列表，从上到下依次尝试，第一个命中生效
    - token_paths: token 路径列表，所有命中值求和
    - token_fields: token 字段名列表，全树递归匹配同名字段并求和
    - token_scope: 可选过滤条件，与 token_fields 配合
    - output_unpack_json: 提取 output 后尝试 json.loads，成功则在解包后对象上继续取 output_field
    - output_field: 解包后取该键（支持点路径 a.b）；空 = 用解包后对象原文；
      取到的布尔/数字统一 stringify 供 exact 匹配
    """
    output_paths: list[str] = Field(default_factory=list)
    token_paths: list[str] = Field(default_factory=list)
    token_fields: list[str] = Field(default_factory=list)
    token_scope: Optional[dict] = None
    output_unpack_json: bool = False
    output_field: Optional[str] = None


class JudgeConfig(BaseModel):
    """LLM-as-Judge 配置（双模式，与 TargetConfig 对齐）

    api_type:
    - openai_compatible（默认）：走 messages + model 注入，旧数据零迁移
    - custom：request_template 必填，渲染模板后不注入 model/messages，响应用 response_parsing 提取分数
    """
    api_type: Literal["openai_compatible", "custom"] = "openai_compatible"
    base_url: str
    api_key: str = ""  # 留空 = 不带 Authorization 头（custom 模式）
    model: Optional[str] = None  # openai_compatible 模式必填
    request_template: Optional[str] = None  # custom 模式必填
    auth: AuthConfig = Field(default_factory=AuthConfig)
    response_parsing: Optional[ResponseParsing] = None
    prompt_template: str = (
        "你是一个评测者。请判断被评测模型的输出是否满足要求。\n"
        "要求：{requirement}\n"
        "被评测输出：{output}\n"
        "只回答一个 0 到 1 之间的数字，表示满足程度（1 为完全满足，0 为完全不满足）。不要输出其他内容。"
    )


class TargetConfig(BaseModel):
    """被评测 API 的请求配置

    api_type:
    - openai_compatible（默认）：model 必填，请求模板默认走 OpenAI /chat/completions
    - custom：request_template 必填，渲染模板后不注入 model/messages，URL 不补 /chat/completions
    校验在 routes 层 PUT /projects 时阻断（422），不在模型层阻断（允许创建空项目）。
    """
    api_type: Literal["openai_compatible", "custom"] = "openai_compatible"
    base_url: str
    api_key: str = ""  # 留空 = 不带 Authorization 头
    model: Optional[str] = None  # openai_compatible 模式必填
    request_template: str = "{input}"  # custom 模式必填
    auth: AuthConfig = Field(default_factory=AuthConfig)
    response_mapping: list[ResponseMapping] = Field(default_factory=list)
    response_parsing: Optional[ResponseParsing] = None


class TokenBudget(BaseModel):
    """token 预算（MVP 只提醒，不中断）"""
    limit: int
    warn_only: bool = True


# ============== 核心模型 ==============

class ProjectVersion(BaseModel):
    """T3-3: 项目版本（时间锚点，run 归属版本用于跨版本对比）"""
    id: str
    name: str
    created_at: str  # ISO 时间


class ScheduleConfig(BaseModel):
    """T3-4: 定时回归配置

    - enabled: 开关
    - cron: 5 字段标准 cron 表达式（分 时 日 月 周），如 "*/30 * * * *" = 每 30 分钟
    - tags: 按标签筛选 case（空 = 全部启用 case）
    - version_id: 归属版本（None = 自动落入最近版本）
    - regression_threshold: pass_rate 绝对降幅阈值（默认 0.1 = 10%）
    """
    enabled: bool = False
    cron: str = "* * * * *"
    tags: list[str] = Field(default_factory=list)
    version_id: Optional[str] = None
    regression_threshold: float = 0.1


class Project(BaseModel):
    id: str
    name: str
    task_shape: Literal["coding", "customer_service", "multi_turn", "general", "custom"] = "general"
    judge_config: JudgeConfig
    target_config: TargetConfig
    token_budget: Optional[TokenBudget] = None
    # REQ-16: 引用全局 Judge 配置 id（优先于内联 judge_config）；
    # None 时 fallback 到内联 judge_config，向后兼容旧项目
    judge_config_id: Optional[str] = None
    # T3-1: 项目级最大并发数（1 = 串行，run 级 concurrency 不能超过此值）
    # 旧项目无此字段时默认 1，行为与现状完全一致
    max_concurrency: int = 1
    # T3-3: 版本列表（时间锚点），旧项目无此字段时为空列表
    versions: list[ProjectVersion] = Field(default_factory=list)
    # T3-4: 定时回归配置（None = 未配置，向后兼容）
    schedule: Optional[ScheduleConfig] = None
    # Q-3: 当前活动版本（被测 Target API 的版本切换锚点）。
    # None = 未切换（向后兼容，新 run 按 created_at 自动落入最近版本）；
    # 非 None 且存在于 versions → 新发起 run 默认归属此版本，概览以该版本为准。
    current_version_id: Optional[str] = None


class EvalCheck(BaseModel):
    """单条 case 的多字段验证项（T1-5: REQ-3 单返回多字段验证；U-10: 对称化）

    - field: 点路径（如 evidence 或 a.b），作用于解包后的对象；空 = 用 actual_output 原文
    - eval_type: 与 EvalCase.eval_type 同类型集合
    - expected: exact/contains/length 用
    - output_requirement: llm_judge 用的评判标准（U-10 新增，与 expected 平级）
    - eval_params: 如 contains 的 substring、length 的 min/max
    - name: 可选，未命名时按 field 或序号兜底（U-10 放宽）
    - R-4: 新增 regex / json_schema / numeric / script 四种确定性类型（免 Judge）
    """
    name: str = ""
    field: str = ""
    eval_type: Literal[
        "exact", "contains", "not_contains", "length", "llm_judge",
        "regex", "json_schema", "numeric", "script",
    ]
    expected: Optional[str] = None
    output_requirement: Optional[str] = None
    eval_params: Optional[dict] = Field(default_factory=dict)


class EvalCase(BaseModel):
    """评测集里的单条 case"""
    id: str
    case_name: str
    input: str
    expected_output: Optional[str] = None       # exact / contains 用
    output_requirement: Optional[str] = None    # llm_judge 用的评判标准
    # U-10: 当 validations 显式提供时，eval_type/expected_output/output_requirement 仅作旧字段兼容
    # 默认 "exact" 使 validations-only case 可直接构造（不强制填旧字段）
    # R-4: 新增 regex / json_schema / numeric / script 四种确定性类型（免 Judge）
    eval_type: Literal[
        "exact", "contains", "not_contains", "length", "llm_judge",
        "regex", "json_schema", "numeric", "script",
    ] = "exact"
    eval_params: Optional[dict] = Field(default_factory=dict)  # 如 contains 的 substring、length 的 min/max
    task_shape: Optional[str] = None            # 覆盖项目默认值
    enabled: bool = True
    # B-13: 模板多占位符变量。键名任意，数量任意，由用户在 request_template 里用 {key} 引用
    # 默认 None（不增字段语义），无 variables 时 {key} 占位符会触发"缺少变量"报错
    variables: Optional[dict] = None
    # T1-2: case 标签，用于发起评测时筛选
    tags: list[str] = Field(default_factory=list)
    # T1-5: 多字段验证项，case 通过 = 主验证通过 AND 所有 checks 通过
    checks: Optional[list[EvalCheck]] = None
    # U-10: 验证组（input + variables → 输入组；validations → 验证组）
    # validations 非空时统一走新结构；为空时由旧字段（主验证 + checks）合成，零迁移
    # 每项 EvalCheck：field 空/="output" 表示主输出验证；首条视为主验证
    validations: Optional[list[EvalCheck]] = None

    def get_validations(self) -> list[EvalCheck]:
        """U-10: 统一获取验证列表。

        - validations 非空：直接返回（新结构）
        - validations 为空（旧数据）：由旧字段合成 [主验证] + checks
          主验证 = EvalCheck(field="", eval_type=case.eval_type,
                              expected=expected_output, output_requirement=output_requirement,
                              eval_params=eval_params)
        """
        if self.validations:
            return list(self.validations)
        synthesized = [EvalCheck(
            name="主输出验证",
            field="",
            eval_type=self.eval_type,
            expected=self.expected_output,
            output_requirement=self.output_requirement,
            eval_params=self.eval_params or {},
        )]
        if self.checks:
            synthesized.extend(self.checks)
        return synthesized


class EvalSet(BaseModel):
    id: str
    project_id: str
    name: str
    cases: list[EvalCase]
    # T3-2: 评测集内容更新时间（PUT/replace 导入时刷新）
    # 采样统计只纳入 run.created_at ≥ content_updated_at 的 run；
    # None（旧数据）= 全部纳入，与现状一致
    content_updated_at: Optional[str] = None


# ============== 结果模型 ==============

class CaseResult(BaseModel):
    """单条 case 的评测结果"""
    case_name: str
    case_id: Optional[str] = None  # T2-1: 从 EvalCase.id 带入；旧 run 无此字段，聚合时 fallback case_name
    actual_output: str
    passed: bool
    score: float = 0.0
    latency_ms: float = 0.0
    token_used: int = 0
    skipped_reason: Optional[str] = None  # 跳过原因，如 "llm_unavailable"
    token_missing: bool = False  # token 无法统计时显式标记
    # T1-4: Judge 消耗的 token（llm_judge case 专有，规则类为 0）
    judge_token: int = 0
    # T1-5: 多字段验证明细
    check_results: list[dict] = Field(default_factory=list)
    # T3-1: 同一 run 内同一 case 的 k 次采样序号（1..k）；samples=1 时为 None，向后兼容
    sample_index: Optional[int] = None


class EvalSummary(BaseModel):
    """一次 run 的汇总 + 成本对比"""
    pass_rate: float
    total_token: int
    total_latency_ms: float
    token_per_pass: float          # 每万 token 完成率 = 通过数 / ((target_token + judge_token)/10000)
    latency_p50: float
    latency_p95: float
    # T1-4: Judge token 总消耗（评测成本 = 被评测消耗 + 评测自身消耗）
    judge_token: int = 0
    # T2-4: 预算硬限制触发标记（warn_only=false 且超限时为 True）
    budget_exceeded: bool = False
    # T3-1: 本次 run 实际并发数（1=串行）；UI 指标卡 tooltip 标注采集条件
    concurrency: int = 1


class EvalRun(BaseModel):
    """评测运行记录"""
    id: str
    project_id: str
    evalset_id: str
    status: Literal["queued", "running", "completed", "failed"] = "queued"
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    results: list[CaseResult] = Field(default_factory=list)
    summary: Optional[EvalSummary] = None
    # T3-3: 归属版本 id（显式指定或按 created_at 落入最近版本）；旧 run 无此字段为 None
    version_id: Optional[str] = None
    # V-3: 发起 run 时的标签筛选（从 case_filter.tags 带入，历史列表展示）
    filter_tags: list[str] = Field(default_factory=list)
    # W-3: 触发来源（手动 / 定时调度器）
    trigger: Literal["manual", "scheduled"] = "manual"
    # Q-1: 发起 run 时解析的 Judge 指纹（稳定 hash，不含 secret）；旧 run 无此字段为 None
    judge_fingerprint: Optional[str] = None


# ============== API 请求/响应模型 ==============

class CreateProjectRequest(BaseModel):
    """创建项目请求"""
    name: str
    task_shape: Literal["coding", "customer_service", "multi_turn", "general", "custom"] = "general"


class CreateEvalSetRequest(BaseModel):
    """创建评测集请求"""
    project_id: str
    name: str
    cases: list[EvalCase] = Field(default_factory=list)


class CaseFilter(BaseModel):
    """T1-2: 发起评测时按标签筛选 case

    - tags: 要匹配的标签列表
    - mode: "any" = 含任一标签即入选（OR）；"all" = 含全部标签才入选（AND）
    - tags 为空 = 不筛选，全部启用 case 参与
    """
    tags: list[str] = Field(default_factory=list)
    mode: Literal["any", "all"] = "any"


class RunEvalRequest(BaseModel):
    """发起评测请求"""
    project_id: str
    evalset_id: str
    # T1-2: 按标签筛选 case（可选，None/空 = 全部启用 case）
    case_filter: Optional[CaseFilter] = None
    # T3-1: 每 case 采样次数 k（默认 1 = 单次执行，行为同现状）
    samples: int = 1
    # T3-1: 本次 run 的并发数（None = 串行；指定值 ≤ project.max_concurrency，超出 422）
    concurrency: Optional[int] = None
    # T3-3: 显式指定归属版本 id（None = 按 created_at 自动落入最近版本）
    version_id: Optional[str] = None


class CreateVersionRequest(BaseModel):
    """T3-3: 开新版本请求"""
    name: str


class CreateTagRequest(BaseModel):
    """V-1: 新建标签请求"""
    name: str


class RenameTagRequest(BaseModel):
    """V-1: 改名标签请求"""
    new_name: str


class UpdateScheduleRequest(BaseModel):
    """W-3: 更新定时配置请求"""
    enabled: bool = False
    cron: str = "* * * * *"
    tags: list[str] = Field(default_factory=list)
    version_id: Optional[str] = None
    regression_threshold: float = 0.1


class ModelPrice(BaseModel):
    """Q-2/Q-5: 模型价格（端点 + 模型双 key 锁定，峰谷定价）

    - endpoint_pattern: 端点子串匹配（如 "api.deepseek" 匹配 "https://api.deepseek.com/v1"）；
      空 = 匹配任意端点（向后兼容旧数据）
    - model_pattern: 模型名前缀匹配（"deepseek" 匹配 "deepseek-v3-chat"）
    - 匹配规则：endpoint_pattern AND model_pattern 都命中；更具体的（合计 pattern 长度更长）优先
    - price_per_mtok: 每百万 token 价格（Q-2 基础价；Q-5 作为峰价兜底，peak_price_per_mtok 为空时用它）
    - peak_price_per_mtok: 峰价（可选，覆盖 price_per_mtok）；off_peak_price_per_mtok: 谷价（可选，空则用峰价）
    - peak_start_hour/peak_end_hour: 峰时段（[start, end) 小时，默认 9–22）；cost_estimate 按 run 时段选价
    - currency: 币种（默认 ¥）
    """
    id: str
    endpoint_pattern: str = ""
    model_pattern: str
    price_per_mtok: float
    peak_price_per_mtok: Optional[float] = None
    off_peak_price_per_mtok: Optional[float] = None
    peak_start_hour: int = 9
    peak_end_hour: int = 22
    currency: str = "¥"
    note: str = ""


class CreateModelPriceRequest(BaseModel):
    """Q-2/Q-5: 新建模型价格请求（峰谷定价可选）"""
    endpoint_pattern: str = ""
    model_pattern: str
    price_per_mtok: float
    peak_price_per_mtok: Optional[float] = None
    off_peak_price_per_mtok: Optional[float] = None
    peak_start_hour: int = 9
    peak_end_hour: int = 22
    currency: str = "¥"
    note: str = ""


class UpdateModelPriceRequest(BaseModel):
    """Q-5: 编辑模型价格请求（所有字段可选）"""
    endpoint_pattern: Optional[str] = None
    model_pattern: Optional[str] = None
    price_per_mtok: Optional[float] = None
    peak_price_per_mtok: Optional[float] = None
    off_peak_price_per_mtok: Optional[float] = None
    peak_start_hour: Optional[int] = None
    peak_end_hour: Optional[int] = None
    currency: Optional[str] = None
    note: Optional[str] = None


class BatchEstimateRequest(BaseModel):
    """Q-6: 批量预估请求（Q-8: 移除标签筛选——生产流量随机分布，无标签语义）

    - count: 批量任务规模 N（每条视为 1 个 case 执行）
    - plan_hour: 计划运行时段（0-23），用于选峰/谷价；None → 峰价兜底
    - version_id: 限定版本作用域采样（None → 用 current_version_id，再 None → 全量）
    - concurrency: 生产端并发度（time 区间按此分摊；默认 1 = 串行）
    """
    count: int
    plan_hour: Optional[int] = None
    version_id: Optional[str] = None
    concurrency: int = 1


class BatchEstimateQualityRequest(BaseModel):
    """Q-7: 质量-成本闭环预估请求

    - count: 批量任务规模 N
    - target_pass_rate: 目标正确率（0~1，默认 1.0 = 全过）
    - rerun_strategy: 仅失败重跑 / 全部重跑
    - time_mode: 峰 / 谷 / 混合
    - production_concurrency: 生产端并发度（默认预填评测环境值）
    - version_id: 指定版本（默认当前版本）
    """
    count: int
    target_pass_rate: float = 1.0
    rerun_strategy: Literal["failed_only", "all"] = "failed_only"
    time_mode: Literal["peak", "off_peak", "mixed"] = "peak"
    production_concurrency: int = 1
    version_id: Optional[str] = None


class TestTargetRequest(BaseModel):
    """测试目标 API 请求"""
    base_url: str
    api_key: str = ""
    model: Optional[str] = None
    request_template: str = "{input}"
    api_type: Literal["openai_compatible", "custom"] = "openai_compatible"
    auth: AuthConfig = Field(default_factory=AuthConfig)
    response_mapping: list[ResponseMapping] = Field(default_factory=list)
    response_parsing: Optional[ResponseParsing] = None


class TestMappingRequest(BaseModel):
    """测试映射提取请求（旧设计，兼容保留）"""
    response_mapping: list[ResponseMapping]
    sample_response: str


class TestParsingRequest(BaseModel):
    """测试响应解析请求（四键模型）"""
    response_parsing: ResponseParsing
    sample_response: str


class TestJudgeRequest(BaseModel):
    """测试 Judge 请求（T1-3: 双模式）"""
    base_url: str
    api_key: str = ""
    model: Optional[str] = None  # openai_compatible 模式必填
    prompt_template: str
    input: str
    output_requirement: str
    actual_output: str
    # T1-3: 双模式字段（可选，不传 = openai_compatible 旧行为）
    api_type: Literal["openai_compatible", "custom"] = "openai_compatible"
    request_template: Optional[str] = None
    auth: AuthConfig = Field(default_factory=AuthConfig)
    response_parsing: Optional[ResponseParsing] = None


class ErrorDetail(BaseModel):
    """错误详情"""
    code: str
    message: str


class ErrorResponse(BaseModel):
    """统一错误响应"""
    error: ErrorDetail
