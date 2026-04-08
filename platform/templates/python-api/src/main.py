"""
{{SERVICE_NAME}} — {{DESCRIPTION}}
Owner: {{OWNER}}
"""

import os
from pathlib import Path
import subprocess

from fastapi import FastAPI
from pydantic import BaseModel

# ── Version info (injected at build time via env vars) ────────────────────────
APP_VERSION = os.environ.get("APP_VERSION", "0.1.0")
GIT_COMMIT  = os.environ.get("GIT_COMMIT", "local")
BUILD_DATE  = os.environ.get("BUILD_DATE", "unknown")

app = FastAPI(
    title="{{SERVICE_NAME}}",
    description="{{DESCRIPTION}}",
    version=APP_VERSION,
)


# ── Standard platform endpoints ───────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str

class InfoResponse(BaseModel):
    app: dict
    git: dict
    build: dict


@app.get("/health", response_model=HealthResponse, tags=["Platform"])
def health():
    """Kubernetes liveness / readiness probe."""
    return HealthResponse(status="UP")


@app.get("/info", response_model=InfoResponse, tags=["Platform"])
def info():
    """Version and build metadata — mirrors Spring Boot /actuator/info."""
    return InfoResponse(
        app={"name": "{{SERVICE_NAME}}", "version": APP_VERSION},
        git={"commit": GIT_COMMIT},
        build={"date": BUILD_DATE},
    )


# ── Business routes ───────────────────────────────────────────────────────────

@app.get("/", tags=["Root"])
def root():
    return {"service": "{{SERVICE_NAME}}", "version": APP_VERSION}
