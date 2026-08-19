"""simpleEval FastAPI 入口"""

from fastapi import FastAPI

from .routes import router

app = FastAPI(
    title="simpleEval",
    description="LLM 与 Agent（黑盒）评测工具",
    version="0.1.0",
)

app.include_router(router)
