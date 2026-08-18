"""评测执行器：跑一个评测集，产出结果 + 性能统计 + 成本对比"""

import time
import statistics

from .models import Project, EvalSet, CaseResult, EvalSummary, EvalRun
from .eval_types import run_rule_based
from .judge import call_target, judge_with_llm

JUDGE_THRESHOLD = 0.5  # llm_judge 分数超过该阈值判为通过


async def run_evalset(project: Project, evalset: EvalSet) -> EvalRun:
    results: list[CaseResult] = []

    for case in evalset.cases:
        # 1. 调用被评测 API
        prompt = project.target_config.request_template.format(input=case.input)
        start = time.perf_counter()
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
        if case.eval_type == "llm_judge":
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

        results.append(
            CaseResult(
                case_name=case.case_name,
                actual_output=actual,
                passed=passed,
                score=score,
                latency_ms=latency_ms,
                token_used=token,
            )
        )

    # 3. 汇总
    total = len(results)
    passed_count = sum(1 for r in results if r.passed)
    total_token = sum(r.token_used for r in results)
    total_latency = sum(r.latency_ms for r in results)
    latencies = sorted(r.latency_ms for r in results)

    pass_rate = passed_count / total if total else 0.0
    token_per_pass = (
        passed_count / (total_token / 10000) if total_token > 0 else 0.0
    )
    latency_p50 = statistics.median(latencies) if latencies else 0.0
    latency_p95 = latencies[int(len(latencies) * 0.95) - 1] if latencies else 0.0

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
