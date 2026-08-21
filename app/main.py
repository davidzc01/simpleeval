"""simpleEval FastAPI 入口"""

import asyncio
import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

from .routes import router
from .storage import list_runs, save_run
from .runner import _utc_now

app = FastAPI(
    title="simpleEval",
    description="LLM 与 Agent（黑盒）评测工具",
    version="0.1.0",
)

app.include_router(router)

# 挂载静态文件
static_path = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_path), html="index.html"))

logger = logging.getLogger(__name__)

# T3-4: 定时回归调度任务句柄（全局引用防止被 GC）
_scheduler_task = None


async def _scheduled_regression_loop():
    """T3-4: 每 60 秒检查项目定时规则，到点自动发起 run。

    规则持久化在 Project.schedule（JSON 文件），服务重启后自动恢复。
    """
    while True:
        try:
            from .scheduler import check_and_trigger_scheduled_runs
            await check_and_trigger_scheduled_runs()
        except Exception as e:
            logger.warning("定时回归调度异常: %s", e)
        await asyncio.sleep(60)


@app.on_event("startup")
async def reclaim_orphan_runs():
    """B-19: 启动时回收僵尸 run。

    BackgroundTasks 跑在 uvicorn 进程内，服务重启/崩溃会让 running/queued
    状态的 run 永久停滞。启动时扫描全部 run，把 running/queued 的置为 failed，
    error = "服务重启导致任务中断"。
    """
    try:
        all_runs = list_runs()
    except Exception:
        return  # 存储 未初始化时跳过
    for run in all_runs:
        if run.status in ("running", "queued"):
            run.status = "failed"
            run.error = "服务重启导致任务中断"
            run.finished_at = _utc_now()
            save_run(run)

    # T3-4: 启动定时回归调度循环
    global _scheduler_task
    if _scheduler_task is None or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(_scheduled_regression_loop())


# 根路径返回 index.html
@app.get("/")
async def root():
    index_path = static_path / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return {"message": "simpleEval API", "docs": "/docs"}
