"""T3-4: 定时回归调度器

- cron_match: 5 字段标准 cron 表达式匹配（分 时 日 月 周）
- detect_regression: 对比 run pass_rate 与 baseline（上次 completed run）
- check_and_trigger_scheduled_runs: 遍历项目，到点自动发起 run
- get_regression_alerts: 查询项目的回归告警

cron 支持格式：* / 具体数字 / 逗号列表 / 范围(a-b) / 步进(*/n 或 a-b/n)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from .models import Project, EvalRun, CaseResult
from .storage import (
    list_projects, list_runs, get_run, save_run, get_evalset, get_project,
    list_evalsets,
)
from .runner import execute_run, _generate_run_id, _utc_now, _apply_case_filter


logger = logging.getLogger(__name__)

# 后台定时 run task 引用集合（防 GC + 便于 done callback 清理）
_pending_scheduled_tasks: set = set()


def _on_scheduled_task_done(task: asyncio.Task) -> None:
    """后台定时 run 任务的完成回调：从 _pending_scheduled_tasks 移除并记录异常。

    - cancelled：调度循环退出时取消，正常清理即可
    - 异常：通过 task.exception() 取回避免 "Task exception was never retrieved"
    """
    _pending_scheduled_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("定时回归 run 执行失败: %s", exc)


def _parse_cron_field(field: str, min_val: int, max_val: int) -> set[int]:
    """解析单个 cron 字段为匹配值集合。

    支持：* / N / */N / a-b / a-b/N / a,b,c / 混合
    """
    result = set()
    for part in field.split(","):
        part = part.strip()
        if part == "*":
            result.update(range(min_val, max_val + 1))
        elif "/" in part:
            range_part, step_str = part.split("/", 1)
            step = int(step_str)
            if range_part == "*":
                vals = range(min_val, max_val + 1, step)
            elif "-" in range_part:
                lo, hi = range_part.split("-")
                vals = range(int(lo), int(hi) + 1, step)
            else:
                vals = range(int(range_part), max_val + 1, step)
            result.update(vals)
        elif "-" in part:
            lo, hi = part.split("-")
            result.update(range(int(lo), int(hi) + 1))
        else:
            result.add(int(part))
    return result


def cron_match(cron_expr: str, dt: datetime) -> bool:
    """判断 cron 表达式是否匹配给定时间（UTC）。

    5 字段：分(0-59) 时(0-23) 日(1-31) 月(1-12) 周(0-6, 0=周日)
    """
    fields = cron_expr.split()
    if len(fields) != 5:
        return False
    minute_set = _parse_cron_field(fields[0], 0, 59)
    hour_set = _parse_cron_field(fields[1], 0, 23)
    day_set = _parse_cron_field(fields[2], 1, 31)
    month_set = _parse_cron_field(fields[3], 1, 12)
    # cron 周 0=周日；Python weekday() 周一=0..周日=6
    cron_dow = (dt.weekday() + 1) % 7  # 转换：Python周一0 → cron周日0
    dow_set = _parse_cron_field(fields[4], 0, 6)
    return (
        dt.minute in minute_set
        and dt.hour in hour_set
        and dt.day in day_set
        and dt.month in month_set
        and cron_dow in dow_set
    )


def _get_baseline_pass_rate(project_id: str, exclude_run_id: Optional[str] = None) -> Optional[float]:
    """获取 baseline pass_rate（排除指定 run 后的最近一次 completed run 的 pass_rate）。

    无历史 run 时返回 None。
    """
    runs = list_runs(project_id)
    completed = [
        r for r in runs
        if r.status == "completed" and r.summary and r.id != exclude_run_id
    ]
    if not completed:
        return None
    # 取最近一次（created_at 降序第一个）
    completed.sort(key=lambda r: r.created_at, reverse=True)
    return completed[0].summary.pass_rate


def detect_regression(
    project_id: str, run_id: str, threshold: float = 0.1
) -> Optional[dict]:
    """检测 run 是否发生回归（pass_rate 相对 baseline 降幅超过阈值）。

    Returns:
        None = 无回归或无 baseline；
        dict = 回归详情 {run_id, pass_rate, baseline_pass_rate, delta, threshold}
    """
    run = get_run(run_id, project_id)
    if not run or run.status != "completed" or not run.summary:
        return None
    baseline = _get_baseline_pass_rate(project_id, exclude_run_id=run_id)
    if baseline is None:
        return None
    delta = run.summary.pass_rate - baseline
    if delta < -threshold:
        return {
            "run_id": run_id,
            "pass_rate": run.summary.pass_rate,
            "baseline_pass_rate": baseline,
            "delta": round(delta, 4),
            "threshold": threshold,
        }
    return None


def get_regression_alerts(project_id: str) -> list[dict]:
    """查询项目的回归告警：检查最近一次 completed run 是否回归。

    返回告警列表（空 = 无告警）。
    """
    project = get_project(project_id)
    if not project or not project.schedule or not project.schedule.enabled:
        return []
    threshold = project.schedule.regression_threshold
    runs = list_runs(project_id)
    completed = [
        r for r in runs
        if r.status == "completed" and r.summary
    ]
    if not completed:
        return []
    completed.sort(key=lambda r: r.created_at, reverse=True)
    latest = completed[0]
    alert = detect_regression(project_id, latest.id, threshold)
    if alert:
        return [alert]
    return []


async def check_and_trigger_scheduled_runs() -> list[str]:
    """遍历所有项目，对 schedule.enabled 且 cron 匹配当前时间的项目发起 run。

    返回触发的 run_id 列表。
    """
    now = datetime.now(timezone.utc)
    triggered = []
    for project in list_projects():
        if not project.schedule or not project.schedule.enabled:
            continue
        if not cron_match(project.schedule.cron, now):
            continue
        # 找项目的第一个评测集
        evalsets = list_evalsets(project.id)
        if not evalsets:
            continue
        evalset = evalsets[0]
        # 检查是否有启用的 case
        enabled_cases = [c for c in evalset.cases if c.enabled]
        if not enabled_cases:
            continue
        # 按标签筛选
        if project.schedule.tags:
            from .models import CaseFilter
            case_filter = CaseFilter(tags=project.schedule.tags, mode="any")
            filtered = _apply_case_filter(enabled_cases, case_filter)
            if not filtered:
                continue
        else:
            case_filter = None
        # 创建 run
        run_id = _generate_run_id()
        run_created_at = _utc_now()
        from .routes import _resolve_version_id
        version_id = _resolve_version_id(
            project, run_created_at, project.schedule.version_id
        )
        run = EvalRun(
            id=run_id,
            project_id=project.id,
            evalset_id=evalset.id,
            status="queued",
            created_at=run_created_at,
            version_id=version_id,
            filter_tags=project.schedule.tags if project.schedule.tags else [],
            trigger="scheduled",
        )
        save_run(run)
        # 后台执行（不阻塞调度循环；用项目默认的 samples=1 / concurrency=None）
        task = asyncio.create_task(
            execute_run(run, project, evalset, case_filter, 1, None)
        )
        _pending_scheduled_tasks.add(task)
        task.add_done_callback(_on_scheduled_task_done)
        triggered.append(run_id)
    return triggered
