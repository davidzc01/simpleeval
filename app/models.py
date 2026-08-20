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

class Project(BaseModel):
    id: str
    name: str
    task_shape: Literal["coding", "customer_service", "multi_turn", "general", "custom"] = "general"
    judge_config: JudgeConfig
    target_config: TargetConfig
    token_budget: Optional[TokenBudget] = None


class EvalCheck(BaseModel):
    """单条 case 的多字段验证项（T1-5: REQ-3 单返回多字段验证）

    - field: 点路径（如 evidence 或 a.b），作用于解包后的对象；空 = 用 actual_output 原文
    - eval_type: 与 EvalCase.eval_type 同类型集合
    - expected: exact/contains/length 用；llm_judge 用 output_requirement 语义
    - eval_params: 如 contains 的 substring、length 的 min/max
    """
    name: str
    field: str = ""
    eval_type: Literal["exact", "contains", "not_contains", "length", "llm_judge"]
    expected: Optional[str] = None
    eval_params: Optional[dict] = Field(default_factory=dict)


class EvalCase(BaseModel):
    """评测集里的单条 case"""
    id: str
    case_name: str
    input: str
    expected_output: Optional[str] = None       # exact / contains 用
    output_requirement: Optional[str] = None    # llm_judge 用的评判标准
    eval_type: Literal["exact", "contains", "not_contains", "length", "llm_judge"]
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


class EvalSet(BaseModel):
    id: str
    project_id: str
    name: str
    cases: list[EvalCase]


# ============== 结果模型 ==============

class CaseResult(BaseModel):
    """单条 case 的评测结果"""
    case_name: str
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
