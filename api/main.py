import os

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.errors import ApiError
from api.routers import ask, auth, feedback, github, repos, webhooks

app = FastAPI(title="rag-codebase-chat")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.environ["FRONTEND_URL"]],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(github.router)
app.include_router(ask.router)
app.include_router(repos.router)
app.include_router(webhooks.router)
app.include_router(feedback.router)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": exc.error, "message": exc.message})


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
