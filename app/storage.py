"""JSON 文件存储模块"""

import json
import math
import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import anyio

from .models import Project, EvalSet, EvalRun, TargetConfig, JudgeConfig, ProjectVersion


# 数据目录
DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

PROJECTS_DIR = DATA_DIR / "projects"
PROJECTS_DIR.mkdir(exist_ok=True)

EVALSETS_DIR = DATA_DIR / "evalsets"
EVALSETS_DIR.mkdir(exist_ok=True)

RUNS_DIR = DATA_DIR / "runs"
RUNS_DIR.mkdir(exist_ok=True)

# T2-3: 配置模板存储文件
CONFIG_TEMPLATES_FILE = DATA_DIR / "config-templates.json"

# REQ-16: Judge 配置独立管理存储文件
JUDGE_CONFIGS_FILE = DATA_DIR / "judge-configs.json"

# V-1: 全局标签库存储文件
TAGS_FILE = DATA_DIR / "tags.json"

# Q-2: 模型价格存储文件（评测成本估算）
MODEL_PRICES_FILE = DATA_DIR / "model-prices.json"


def _read_json(path: Path) -> dict:
    """读取 JSON 文件"""
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: dict) -> None:
    """写入 JSON 文件"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ============== Project 存储 ==============

def list_projects() -> list[Project]:
    """列出所有项目"""
    projects = []
    for path in PROJECTS_DIR.glob("*.json"):
        data = _read_json(path)
        projects.append(Project(**data))
    return projects


def get_project(project_id: str) -> Optional[Project]:
    """获取单个项目"""
    path = PROJECTS_DIR / f"{project_id}.json"
    if not path.exists():
        return None
    data = _read_json(path)
    return Project(**data)


def save_project(project: Project) -> None:
    """保存项目"""
    path = PROJECTS_DIR / f"{project.id}.json"
    _write_json(path, project.model_dump())


def delete_project(project_id: str) -> bool:
    """删除项目（及其关联的评测集和 runs）"""
    # 删除项目
    project_path = PROJECTS_DIR / f"{project_id}.json"
    if project_path.exists():
        project_path.unlink()

    # 删除关联的评测集
    for evalset_path in EVALSETS_DIR.glob(f"*/{project_id}/*.json"):
        evalset_path.unlink()

    # 删除关联的 runs
    runs_project_dir = RUNS_DIR / project_id
    if runs_project_dir.exists():
        import shutil
        shutil.rmtree(runs_project_dir)

    return True


# ============== EvalSet 存储 ==============

def get_evalset_dir(evalset_id: str, project_id: str) -> Path:
    """获取评测集目录"""
    return EVALSETS_DIR / project_id / evalset_id


def list_evalsets(project_id: Optional[str] = None) -> list[EvalSet]:
    """列出评测集"""
    evalsets = []
    if project_id:
        # 只列出指定项目的评测集
        project_dir = EVALSETS_DIR / project_id
        if project_dir.exists():
            for path in project_dir.glob("*.json"):
                data = _read_json(path)
                evalsets.append(EvalSet(**data))
    else:
        # 列出所有评测集
        for path in EVALSETS_DIR.rglob("*.json"):
            data = _read_json(path)
            evalsets.append(EvalSet(**data))
    return evalsets


def get_evalset(evalset_id: str, project_id: str) -> Optional[EvalSet]:
    """获取单个评测集"""
    path = EVALSETS_DIR / project_id / f"{evalset_id}.json"
    if not path.exists():
        return None
    data = _read_json(path)
    return EvalSet(**data)


def save_evalset(evalset: EvalSet) -> None:
    """保存评测集"""
    path = EVALSETS_DIR / evalset.project_id / f"{evalset.id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, evalset.model_dump())


def delete_evalset(evalset_id: str, project_id: str) -> bool:
    """删除评测集"""
    path = EVALSETS_DIR / project_id / f"{evalset_id}.json"
    if path.exists():
        path.unlink()
        return True
    return False


# ============== Run 存储 ==============

def get_run_dir(project_id: str) -> Path:
    """获取 run 目录"""
    return RUNS_DIR / project_id


def list_runs(project_id: Optional[str] = None) -> list[EvalRun]:
    """列出 runs"""
    runs = []
    if project_id:
        project_dir = RUNS_DIR / project_id
        if project_dir.exists():
            for path in project_dir.glob("*.json"):
                data = _read_json(path)
                runs.append(EvalRun(**data))
    else:
        for path in RUNS_DIR.rglob("*.json"):
            data = _read_json(path)
            runs.append(EvalRun(**data))

    # 按 created_at 倒序
    runs.sort(key=lambda x: x.created_at, reverse=True)
    return runs


def get_run(run_id: str, project_id: str) -> Optional[EvalRun]:
    """获取单个 run"""
    path = RUNS_DIR / project_id / f"{run_id}.json"
    if not path.exists():
        return None
    data = _read_json(path)
    return EvalRun(**data)


def save_run(run: EvalRun) -> None:
    """保存 run（同步写入）

    注意：run 执行循环内每 case 落盘请改用 async_save_run，避免同步文件 IO
    阻塞事件循环、且与 uvicorn --reload 的文件监控交互触发静默重启
    （BUG-1 根因：--reload 监控 data/ 写入触发 worker 重启，杀掉后台任务）。
    """
    path = RUNS_DIR / run.project_id / f"{run.id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, run.model_dump())


async def async_save_run(run: EvalRun) -> None:
    """异步保存 run（BUG-1 根治：把同步文件 IO 卸到线程池，不阻塞事件循环）

    - 评测执行循环内每 case 落盘使用此函数；
    - anyio.to_thread 在 asyncio 事件循环之外执行阻塞 IO，避免同步写文件
      拖住协程调度，同时不与 uvicorn --reload 的文件监控产生交互
      （写文件动作仍在主进程，但异步上下文不会让 reload 的 worker 静默重启
      卡死整个事件循环）。
    """
    await anyio.to_thread.run_sync(_save_run_sync, run)


def _save_run_sync(run: EvalRun) -> None:
    """async_save_run 的同步实现（在线程池中执行）"""
    path = RUNS_DIR / run.project_id / f"{run.id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, run.model_dump())


def get_project_last_run(project_id: str) -> Optional[EvalRun]:
    """获取项目最近一次 run"""
    runs = list_runs(project_id)
    return runs[0] if runs else None


def get_project_trend(project_id: str, limit: int = 8) -> list[dict]:
    """获取项目最近 N 次 run 的 pass_rate + total_token 趋势（按时间正序：旧→新）

    时序图惯例是左旧右新；list_runs 返回倒序（新→旧），取最近 N 次后反转。
    """
    runs = list_runs(project_id)
    runs = list(reversed(runs[:limit]))
    return [{
        "run_id": r.id,
        "pass_rate": r.summary.pass_rate if r.summary else 0,
        "total_token": r.summary.total_token if r.summary else 0,
        "judge_token": r.summary.judge_token if r.summary else 0,
        "created_at": r.created_at,
        "version_id": r.version_id,
        "status": r.status,
    } for r in runs]


# ============== T2-3: 配置模板存储 ==============

def _mask_target_config_secret(tc: dict) -> dict:
    """掩码 target_config 中的敏感字段（用于列表展示）"""
    out = dict(tc)
    if out.get("api_key"):
        out["api_key"] = "__MASKED__"
    auth = out.get("auth")
    if auth:
        if auth.get("bearer_token"):
            auth = dict(auth)
            auth["bearer_token"] = "__MASKED__"
            if auth.get("api_key_value"):
                auth["api_key_value"] = "__MASKED__"
            for c in auth.get("cookies", []):
                if c.get("value"):
                    c = dict(c)
                    c["value"] = "__MASKED__"
            out["auth"] = auth
    return out


def list_config_templates() -> list[dict]:
    """列出全部 Target 配置模板（api_key 等敏感字段 masked）"""
    if not CONFIG_TEMPLATES_FILE.exists():
        return []
    data = _read_json(CONFIG_TEMPLATES_FILE)
    templates = data.get("templates", [])
    # 对每条 target_config 做 secret 掩码
    for t in templates:
        if t.get("target_config"):
            t["target_config"] = _mask_target_config_secret(t["target_config"])
    return templates


def get_config_template(template_id: str) -> Optional[dict]:
    """获取单个配置模板（含完整 target_config，含 api_key 原值用于加载到其他项目）"""
    templates = list_config_templates_with_secrets()
    for t in templates:
        if t["id"] == template_id:
            return t
    return None


def list_config_templates_with_secrets() -> list[dict]:
    """列出全部配置模板（保留 secret，仅供加载模板时内部使用）"""
    if not CONFIG_TEMPLATES_FILE.exists():
        return []
    data = _read_json(CONFIG_TEMPLATES_FILE)
    return data.get("templates", [])


def save_config_template(name: str, target_config: TargetConfig) -> dict:
    """保存当前 target_config 为命名模板，返回新模板对象（含 masked 字段）"""
    templates = list_config_templates_with_secrets()
    new_id = f"tpl-{uuid.uuid4().hex[:8]}"
    new_tpl = {
        "id": new_id,
        "name": name,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "target_config": target_config.model_dump(),
    }
    templates.append(new_tpl)
    _write_json(CONFIG_TEMPLATES_FILE, {"templates": templates})
    # 返回时 mask
    new_tpl_masked = dict(new_tpl)
    new_tpl_masked["target_config"] = _mask_target_config_secret(new_tpl["target_config"])
    return new_tpl_masked


def delete_config_template(template_id: str) -> bool:
    """删除配置模板"""
    templates = list_config_templates_with_secrets()
    new_list = [t for t in templates if t["id"] != template_id]
    if len(new_list) == len(templates):
        return False  # 未找到
    _write_json(CONFIG_TEMPLATES_FILE, {"templates": new_list})
    return True


# ============== REQ-16: Judge 配置独立管理 ==============

def _mask_judge_config_secret(jc: dict) -> dict:
    """掩码 JudgeConfig 中的敏感字段（api_key / auth bearer_token / api_key_value / cookies value）

    用于列表展示与单条返回（与 target_config 同口径）
    """
    if not isinstance(jc, dict):
        return jc
    out = dict(jc)
    if out.get("api_key"):
        out["api_key"] = "__MASKED__"
    auth = out.get("auth")
    if auth and isinstance(auth, dict):
        auth = dict(auth)
        if auth.get("bearer_token"):
            auth["bearer_token"] = "__MASKED__"
        if auth.get("api_key_value"):
            auth["api_key_value"] = "__MASKED__"
        for c in auth.get("cookies", []):
            if c.get("value"):
                c = dict(c)
                c["value"] = "__MASKED__"
        out["auth"] = auth
    return out


def _mask_judge_config_item(item: dict) -> dict:
    """掩码 Judge 配置条目（外层 id/name/created_at + 内层 judge_config 的 secret）

    list_judge_configs / get_judge_config 用此函数（与 save_judge_config 返回结构一致）
    """
    if not isinstance(item, dict):
        return item
    out = dict(item)
    jc = out.get("judge_config")
    if jc and isinstance(jc, dict):
        out["judge_config"] = _mask_judge_config_secret(jc)
    return out


def _read_judge_configs() -> list[dict]:
    """读取全部 Judge 配置（带 secret，仅供内部读取/查找）"""
    if not JUDGE_CONFIGS_FILE.exists():
        return []
    data = _read_json(JUDGE_CONFIGS_FILE)
    return data.get("judge_configs", [])


def _write_judge_configs(judge_configs: list[dict]) -> None:
    """写入全部 Judge 配置"""
    _write_json(JUDGE_CONFIGS_FILE, {"judge_configs": judge_configs})


def list_judge_configs() -> list[dict]:
    """列出全部 Judge 配置（api_key 等敏感字段 masked）

    顺序：按 created_at 升序（旧→新），同 created_at 时按 id 排序
    """
    items = _read_judge_configs()
    items.sort(key=lambda x: (x.get("created_at", ""), x.get("id", "")))
    return [_mask_judge_config_item(x) for x in items]


def get_judge_config(judge_config_id: str) -> Optional[dict]:
    """获取单个 Judge 配置（masked，用于展示）"""
    for x in _read_judge_configs():
        if x.get("id") == judge_config_id:
            return _mask_judge_config_item(x)
    return None


def get_judge_config_with_secrets(judge_config_id: str) -> Optional[dict]:
    """获取单个 Judge 配置（含 secret 原值，供运行时 judge_with_llm 直接消费）"""
    for x in _read_judge_configs():
        if x.get("id") == judge_config_id:
            return x
    return None


def save_judge_config(name: str, judge_config: JudgeConfig) -> dict:
    """新建 Judge 配置

    - 生成 id 与 created_at
    - secret 原值落盘（便于跨项目引用）
    - 返回时 masked
    """
    items = _read_judge_configs()
    new_id = f"jc-{uuid.uuid4().hex[:8]}"
    new_item = {
        "id": new_id,
        "name": name,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "judge_config": judge_config.model_dump(),
    }
    items.append(new_item)
    _write_judge_configs(items)
    return _mask_judge_config_item(new_item)


def update_judge_config(judge_config_id: str, name: str, judge_config: JudgeConfig) -> Optional[dict]:
    """更新 Judge 配置（全量替换；secret 哨兵 __UNCHANGED__ 由调用方处理）"""
    items = _read_judge_configs()
    for i, x in enumerate(items):
        if x.get("id") == judge_config_id:
            x["name"] = name
            x["judge_config"] = judge_config.model_dump()
            items[i] = x
            _write_judge_configs(items)
            return _mask_judge_config_item(x)
    return None


def delete_judge_config(judge_config_id: str) -> bool:
    """删除 Judge 配置（不连带改项目里的 judge_config_id 引用——
    运行时 fallback 到内联 judge_config，与"配置丢失时回退"语义一致）"""
    items = _read_judge_configs()
    new_list = [x for x in items if x.get("id") != judge_config_id]
    if len(new_list) == len(items):
        return False
    _write_judge_configs(new_list)
    return True


def find_judge_config_by_name(name: str) -> Optional[dict]:
    """按名称查 Judge 配置（用于 REQ-17 同名覆盖检查；
    返回含 secret 原值供调用方比对）"""
    for x in _read_judge_configs():
        if x.get("name") == name:
            return x
    return None


# ============== V-1: 全局标签库存储 ==============

def _read_tags() -> list[dict]:
    """读取标签库原始数据"""
    if not TAGS_FILE.exists():
        return []
    data = _read_json(TAGS_FILE)
    return data.get("tags", [])


def _write_tags(tags: list[dict]) -> None:
    """写入标签库"""
    _write_json(TAGS_FILE, {"tags": tags})


def _migrate_legacy_tags() -> int:
    """扫描所有 evalset 的 case.tags，将未注册到全局标签库的标签自动补录。

    返回本次新注册的标签数量。幂等：重复调用无副作用。
    """
    existing = {t["name"] for t in _read_tags()}
    all_evalsets = list_evalsets()
    discovered = set()
    for es in all_evalsets:
        for c in es.cases:
            for name in (c.tags or []):
                if name:
                    discovered.add(name)
    missing = discovered - existing
    if not missing:
        return 0
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    tags = _read_tags()
    for name in sorted(missing):
        tags.append({"name": name, "created_at": now})
    _write_tags(tags)
    return len(missing)


def list_tags() -> list[dict]:
    """列出全部标签（含引用统计：case 数 / project 数）

    单次遍历全部 evalset case 统计每个标签的引用，O(total_case_count)。
    """
    # O-6: 自动迁移历史项目中未注册到全局标签库的标签
    _migrate_legacy_tags()
    tags = _read_tags()
    all_evalsets = list_evalsets()
    case_count = defaultdict(int)
    project_ids = defaultdict(set)
    for es in all_evalsets:
        for c in es.cases:
            for name in (c.tags or []):
                case_count[name] += 1
                project_ids[name].add(es.project_id)
    for t in tags:
        name = t["name"]
        t["case_count"] = case_count.get(name, 0)
        t["project_count"] = len(project_ids.get(name, set()))
        t["projects"] = sorted(project_ids.get(name, set()))
    return tags


def save_tag(name: str) -> dict:
    """新建标签（重名报错由调用方处理）"""
    tags = _read_tags()
    new_tag = {
        "name": name,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    tags.append(new_tag)
    _write_tags(tags)
    return new_tag


def rename_tag(old_name: str, new_name: str) -> dict:
    """改名：同步更新所有 evalset case 的标签字符串"""
    tags = _read_tags()
    # 更新标签库
    found = False
    for t in tags:
        if t["name"] == old_name:
            t["name"] = new_name
            found = True
            break
    if not found:
        return None
    _write_tags(tags)
    # 同步更新所有 evalset 中的 case tags
    all_evalsets = list_evalsets()
    affected_projects = set()
    for es in all_evalsets:
        changed = False
        for c in es.cases:
            if old_name in (c.tags or []):
                c.tags = [new_name if t == old_name else t for t in c.tags]
                changed = True
                affected_projects.add(es.project_id)
        if changed:
            save_evalset(es)
    return {"old_name": old_name, "new_name": new_name, "affected_projects": sorted(affected_projects)}


def delete_tag(name: str) -> dict:
    """删除：从所有 evalset case 移除该标签"""
    tags = _read_tags()
    new_list = [t for t in tags if t["name"] != name]
    if len(new_list) == len(tags):
        return None
    _write_tags(new_list)
    # 从所有 evalset 的 case tags 中移除
    all_evalsets = list_evalsets()
    affected_projects = set()
    for es in all_evalsets:
        changed = False
        for c in es.cases:
            if name in (c.tags or []):
                c.tags = [t for t in c.tags if t != name]
                changed = True
                affected_projects.add(es.project_id)
        if changed:
            save_evalset(es)
    return {"name": name, "affected_projects": sorted(affected_projects)}


def _utc_now() -> str:
    """获取当前 UTC 时间（ISO 8601 格式）"""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _project_file_mtime(project_id: str) -> str:
    """获取项目文件的修改时间作为旧项目的初始版本时间（fallback 到当前时间）"""
    path = PROJECTS_DIR / f"{project_id}.json"
    if path.exists():
        mtime = path.stat().st_mtime
        return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return _utc_now()


def migrate_missing_initial_versions() -> int:
    """W-7 追补：启动时扫描所有项目，为 versions 为空的项目补「初始版本」。

    创建时间优先用项目文件的 mtime（近似项目真实创建时间），否则用当前时间。
    返回被迁移的项目数量（仅日志用）。
    """
    count = 0
    for path in PROJECTS_DIR.glob("*.json"):
        try:
            data = _read_json(path)
        except Exception:
            continue
        versions = data.get("versions") or []
        if len(versions) > 0:
            continue  # 已有版本，跳过
        # 补初始版本
        project_id = path.stem
        created_at = _project_file_mtime(project_id)
        version_id = f"ver-{uuid.uuid4().hex[:8]}"
        data["versions"] = [{
            "id": version_id,
            "name": "初始版本",
            "created_at": created_at,
        }]
        _write_json(path, data)
        count += 1
    return count


# ============== Q-2: 模型价格（评测成本估算） ==============

def _read_model_prices() -> list[dict]:
    """读取全部模型价格"""
    if not MODEL_PRICES_FILE.exists():
        return []
    data = _read_json(MODEL_PRICES_FILE)
    return data.get("model_prices", [])


def _write_model_prices(prices: list[dict]) -> None:
    _write_json(MODEL_PRICES_FILE, {"model_prices": prices})


def list_model_prices() -> list[dict]:
    """列出全部模型价格（按 model_pattern 升序）"""
    prices = _read_model_prices()
    prices.sort(key=lambda x: x.get("model_pattern", ""))
    return prices


def save_model_price(endpoint_pattern: str, model_pattern: str, price_per_mtok: float,
                     currency: str = "¥", note: str = "",
                     peak_price_per_mtok: Optional[float] = None,
                     off_peak_price_per_mtok: Optional[float] = None,
                     peak_start_hour: int = 9, peak_end_hour: int = 22) -> dict:
    """新建模型价格（端点 + 模型双 key + Q-5 峰谷定价）"""
    prices = _read_model_prices()
    new_id = f"mp-{uuid.uuid4().hex[:8]}"
    new_item = {
        "id": new_id,
        "endpoint_pattern": endpoint_pattern,
        "model_pattern": model_pattern,
        "price_per_mtok": price_per_mtok,
        "peak_price_per_mtok": peak_price_per_mtok,
        "off_peak_price_per_mtok": off_peak_price_per_mtok,
        "peak_start_hour": peak_start_hour,
        "peak_end_hour": peak_end_hour,
        "currency": currency,
        "note": note,
    }
    prices.append(new_item)
    _write_model_prices(prices)
    return new_item


def update_model_price(price_id: str, fields: dict) -> Optional[dict]:
    """Q-5: 编辑模型价格（仅更新给定字段，未给字段保留原值）"""
    prices = _read_model_prices()
    for p in prices:
        if p.get("id") == price_id:
            for k, v in fields.items():
                if v is not None:
                    p[k] = v
            _write_model_prices(prices)
            return p
    return None


def delete_model_price(price_id: str) -> Optional[dict]:
    """删除模型价格"""
    prices = _read_model_prices()
    new_list = [p for p in prices if p.get("id") != price_id]
    if len(new_list) == len(prices):
        return None
    _write_model_prices(new_list)
    return {"id": price_id}


def _effective_peak_price(p: dict) -> float:
    """Q-5: 峰价 = peak_price_per_mtok（若有）否则 price_per_mtok（兼容旧条目迁移）"""
    pp = p.get("peak_price_per_mtok")
    return pp if pp is not None else p.get("price_per_mtok", 0)


def _effective_off_peak_price(p: dict) -> float:
    """Q-5: 谷价 = off_peak_price_per_mtok（若有）否则回退到峰价"""
    op = p.get("off_peak_price_per_mtok")
    if op is not None:
        return op
    return _effective_peak_price(p)


def _is_peak_hour(p: dict, hour: int) -> bool:
    """Q-5: 给定小时是否在峰时段 [peak_start_hour, peak_end_hour)"""
    start = p.get("peak_start_hour", 9)
    end = p.get("peak_end_hour", 22)
    # 支持跨午夜（start > end，如 22..6）
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def _select_price_for_hour(p: dict, hour: Optional[int]) -> float:
    """Q-5: 按 run 所在小时选峰/谷价；hour=None → 峰价（向后兼容旧调用）"""
    peak = _effective_peak_price(p)
    if hour is None:
        return peak
    return peak if _is_peak_hour(p, hour) else _effective_off_peak_price(p)


def _parse_hour(created_at: Optional[str]) -> Optional[int]:
    """Q-5: 从 ISO 时间串解析小时（解析失败返回 None → 走峰价兜底）"""
    if not created_at:
        return None
    try:
        from datetime import datetime
        ts = created_at.replace("Z", "+00:00")
        return datetime.fromisoformat(ts).hour
    except Exception:
        return None


def _match_model_price(endpoint: str, model_name: str) -> Optional[dict]:
    """端点 + 模型双 key 匹配（AND 逻辑，更具体的优先）

    - endpoint_pattern 为空 → 匹配任意端点（向后兼容）
    - model_pattern 为空 → 匹配任意模型
    - 两者都需命中；合计 pattern 长度更长 = 更具体 = 优先
    （endpoint 用子串匹配 `ep_pat in ep`，model 用前缀匹配 `startswith`）
    """
    if not model_name and not endpoint:
        return None
    prices = _read_model_prices()
    ep = (endpoint or "").rstrip("/")
    candidates = []
    for p in prices:
        ep_pat = p.get("endpoint_pattern", "")
        mod_pat = p.get("model_pattern", "")
        # endpoint 匹配（空 pattern = 通配）
        ep_ok = (not ep_pat) or (ep and ep_pat in ep)
        # model 匹配（空 pattern = 通配）
        mod_ok = (not mod_pat) or (model_name and model_name.startswith(mod_pat))
        if ep_ok and mod_ok:
            candidates.append(p)
    if not candidates:
        return None
    # 更具体的优先：endpoint_pattern + model_pattern 合计长度降序
    candidates.sort(
        key=lambda x: len(x.get("endpoint_pattern", "")) + len(x.get("model_pattern", "")),
        reverse=True,
    )
    return candidates[0]


def cost_estimate(target_endpoint: Optional[str], target_model: Optional[str],
                  judge_endpoint: Optional[str], judge_model: Optional[str],
                  total_token: int, judge_token: int,
                  run_created_at: Optional[str] = None) -> dict:
    """Q-2/Q-5: run 成本估算（端点 + 模型双 key 各自独立查价 + 峰谷按时段选价）

    - target_cost: 运行 token × target 端点+模型价格 / 1e6
    - judge_cost:  评测 token × judge 端点+模型价格 / 1e6
    - Q-5: run_created_at 解析小时 → 按价格条目的峰谷时段选价；未提供时间 → 峰价兜底
    - 任一端无匹配价格 → 对应字段为 None；货币随匹配到的价格
    返回 {target_cost, judge_cost, total_cost, currency}
    """
    result = {"target_cost": None, "judge_cost": None, "total_cost": None, "currency": None}
    hour = _parse_hour(run_created_at)
    target_price = _match_model_price(target_endpoint or "", target_model or "") if (target_endpoint or target_model) else None
    judge_price = _match_model_price(judge_endpoint or "", judge_model or "") if (judge_endpoint or judge_model) else None
    if target_price:
        t_price = _select_price_for_hour(target_price, hour)
        result["target_cost"] = round(total_token / 1_000_000 * t_price, 4)
        result["currency"] = target_price.get("currency", "¥")
    if judge_price:
        j_price = _select_price_for_hour(judge_price, hour)
        result["judge_cost"] = round(judge_token / 1_000_000 * j_price, 4)
        if result["currency"] is None:
            result["currency"] = judge_price.get("currency", "¥")
    if result["target_cost"] is not None or result["judge_cost"] is not None:
        result["total_cost"] = round((result["target_cost"] or 0) + (result["judge_cost"] or 0), 4)
    return result


# ============== Q-6: 批量预估 ==============

def _percentile(sorted_vals: list[float], p: float) -> float:
    """线性插值分位数（p ∈ [0,1]）。空列表返回 0.0。"""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    s = sorted(sorted_vals)
    rank = p * (len(s) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(s) - 1)
    frac = rank - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def _is_skipped_sample(cr: dict) -> bool:
    """Q-6: 跳过类 case = skipped_reason 非空或 token 全为 0"""
    if cr.get("skipped_reason"):
        return True
    return (cr.get("token_used", 0) or 0) == 0 and (cr.get("judge_token", 0) or 0) == 0


def batch_estimate(project: Project, count: int,
                   plan_hour: Optional[int] = None,
                   version_id: Optional[str] = None,
                   concurrency: int = 1) -> dict:
    """Q-6: 基于 case 级历史样本预估批量任务的成本/用时区间（Q-8: 移除标签筛选；Q-9: 剔除 Judge）

    - 样本 = 每条 case 每次执行（CaseResult 级，非 run 级）
    - 仅取 project_id 下 completed run；version_id 给定时按版本过滤，否则按 current_version_id
    - 跳过类 case（skipped_reason 非空或 token 全 0）分离统计：skipped_ratio
    - 单样本成本 = token_used / 1e6 × 选定时段价（Q-9: 仅 target，Judge 是评测仪器不计入生产成本；
      target 端点+模型匹配价格；plan_hour 给定时按峰/谷选价，None → 峰价兜底）
    - 单样本时长 = latency_ms（毫秒）
    - 区间 = P5/P50/P95 × N；time 区间按并发度 ÷ concurrency
    - 最小样本量：< 30 拒绝（low_confidence=False，error）；30~100 低置信标注
    - 跨 ≥ 2 次 run 校验（防单次系统偏差冒充多样性）

    返回 {cost: {median, p5, p95, currency}, time: {median, p5, p95, unit},
           skipped_ratio, sample_count, run_count, low_confidence, note}
    """
    if count <= 0:
        return {"error": "invalid_count", "message": "count 必须 > 0"}
    if concurrency < 1:
        concurrency = 1

    runs = list_runs(project.id)
    completed = [r for r in runs if r.status == "completed"]
    # 版本作用域：显式 > current_version_id > 全量（None）
    target_vid = version_id or project.current_version_id
    if target_vid:
        scoped = [r for r in completed if r.version_id == target_vid]
        completed = scoped if scoped else completed  # 没数据则回退全量避免空结果

    # 收集 case 级样本
    exec_samples_token: list[int] = []  # 每条 case 的 token_used（Q-9: 仅 target，剔除 judge）
    exec_samples_latency: list[float] = []  # 每条 case 的 latency_ms
    skipped_count = 0
    seen_run_ids: set[str] = set()
    for r in completed:
        seen_run_ids.add(r.id)
        for cr in (r.results or []):
            cr_dict = cr.model_dump() if hasattr(cr, "model_dump") else cr
            if _is_skipped_sample(cr_dict):
                skipped_count += 1
                continue
            t = cr_dict.get("token_used") or 0
            exec_samples_token.append(t)
            exec_samples_latency.append(cr_dict.get("latency_ms") or 0.0)

    total_samples = len(exec_samples_token) + skipped_count
    run_count = len(seen_run_ids)
    skipped_ratio = round(skipped_count / total_samples, 4) if total_samples else 0.0

    # 最小样本量门槛
    if total_samples < 30:
        return {
            "error": "insufficient_samples",
            "message": f"样本量不足：当前 {total_samples} 条，需 ≥ 30 条 case 样本（含跨 ≥ 2 次 run）才能预估",
            "sample_count": total_samples,
            "run_count": run_count,
        }
    low_confidence = total_samples < 100
    if run_count < 2:
        return {
            "error": "insufficient_runs",
            "message": f"样本仅来自 {run_count} 次 run，需跨 ≥ 2 次 run 才能预估（防单次系统偏差）",
            "sample_count": total_samples,
            "run_count": run_count,
        }

    # 价格匹配：仅 target（Q-9: Judge 是评测仪器，生产成本不含 judge）
    target_ep = getattr(project.target_config, "base_url", None)
    target_mod = getattr(project.target_config, "model", None)

    # 每样本成本：token × 价格 / 1e6；plan_hour 给定 → 按价格条目峰谷选价
    target_price = _match_model_price(target_ep or "", target_mod or "") if (target_ep or target_mod) else None
    t_price = _select_price_for_hour(target_price, plan_hour) if target_price else None
    currency = (target_price or {}).get("currency", "¥")

    def _sample_cost(target_tok: int) -> Optional[float]:
        if t_price is None:
            return None
        return target_tok / 1_000_000 * t_price

    # 仅取 target token（CaseResult.token_used = target 消耗）
    cost_samples: list[float] = []
    for r in completed:
        for cr in (r.results or []):
            cr_dict = cr.model_dump() if hasattr(cr, "model_dump") else cr
            if _is_skipped_sample(cr_dict):
                continue
            target_tok = cr_dict.get("token_used") or 0
            c = _sample_cost(target_tok)
            if c is None:
                continue
            cost_samples.append(c)

    # 区间 = 分位 × N；时间区间按并发度 ÷ concurrency（毫秒 → 秒）
    cost_median = _percentile(cost_samples, 0.50) * count
    cost_p5 = _percentile(cost_samples, 0.05) * count
    cost_p95 = _percentile(cost_samples, 0.95) * count

    time_median_s = _percentile(exec_samples_latency, 0.50) * count / 1000.0 / concurrency
    time_p5_s = _percentile(exec_samples_latency, 0.05) * count / 1000.0 / concurrency
    time_p95_s = _percentile(exec_samples_latency, 0.95) * count / 1000.0 / concurrency

    note_parts = []
    if low_confidence:
        note_parts.append(f"样本量 {total_samples} < 100，区间为低置信估计")
    if skipped_ratio > 0:
        note_parts.append(f"含跳过 case 比例 {skipped_ratio * 100:.1f}%（已分离统计，不计入区间）")
    if target_vid:
        v = next((v for v in (project.versions or []) if v.id == target_vid), None)
        if v:
            note_parts.append(f"按版本「{v.name}」作用域采样")
    note = "；".join(note_parts) if note_parts else None

    return {
        "cost": {
            "median": round(cost_median, 2),
            "p5": round(cost_p5, 2),
            "p95": round(cost_p95, 2),
            "currency": currency,
        },
        "time": {
            "median": round(time_median_s, 2),
            "p5": round(time_p5_s, 2),
            "p95": round(time_p95_s, 2),
            "unit": "seconds",
        },
        "skipped_ratio": skipped_ratio,
        "sample_count": total_samples,
        "run_count": run_count,
        "low_confidence": low_confidence,
        "note": note,
    }


# ============== Q-7: 质量-成本闭环预估 ==============

def _peak_window_length(p: dict) -> float:
    """峰时段长度（小时），支持跨午夜"""
    start = p.get("peak_start_hour", 9)
    end = p.get("peak_end_hour", 22)
    if start <= end:
        return end - start
    return 24 - start + end


def _effective_price_for_mode(price: Optional[dict], mode: str) -> Optional[float]:
    """Q-7: 按 time_mode 返回有效单价

    - peak: 峰价
    - off_peak: 谷价
    - mixed: 峰谷加权（按峰/谷时段占比）
    """
    if price is None:
        return None
    peak = _effective_peak_price(price)
    off = _effective_off_peak_price(price)
    if mode == "off_peak":
        return off
    if mode == "mixed":
        pw = _peak_window_length(price)
        peak_frac = pw / 24.0
        off_frac = 1.0 - peak_frac
        return peak * peak_frac + off * off_frac
    return peak


def batch_estimate_quality_cost(project: Project, count: int,
                                target_pass_rate: float = 1.0,
                                rerun_strategy: str = "failed_only",
                                time_mode: str = "peak",
                                production_concurrency: int = 1,
                                version_id: Optional[str] = None) -> dict:
    """Q-7: 质量-成本闭环预估——回答"达到目标正确率总共要花多少钱、多少时间"（Q-9: 剔除 Judge）

    - p = 版本内历史 pass rate（非跳过 case 中 passed 比例）
    - p 不确定性：正态近似 90% CI（z=1.645）→ 总执行数区间
    - 仅失败重跑：总执行数 = N/p；全部重跑：N × ceil(log(1-目标)/log(1-p))
    - 成本区间 = 总执行数 × 单样本成本分布（P5/P50/P95）× 时段有效价
      （Q-9: 单样本成本仅取 token_used（target），Judge 是评测仪器不计入生产成本）
    - 时长区间 = 总执行数 × 延迟分布 ÷ 生产并发度；仅谷时含停产
    - note[] 含并发不一致提示、准确度条件、对比陈述

    返回 {rounds_expected, total_cost: {median, p5, p95, currency},
           total_time: {median, p5, p95}, skipped_ratio, sample_count,
           run_count, low_confidence, note[]}
    """
    if count <= 0:
        return {"error": "invalid_count", "message": "count 必须 > 0"}
    if not (0 < target_pass_rate <= 1.0):
        return {"error": "invalid_target_pass_rate",
                "message": "目标正确率必须在 (0, 1] 区间"}
    if production_concurrency < 1:
        production_concurrency = 1

    runs = list_runs(project.id)
    completed = [r for r in runs if r.status == "completed"]
    target_vid = version_id or project.current_version_id
    if target_vid:
        scoped = [r for r in completed if r.version_id == target_vid]
        completed = scoped if scoped else completed

    # 收集 case 级样本 + pass 统计
    exec_samples_token: list[int] = []
    exec_samples_latency: list[float] = []
    passed_count = 0
    skipped_count = 0
    seen_run_ids: set[str] = set()
    for r in completed:
        seen_run_ids.add(r.id)
        for cr in (r.results or []):
            cr_dict = cr.model_dump() if hasattr(cr, "model_dump") else cr
            if _is_skipped_sample(cr_dict):
                skipped_count += 1
                continue
            t = cr_dict.get("token_used") or 0
            exec_samples_token.append(t)
            exec_samples_latency.append(cr_dict.get("latency_ms") or 0.0)
            if cr_dict.get("passed"):
                passed_count += 1

    non_skipped = len(exec_samples_token)
    total_samples = non_skipped + skipped_count
    run_count = len(seen_run_ids)
    skipped_ratio = round(skipped_count / total_samples, 4) if total_samples else 0.0

    if total_samples < 30:
        return {
            "error": "insufficient_samples",
            "message": f"样本量不足：当前 {total_samples} 条，需 ≥ 30 条 case 样本（含跨 ≥ 2 次 run）才能预估",
            "sample_count": total_samples,
            "run_count": run_count,
        }
    low_confidence = total_samples < 100
    if run_count < 2:
        return {
            "error": "insufficient_runs",
            "message": f"样本仅来自 {run_count} 次 run，需跨 ≥ 2 次 run 才能预估（防单次系统偏差）",
            "sample_count": total_samples,
            "run_count": run_count,
        }
    if non_skipped == 0:
        return {"error": "all_skipped", "message": "所有样本均为跳过类，无法预估通过率"}

    # 通过率 p + 90% CI（正态近似 z=1.645）
    p = passed_count / non_skipped
    z = 1.645
    margin = z * math.sqrt(p * (1 - p) / non_skipped) if non_skipped > 0 else 0.0
    p_lower = max(0.001, p - margin)
    p_upper = min(1.0, p + margin)

    # 总执行数区间（按策略）
    if rerun_strategy == "failed_only":
        # 总执行数 = N/p
        if p <= 0:
            return {"error": "zero_pass_rate", "message": "历史通过率为 0，无法预估（仅失败重跑需无穷多次）"}
        total_exec_p50 = count / p
        total_exec_p5 = count / p_upper if p_upper > 0 else count / 0.001
        total_exec_p95 = count / p_lower
        rounds_expected = round(1.0 / p, 2)
    else:  # "all"
        if target_pass_rate >= 1.0:
            return {"error": "infinite_rounds",
                    "message": "目标正确率 100% 在「全部重跑」策略下需无穷多轮；请降低目标正确率或改用「仅失败重跑」"}
        log_target = math.log(1 - target_pass_rate)
        if p >= 1.0:
            k_p50 = 1
        else:
            k_p50 = max(1, math.ceil(log_target / math.log(1 - p)))
        k_p5 = max(1, math.ceil(log_target / math.log(1 - p_upper))) if p_upper < 1.0 else 1
        k_p95 = max(1, math.ceil(log_target / math.log(1 - p_lower))) if p_lower < 1.0 else 1
        total_exec_p50 = count * k_p50
        total_exec_p5 = count * k_p5
        total_exec_p95 = count * k_p95
        rounds_expected = k_p50

    # 价格匹配（Q-9: 仅 target；Judge 是评测仪器，不计入生产成本）
    target_ep = getattr(project.target_config, "base_url", None)
    target_mod = getattr(project.target_config, "model", None)

    target_price = _match_model_price(target_ep or "", target_mod or "") if (target_ep or target_mod) else None
    currency = (target_price or {}).get("currency", "¥")

    # 时段有效价
    t_eff = _effective_price_for_mode(target_price, time_mode)

    # 单样本成本 = token_used（仅 target）× 时段有效价 / 1e6
    cost_samples: list[float] = []
    if t_eff is not None:
        for r in completed:
            for cr in (r.results or []):
                cr_dict = cr.model_dump() if hasattr(cr, "model_dump") else cr
                if _is_skipped_sample(cr_dict):
                    continue
                target_tok = cr_dict.get("token_used") or 0
                cost_samples.append(target_tok / 1_000_000 * t_eff)

    # 成本区间 = 总执行数 × 单样本成本分位（两个不确定性同向复合）
    if cost_samples:
        cost_p50 = total_exec_p50 * _percentile(cost_samples, 0.50)
        cost_p5 = total_exec_p5 * _percentile(cost_samples, 0.05)
        cost_p95 = total_exec_p95 * _percentile(cost_samples, 0.95)
    else:
        cost_p50 = cost_p5 = cost_p95 = 0.0

    # 时长区间（毫秒 → 秒，按生产并发分摊）
    lat_p50 = _percentile(exec_samples_latency, 0.50)
    lat_p5 = _percentile(exec_samples_latency, 0.05)
    lat_p95 = _percentile(exec_samples_latency, 0.95)
    time_p50_s = total_exec_p50 * lat_p50 / 1000.0 / production_concurrency
    time_p5_s = total_exec_p5 * lat_p5 / 1000.0 / production_concurrency
    time_p95_s = total_exec_p95 * lat_p95 / 1000.0 / production_concurrency

    # 仅谷时模式：总时长含停产（E + (ceil(E/W) - 1) × P）
    if time_mode == "off_peak" and target_price:
        pw = _peak_window_length(target_price)
        valley_h = 24.0 - pw
        peak_h = pw
        valley_s = valley_h * 3600
        peak_s = peak_h * 3600
        if valley_s > 0:
            if time_p50_s > valley_s:
                time_p50_s = time_p50_s + (math.ceil(time_p50_s / valley_s) - 1) * peak_s
            if time_p5_s > valley_s:
                time_p5_s = time_p5_s + (math.ceil(time_p5_s / valley_s) - 1) * peak_s
            if time_p95_s > valley_s:
                time_p95_s = time_p95_s + (math.ceil(time_p95_s / valley_s) - 1) * peak_s

    # 对比陈述：基线（单轮 N 条）vs 目标方案
    baseline_cost = count * _percentile(cost_samples, 0.50) if cost_samples else 0.0
    baseline_time = count * lat_p50 / 1000.0 / production_concurrency
    extra_cost = cost_p50 - baseline_cost
    extra_time_min = (time_p50_s - baseline_time) / 60.0
    baseline_fail = (1 - p) * 100
    target_fail = (1 - target_pass_rate) * 100

    # notes
    notes: list[str] = []
    # 采集并发 vs 生产并发（用 project.max_concurrency 作为采集并发的代理）
    coll_conc = getattr(project, "max_concurrency", 1) or 1
    if coll_conc != production_concurrency:
        notes.append(f"评测数据在并发 {coll_conc} 下采集，与生产并发 {production_concurrency} 不一致，延迟估计可能偏差")
    # 准确度条件
    notes.append(f"准确度基于评测集 {non_skipped} 条估计，生产流量分布可能与评测集不同，建议定期用生产样本扩充评测集")
    # 版本作用域
    if target_vid:
        v = next((v for v in (project.versions or []) if v.id == target_vid), None)
        if v:
            notes.append(f"按版本「{v.name}」作用域采样")
    # 低置信
    if low_confidence:
        notes.append(f"样本量 {total_samples} < 100，区间为低置信估计")
    # 跳过比例
    if skipped_ratio > 0:
        notes.append(f"含跳过 case 比例 {skipped_ratio * 100:.1f}%（已分离统计，不计入区间）")
    # 对比陈述
    notes.append(f"多花 {currency}{extra_cost:.2f}、多跑 {extra_time_min:.1f} 分钟，把失败率从 {baseline_fail:.1f}% 压到 {target_fail:.1f}%")

    return {
        "rounds_expected": rounds_expected,
        "total_cost": {
            "median": round(cost_p50, 2),
            "p5": round(cost_p5, 2),
            "p95": round(cost_p95, 2),
            "currency": currency,
        },
        "total_time": {
            "median": round(time_p50_s, 2),
            "p5": round(time_p5_s, 2),
            "p95": round(time_p95_s, 2),
        },
        "skipped_ratio": skipped_ratio,
        "sample_count": total_samples,
        "run_count": run_count,
        "low_confidence": low_confidence,
        "pass_rate": round(p, 4),
        "note": notes,
    }
