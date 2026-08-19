"""simpleEval FastAPI 入口"""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path

from .routes import router

app = FastAPI(
    title="simpleEval",
    description="LLM 与 Agent（黑盒）评测工具",
    version="0.1.0",
)

app.include_router(router)

# 挂载静态文件
static_path = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_path), html="index.html"))


# 根路径返回 index.html
@app.get("/")
async def root():
    index_path = static_path / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return {"message": "simpleEval API", "docs": "/docs"}
