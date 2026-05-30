"""FastAPI 入口 —— 暴露 REST API 供 Streamlit 前端调用。

启动方式（先激活 venv）:

    # 在项目根目录执行:
    .venv/Scripts/activate
    cd ai-pr-review
    uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000

    # 或不用 activate，直接用 venv 里的 python:
    ../.venv/Scripts/python.exe -m uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000

    ⚠ 若 --reload 报错（Python 3.13 + Windows 的 watchfiles 兼容问题），可先去掉 --reload。

端点:
    GET  /api/health  →  健康检查
    POST /api/review  →  PR 审查（同步阻塞，最长需数十秒）
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.github_client import GitHubAPIError
from backend.llm_client import LLMError
from backend.models import ReviewRequest, ReviewResponse
from backend.orchestrator import run_review

# ======================================================================
# 应用
# ======================================================================

app = FastAPI(
    title="AI PR Review Assistant",
    version="0.1.0",
    description="粘贴公开 GitHub PR URL，AI 自动产出变更总结 + 结构化风险 Findings",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8501",   # Streamlit 默认端口
        "http://localhost:8502",
        "http://127.0.0.1:8501",
        "http://127.0.0.1:8502",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ======================================================================
# 统一异常处理
# ======================================================================

@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": str(exc)},
    )


@app.exception_handler(GitHubAPIError)
async def github_error_handler(request: Request, exc: GitHubAPIError) -> JSONResponse:
    status_code = 404 if "不存在" in str(exc) or "私有" in str(exc) else 403
    return JSONResponse(
        status_code=status_code,
        content={"error": str(exc)},
    )


@app.exception_handler(LLMError)
async def llm_error_handler(request: Request, exc: LLMError) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={"error": str(exc)},
    )


@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"error": f"服务内部错误: {type(exc).__name__}: {exc}"},
    )


# ======================================================================
# 端点
# ======================================================================

@app.get("/api/health")
async def health_check():
    """健康检查。"""
    return {"status": "ok"}


@app.post("/api/review", response_model=ReviewResponse)
async def review_pr(req: ReviewRequest):
    """对公开 GitHub PR 执行 AI 审查。

    接收 ReviewRequest（含 pr_url 与可选参数），返回 ReviewResponse
    （含 summary / findings / file_metas / stats / warnings）。

    此接口为同步阻塞调用，大型 PR 可能耗时 30-120 秒。
    """
    return await run_review(req)


# ======================================================================
# 自测（仅直接运行时）
# ======================================================================
if __name__ == "__main__":
    import uvicorn

    print("启动 FastAPI 服务: http://localhost:8000")
    print("  健康检查: http://localhost:8000/api/health")
    print("  审查接口: POST http://localhost:8000/api/review")
    print()
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
