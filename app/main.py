"""simpleEval FastAPI 入口"""

from fastapi import FastAPI

from .models import Project, EvalSet
from .runner import run_evalset

app = FastAPI(title="simpleEval", description="LLM 与 Agent（黑盒）评测工具")


@app.post("/api/run")
async def run(project: Project, evalset: EvalSet):
    """跑一次评测，返回完整 EvalRun（结果 + 汇总 + 成本对比）"""
    return await run_evalset(project, evalset)


@app.get("/health")
async def health():
    return {"status": "ok"}
