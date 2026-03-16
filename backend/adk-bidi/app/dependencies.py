"""Shared application dependencies."""

from app.session_manager import SessionManager

# Singleton service container for the backend process.
session_manager = SessionManager()
