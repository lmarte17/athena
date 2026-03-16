"""FastAPI application lifecycle hooks."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.dependencies import session_manager


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await session_manager.startup()
    yield
