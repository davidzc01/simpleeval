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
    """响应解析配置（四键模型，无 mode 概念）

    - output_paths: 输出候选路径列表，从上到下依次尝试，第一个命中生效
    - token_paths: token 路径列表，所有命中值求和
    - token_fields: token 字段名列表，全树递归匹配同名字段并求和
    - token_scope: 可选过滤条件，与 token_fields 配合
    """
    output_paths: list[str] = Field(default_factory=list)
    token_paths: list[str] = Field(default_factory=list)
    token_fields: list[str] = Field(default_factory=list)
    token_scope: Optional[dict] = None


class JudgeConfig(BaseModel):
    """LLM-as-Judge 用的模型配置（OpenAI Compatible）"""
    base_url: str
    api_key: str
    model: str
    prompt_template: str = (
        "你是一个评测者。请判断被评测模型的输出是否满足要求。\n"
        "要求：{requirement}\n"
        "被评测输出：{output}\n"
        "只回答一个 0 到 1 之间的数字，表示满足程度（1 为完全满足，0 为完全不满足）。不要输出其他内容。"
    )


class TargetConfig(BaseModel):
    """被评测 API 的请求配置（OpenAI Compatible）"""
    base_url: str
    api_key: str
    model: str
    request_template: str = "{input}"  # 默认直接把 case.input 塞进 prompt
    auth: AuthConfig = Field(default_factory=AuthConfig)
    response_mapping: list[ResponseMapping] = Field(default_factory=list)
    response_parsing: Optional[ResponseParsing] = None  # 新增：四键模型，优先于 response_mapping


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


class EvalSummary(BaseModel):
    """一次 run 的汇总 + 成本对比"""
    pass_rate: float
    total_token: int
    total_latency_ms: float
    token_per_pass: float          # 每万 token 完成率 = 通过数 / (总token/10000)
    latency_p50: float
    latency_p95: float


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


class RunEvalRequest(BaseModel):
    """发起评测请求"""
    project_id: str
    evalset_id: str


class TestTargetRequest(BaseModel):
    """测试目标 API 请求"""
    base_url: str
    api_key: str
    model: str
    request_template: str = "{input}"
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
    """测试 Judge 请求"""
    base_url: str
    api_key: str
    model: str
    prompt_template: str
    input: str
    output_requirement: str
    actual_output: str


class ErrorDetail(BaseModel):
    """错误详情"""
    code: str
    message: str


class ErrorResponse(BaseModel):
    """统一错误响应"""
    error: ErrorDetail
