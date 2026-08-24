"""JSON 文件存储模块"""

import json
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


def save_model_price(endpoint_pattern: str, model_pattern: str, price_per_mtok: float, currency: str = "¥", note: str = "") -> dict:
    """新建模型价格（端点 + 模型双 key）"""
    prices = _read_model_prices()
    new_id = f"mp-{uuid.uuid4().hex[:8]}"
    new_item = {
        "id": new_id,
        "endpoint_pattern": endpoint_pattern,
        "model_pattern": model_pattern,
        "price_per_mtok": price_per_mtok,
        "currency": currency,
        "note": note,
    }
    prices.append(new_item)
    _write_model_prices(prices)
    return new_item


def delete_model_price(price_id: str) -> Optional[dict]:
    """删除模型价格"""
    prices = _read_model_prices()
    new_list = [p for p in prices if p.get("id") != price_id]
    if len(new_list) == len(prices):
        return None
    _write_model_prices(new_list)
    return {"id": price_id}


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
                  total_token: int, judge_token: int) -> dict:
    """Q-2: run 成本估算（端点 + 模型双 key 各自独立查价）

    - target_cost: 运行 token × target 端点+模型价格 / 1e6
    - judge_cost:  评测 token × judge 端点+模型价格 / 1e6
    - 任一端无匹配价格 → 对应字段为 None；货币随匹配到的价格
    返回 {target_cost, judge_cost, total_cost, currency}
    """
    result = {"target_cost": None, "judge_cost": None, "total_cost": None, "currency": None}
    target_price = _match_model_price(target_endpoint or "", target_model or "") if (target_endpoint or target_model) else None
    judge_price = _match_model_price(judge_endpoint or "", judge_model or "") if (judge_endpoint or judge_model) else None
    if target_price:
        result["target_cost"] = round(total_token / 1_000_000 * target_price["price_per_mtok"], 4)
        result["currency"] = target_price["currency"]
    if judge_price:
        result["judge_cost"] = round(judge_token / 1_000_000 * judge_price["price_per_mtok"], 4)
        if result["currency"] is None:
            result["currency"] = judge_price["currency"]
    if result["target_cost"] is not None or result["judge_cost"] is not None:
        result["total_cost"] = round((result["target_cost"] or 0) + (result["judge_cost"] or 0), 4)
    return result
