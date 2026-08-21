"""JSON 文件存储模块"""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import Project, EvalSet, EvalRun, TargetConfig, JudgeConfig


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
    import anyio
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
