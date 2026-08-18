"""simpleEval 数据模型（pydantic）"""

from typing import Literal, Optional
from pydantic import BaseModel, Field


class JudgeConfig(BaseModel):
    """LLM-as-Judge 用的模型配置（OpenAI Compatible）"""
    base_url: str
    api_key: str
    model: str


class TargetConfig(BaseModel):
    """被评测 API 的请求配置（OpenAI Compatible）"""
    base_url: str
    api_key: str
    model: str
    request_template: str = "{input}"  # 默认直接把 case.input 塞进 prompt


class TokenBudget(BaseModel):
    """token 预算（MVP 只提醒，不中断）"""
    limit: int
    warn_only: bool = True


class Project(BaseModel):
    id: str
    name: str
    task_shape: Literal["coding", "customer_service", "multi_turn", "general", "custom"] = "general"
    judge_config: JudgeConfig
    target_config: TargetConfig
    token_budget: Optional[TokenBudget] = None


class EvalCase(BaseModel):
    """评测集里的单条 case"""
    case_name: str
    input: str
    expected_output: Optional[str] = None       # exact / contains 用
    output_requirement: Optional[str] = None    # llm_judge 用的评判标准
    eval_type: Literal["exact", "contains", "not_contains", "length", "llm_judge"]
    eval_params: Optional[dict] = Field(default_factory=dict)  # 如 contains 的 substring、length 的 min/max
    task_shape: Optional[str] = None            # 覆盖项目默认值


class EvalSet(BaseModel):
    id: str
    project_id: str
    name: str
    cases: list[EvalCase]


class CaseResult(BaseModel):
    """单条 case 的评测结果"""
    case_name: str
    actual_output: str
    passed: bool
    score: float = 0.0
    latency_ms: float = 0.0
    token_used: int = 0


class EvalSummary(BaseModel):
    """一次 run 的汇总 + 成本对比"""
    pass_rate: float
    total_token: int
    total_latency_ms: float
    token_per_pass: float          # 每万 token 完成率 = 通过数 / (总token/10000)
    latency_p50: float
    latency_p95: float


class EvalRun(BaseModel):
    id: str
    project_id: str
    evalset_id: str
    created_at: str
    results: list[CaseResult]
    summary: EvalSummary
