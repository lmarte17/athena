"""Athena backend ASGI entrypoint."""

import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
load_dotenv()

from app.tracing import bootstrap_tracing

bootstrap_tracing()

from fastapi import FastAPI

from app.lifecycle import lifespan
from app.routes.memory import router as memory_router
from app.routes.system import router as system_router
from app.ws import router as ws_router

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Athena Backend", lifespan=lifespan)
app.include_router(system_router)
app.include_router(memory_router)
app.include_router(ws_router)
