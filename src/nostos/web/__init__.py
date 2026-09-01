"""Local web UI for browsing and acting on rental listings."""

from __future__ import annotations

from nostos.web.app import create_app
from nostos.web.static_export import render_static_export, write_static_export

__all__ = ["create_app", "render_static_export", "write_static_export"]
