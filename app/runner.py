"""评测执行器：跑一个评测集，产出结果 + 性能统计 + 成本对比"""

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from .models import Project, EvalSet, CaseResult, EvalSummary, EvalRun, CaseFilter
from .eval_types import run_rule_based
from .judge import call_target, judge_with_llm, APIError, NetworkError, ResponseFormatError
from .storage import save_run

JUDGE_THRESHOLD = 0.5  # llm_judge 分数超过该阈值判为通过


def _token_missing_on_error(project: Project) -> bool:
    """call_target 异常时的 token_missing 标记。

    - 未配置 response_parsing：按 OpenAI 默认，不标记缺失
    - 配置了 response_parsing：call 失败 → 无法获取 token → missing
    """
    return project.target_config.response_parsing is not None


def _utc_now() -> str:
    """获取当前 UTC 时间（ISO 8601 格式）"""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _generate_run_id() -> str:
    """生成唯一的 run id（UUID + 时间戳前缀）"""
    return f"run-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"


def _apply_case_filter(cases: list, case_filter: Optional[CaseFilter]) -> list:
    """T1-2: 按 tags 筛选 case

    - case_filter 为 None 或 tags 为空 → 不筛选，返回原列表
    - mode="any" → case 含任一标签即入选（OR）
    - mode="all" → case 须含全部标签才入选（AND）
    """
    if not case_filter or not case_filter.tags:
        return cases
    # EvalCase.tags 是 list[str] = Field(default_factory=list)，Pydantic 保证永不为 None
    if case_filter.mode == "all":
        return [c for c in cases if all(t in c.tags for t in case_filter.tags)]
    # any
    return [c for c in cases if any(t in c.tags for t in case_filter.tags)]


def _extract_check_field(actual_output: str, field: str) -> str:
    """T1-5: 从 actual_output 中提取 check.field 指定的字段值。

    - field 为空 → 返回 actual_output 原文
    - 尝试 json.loads(actual_output)，成功则按点路径取值
    - 取到的标量统一 stringify（bool → "true"/"false"）
    - json.loads 失败或路径未命中 → 返回 actual_output 原文兜底
    """
    if not field:
        return actual_output
    try:
        obj = json.loads(actual_output)
    except (json.JSONDecodeError, TypeError):
        return actual_output
    cur = obj
    for seg in field.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        elif isinstance(cur, list) and seg.isdigit():
            idx = int(seg)
            if 0 <= idx < len(cur):
                cur = cur[idx]
            else:
                return actual_output
        else:
            return actual_output
    if isinstance(cur, bool):
        return "true" if cur else "false"
    if isinstance(cur, (dict, list)):
        return json.dumps(cur, ensure_ascii=False)
    return str(cur)


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
    """测试 Judge LLM 是否可用，返回 (可用, 错误信息)

    T1-3: 支持双模式——custom 模式用 project.judge_config 的 request_template/auth/response_parsing
    """
    try:
        jc = project.judge_config
        await judge_with_llm(
            base_url=jc.base_url,
            api_key=jc.api_key,
            model=jc.model or "",
            requirement="测试",
            output="测试",
            judge_prompt=jc.prompt_template,
            api_type=jc.api_type,
            request_template=jc.request_template,
            auth=jc.auth,
            response_parsing=jc.response_parsing,
        )
        return True, ""
    except NetworkError as e:
        return False, f"llm_unavailable: 网络错误 - {e.message}"
    except APIError as e:
        return False, f"llm_unavailable: API 错误 (status={e.status_code}) - {e.message}"
    except ResponseFormatError as e:
        # 格式错误但 API 可达，也算可用
        return True, ""


async def _evaluate_case(
    project: Project,
    case,
    actual: str,
    token: int,
    token_missing: bool,
    judge_available: bool,
    judge_error: str,
) -> tuple[bool, float, Optional[str], int, list[dict], str]:
    """评测单条 case，返回 (passed, score, skipped_reason, judge_token, check_results, actual_output)

    T1-4: 收集 judge_token（llm_judge case 才有）
    T1-5: 运行 checks（多字段验证），case 通过 = 主验证通过 AND 所有 checks 通过
    actual_output 会被修正（如 judge 不可用时标 [SKIPPED]）
    """
    passed = False
    score = 0.0
    skipped_reason: Optional[str] = None
    judge_token = 0
    check_results: list[dict] = []

    if case.eval_type == "llm_judge":
        if not judge_available:
            skipped_reason = judge_error
            actual = f"[SKIPPED] {skipped_reason}"
            token = 0
        else:
            requirement = case.output_requirement or case.expected_output or ""
            jc = project.judge_config
            score, judge_token = await judge_with_llm(
                base_url=jc.base_url,
                api_key=jc.api_key,
                model=jc.model or "",
                requirement=requirement,
                output=actual,
                judge_prompt=jc.prompt_template,
                api_type=jc.api_type,
                request_template=jc.request_template,
                auth=jc.auth,
                response_parsing=jc.response_parsing,
            )
            passed = score >= JUDGE_THRESHOLD
    else:
        passed = run_rule_based(
            case.eval_type, actual, case.expected_output, case.eval_params or {}
        )
        score = 1.0 if passed else 0.0

    # T1-5: 多字段验证（checks）
    if case.checks and not skipped_reason:
        all_checks_passed = True
        for chk in case.checks:
            chk_value = _extract_check_field(actual, chk.field)
            if chk.eval_type == "llm_judge":
                if not judge_available:
                    chk_passed = False
                    chk_score = 0.0
                else:
                    chk_requirement = chk.expected or ""
                    jc = project.judge_config
                    chk_score, chk_judge_token = await judge_with_llm(
                        base_url=jc.base_url,
                        api_key=jc.api_key,
                        model=jc.model or "",
                        requirement=chk_requirement,
                        output=chk_value,
                        judge_prompt=jc.prompt_template,
                        api_type=jc.api_type,
                        request_template=jc.request_template,
                        auth=jc.auth,
                        response_parsing=jc.response_parsing,
                    )
                    chk_passed = chk_score >= JUDGE_THRESHOLD
                    judge_token += chk_judge_token
            else:
                chk_passed = run_rule_based(
                    chk.eval_type, chk_value, chk.expected, chk.eval_params or {}
                )
                chk_score = 1.0 if chk_passed else 0.0
            check_results.append({"name": chk.name, "passed": chk_passed, "score": chk_score})
            if not chk_passed:
                all_checks_passed = False
        passed = passed and all_checks_passed

    return passed, score, skipped_reason, judge_token, check_results, actual


async def run_evalset(project: Project, evalset: EvalSet) -> EvalRun:
    """同步执行评测（保留用于直接调用）"""
    # 过滤启用的 case
    enabled_cases = [c for c in evalset.cases if c.enabled]
    if not enabled_cases:
        raise ValueError("评测集没有启用的 case")

    # 0. 检查 Judge LLM 是否可用
    judge_available, judge_error = await check_judge_available(project)

    results: list[CaseResult] = []

    for case in enabled_cases:
        # 1. 调用被评测 API（模板渲染在 call_target 内统一处理，防 JSON 花括号/换行问题）
        prompt = case.input
        start = time.perf_counter()

        try:
            actual, token, token_missing = await call_target(
                base_url=project.target_config.base_url,
                api_key=project.target_config.api_key,
                model=project.target_config.model or "",
                prompt=prompt,
                request_template=project.target_config.request_template,
                auth=project.target_config.auth,
                response_mapping=project.target_config.response_mapping,
                response_parsing=project.target_config.response_parsing,
                api_type=project.target_config.api_type,
                variables=case.variables,
                case_name=case.case_name,
                task_shape=case.task_shape or project.task_shape,
            )
            latency_ms = (time.perf_counter() - start) * 1000

            passed, score, skipped_reason, judge_token, check_results, actual = await _evaluate_case(
                project, case, actual, token, token_missing,
                judge_available, judge_error,
            )

        except (APIError, NetworkError, ResponseFormatError) as e:
            latency_ms = (time.perf_counter() - start) * 1000
            actual = f"[ERROR] {type(e).__name__}: {e.message}"
            passed = False
            score = 0.0
            token = 0
            skipped_reason = None
            token_missing = _token_missing_on_error(project)
            judge_token = 0
            check_results = []

        results.append(
            CaseResult(
                case_name=case.case_name,
                actual_output=actual,
                passed=passed,
                score=score,
                latency_ms=latency_ms,
                token_used=token,
                skipped_reason=skipped_reason,
                token_missing=token_missing,
                judge_token=judge_token,
                check_results=check_results,
            )
        )

    # 3. 汇总
    return _build_run_result(
        run_id=_generate_run_id(),
        project_id=project.id,
        evalset_id=evalset.id,
        results=results,
    )


def _build_run_result(
    run_id: str,
    project_id: str,
    evalset_id: str,
    results: list[CaseResult],
    status: str = "completed",
    error: str = None,
) -> EvalRun:
    """构建评测结果"""
    total = len(results)
    passed_count = sum(1 for r in results if r.passed)
    skipped_count = sum(1 for r in results if r.skipped_reason is not None)
    total_token = sum(r.token_used for r in results)
    # T1-4: 评测成本 = 被评测消耗 + 评测自身消耗（judge token）
    judge_token_total = sum(r.judge_token for r in results)
    total_latency = sum(r.latency_ms for r in results)
    latencies = [r.latency_ms for r in results]

    valid_count = total - skipped_count
    pass_rate = passed_count / valid_count if valid_count > 0 else 0.0
    # T1-4: token_per_pass = 通过数 / ((target_token + judge_token)/10000)
    cost_token = total_token + judge_token_total
    token_per_pass = passed_count / (cost_token / 10000) if cost_token > 0 else 0.0
    latency_p50 = percentile(latencies, 50)
    latency_p95 = percentile(latencies, 95)

    summary = EvalSummary(
        pass_rate=round(pass_rate, 4),
        total_token=total_token,
        total_latency_ms=round(total_latency, 2),
        token_per_pass=round(token_per_pass, 4),
        latency_p50=round(latency_p50, 2),
        latency_p95=round(latency_p95, 2),
        judge_token=judge_token_total,
    )

    return EvalRun(
        id=run_id,
        project_id=project_id,
        evalset_id=evalset_id,
        status=status,
        created_at=_utc_now(),
        finished_at=_utc_now(),
        error=error,
        results=results,
        summary=summary,
    )


async def execute_run(run: EvalRun, project: Project, evalset: EvalSet, case_filter: Optional[CaseFilter] = None) -> EvalRun:
    """异步执行评测（供 BackgroundTasks 调用）

    T1-2: case_filter 按标签筛选 case（None/空 = 全部启用 case）
    """
    # 过滤启用的 case，再按 case_filter 筛选
    enabled_cases = [c for c in evalset.cases if c.enabled]
    enabled_cases = _apply_case_filter(enabled_cases, case_filter)

    # 更新状态为 running
    run.status = "running"
    run.started_at = _utc_now()
    save_run(run)

    try:
        # 检查 Judge 可用性
        judge_available, judge_error = await check_judge_available(project)

        results: list[CaseResult] = []

        for case in enabled_cases:
            # 模板渲染在 call_target 内统一处理（run_evalset 同构）
            prompt = case.input
            start = time.perf_counter()

            try:
                actual, token, token_missing = await call_target(
                    base_url=project.target_config.base_url,
                    api_key=project.target_config.api_key,
                    model=project.target_config.model or "",
                    prompt=prompt,
                    request_template=project.target_config.request_template,
                    auth=project.target_config.auth,
                    response_mapping=project.target_config.response_mapping,
                    response_parsing=project.target_config.response_parsing,
                    api_type=project.target_config.api_type,
                    variables=case.variables,
                    case_name=case.case_name,
                    task_shape=case.task_shape or project.task_shape,
                )
                latency_ms = (time.perf_counter() - start) * 1000

                passed, score, skipped_reason, judge_token, check_results, actual = await _evaluate_case(
                    project, case, actual, token, token_missing,
                    judge_available, judge_error,
                )

            except (APIError, NetworkError, ResponseFormatError) as e:
                latency_ms = (time.perf_counter() - start) * 1000
                actual = f"[ERROR] {type(e).__name__}: {e.message}"
                passed = False
                score = 0.0
                token = 0
                skipped_reason = None
                token_missing = _token_missing_on_error(project)
                judge_token = 0
                check_results = []

            results.append(
                CaseResult(
                    case_name=case.case_name,
                    actual_output=actual,
                    passed=passed,
                    score=score,
                    latency_ms=latency_ms,
                    token_used=token,
                    skipped_reason=skipped_reason,
                    token_missing=token_missing,
                    judge_token=judge_token,
                    check_results=check_results,
                )
            )
            # B-20: 每 case 落盘，前端轮询能看到 results 增长（status 保持 running）
            run.results = list(results)
            save_run(run)

        # 构建结果并保存
        completed_run = _build_run_result(
            run_id=run.id,
            project_id=run.project_id,
            evalset_id=run.evalset_id,
            results=results,
        )
        save_run(completed_run)
        return completed_run

    except Exception as e:
        # Run 级异常
        run.status = "failed"
        run.error = str(e)
        run.finished_at = _utc_now()
        save_run(run)
        raise
