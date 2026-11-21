"""
COLLIVRA FastAPI Application Entrypoint.

Configures application lifecycle, global routers, middleware, and process-wide policies.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from astropy.utils import iers
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from orbital_core.exceptions import EarthOrientationPolicyError
from orbital_core.iers_policy import configure_iers, get_iers_mode

from zorbit_api.routers.conjunctions import router as conjunctions_router
from zorbit_api.routers.remediation import router as remediation_router
from zorbit_api.routers.risk import router as risk_router

logger = logging.getLogger("zorbit_api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan context manager for COLLIVRA API service.

    Enforces process-wide offline IERS policy at startup before any API requests
    can execute orbital frame transformations:
    - Sets iers.conf.auto_download = False and iers.conf.auto_max_age = None.
    - Prevents unexpected live network requests, telemetry delays, or remote socket hangs.
    - Guarantees deterministic, reproducible offline astrodynamics calculations.
    - Eliminates thread-safety issues of runtime temporary configuration mutation.
    """
    # Intentional process-wide offline policy for COLLIVRA API service (AGENTS.md)
    logger.info("Initializing COLLIVRA API: configuring IERS offline policy")
    configure_iers()
    yield


def create_app() -> FastAPI:
    """Create and configure the COLLIVRA FastAPI application."""
    app = FastAPI(
        title="COLLIVRA Orbital Intelligence API",
        version="0.1.0",
        description="FastAPI backend application for COLLIVRA orbital intelligence platform.",
        lifespan=lifespan,
    )

    origins = [
        "https://collivra-one.vercel.app",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]

    frontend_origins_env = os.environ.get("FRONTEND_ORIGINS", "").strip()
    if frontend_origins_env:
        for raw_origin in frontend_origins_env.split(","):
            cleaned = raw_origin.strip()
            if cleaned and cleaned not in origins:
                origins.append(cleaned)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(EarthOrientationPolicyError)
    async def earth_orientation_policy_exception_handler(
        request: Request, exc: EarthOrientationPolicyError
    ) -> JSONResponse:
        """Map domain EarthOrientationPolicyError to structured HTTP 422 Unprocessable Content."""
        logger.warning("Earth orientation policy violation: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "code": "EARTH_ORIENTATION_POLICY_VIOLATION",
                "detail": exc.message,
                "epoch": exc.epoch.isoformat() if exc.epoch else None,
                "data_source": exc.data_source,
                "details": exc.details,
            },
        )

    @app.get("/health")
    async def health_check():
        return {
            "status": "healthy",
            "service": "zorbit_api",
            "version": "0.1.0",
            "offline_mode": not iers.conf.auto_download,
            "iers_mode": get_iers_mode(),
        }

    app.include_router(conjunctions_router)
    app.include_router(risk_router)
    app.include_router(remediation_router)

    return app


app = create_app()
