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
    CreateTagRequest, RenameTagRequest, ScheduleConfig, UpdateScheduleRequest,
    ModelPrice, CreateModelPriceRequest, UpdateModelPriceRequest,
    BatchEstimateRequest, BatchEstimateQualityRequest,
)
from .storage import (
    list_projects, get_project, save_project, delete_project,
    list_evalsets, get_evalset, save_evalset, delete_evalset,
    list_runs, get_run, save_run,
    get_project_last_run, get_project_trend,
    list_config_templates, get_config_template, save_config_template, delete_config_template,
    list_judge_configs, get_judge_config, get_judge_config_with_secrets, save_judge_config,
    update_judge_config, delete_judge_config, find_judge_config_by_name,
    list_tags, save_tag, rename_tag, delete_tag, _migrate_legacy_tags,
    list_model_prices, save_model_price, update_model_price, delete_model_price, cost_estimate,
    batch_estimate, batch_estimate_quality_cost,
)
from .runner import execute_run, _utc_now, _generate_run_id, _apply_case_filter, _resolve_effective_judge_config
from .judge import call_target, judge_with_llm, compute_judge_fingerprint, NetworkError, APIError, ResponseFormatError
from .sampling import pass_at_k_case, pass_pow_k_case, compute_project_sampling, compute_evalset_sampling
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
    """T3-3 / Q-3: 解析 run 的归属版本 id。

    - explicit 非空：校验是否在 project.versions 中，存在则用，不存在返回 None（不阻断，兼容旧数据）
    - explicit 为空：
      Q-3: 若 project.current_version_id 存在且在 versions 中 → 用它（切换版本后新 run 归属当前版本）
      否则按 run_created_at 落入最近版本（version.created_at ≤ run_created_at 的最大者）
    - 项目无版本：返回 None（向后兼容，旧 run 无 version_id）
    """
    versions = project.versions or []
    if not versions:
        return None
    if explicit:
        if any(v.id == explicit for v in versions):
            return explicit
        return None
    # Q-3: 优先 current_version_id（被测 API 版本切换锚点）
    cv = project.current_version_id
    if cv and any(v.id == cv for v in versions):
        return cv
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
    now = _utc_now()
    project = Project(
        id=project_id,
        name=req.name,
        task_shape=req.task_shape,
        judge_config={"base_url": "", "api_key": "", "model": ""},
        target_config={"base_url": "", "api_key": "", "model": None},
        # W-7: 新建项目自动创建初始版本
        versions=[ProjectVersion(id=_generate_id("ver"), name="初始版本", created_at=now)],
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
    # Q-3: 新建版本即成为当前活动版本（新发起 run 默认归属此版本）
    project.current_version_id = version.id
    save_project(project)
    return version.model_dump()


@router.post("/projects/{project_id}/versions/{version_id}/activate")
async def activate_version(project_id: str, version_id: str):
    """Q-3: 切换当前活动版本（被测 Target API 版本切换）。

    切到指定版本后，新发起的 run 默认归属此版本，概览/统计以该版本为准。
    """
    project = get_project(project_id)
    if not project:
        project_not_found(project_id)
    if not any(v.id == version_id for v in (project.versions or [])):
        raise HTTPException(status_code=404, detail={
            "error": {"code": "version_not_found", "message": f"版本 {version_id} 不存在"}
        })
    project.current_version_id = version_id
    save_project(project)
    return {"project_id": project_id, "current_version_id": version_id}


@router.put("/projects/{project_id}/versions/{version_id}")
async def rename_version(project_id: str, version_id: str, req: CreateVersionRequest):
    """W-7: 版本改名（PUT 接口，复用 CreateVersionRequest）"""
    project = get_project(project_id)
    if not project:
        project_not_found(project_id)
    if not req.name or not req.name.strip():
        raise HTTPException(status_code=422, detail={
            "error": {"code": "invalid_config", "message": "版本名称必填"}
        })
    for v in (project.versions or []):
        if v.id == version_id:
            v.name = req.name.strip()
            save_project(project)
            return v.model_dump()
    raise HTTPException(status_code=404, detail={
        "error": {"code": "version_not_found", "message": f"版本 {version_id} 不存在"}
    })


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
    # Q-3: 删除的正是当前活动版本 → 清空，回到按 created_at 自动落入最近版本的行为
    if project.current_version_id == version_id:
        project.current_version_id = None
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


# ============== W-3: 定时任务管理 ==============

@router.get("/schedules")
async def list_schedules():
    """列出全部项目的定时规则 + 状态（上次执行时间/结果）"""
    result = []
    for p in list_projects():
        if not p.schedule:
            continue
        # 找该项目最近的 scheduled run
        all_runs = list_runs(p.id)
        scheduled_runs = [r for r in all_runs if getattr(r, "trigger", "manual") == "scheduled"]
        scheduled_runs.sort(key=lambda r: r.created_at or "", reverse=True)
        last = scheduled_runs[0] if scheduled_runs else None
        result.append({
            "project_id": p.id,
            "project_name": p.name,
            "enabled": p.schedule.enabled,
            "cron": p.schedule.cron,
            "tags": p.schedule.tags or [],
            "version_id": p.schedule.version_id,
            "regression_threshold": p.schedule.regression_threshold,
            "last_run": {
                "run_id": last.id,
                "created_at": last.created_at,
                "status": last.status,
                "pass_rate": last.summary.pass_rate if last.summary else None,
            } if last else None,
        })
    return {"schedules": result}


@router.get("/schedules/logs")
async def list_schedule_logs(limit: int = 20):
    """最近 N 次定时触发记录"""
    all_logs = []
    project_map = {p.id: p.name for p in list_projects()}
    for pid, pname in project_map.items():
        runs = list_runs(pid)
        for r in runs:
            if getattr(r, "trigger", "manual") != "scheduled":
                continue
            all_logs.append({
                "project_id": pid,
                "project_name": pname,
                "run_id": r.id,
                "created_at": r.created_at,
                "status": r.status,
                "pass_rate": r.summary.pass_rate if r.summary else None,
            })
    all_logs.sort(key=lambda x: x["created_at"] or "", reverse=True)
    return {"logs": all_logs[:limit]}


@router.put("/projects/{project_id}/schedule")
async def update_schedule(project_id: str, req: UpdateScheduleRequest):
    """创建/更新项目定时配置"""
    project = get_project(project_id)
    if not project:
        project_not_found(project_id)
    # cron 格式校验（5 字段）
    cron = req.cron.strip()
    parts = cron.split()
    if len(parts) != 5:
        raise HTTPException(status_code=422, detail={
            "error": {"code": "invalid_cron", "message": "cron 表达式必须为 5 字段（分 时 日 月 周）"}
        })
    project.schedule = ScheduleConfig(
        enabled=req.enabled,
        cron=cron,
        tags=req.tags or [],
        version_id=req.version_id,
        regression_threshold=req.regression_threshold,
    )
    save_project(project)
    return project.schedule.model_dump()


@router.delete("/projects/{project_id}/schedule")
async def delete_schedule(project_id: str):
    """删除项目定时配置"""
    project = get_project(project_id)
    if not project:
        project_not_found(project_id)
    project.schedule = None
    save_project(project)
    return {"deleted": project_id}


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
    # Q-1: 解析实际 Judge 配置并写入指纹（不含 secret），用于跨 run 可比性判断
    effective_judge = _resolve_effective_judge_config(project)
    judge_fingerprint = compute_judge_fingerprint(effective_judge)
    run = EvalRun(
        id=run_id,
        project_id=req.project_id,
        evalset_id=req.evalset_id,
        status="queued",
        created_at=run_created_at,
        version_id=version_id,
        # V-3: 带入标签筛选信息供历史列表展示
        filter_tags=req.case_filter.tags if req.case_filter and req.case_filter.tags else [],
        judge_fingerprint=judge_fingerprint,
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
    """获取 run 详情

    P-1: results 各项合并 case 级字段（eval_type / input / variables / expected_output /
    output_requirement / eval_params / validations / version_name），使 run 详情「查看」
    与统计弹窗「详情」返回同一字段规格，前端 renderCaseRunDetailBody 可从同一份数据渲染。
    """
    run = get_run(run_id, project_id)
    if not run:
        run_not_found(run_id)
    data = run.model_dump()

    # join 评测集 case 定义（按 case_id 或 case_name 匹配）
    evalset = get_evalset(run.evalset_id, project_id) if run.evalset_id else None
    case_map = {}
    if evalset:
        for c in evalset.cases:
            key = c.id or c.case_name
            if key:
                case_map[key] = c
            case_map[c.case_name] = c

    # version_id → version_name
    project = get_project(project_id)
    version_name = None
    if project and project.versions and run.version_id:
        version_name = next((v.name for v in project.versions if v.id == run.version_id), None)

    for item in data.get("results", []):
        cs = case_map.get(item.get("case_id")) or case_map.get(item.get("case_name"))
        if cs:
            item.setdefault("eval_type", cs.eval_type)
            item.setdefault("input", cs.input)
            item.setdefault("variables", cs.variables or {})
            item.setdefault("expected_output", cs.expected_output)
            item.setdefault("output_requirement", cs.output_requirement)
            item.setdefault("eval_params", cs.eval_params or {})
            item.setdefault("validations", cs.validations or [])
        item.setdefault("version_name", version_name)
    # Q-2: run 成本估算（端点 + 模型双 key + run token）
    effective_judge = _resolve_effective_judge_config(project) if project else None
    target_endpoint = project.target_config.base_url if project else None
    target_model_name = project.target_config.model if project else None
    judge_endpoint = effective_judge.base_url if effective_judge else None
    judge_model_name = effective_judge.model if effective_judge else None
    if run.summary:
        data["cost"] = cost_estimate(
            target_endpoint, target_model_name,
            judge_endpoint, judge_model_name,
            run.summary.total_token, run.summary.judge_token,
            run.created_at,
        )
    return data


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
            # V-3: 标签筛选 + case 数
            "filter_tags": getattr(r, "filter_tags", None) or [],
            "case_count": len(r.results) if r.results else 0,
            # W-3: 触发来源
            "trigger": getattr(r, "trigger", "manual"),
            "summary": r.summary.model_dump() if r.summary else None,
        })

    return {"runs": result}


@router.get("/projects/{project_id}/sampling")
async def get_project_sampling(project_id: str):
    """项目采样稳定性（pass@k / pass^k，k=1,2,3）"""
    project = get_project(project_id)
    if not project:
        project_not_found(project_id)
    return compute_project_sampling(project_id)


@router.get("/projects/{project_id}/overview")
async def get_project_overview(project_id: str, rule_only: bool = False):
    """P-3: 概览页数据（依据 docs/overview-redesign.md v2）

    返回 4 区块数据：
    ① delta：最近 completed run 与同版本内上次 completed run 的 pass_rate / 运行 token / 评测 token
    ② 趋势：含 version_id/created_at/status 的 run 序列（旧→新，最多 20）
    ③ 稳定性：min(pass^3) + 阈值计数 + 最不稳 3 case 列表（来自 sampling API）
    ④ 上次 run 失败 case 列表（导航到问题）
    另附 versions 与 content_updated_at（用于趋势分段与变更标记）
    Q-1: rule_only=true 时趋势/delta 的 pass_rate 仅统计规则类 case（eval_type 非 llm_judge），
         规则类线不受 Judge 影响，恒可比。
    """
    project = get_project(project_id)
    if not project:
        project_not_found(project_id)

    runs = list_runs(project_id)
    completed = [r for r in runs if r.status == "completed"]

    # Q-3: 切换版本后概览以当前活动版本为准——delta/latest/失败 case 聚焦该版本
    cv = project.current_version_id
    cv_valid = bool(cv and any(v.id == cv for v in (project.versions or [])))
    scoped = [r for r in completed if r.version_id == cv] if cv_valid else completed

    # Q-4: 口径（caliber）= 标签筛选集合；空 = 全量 run。
    def _caliber(r):
        if not r:
            return None
        ts = getattr(r, "filter_tags", None) or []
        return "FULL" if not ts else ",".join(sorted(ts))

    # ① delta headline：优先最近一次全量 run（口径可比、不误导）；无全量则取 scoped 最新
    full_scoped = [r for r in scoped if not (getattr(r, "filter_tags", None) or [])]
    latest = full_scoped[0] if full_scoped else (scoped[0] if scoped
              else (completed[0] if completed and not cv_valid else None))
    latest_caliber = _caliber(latest) if latest else None
    # Q-3 兼容：baseline 限定在 latest 同版本内（未切换版本时 = latest 所在版本）
    _lv = latest.version_id if latest else None
    version_pool = [r for r in completed if (r.version_id or None) == (_lv or None)]
    version_pool_excl_latest = [r for r in version_pool if latest and r.id != latest.id] if latest else []
    same_caliber = [r for r in version_pool_excl_latest if _caliber(r) == latest_caliber]
    baseline = None
    is_first_in_version = True
    # Q-4: 口径一致性——baseline 是否与 latest 同口径（同 case 集合才可直接对比）
    caliber_consistent = False
    if same_caliber:
        baseline = same_caliber[0]
        is_first_in_version = False
        caliber_consistent = True
    elif version_pool_excl_latest:
        # 无同口径 baseline → 退到同版本最近一次（口径不同，前端标「仅供参考」）
        baseline = version_pool_excl_latest[0]
        is_first_in_version = False
        caliber_consistent = False

    def _delta(cur, base, key):
        if not cur or not base:
            return None
        cv = (cur.summary.pass_rate if key == "pass_rate" and cur.summary else
              cur.summary.total_token if key == "total_token" and cur.summary else
              cur.summary.judge_token if cur.summary else 0)
        bv = (base.summary.pass_rate if key == "pass_rate" and base.summary else
              base.summary.total_token if key == "total_token" and base.summary else
              base.summary.judge_token if base.summary else 0)
        return cv - bv

    # Q-1: rule_only 模式下，从 run.results 重算规则类 pass_rate（排除 llm_judge case）
    # 需 join evalset 拿每 case 的 eval_type；run.results 里也有 check_results 但 case.eval_type 更准
    _evalset_for_rule = get_evalset(latest.evalset_id, project_id) if latest and latest.evalset_id else None
    _case_rule_map = {}  # case_name / case_id → 是否规则类（非 llm_judge）
    if _evalset_for_rule:
        for c in _evalset_for_rule.cases:
            is_rule = c.eval_type != "llm_judge"
            _case_rule_map[c.case_name] = is_rule
            if c.id:
                _case_rule_map[c.id] = is_rule
    def _rule_pass_rate(run):
        """重算规则类 pass_rate（排除 llm_judge case）。无规则类 case 时返回原 pass_rate。"""
        if not rule_only or not run or not run.results or not _case_rule_map:
            return run.summary.pass_rate if run and run.summary else 0
        rule_results = [r for r in run.results
                        if not r.skipped_reason
                        and _case_rule_map.get(r.case_name, _case_rule_map.get(r.case_id, True))]
        if not rule_results:
            return run.summary.pass_rate if run.summary else 0
        passed = sum(1 for r in rule_results if r.passed)
        return round(passed / len(rule_results), 4)
    def _get_pr(run):
        return _rule_pass_rate(run)

    # Q-2: 成本估算（基于当前项目 target/judge 端点+模型双 key + run token）
    effective_judge = _resolve_effective_judge_config(project)
    target_endpoint = project.target_config.base_url
    target_model_name = project.target_config.model
    judge_endpoint = effective_judge.base_url if effective_judge else None
    judge_model_name = effective_judge.model if effective_judge else None
    def _run_cost(r):
        if not r or not r.summary:
            return None
        return cost_estimate(target_endpoint, target_model_name,
                             judge_endpoint, judge_model_name,
                             r.summary.total_token, r.summary.judge_token,
                             r.created_at)

    delta = None
    if latest:
        # Q-4: 口径标注——基于 N/M 条 case（占评测集 X%）；子集 run 标组名
        _es_for_caliber = get_evalset(latest.evalset_id, project_id) if latest.evalset_id else None
        _enabled_total = len([c for c in (_es_for_caliber.cases if _es_for_caliber else []) if c.enabled]) if _es_for_caliber else 0
        _exec_count = len([r for r in (latest.results or []) if not r.skipped_reason]) if latest.results else 0
        _coverage = round(_exec_count / _enabled_total, 4) if _enabled_total else None
        _latest_tags = list(getattr(latest, "filter_tags", None) or [])
        _base_tags = list(getattr(baseline, "filter_tags", None) or []) if baseline else []
        # 组名：全量→"全量"；子集→标签名拼接
        def _group_name(tags):
            return "全量" if not tags else "、".join(tags)
        delta = {
            "pass_rate": {"current": _get_pr(latest),
                          "previous": _get_pr(baseline) if baseline else None,
                          "diff": (_get_pr(latest) - _get_pr(baseline)) if baseline else None},
            "total_token": {"current": latest.summary.total_token if latest.summary else 0,
                            "previous": baseline.summary.total_token if baseline and baseline.summary else None,
                            "diff": _delta(latest, baseline, "total_token")},
            "judge_token": {"current": latest.summary.judge_token if latest.summary else 0,
                            "previous": baseline.summary.judge_token if baseline and baseline.summary else None,
                            "diff": _delta(latest, baseline, "judge_token")},
            "is_first_in_version": is_first_in_version,
            "cost": _run_cost(latest),
            # Q-4: 口径信息
            "caliber": {
                "case_count": _exec_count,
                "evalset_case_count": _enabled_total,
                "coverage_ratio": _coverage,
                "group": _group_name(_latest_tags),
                "tags": _latest_tags,
                "is_full": not _latest_tags,
                "current_group": _group_name(_latest_tags),
                "previous_group": _group_name(_base_tags) if baseline else None,
                "consistent": caliber_consistent if baseline else None,
                "note": (None if (not baseline or caliber_consistent)
                         else "口径不同，仅供参考"),
            },
        }

    # ② 趋势（旧→新，最多 20）+ 每条 run 的 judge 指纹（Q-1: 可比性标记）
    trend = list(reversed(runs[:20]))
    # Q-4: 预加载每个 run 的 evalset 启用 case 数（用于口径标注 N/M + coverage）
    _es_cache = {}  # evalset_id → enabled_count
    def _enabled_count(evalset_id):
        if not evalset_id:
            return 0
        if evalset_id not in _es_cache:
            es = get_evalset(evalset_id, project_id)
            _es_cache[evalset_id] = len([c for c in (es.cases if es else []) if c.enabled]) if es else 0
        return _es_cache[evalset_id]
    trend_data = [{
        "run_id": r.id, "pass_rate": _get_pr(r),
        "total_token": r.summary.total_token if r.summary else 0,
        "judge_token": r.summary.judge_token if r.summary else 0,
        "created_at": r.created_at, "version_id": r.version_id,
        "status": r.status,
        "judge_fingerprint": r.judge_fingerprint,
        # Q-4: 口径字段（分色连线 + 口径标注）
        "filter_tags": list(getattr(r, "filter_tags", None) or []),
        "is_full": not (getattr(r, "filter_tags", None) or []),
        "case_count": len([x for x in (r.results or []) if not x.skipped_reason]),
        "evalset_case_count": _enabled_count(r.evalset_id),
        "coverage_ratio": (round(len([x for x in (r.results or []) if not x.skipped_reason]) / _en, 4)
                           if r.results and (_en := _enabled_count(r.evalset_id)) else None),
    } for r in trend]

    # Q-1: judge_changed —— 最近 run vs 上次 run 的指纹是否不同
    judge_changed = False
    judge_fingerprints = {"latest": None, "previous": None}
    if latest:
        judge_fingerprints["latest"] = latest.judge_fingerprint
        if baseline:
            judge_fingerprints["previous"] = baseline.judge_fingerprint
            if latest.judge_fingerprint and baseline.judge_fingerprint:
                judge_changed = latest.judge_fingerprint != baseline.judge_fingerprint

    # ③ 稳定性：来自 sampling
    sampling = compute_project_sampling(project_id)
    evalsets = list_evalsets(project_id)
    content_updated_at = evalsets[0].content_updated_at if evalsets else None
    # min(pass^3) + 阈值计数：取 evalset sampling 的 case 级数据
    case_stability = []
    if evalsets:
        es_sampling = compute_evalset_sampling(project_id, evalsets[0].id)
        case_stability = es_sampling.get("cases", [])
    # 阈值计数
    below_50 = [c for c in case_stability if c.get("pass_pow_3") is not None and c["pass_pow_3"] < 0.5]
    below_80 = [c for c in case_stability if c.get("pass_pow_3") is not None and 0.5 <= c["pass_pow_3"] < 0.8]
    min_pow3 = min((c["pass_pow_3"] for c in case_stability if c.get("pass_pow_3") is not None), default=None)
    # 最不稳 3 case（pass^3 < 0.8 才算不稳，升序，排除 None）
    unstable = sorted([c for c in case_stability if c.get("pass_pow_3") is not None and c["pass_pow_3"] < 0.8],
                      key=lambda c: c["pass_pow_3"])[:3]

    # ④ 上次 run 失败 case 列表
    failed_cases = []
    if latest:
        failed_cases = [{"case_name": r.case_name, "case_id": r.case_id}
                        for r in (latest.results or [])
                        if not r.passed and not r.skipped_reason]

    # P-5: 最近 run 列表（最多 10 条，含摘要信息）
    version_map = {v.id: v.name for v in (project.versions or [])}
    recent_runs = [{
        "id": r.id,
        "status": r.status,
        "created_at": r.created_at,
        "version_id": r.version_id,
        "version_name": version_map.get(r.version_id) if r.version_id else None,
        "pass_rate": r.summary.pass_rate if r.summary else 0,
        "total_token": r.summary.total_token if r.summary else 0,
        "judge_token": r.summary.judge_token if r.summary else 0,
        "case_count": len(r.results) if r.results else 0,
        "filter_tags": getattr(r, "filter_tags", None) or [],
        "trigger": getattr(r, "trigger", "manual"),
    } for r in runs[:10]]

    # 版本信息（用于趋势分段）
    versions = [{"id": v.id, "name": v.name, "created_at": v.created_at}
                for v in (project.versions or [])]

    return {
        "project_id": project_id,
        "delta": delta,
        "trend": trend_data,
        "stability": {
            "min_pass_pow_3": min_pow3,
            "below_50_count": len(below_50),
            "below_80_count": len(below_80),
            "unstable_top3": unstable,
        },
        "failed_cases": failed_cases,
        "versions": versions,
        # Q-3: 当前活动版本（被测 API 版本切换锚点），None = 未切换
        "current_version_id": project.current_version_id,
        "content_updated_at": content_updated_at,
        "latest_run_id": latest.id if latest else None,
        "recent_runs": recent_runs,
        "judge_changed": judge_changed,
        "judge_fingerprints": judge_fingerprints,
        "rule_only": rule_only,
    }


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
                "prompt_full": prompt,
            }
    # 查全部 completed run
    all_runs = list_runs(project_id)
    completed_runs = [r for r in all_runs if r.status == "completed" and r.evalset_id == evalset_id]
    completed_runs.sort(key=lambda r: r.created_at or "")

    # O-3: version_id → 版本名映射（用于历史表版本列）
    version_map = {}
    if project and project.versions:
        for v in project.versions:
            version_map[v.id] = v.name

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
            "score": result.score,
            "skipped_reason": result.skipped_reason,
            "latency_ms": result.latency_ms,
            "token_used": result.token_used,
            "judge_token": result.judge_token,
            "eval_type": case.eval_type,
            "input": case.input,
            "variables": case.variables or {},
            "eval_params": case.eval_params or {},
            "validations": case.validations or [],
            "expected_output": case.expected_output,
            "output_requirement": case.output_requirement,
            "actual_output": result.actual_output,
            "created_at": run.created_at,
            "sample_index": result.sample_index,
            "version_id": run.version_id,
            "version_name": version_map.get(run.version_id) if run.version_id else None,
            "check_results": result.check_results or [],
            "judge_fingerprint": run.judge_fingerprint,
        })

    # 聚合
    non_skipped = [h for h in history if not h["skipped_reason"]]
    n = len(non_skipped)
    c = sum(1 for h in non_skipped if h["passed"])
    pass_rate = c / n if n > 0 else 0.0
    # pass@3 / pass^3：与 sampling.py 同一套无放回公式，保证全站口径一致
    _at3 = pass_at_k_case(n, c, 3)
    _pow3 = pass_pow_k_case(n, c, 3)
    pass_at_3 = _at3 if _at3 is not None else 0.0
    pass_pow_3 = _pow3 if _pow3 is not None else 0.0
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
        score, token_used, _judge_raw = await judge_with_llm(
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


# ============== V-1: 全局标签管理 ==============

@router.get("/tags")
async def get_tags():
    """列出全部标签（含引用统计：case 数 / project 数 / project 列表）"""
    return {"tags": list_tags()}


@router.post("/tags", status_code=201)
async def create_tag(request: CreateTagRequest):
    """新建标签（重名 409）"""
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail={
            "error": {"code": "invalid_tag", "message": "标签名不能为空"}
        })
    # 重名检查
    existing = {t["name"] for t in list_tags()}
    if name in existing:
        raise HTTPException(status_code=409, detail={
            "error": {"code": "tag_already_exists", "message": f"标签 '{name}' 已存在"}
        })
    tag = save_tag(name)
    return tag


@router.put("/tags/{tag_name}")
async def rename_tag_route(tag_name: str, request: RenameTagRequest):
    """改名：同步更新所有 evalset case 的标签字符串"""
    new_name = request.new_name.strip()
    if not new_name:
        raise HTTPException(status_code=422, detail={
            "error": {"code": "invalid_tag", "message": "新标签名不能为空"}
        })
    # 重名检查（新名与已有其他标签同名）
    existing = {t["name"] for t in list_tags()}
    if new_name in existing and new_name != tag_name:
        raise HTTPException(status_code=409, detail={
            "error": {"code": "tag_already_exists", "message": f"标签 '{new_name}' 已存在"}
        })
    result = rename_tag(tag_name, new_name)
    if not result:
        raise HTTPException(status_code=404, detail={
            "error": {"code": "tag_not_found", "message": f"标签 '{tag_name}' 不存在"}
        })
    return result


@router.delete("/tags/{tag_name}")
async def delete_tag_route(tag_name: str):
    """删除：从所有 evalset case 移除该标签；返回受影响的 project 列表"""
    result = delete_tag(tag_name)
    if not result:
        raise HTTPException(status_code=404, detail={
            "error": {"code": "tag_not_found", "message": f"标签 '{tag_name}' 不存在"}
        })
    return result


@router.post("/tags/migrate")
async def migrate_tags_route():
    """一键迁移：将历史项目中未注册到全局标签库的标签自动补录。

    返回本次新注册的标签数量。幂等，重复调用安全。
    """
    count = _migrate_legacy_tags()
    return {"migrated_count": count}


# ============== Q-2: 模型价格管理 ==============

@router.get("/model-prices")
async def get_model_prices():
    """列出全部模型价格（评测成本估算用，不影响评测逻辑）"""
    return {"model_prices": list_model_prices()}


@router.post("/model-prices", status_code=201)
async def create_model_price(req: CreateModelPriceRequest):
    """新建模型价格（端点 + 模型双 key + Q-5 峰谷定价）"""
    if not req.model_pattern.strip() and not req.endpoint_pattern.strip():
        raise HTTPException(status_code=422, detail={
            "error": {"code": "invalid_config", "message": "endpoint_pattern 和 model_pattern 至少填一个"}
        })
    item = save_model_price(
        req.endpoint_pattern.strip(), req.model_pattern.strip(), req.price_per_mtok,
        req.currency, req.note,
        peak_price_per_mtok=req.peak_price_per_mtok,
        off_peak_price_per_mtok=req.off_peak_price_per_mtok,
        peak_start_hour=req.peak_start_hour, peak_end_hour=req.peak_end_hour,
    )
    return item


@router.put("/model-prices/{price_id}")
async def edit_model_price(price_id: str, req: UpdateModelPriceRequest):
    """Q-5: 编辑模型价格（峰谷字段可选更新）"""
    fields = req.model_dump(exclude_none=True)
    updated = update_model_price(price_id, fields)
    if not updated:
        raise HTTPException(status_code=404, detail={
            "error": {"code": "price_not_found", "message": f"模型价格 {price_id} 不存在"}
        })
    return updated


@router.get("/model-prices/sources")
async def get_model_price_sources():
    """Q-5: 引用式模型选择——从已保存的 Judge 配置与 Target API 配置中收集模型名供价格下拉选择。

    返回 {judge_models: [{name, endpoint, config_id, config_name}], target_models: [{name, endpoint, project_id, project_name}]}
    """
    judge_models = []
    seen_j = set()
    for jc in list_judge_configs():
        name = jc.get("judge_config", {}).get("model")
        if name and name not in seen_j:
            seen_j.add(name)
            judge_models.append({
                "name": name,
                "endpoint": jc.get("judge_config", {}).get("base_url", ""),
                "config_id": jc.get("id"),
                "config_name": jc.get("name", ""),
            })
    target_models = []
    seen_t = set()
    for p in list_projects():
        tc = p.target_config
        name = getattr(tc, "model", None)
        if name and name not in seen_t:
            seen_t.add(name)
            target_models.append({
                "name": name,
                "endpoint": getattr(tc, "base_url", ""),
                "project_id": p.id,
                "project_name": p.name,
            })
    return {"judge_models": judge_models, "target_models": target_models}


@router.delete("/model-prices/{price_id}")
async def remove_model_price(price_id: str):
    """删除模型价格"""
    result = delete_model_price(price_id)
    if not result:
        raise HTTPException(status_code=404, detail={
            "error": {"code": "price_not_found", "message": f"模型价格 {price_id} 不存在"}
        })
    return result


@router.post("/projects/{project_id}/estimate")
async def batch_estimate_route(project_id: str, req: BatchEstimateRequest):
    """Q-6: 批量预估——基于历史 case 级样本预估 N 条任务的成本/用时区间

    返回 {cost: {median, p5, p95, currency}, time: {median, p5, p95, unit},
           skipped_ratio, sample_count, run_count, low_confidence, note}
    样本不足或仅 1 次 run → 422 + error.code
    """
    project = get_project(project_id)
    if not project:
        project_not_found(project_id)
    result = batch_estimate(
        project, req.count,
        plan_hour=req.plan_hour,
        version_id=req.version_id,
        concurrency=req.concurrency,
    )
    if "error" in result:
        raise HTTPException(status_code=422, detail={"error": {
            "code": result["error"], "message": result.get("message", ""),
            "sample_count": result.get("sample_count", 0),
            "run_count": result.get("run_count", 0),
        }})
    return result


@router.post("/projects/{project_id}/estimate-quality")
async def batch_estimate_quality_route(project_id: str, req: BatchEstimateQualityRequest):
    """Q-7: 质量-成本闭环预估——回答"达到目标正确率总共要花多少钱、多少时间"

    返回 {rounds_expected, total_cost: {median, p5, p95, currency},
           total_time: {median, p5, p95}, skipped_ratio, sample_count,
           run_count, low_confidence, pass_rate, note[]}
    样本不足 / 仅 1 次 run / 目标 100%+全部重跑 → 422 + error.code
    """
    project = get_project(project_id)
    if not project:
        project_not_found(project_id)
    result = batch_estimate_quality_cost(
        project, req.count,
        target_pass_rate=req.target_pass_rate,
        rerun_strategy=req.rerun_strategy,
        time_mode=req.time_mode,
        production_concurrency=req.production_concurrency,
        version_id=req.version_id,
    )
    if "error" in result:
        raise HTTPException(status_code=422, detail={"error": {
            "code": result["error"], "message": result.get("message", ""),
            "sample_count": result.get("sample_count", 0),
            "run_count": result.get("run_count", 0),
        }})
    return result
