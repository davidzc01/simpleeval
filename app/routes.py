"""API 路由定义"""

import csv
import io
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Form

from .models import (
    Project, EvalSet, EvalRun, EvalCase,
    CreateProjectRequest, CreateEvalSetRequest, RunEvalRequest,
    TestTargetRequest, TestMappingRequest, TestParsingRequest, TestJudgeRequest,
    CaseResult, ErrorResponse,
)
from .storage import (
    list_projects, get_project, save_project, delete_project,
    list_evalsets, get_evalset, save_evalset, delete_evalset,
    list_runs, get_run, save_run,
    get_project_last_run, get_project_trend,
)
from .runner import execute_run, _utc_now, _generate_run_id, _apply_case_filter
from .judge import call_target, judge_with_llm, NetworkError, APIError, ResponseFormatError
from .errors import (
    project_not_found, evalset_not_found, run_not_found,
    no_enabled_cases, import_format_error, mapping_invalid,
    target_api_error, judge_api_error, network_error,
)


router = APIRouter(prefix="/api")


# ============== 辅助函数 ==============

def _mask_secret(value: str) -> dict:
    """掩码敏感信息"""
    return {"masked": True}


def _project_to_response(project: Project) -> dict:
    """转换项目为 API 响应（掩码敏感字段）"""
    data = project.model_dump()
    # 掩码 judge_config 的 api_key
    data["judge_config"]["api_key"] = _mask_secret(project.judge_config.api_key)
    # 掩码 target_config 的 api_key
    data["target_config"]["api_key"] = _mask_secret(project.target_config.api_key)
    # 掩码 auth 中的敏感信息
    if data["target_config"].get("auth"):
        auth = data["target_config"]["auth"]
        if auth.get("bearer_token"):
            auth["bearer_token"] = _mask_secret(auth["bearer_token"])
        if auth.get("api_key_value"):
            auth["api_key_value"] = _mask_secret(auth["api_key_value"])
        if auth.get("cookies"):
            for cookie in auth["cookies"]:
                if cookie.get("value"):
                    cookie["value"] = _mask_secret(cookie["value"])
    return data


def _generate_id(prefix: str) -> str:
    """生成 ID"""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ============== Projects ==============

def _last_run_summary(last_run):
    """B-15: 提取 last_run 摘要（列表接口和详情接口共用）"""
    if not last_run:
        return None
    s = last_run.summary
    failed_count = sum(
        1 for r in (last_run.results or [])
        if not r.passed and not r.skipped_reason
    )
    # B-18: 全部 case 的 token 都不可得时标记，供前端显示「token 不可得」
    results = last_run.results or []
    token_missing = bool(results) and all(r.token_missing for r in results)
    return {
        "id": last_run.id,
        "status": last_run.status,
        "created_at": last_run.created_at,
        "pass_rate": s.pass_rate if s else 0,
        "total_token": s.total_token if s else 0,
        "token_per_pass": s.token_per_pass if s else 0,
        "latency_p50": s.latency_p50 if s else 0,
        "latency_p95": s.latency_p95 if s else 0,
        "failed_count": failed_count,
        "token_missing": token_missing,
        "judge_token": s.judge_token if s else 0,
    }


def _attach_run_summary(data: dict, project_id: str) -> dict:
    """B-15: 给项目响应附 last_run + trend（列表/详情共用）"""
    data["last_run"] = _last_run_summary(get_project_last_run(project_id))
    data["trend"] = get_project_trend(project_id, limit=8)
    return data


@router.get("/projects")
async def list_all_projects():
    """列出所有项目（含 last_run + trend）"""
    projects = list_projects()
    result = []
    for p in projects:
        data = _project_to_response(p)
        _attach_run_summary(data, p.id)
        result.append(data)

    return {"projects": result}


@router.post("/projects", status_code=201)
async def create_project(req: CreateProjectRequest):
    """新建项目"""
    project_id = _generate_id("proj")
    project = Project(
        id=project_id,
        name=req.name,
        task_shape=req.task_shape,
        judge_config={"base_url": "", "api_key": "", "model": ""},
        target_config={"base_url": "", "api_key": "", "model": None},
    )
    save_project(project)
    return _project_to_response(project)


@router.get("/projects/{project_id}")
async def get_project_detail(project_id: str):
    """获取项目详情（B-15: 附 last_run + trend，与列表接口对齐）"""
    project = get_project(project_id)
    if not project:
        project_not_found(project_id)
    data = _project_to_response(project)
    _attach_run_summary(data, project_id)
    return data


@router.put("/projects/{project_id}")
async def update_project(project_id: str, project: Project):
    """全量更新项目配置（支持 __UNCHANGED__ 哨兵值保留原 secret）"""
    existing = get_project(project_id)
    if not existing:
        project_not_found(project_id)

    project.id = project_id

    # 哨兵值处理：提交 "__UNCHANGED__" 表示保留原值，避免掩码字符串覆盖真实 secret
    if project.judge_config.api_key == "__UNCHANGED__":
        project.judge_config.api_key = existing.judge_config.api_key
    if project.target_config.api_key == "__UNCHANGED__":
        project.target_config.api_key = existing.target_config.api_key

    auth = project.target_config.auth
    existing_auth = existing.target_config.auth
    if auth and existing_auth:
        if auth.bearer_token == "__UNCHANGED__":
            auth.bearer_token = existing_auth.bearer_token
        if auth.api_key_value == "__UNCHANGED__":
            auth.api_key_value = existing_auth.api_key_value
        for i, cookie in enumerate(auth.cookies):
            if cookie.get("value") == "__UNCHANGED__" and i < len(existing_auth.cookies):
                cookie["value"] = existing_auth.cookies[i].get("value")

    # A-4: api_type 校验（保存时阻断，422）
    tc = project.target_config
    if tc.api_type == "openai_compatible" and not tc.model:
        raise HTTPException(status_code=422, detail={
            "error": {"code": "invalid_config",
                      "message": "openai_compatible 模式下 model 必填"}
        })
    if tc.api_type == "custom" and not tc.request_template.strip():
        raise HTTPException(status_code=422, detail={
            "error": {"code": "invalid_config",
                      "message": "custom 模式下 request_template 必填"}
        })

    # T1-3: judge_config 对称校验——与 target_config 同口径
    jc = project.judge_config
    if jc.api_type == "openai_compatible" and not (jc.model and jc.model.strip()):
        raise HTTPException(status_code=422, detail={
            "error": {"code": "invalid_config",
                      "message": "openai_compatible 模式下 judge model 必填"}
        })
    if jc.api_type == "custom":
        if not (jc.request_template and jc.request_template.strip()):
            raise HTTPException(status_code=422, detail={
                "error": {"code": "invalid_config",
                          "message": "custom 模式下 judge request_template 必填"}
            })
        if jc.response_parsing is None:
            raise HTTPException(status_code=422, detail={
                "error": {"code": "invalid_config",
                          "message": "custom 模式下 judge response_parsing 必填（用于从自定义 API 响应提取分数与 token）"}
            })

    save_project(project)
    return _project_to_response(project)


@router.delete("/projects/{project_id}")
async def remove_project(project_id: str):
    """T1-1 / B-23: 删除项目（连带删评测集与全部 runs）

    前端用输入名称二次确认，后端只做物理删除。
    """
    project = get_project(project_id)
    if not project:
        project_not_found(project_id)
    delete_project(project_id)
    return {"deleted": project_id, "project_name": project.name if project else None}


@router.get("/projects/{project_id}/evalsets")
async def list_project_evalsets(project_id: str):
    """列出项目下所有评测集（列表项不含 cases 明细以外的字段）"""
    project = get_project(project_id)
    if not project:
        project_not_found(project_id)
    evalsets = list_evalsets(project_id)
    # 列表项精简：包含 id/name/project_id/cases（前端表格需要 cases）
    return {"evalsets": [e.model_dump() for e in evalsets]}


# ============== EvalSets ==============

@router.post("/evalsets", status_code=201)
async def create_evalset(req: CreateEvalSetRequest):
    """新建评测集"""
    # 验证项目存在
    project = get_project(req.project_id)
    if not project:
        project_not_found(req.project_id)

    evalset_id = _generate_id("evalset")
    # 为没有 id 的 case 生成 id
    cases = []
    for i, case in enumerate(req.cases):
        if not case.id:
            case.id = _generate_id("case")
        cases.append(case)

    evalset = EvalSet(
        id=evalset_id,
        project_id=req.project_id,
        name=req.name,
        cases=cases,
    )
    save_evalset(evalset)
    return evalset.model_dump()


@router.get("/evalsets/{evalset_id}")
async def get_evalset_detail(evalset_id: str, project_id: str):
    """获取评测集详情"""
    evalset = get_evalset(evalset_id, project_id)
    if not evalset:
        evalset_not_found(evalset_id)
    return evalset.model_dump()


@router.put("/evalsets/{evalset_id}")
async def update_evalset(evalset_id: str, evalset: EvalSet):
    """全量替换评测集"""
    existing = get_evalset(evalset_id, evalset.project_id)
    if not existing:
        evalset_not_found(evalset_id)

    # 保持 ID 不变，为没有 id 的 case 生成 id
    evalset.id = evalset_id
    for i, case in enumerate(evalset.cases):
        if not case.id:
            case.id = _generate_id("case")
    save_evalset(evalset)
    return evalset.model_dump()


@router.post("/evalsets/{evalset_id}/import")
async def import_evalset(
    evalset_id: str,
    project_id: str,
    file_content: str = Form(...),
    mode: str = "merge",
):
    """导入 CSV/JSON 评测集（merge | replace）+ 行级错误收集"""
    evalset = get_evalset(evalset_id, project_id)
    if not evalset:
        evalset_not_found(evalset_id)

    if mode not in ("merge", "replace"):
        import_format_error(f"不支持的 mode: {mode}（仅支持 merge / replace）")

    # 解析文件内容（支持 CSV 和 JSON 数组）
    try:
        if file_content.strip().startswith("["):
            new_cases_data = json.loads(file_content)
        else:
            reader = csv.DictReader(io.StringIO(file_content))
            new_cases_data = list(reader)
    except Exception as e:
        import_format_error(f"文件解析失败: {e}")

    # 行级收集：成功 case + 错误列表
    new_cases = []
    errors = []
    for i, row in enumerate(new_cases_data):
        try:
            case_id = row.get("id") or _generate_id("case")
            # eval_params 支持对象或 JSON 字符串
            ep = row.get("eval_params")
            if isinstance(ep, str):
                ep = json.loads(ep) if ep and ep != "{}" else {}
            elif ep is None:
                ep = {}
            # enabled 字段容忍字符串/布尔
            enabled_raw = row.get("enabled", "true")
            if isinstance(enabled_raw, str):
                enabled = enabled_raw.lower() == "true"
            else:
                enabled = bool(enabled_raw)
            # task_shape 可选
            task_shape = row.get("task_shape") or None
            # T1-2: tags 可选（CSV JSON 字符串数组 / JSON 数组 / 逗号或分号分隔字符串）
            tags = []
            t_raw = row.get("tags")
            if t_raw is not None and t_raw != "":
                if isinstance(t_raw, str):
                    stripped = t_raw.strip()
                    if stripped.startswith("["):
                        try:
                            tags = json.loads(stripped)
                        except json.JSONDecodeError:
                            tags = [t.strip() for t in stripped.replace(";", ",").split(",") if t.strip()]
                    else:
                        # 逗号或分号分隔
                        tags = [t.strip() for t in stripped.replace(";", ",").split(",") if t.strip()]
                elif isinstance(t_raw, list):
                    tags = t_raw
            # variables 可选（B-13）：对象或 JSON 字符串两种形态
            variables = None
            v_raw = row.get("variables")
            if v_raw is not None and v_raw != "":
                if isinstance(v_raw, str):
                    try:
                        variables = json.loads(v_raw)
                    except json.JSONDecodeError:
                        errors.append({"row": i + 1, "error": f"variables 不是合法 JSON: {v_raw[:50]}"})
                        continue
                elif isinstance(v_raw, dict):
                    variables = v_raw
            kwargs = dict(
                id=case_id,
                case_name=row.get("case_name") or f"case-{i}",
                input=row.get("input", ""),
                expected_output=row.get("expected_output") or None,
                output_requirement=row.get("output_requirement") or None,
                eval_type=row.get("eval_type", "exact"),
                eval_params=ep,
                enabled=enabled,
                tags=tags,
            )
            if task_shape:
                kwargs["task_shape"] = task_shape
            if variables is not None:
                kwargs["variables"] = variables
            new_cases.append(EvalCase(**kwargs))
        except Exception as e:
            errors.append({"row": i + 1, "error": str(e)})

    # 有行级错误时不保存，返回 422 + errors
    if errors:
        raise HTTPException(status_code=422, detail={
            "error": "import_validation_failed",
            "imported": 0,
            "total": len(new_cases_data),
            "errors": errors,
        })

    if mode == "replace":
        evalset.cases = new_cases
    else:  # merge：按 id 去重
        existing_ids = {c.id for c in evalset.cases}
        for case in new_cases:
            if case.id not in existing_ids:
                evalset.cases.append(case)

    save_evalset(evalset)
    return {
        "imported": len(new_cases),
        "total": len(new_cases_data),
        "mode": mode,
        "evalset": evalset.model_dump(),
    }


@router.get("/evalsets/{evalset_id}/export")
async def export_evalset(evalset_id: str, project_id: str):
    """导出评测集为 CSV"""
    evalset = get_evalset(evalset_id, project_id)
    if not evalset:
        evalset_not_found(evalset_id)

    # 生成 CSV
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "case_name", "input", "expected_output", "output_requirement", "eval_type", "eval_params", "enabled", "tags"])

    for case in evalset.cases:
        writer.writerow([
            case.id,
            case.case_name,
            case.input,
            case.expected_output or "",
            case.output_requirement or "",
            case.eval_type,
            json.dumps(case.eval_params) if case.eval_params else "{}",
            "true" if case.enabled else "false",
            json.dumps(case.tags, ensure_ascii=False) if case.tags else "[]",
        ])

    # 添加 UTF-8 BOM 以支持 Excel
    content = "\ufeff" + output.getvalue()
    return {"content": content, "filename": f"{evalset.name}.csv"}


# ============== Runs ==============

@router.post("/runs", status_code=201)
async def create_run(req: RunEvalRequest, background_tasks: BackgroundTasks):
    """发起评测（异步）"""
    # 验证项目存在
    project = get_project(req.project_id)
    if not project:
        project_not_found(req.project_id)

    # 验证评测集存在
    evalset = get_evalset(req.evalset_id, req.project_id)
    if not evalset:
        evalset_not_found(req.evalset_id)

    # 检查是否有启用的 case（T1-2: 按 case_filter 筛选后判断）
    enabled_cases = [c for c in evalset.cases if c.enabled]
    filtered_cases = _apply_case_filter(enabled_cases, req.case_filter)
    if not filtered_cases:
        no_enabled_cases()

    # 创建 run 记录
    run_id = _generate_run_id()
    run = EvalRun(
        id=run_id,
        project_id=req.project_id,
        evalset_id=req.evalset_id,
        status="queued",
        created_at=_utc_now(),
    )
    save_run(run)

    # 后台执行（T1-2: 传 case_filter）
    background_tasks.add_task(execute_run, run, project, evalset, req.case_filter)

    return {"run_id": run_id, "status": "queued"}


@router.get("/runs/{run_id}")
async def get_run_detail(run_id: str, project_id: str):
    """获取 run 详情"""
    run = get_run(run_id, project_id)
    if not run:
        run_not_found(run_id)
    return run.model_dump()


@router.get("/projects/{project_id}/runs")
async def list_project_runs(project_id: str):
    """列出项目的历史 runs"""
    project = get_project(project_id)
    if not project:
        project_not_found(project_id)

    runs = list_runs(project_id)

    # 只返回摘要（不含 results）
    result = []
    for r in runs:
        result.append({
            "id": r.id,
            "evalset_id": r.evalset_id,
            "status": r.status,
            "created_at": r.created_at,
            "summary": r.summary.model_dump() if r.summary else None,
        })

    return {"runs": result}


@router.get("/projects/{project_id}/sampling")
async def get_project_sampling(project_id: str):
    """项目采样稳定性（pass@k / pass^k，k=1,2,3）"""
    project = get_project(project_id)
    if not project:
        project_not_found(project_id)
    # 延迟导入避免循环依赖（sampling 依赖 storage，storage 不依赖 routes）
    from .sampling import compute_project_sampling
    return compute_project_sampling(project_id)


@router.get("/runs/{run_id}/export")
async def export_run(run_id: str, project_id: str):
    """导出 run 结果为 CSV"""
    run = get_run(run_id, project_id)
    if not run:
        run_not_found(run_id)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["case_name", "input", "expected_output", "actual_output", "passed", "score", "latency_ms", "token_used", "judge_token", "check_results", "skipped_reason"])

    # 获取评测集获取 input 和 expected_output
    evalset = get_evalset(run.evalset_id, project_id)
    case_map = {c.case_name: c for c in evalset.cases} if evalset else {}

    for result in run.results:
        case = case_map.get(result.case_name, {})
        writer.writerow([
            result.case_name,
            case.input if hasattr(case, 'input') else "",
            case.expected_output if hasattr(case, 'expected_output') else "",
            result.actual_output,
            "true" if result.passed else "false",
            result.score,
            result.latency_ms,
            result.token_used,
            result.judge_token,
            json.dumps(result.check_results, ensure_ascii=False) if result.check_results else "[]",
            result.skipped_reason or "",
        ])

    content = "\ufeff" + output.getvalue()
    return {"content": content, "filename": f"run-{run_id}.csv"}


# ============== Test 端点 ==============

@router.post("/test/target")
async def test_target(req: TestTargetRequest, project_id: Optional[str] = None):
    """测试目标 API（支持 __UNCHANGED__ 哨兵值，需带 project_id 查询参数）"""
    try:
        import time
        api_key = req.api_key
        auth = req.auth
        if project_id:
            saved = get_project(project_id)
            if saved:
                if api_key == "__UNCHANGED__":
                    api_key = saved.target_config.api_key
                if auth and saved.target_config.auth:
                    if auth.bearer_token == "__UNCHANGED__":
                        auth.bearer_token = saved.target_config.auth.bearer_token
                    if auth.api_key_value == "__UNCHANGED__":
                        auth.api_key_value = saved.target_config.auth.api_key_value
        start = time.perf_counter()
        output, token, _missing = await call_target(
            base_url=req.base_url,
            api_key=api_key,
            model=req.model or "",
            prompt="ping",
            request_template=req.request_template,
            auth=auth,
            response_mapping=req.response_mapping,
            response_parsing=req.response_parsing,
            api_type=req.api_type,
            # 测试连接宽松模式：模板里的自定义变量用占位值填充（连通性测试不依赖真实变量值）
            default_missing="test",
        )
        latency_ms = (time.perf_counter() - start) * 1000
        return {
            "ok": True,
            "latency_ms": round(latency_ms, 2),
            "token_used": token,
            "status_code": 200,
            "output": output[:200],  # 截断显示
        }
    except NetworkError as e:
        return {"ok": False, "error": {"code": "network_error", "message": e.message}}
    except APIError as e:
        return {"ok": False, "error": {"code": "target_api_error", "message": e.message}}
    except ResponseFormatError as e:
        return {"ok": False, "error": {"code": "mapping_invalid", "message": e.message}}


@router.post("/test/mapping")
async def test_mapping(req: TestMappingRequest):
    """测试响应映射提取（旧设计，兼容保留）"""
    try:
        from .judge import _extract_response
        result = _extract_response(req.sample_response, req.response_mapping)
        return {"ok": True, "result": result}
    except Exception as e:
        mapping_invalid(f"映射提取失败: {e}")


@router.post("/test/parsing")
async def test_parsing(req: TestParsingRequest):
    """测试响应解析（四键模型：output_paths / token_paths / token_fields / token_scope）

    返回输出提取结果、token 计数与缺失标记，供配置页「测试映射」面板使用。
    """
    from .parser import parse_response
    try:
        result = parse_response(req.sample_response, req.response_parsing)
        return {
            "ok": True,
            "output": result["output"],
            "token_used": result["token_used"],
            "token_missing": result["token_missing"],
            "output_found": result["output_found"],
        }
    except Exception as e:
        mapping_invalid(f"解析失败: {e}")


@router.post("/test/judge")
async def test_judge(req: TestJudgeRequest, project_id: Optional[str] = None):
    """测试 Judge（支持 __UNCHANGED__ 哨兵值 + T1-3 双模式）"""
    try:
        api_key = req.api_key
        if project_id and api_key == "__UNCHANGED__":
            saved = get_project(project_id)
            if saved:
                api_key = saved.judge_config.api_key
        score, token_used = await judge_with_llm(
            base_url=req.base_url,
            api_key=api_key,
            model=req.model or "",
            requirement=req.output_requirement,
            output=req.actual_output,
            judge_prompt=req.prompt_template,
            api_type=req.api_type,
            request_template=req.request_template,
            auth=req.auth,
            response_parsing=req.response_parsing,
        )
        return {
            "ok": True,
            "score": score,
            "passed": score >= 0.5,
            "token_used": token_used,
        }
    except NetworkError as e:
        return {"ok": False, "error": {"code": "network_error", "message": e.message}}
    except APIError as e:
        return {"ok": False, "error": {"code": "judge_api_error", "message": e.message}}
    except ResponseFormatError as e:
        return {"ok": False, "error": {"code": "mapping_invalid", "message": e.message}}


# ============== Health ==============

@router.get("/health")
async def health():
    """健康检查"""
    return {"status": "ok"}
