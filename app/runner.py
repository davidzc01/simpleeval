"""评测执行器：跑一个评测集，产出结果 + 性能统计 + 成本对比"""

import time

from .models import Project, EvalSet, CaseResult, EvalSummary, EvalRun
from .eval_types import run_rule_based
from .judge import call_target, judge_with_llm, APIError, NetworkError, ResponseFormatError

JUDGE_THRESHOLD = 0.5  # llm_judge 分数超过该阈值判为通过


def percentile(data: list[float], p: float) -> float:
    """计算分位数，使用线性插值"""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    n = len(sorted_data)
    k = (n - 1) * p / 100
    f = int(k)
    c = f + 1 if f + 1 < n else f
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])


async def check_judge_available(project: Project) -> tuple[bool, str]:
    """测试 Judge LLM 是否可用，返回 (可用, 错误信息)"""
    try:
        # 用一个简单的测试请求验证 Judge API 是否可用
        await judge_with_llm(
            base_url=project.judge_config.base_url,
            api_key=project.judge_config.api_key,
            model=project.judge_config.model,
            requirement="测试",
            output="测试",
        )
        return True, ""
    except NetworkError as e:
        return False, f"llm_unavailable: 网络错误 - {e.message}"
    except APIError as e:
        return False, f"llm_unavailable: API 错误 (status={e.status_code}) - {e.message}"
    except ResponseFormatError as e:
        # 格式错误但 API 可达，也算可用
        return True, ""


async def run_evalset(project: Project, evalset: EvalSet) -> EvalRun:
    # 0. 检查 Judge LLM 是否可用
    judge_available, judge_error = await check_judge_available(project)
    llm_case_skipped = False

    results: list[CaseResult] = []

    for case in evalset.cases:
        # 1. 调用被评测 API
        prompt = project.target_config.request_template.format(input=case.input)
        start = time.perf_counter()
        
        try:
            actual, token = await call_target(
                project.target_config.base_url,
                project.target_config.api_key,
                project.target_config.model,
                prompt,
            )
            latency_ms = (time.perf_counter() - start) * 1000

            # 2. 评测
            passed = False
            score = 0.0
            skipped_reason = None
            
            if case.eval_type == "llm_judge":
                if not judge_available:
                    # Judge 不可用，跳过该 case
                    skipped_reason = judge_error
                    llm_case_skipped = True
                    actual = f"[SKIPPED] {skipped_reason}"
                    token = 0
                else:
                    requirement = case.output_requirement or case.expected_output or ""
                    score = await judge_with_llm(
                        project.judge_config.base_url,
                        project.judge_config.api_key,
                        project.judge_config.model,
                        requirement,
                        actual,
                    )
                    passed = score >= JUDGE_THRESHOLD
            else:
                passed = run_rule_based(
                    case.eval_type, actual, case.expected_output, case.eval_params or {}
                )
                score = 1.0 if passed else 0.0

        except (APIError, NetworkError, ResponseFormatError) as e:
            # API 调用失败，该 case 标记为失败
            latency_ms = (time.perf_counter() - start) * 1000
            actual = f"[ERROR] {type(e).__name__}: {e.message}"
            passed = False
            score = 0.0
            token = 0
            skipped_reason = None

        results.append(
            CaseResult(
                case_name=case.case_name,
                actual_output=actual,
                passed=passed,
                score=score,
                latency_ms=latency_ms,
                token_used=token,
                skipped_reason=skipped_reason,
            )
        )

    # 3. 汇总
    total = len(results)
    passed_count = sum(1 for r in results if r.passed)
    skipped_count = sum(1 for r in results if r.skipped_reason is not None)
    total_token = sum(r.token_used for r in results)
    total_latency = sum(r.latency_ms for r in results)
    latencies = [r.latency_ms for r in results]

    # 只计算有效 case 的通过率（排除跳过的）
    valid_count = total - skipped_count
    pass_rate = passed_count / valid_count if valid_count > 0 else 0.0
    token_per_pass = (
        passed_count / (total_token / 10000) if total_token > 0 else 0.0
    )
    latency_p50 = percentile(latencies, 50)
    latency_p95 = percentile(latencies, 95)

    summary = EvalSummary(
        pass_rate=round(pass_rate, 4),
        total_token=total_token,
        total_latency_ms=round(total_latency, 2),
        token_per_pass=round(token_per_pass, 4),
        latency_p50=round(latency_p50, 2),
        latency_p95=round(latency_p95, 2),
    )

    return EvalRun(
        id=f"run-{int(time.time())}",
        project_id=project.id,
        evalset_id=evalset.id,
        created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        results=results,
        summary=summary,
    )
