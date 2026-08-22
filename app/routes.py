"""API 路由定义"""

import csv
import io
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Form

from .models import (
    Project, EvalSet, EvalRun, EvalCase, JudgeConfig, EvalCheck,
    CreateProjectRequest, CreateEvalSetRequest, RunEvalRequest,
    TestTargetRequest, TestMappingRequest, TestParsingRequest, TestJudgeRequest,
    CaseResult, ErrorResponse, CreateVersionRequest, ProjectVersion,
)
from .storage import (
    list_projects, get_project, save_project, delete_project,
    list_evalsets, get_evalset, save_evalset, delete_evalset,
    list_runs, get_run, save_run,
    get_project_last_run, get_project_trend,
    list_config_templates, get_config_template, save_config_template, delete_config_template,
    list_judge_configs, get_judge_config, get_judge_config_with_secrets, save_judge_config,
    update_judge_config, delete_judge_config, find_judge_config_by_name,
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


def _resolve_version_id(project: Project, run_created_at: str, explicit: Optional[str] = None) -> Optional[str]:
    """T3-3: 解析 run 的归属版本 id。

    - explicit 非空：校验是否在 project.versions 中，存在则用，不存在返回 None（不阻断，兼容旧数据）
    - explicit 为空：按 run_created_at 落入最近版本（version.created_at ≤ run_created_at 的最大者）
    - 项目无版本：返回 None（向后兼容，旧 run 无 version_id）
    """
    versions = project.versions or []
    if not versions:
        return None
    if explicit:
        if any(v.id == explicit for v in versions):
            return explicit
        return None
    # 按 created_at 降序找第一个 <= run_created_at 的版本
    sorted_vers = sorted(versions, key=lambda v: v.created_at, reverse=True)
    for v in sorted_vers:
        if v.created_at <= run_created_at:
            return v.id
    # run 创建时间早于所有版本 → 归入最早的版本
    return sorted_vers[-1].id if sorted_vers else None


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


def _attach_evalset_id(data: dict, project_id: str, project_name: str) -> dict:
    """REQ-10: 给项目响应附 evalset_id（取该项目的第一个评测集；无则补建空集）"""
    evalsets = list_evalsets(project_id)
    if not evalsets:
        evalset_id = _generate_id("evalset")
        evalset = EvalSet(
            id=evalset_id,
            project_id=project_id,
            name=f"{project_name}-评测集",
            cases=[],
        )
        save_evalset(evalset)
        data["evalset_id"] = evalset_id
    else:
        data["evalset_id"] = evalsets[0].id
    return data


@router.get("/projects")
async def list_all_projects():
    """列出所有项目（含 last_run + trend）"""
    projects = list_projects()
    result = []
    for p in projects:
        data = _project_to_response(p)
        _attach_run_summary(data, p.id)
        _attach_evalset_id(data, p.id, p.name)
        result.append(data)

    return {"projects": result}


@router.post("/projects", status_code=201)
async def create_project(req: CreateProjectRequest):
    """新建项目

    REQ-10: 一并创建一个空评测集（name = 项目名 + "-评测集"），
    UI 删除「新建评测集」入口；旧项目首次进入评测集 tab 时由前端按需补建。
    """
    project_id = _generate_id("proj")
    project = Project(
        id=project_id,
        name=req.name,
        task_shape=req.task_shape,
        judge_config={"base_url": "", "api_key": "", "model": ""},
        target_config={"base_url": "", "api_key": "", "model": None},
    )
    save_project(project)

    # REQ-10: 顺带创建空评测集
    evalset_id = _generate_id("evalset")
    evalset = EvalSet(
        id=evalset_id,
        project_id=project_id,
        name=f"{req.name}-评测集",
        cases=[],
    )
    save_evalset(evalset)

    # 返回体附 evalset_id，方便前端直接进入评测集 tab
    data = _project_to_response(project)
    data["evalset_id"] = evalset_id
    return data


@router.get("/projects/{project_id}")
async def get_project_detail(project_id: str):
    """获取项目详情（B-15: 附 last_run + trend，与列表接口对齐）

    REQ-10: 附 evalset_id（取该项目的第一个评测集；若旧项目无评测集，自动补建空集）
    """
    project = get_project(project_id)
    if not project:
        project_not_found(project_id)
    data = _project_to_response(project)
    _attach_run_summary(data, project_id)
    _attach_evalset_id(data, project_id, project.name)
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

    # REQ-16: use_target_config 字段已废弃（保留兼容：旧前端可能仍传，忽略）
    # judge_config_id 由 Project 模型接收，PUT 时不在这里二次校验——
    # 引用不存在的 id 时 runner 会 fallback 到内联 judge_config

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


# ============== T3-3: 版本管理 ==============

@router.post("/projects/{project_id}/versions", status_code=201)
async def create_version(project_id: str, req: CreateVersionRequest):
    """开新版本（时间锚点，后续 run 自动归属此版本直到下一版本创建）"""
    project = get_project(project_id)
    if not project:
        project_not_found(project_id)
    if not req.name or not req.name.strip():
        raise HTTPException(status_code=422, detail={
            "error": {"code": "invalid_config", "message": "版本名称必填"}
        })
    version = ProjectVersion(
        id=_generate_id("ver"),
        name=req.name.strip(),
        created_at=_utc_now(),
    )
    project.versions = (project.versions or []) + [version]
    save_project(project)
    return version.model_dump()


@router.delete("/projects/{project_id}/versions/{version_id}")
async def delete_version(project_id: str, version_id: str):
    """删除版本（不连带删 run；run.version_id 变为孤儿引用，对比时归入「未分版本」桶）"""
    project = get_project(project_id)
    if not project:
        project_not_found(project_id)
    original_len = len(project.versions or [])
    project.versions = [v for v in (project.versions or []) if v.id != version_id]
    if len(project.versions) == original_len:
        raise HTTPException(status_code=404, detail={
            "error": {"code": "version_not_found", "message": f"版本 {version_id} 不存在"}
        })
    save_project(project)
    return {"deleted": version_id}


@router.get("/projects/{project_id}/versions/compare")
async def compare_versions(project_id: str):
    """跨版本对比：每版本聚合 pass_rate / total_token / token_per_pass / run 数 + delta

    - 无版本的项目返回空数组（向后兼容）
    - run.version_id 为 None（旧 run 或无版本时创建）归入「未分版本」桶
    """
    project = get_project(project_id)
    if not project:
        project_not_found(project_id)
    runs = list_runs(project_id)
    completed = [r for r in runs if r.status == "completed" and r.summary]
    versions = project.versions or []

    def _aggregate(run_list):
        if not run_list:
            return {"run_count": 0, "pass_rate": 0.0, "total_token": 0,
                    "token_per_pass": 0.0, "latency_p50": 0.0}
        n = len(run_list)
        avg_pass = sum(r.summary.pass_rate for r in run_list) / n
        total_tok = sum(r.summary.total_token for r in run_list)
        avg_tpp = sum(r.summary.token_per_pass for r in run_list) / n
        avg_p50 = sum(r.summary.latency_p50 for r in run_list) / n
        return {"run_count": n, "pass_rate": round(avg_pass, 4),
                "total_token": total_tok, "token_per_pass": round(avg_tpp, 4),
                "latency_p50": round(avg_p50, 2)}

    buckets = {}
    for v in versions:
        buckets[v.id] = {"version_id": v.id, "version_name": v.name,
                         "version_created_at": v.created_at, **_aggregate(
                             [r for r in completed if r.version_id == v.id])}
    # 未分版本桶
    unassigned = [r for r in completed if not r.version_id or r.version_id not in {v.id for v in versions}]
    buckets["_unassigned"] = {"version_id": None, "version_name": "未分版本",
                              "version_created_at": None, **_aggregate(unassigned)}

    version_list = [buckets[v.id] for v in versions]
    if unassigned:
        version_list.append(buckets["_unassigned"])

    # delta：相邻版本 pass_rate / total_token 差值
    for i in range(1, len(version_list)):
        prev = version_list[i - 1]
        curr = version_list[i]
        curr["delta_pass_rate"] = round(curr["pass_rate"] - prev["pass_rate"], 4)
        curr["delta_total_token"] = curr["total_token"] - prev["total_token"]
    if version_list:
        version_list[0]["delta_pass_rate"] = 0.0
        version_list[0]["delta_total_token"] = 0

    return {"project_id": project_id, "versions": version_list}


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
    # T3-2: 全量替换后采样历史失效（内容已变），刷新 content_updated_at
    evalset.content_updated_at = _utc_now()
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
            # U-10: validations 可选（JSON 字符串数组）；为空时由旧字段自动合成
            vld_raw = row.get("validations")
            if vld_raw is not None and vld_raw != "":
                if isinstance(vld_raw, str):
                    try:
                        vld_list = json.loads(vld_raw)
                    except json.JSONDecodeError:
                        errors.append({"row": i + 1, "error": f"validations 不是合法 JSON: {vld_raw[:50]}"})
                        continue
                else:
                    vld_list = vld_raw
                try:
                    kwargs["validations"] = [EvalCheck(**v) if isinstance(v, dict) else v for v in vld_list]
                except Exception as ve:
                    errors.append({"row": i + 1, "error": f"validations 解析失败: {ve}"})
                    continue
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
        # T3-2: replace 后采样历史失效（内容已变），刷新 content_updated_at
        evalset.content_updated_at = _utc_now()
    else:  # merge：T2-1 按 case_name 匹配，同名复用 id + 更新内容（重导不换 id，采样历史连续）
        existing_by_name = {c.case_name: c for c in evalset.cases}
        seen_new_names: set = set()
        for case in new_cases:
            if case.case_name in seen_new_names:
                # 导入数据内同名（罕见）→ 跳过，避免重复
                continue
            seen_new_names.add(case.case_name)
            if case.case_name in existing_by_name:
                # 同名：复用已有 id，用新内容覆盖旧 case 字段（input/eval_type 等可能变化）
                old = existing_by_name[case.case_name]
                case.id = old.id
                idx = evalset.cases.index(old)
                evalset.cases[idx] = case
            else:
                # 不同名：新增
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
    writer.writerow(["id", "case_name", "input", "expected_output", "output_requirement", "eval_type", "eval_params", "enabled", "tags", "validations"])

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
            json.dumps([v.model_dump() for v in case.validations], ensure_ascii=False) if case.validations is not None else "",
        ])

    # 添加 UTF-8 BOM 以支持 Excel
    content = "\ufeff" + output.getvalue()
    return {"content": content, "filename": f"{evalset.name}.csv"}


# ============== Runs ==============

@router.post("/runs", status_code=201)
async def create_run(req: RunEvalRequest, background_tasks: BackgroundTasks):
    """发起评测（异步）

    T1-2: case_filter 按标签筛选 case
    T3-1: samples = 每 case 采样次数 k；concurrency = run 级并发数（≤ project.max_concurrency）
    """
    # 验证项目存在
    project = get_project(req.project_id)
    if not project:
        project_not_found(req.project_id)

    # 验证评测集存在
    evalset = get_evalset(req.evalset_id, req.project_id)
    if not evalset:
        evalset_not_found(req.evalset_id)

    # T3-1: samples 必须 ≥ 1
    if req.samples < 1:
        raise HTTPException(status_code=422, detail={
            "error": {"code": "invalid_config",
                      "message": "samples 必须 ≥ 1（1 = 单次执行）"}
        })

    # T3-1: concurrency 越限校验——超过 project.max_concurrency 时 422
    max_conc = project.max_concurrency or 1
    if req.concurrency is not None and req.concurrency > max_conc:
        raise HTTPException(status_code=422, detail={
            "error": {"code": "invalid_config",
                      "message": f"concurrency={req.concurrency} 超过项目最大并发 {max_conc}"}
        })
    if req.concurrency is not None and req.concurrency < 1:
        raise HTTPException(status_code=422, detail={
            "error": {"code": "invalid_config",
                      "message": "concurrency 必须 ≥ 1"}
        })

    # 检查是否有启用的 case（T1-2: 按 case_filter 筛选后判断）
    enabled_cases = [c for c in evalset.cases if c.enabled]
    filtered_cases = _apply_case_filter(enabled_cases, req.case_filter)
    if not filtered_cases:
        no_enabled_cases()

    # 创建 run 记录
    run_id = _generate_run_id()
    run_created_at = _utc_now()
    # T3-3: 解析归属版本（显式指定或按 created_at 自动落入最近版本）
    version_id = _resolve_version_id(project, run_created_at, req.version_id)
    run = EvalRun(
        id=run_id,
        project_id=req.project_id,
        evalset_id=req.evalset_id,
        status="queued",
        created_at=run_created_at,
        version_id=version_id,
    )
    save_run(run)

    # 后台执行（T1-2: 传 case_filter；T3-1: 传 samples/concurrency）
    background_tasks.add_task(
        execute_run, run, project, evalset, req.case_filter,
        req.samples, req.concurrency,
    )

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
            "version_id": r.version_id,
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


@router.get("/projects/{project_id}/regression-alerts")
async def get_regression_alerts_route(project_id: str):
    """T3-4: 查询项目回归告警（最新 run 相对 baseline 的 pass_rate 降幅）"""
    project = get_project(project_id)
    if not project:
        project_not_found(project_id)
    from .scheduler import get_regression_alerts
    return {"alerts": get_regression_alerts(project_id)}


@router.get("/evalsets/{evalset_id}/sampling")
async def get_evalset_sampling(evalset_id: str, project_id: str):
    """T2-1: 评测集级 case 粒度采样分析（pass_at_3 / pass_pow_3 per case）"""
    evalset = get_evalset(evalset_id, project_id)
    if not evalset:
        evalset_not_found(evalset_id)
    from .sampling import compute_evalset_sampling
    return compute_evalset_sampling(project_id, evalset_id)


@router.get("/evalsets/{evalset_id}/cases/{case_id}/history")
async def get_case_history(evalset_id: str, case_id: str, project_id: str):
    """U-8: 单 case 历史记录 + 聚合指标

    返回该 case 在全部 completed run 中的记录及聚合统计。
    llm_judge case 的 judge 模型 + 提示词摘要来自当前项目 Judge 配置（run 不快照，
    历史 run 可能与当前不一致，UI 已加注脚）。
    """
    evalset = get_evalset(evalset_id, project_id)
    if not evalset:
        evalset_not_found(evalset_id)
    # 找到 case 定义
    case = None
    for c in evalset.cases:
        if c.id == case_id or c.case_name == case_id:
            case = c
            break
    if not case:
        raise HTTPException(status_code=404, detail={
            "error": {"code": "case_not_found", "message": f"case {case_id} 不存在"}
        })
    # 解析当前项目 Judge 配置摘要（llm_judge 时附在每行）
    project = get_project(project_id)
    judge_summary = None
    if project:
        jc = None
        if project.judge_config_id:
            # get_judge_config 返回 dict（masked），其 judge_config 子字段是真实配置
            jc_item = get_judge_config(project.judge_config_id)
            if jc_item:
                try:
                    jc = JudgeConfig(**jc_item.get("judge_config", jc_item))
                except Exception:
                    jc = None
        if not jc:
            jc = project.judge_config
        if jc:
            prompt = (jc.prompt_template or "").strip()
            judge_summary = {
                "model": jc.model or "",
                "api_type": jc.api_type,
                "prompt_summary": (prompt[:80] + ("…" if len(prompt) > 80 else "")) or "",
            }
    # 查全部 completed run
    all_runs = list_runs(project_id)
    completed_runs = [r for r in all_runs if r.status == "completed" and r.evalset_id == evalset_id]
    completed_runs.sort(key=lambda r: r.created_at or "")

    # 构建历史记录
    history = []
    for run in completed_runs:
        result = None
        for r in run.results:
            if (r.case_id and r.case_id == case.id) or r.case_name == case.case_name:
                result = r
                break
        if not result:
            continue
        history.append({
            "run_id": run.id,
            "passed": result.passed,
            "skipped_reason": result.skipped_reason,
            "latency_ms": result.latency_ms,
            "token_used": result.token_used,
            "judge_token": result.judge_token,
            "eval_type": case.eval_type,
            "input": case.input,
            "expected_output": case.expected_output,
            "output_requirement": case.output_requirement,
            "actual_output": result.actual_output,
            "created_at": run.created_at,
            "sample_index": result.sample_index,
            "version_id": run.version_id,
            "check_results": result.check_results or [],
        })

    # 聚合
    non_skipped = [h for h in history if not h["skipped_reason"]]
    n = len(non_skipped)
    c = sum(1 for h in non_skipped if h["passed"])
    pass_rate = c / n if n > 0 else 0.0
    # pass@3: 最近 3 次全过
    recent = [h["passed"] for h in non_skipped[-3:]]
    pass_at_3 = 1.0 if (len(recent) >= 3 and all(recent)) else 0.0
    # pass^3 = pass_rate^3（概率估算）
    pass_pow_3 = pass_rate ** 3 if n > 0 else 0.0
    # 延迟
    latencies = sorted([h["latency_ms"] for h in non_skipped if h["latency_ms"] > 0])
    latency_p50 = latencies[len(latencies) // 2] if latencies else 0.0
    latency_p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0.0
    total_token = sum(h["token_used"] + h["judge_token"] for h in non_skipped)
    token_per_pass = (c / (total_token / 10000)) if total_token > 0 and c > 0 else 0.0

    aggregate = {
        "n": n,
        "c": c,
        "pass_rate": pass_rate,
        "pass_at_3": pass_at_3,
        "pass_pow_3": pass_pow_3,
        "latency_p50": latency_p50,
        "latency_p95": latency_p95,
        "total_token": total_token,
        "token_per_pass": token_per_pass,
        "judge_summary": judge_summary,
    }
    return {"history": history, "aggregate": aggregate}


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


# ============== T2-3: 配置模板 ==============

@router.get("/config-templates")
async def list_templates():
    """列出全部 Target 配置模板（secret 字段 masked）"""
    return {"templates": list_config_templates()}


@router.post("/config-templates", status_code=201)
async def save_template(project_id: str, name: str = Form(...)):
    """保存当前项目 target_config 为命名模板

    - secret 字段（api_key/auth bearer_token 等）原值存盘，便于跨项目引用
    - 返回时 masked，前端展示用 ***
    """
    project = get_project(project_id)
    if not project:
        project_not_found(project_id)
    if not name or not name.strip():
        raise HTTPException(status_code=422, detail={
            "error": {"code": "invalid_config", "message": "模板名称必填"}
        })
    return save_config_template(name.strip(), project.target_config)


@router.get("/config-templates/{template_id}")
async def get_template(template_id: str):
    """获取单个模板详情（含完整 target_config，用于加载到其他项目表单）

    注意：返回值含 secret 原值——加载到新项目时由前端提示用户手动补 key。
    """
    tpl = get_config_template(template_id)
    if not tpl:
        raise HTTPException(status_code=404, detail={
            "error": {"code": "template_not_found", "message": f"模板 {template_id} 不存在"}
        })
    return tpl


@router.delete("/config-templates/{template_id}")
async def remove_template(template_id: str):
    """删除配置模板"""
    ok = delete_config_template(template_id)
    if not ok:
        raise HTTPException(status_code=404, detail={
            "error": {"code": "template_not_found", "message": f"模板 {template_id} 不存在"}
        })
    return {"deleted": template_id}


# ============== REQ-16: Judge 配置独立管理 ==============

@router.get("/judge-configs")
async def list_judge_configs_route():
    """列出全部 Judge 配置（secret 字段 masked）"""
    return {"judge_configs": list_judge_configs()}


@router.post("/judge-configs", status_code=201)
async def create_judge_config_route(name: str = Form(...), judge_config_json: str = Form(...)):
    """新建 Judge 配置

    Form 字段：
    - name: 配置名称（必填，非空）
    - judge_config_json: JudgeConfig 序列化 JSON（必填）
    secret 字段（api_key/auth bearer_token 等）原值落盘
    """
    if not name or not name.strip():
        raise HTTPException(status_code=422, detail={
            "error": {"code": "invalid_config", "message": "Judge 配置名称必填"}
        })
    try:
        jc_data = json.loads(judge_config_json)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail={
            "error": {"code": "invalid_config", "message": f"judge_config 不是合法 JSON: {e}"}
        })
    # 校验 + 标准化（Pydantic 校验 api_type 模式 / model 必填等）
    try:
        jc = JudgeConfig(**jc_data)
    except Exception as e:
        raise HTTPException(status_code=422, detail={
            "error": {"code": "invalid_config", "message": f"Judge 配置校验失败: {e}"}
        })
    # api_type 对称校验（与 update_project 同口径）
    _validate_judge_config(jc)
    return save_judge_config(name.strip(), jc)


@router.get("/judge-configs/{judge_config_id}")
async def get_judge_config_route(judge_config_id: str):
    """获取单个 Judge 配置详情（masked，供展示用）"""
    jc = get_judge_config(judge_config_id)
    if not jc:
        raise HTTPException(status_code=404, detail={
            "error": {"code": "judge_config_not_found", "message": f"Judge 配置 {judge_config_id} 不存在"}
        })
    return jc


@router.put("/judge-configs/{judge_config_id}")
async def update_judge_config_route(
    judge_config_id: str,
    name: str = Form(...),
    judge_config_json: str = Form(...),
    overwrite: str = Form("false"),
):
    """更新 Judge 配置（全量替换）

    - name 与原配置同名 → 直接覆盖（无需 overwrite）
    - name 改为其他已存在的名称 → 需 overwrite=true 二次确认（避免误覆盖）
    """
    existing = get_judge_config(judge_config_id)
    if not existing:
        raise HTTPException(status_code=404, detail={
            "error": {"code": "judge_config_not_found", "message": f"Judge 配置 {judge_config_id} 不存在"}
        })
    if not name or not name.strip():
        raise HTTPException(status_code=422, detail={
            "error": {"code": "invalid_config", "message": "Judge 配置名称必填"}
        })
    name = name.strip()

    # 同名覆盖检查（REQ-17 思路）：改名为其他已存在配置的名字需 overwrite
    if name != existing.get("name"):
        same_name = find_judge_config_by_name(name)
        if same_name and same_name.get("id") != judge_config_id:
            if overwrite.lower() != "true":
                raise HTTPException(status_code=409, detail={
                    "error": {"code": "name_conflict",
                              "message": f"已存在名为「{name}」的 Judge 配置，需 overwrite=true 确认覆盖"}
                })

    try:
        jc_data = json.loads(judge_config_json)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail={
            "error": {"code": "invalid_config", "message": f"judge_config 不是合法 JSON: {e}"}
        })

    # __UNCHANGED__ 哨兵处理：保留原 secret 值（与 update_project 同口径）
    existing_with_secrets = get_judge_config_with_secrets(judge_config_id)
    if existing_with_secrets and "judge_config" in existing_with_secrets:
        old_jc = existing_with_secrets["judge_config"]
        if jc_data.get("api_key") == "__UNCHANGED__":
            jc_data["api_key"] = old_jc.get("api_key", "")
        old_auth = old_jc.get("auth")
        new_auth = jc_data.get("auth")
        if new_auth and old_auth and isinstance(new_auth, dict):
            if new_auth.get("bearer_token") == "__UNCHANGED__":
                new_auth["bearer_token"] = old_auth.get("bearer_token")
            if new_auth.get("api_key_value") == "__UNCHANGED__":
                new_auth["api_key_value"] = old_auth.get("api_key_value")
            for i, cookie in enumerate(new_auth.get("cookies", [])):
                if cookie.get("value") == "__UNCHANGED__" and i < len(old_auth.get("cookies", [])):
                    cookie["value"] = old_auth["cookies"][i].get("value")

    try:
        jc = JudgeConfig(**jc_data)
    except Exception as e:
        raise HTTPException(status_code=422, detail={
            "error": {"code": "invalid_config", "message": f"Judge 配置校验失败: {e}"}
        })
    _validate_judge_config(jc)
    updated = update_judge_config(judge_config_id, name, jc)
    if not updated:
        raise HTTPException(status_code=404, detail={
            "error": {"code": "judge_config_not_found", "message": f"Judge 配置 {judge_config_id} 不存在"}
        })
    return updated


@router.delete("/judge-configs/{judge_config_id}")
async def delete_judge_config_route(judge_config_id: str):
    """删除 Judge 配置（不连带改项目引用；运行时 fallback 到内联 judge_config）"""
    ok = delete_judge_config(judge_config_id)
    if not ok:
        raise HTTPException(status_code=404, detail={
            "error": {"code": "judge_config_not_found", "message": f"Judge 配置 {judge_config_id} 不存在"}
        })
    return {"deleted": judge_config_id}


def _validate_judge_config(jc: JudgeConfig) -> None:
    """JudgeConfig 对称校验（与 update_project 同口径）"""
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
                          "message": "custom 模式下 judge response_parsing 必填"}
            })
