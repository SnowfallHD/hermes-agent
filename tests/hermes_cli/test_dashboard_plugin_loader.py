"""Regression tests for dashboard plugin backend API loading."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_dashboard_plugin_api_loader_supports_relative_imports(tmp_path, monkeypatch):
    """Plugin API modules should be loaded as packages, not SPA-fallback silently.

    The Thoughts plugin imports a sibling module via ``from .material_action_reducer``.
    Loading plugin_api.py with spec_from_file_location("flat_name", path) gives the
    module no package context, so the relative import fails and /api/plugins/<name>/...
    falls through to the SPA catch-all as HTML.
    """
    from hermes_cli import web_server

    dashboard_dir = tmp_path / "dashboard"
    dashboard_dir.mkdir()
    (dashboard_dir / "helper.py").write_text("VALUE = 'relative-ok'\n", encoding="utf-8")
    (dashboard_dir / "plugin_api.py").write_text(
        "from fastapi import APIRouter\n"
        "from .helper import VALUE\n"
        "router = APIRouter()\n"
        "@router.get('/probe')\n"
        "def probe():\n"
        "    return {'ok': VALUE}\n",
        encoding="utf-8",
    )

    plugin = {
        "name": "relative-test",
        "source": "user",
        "_dir": str(dashboard_dir),
        "_api_file": "plugin_api.py",
    }

    test_app = FastAPI()
    old_app = web_server.app
    monkeypatch.setattr(web_server, "app", test_app)
    monkeypatch.setattr(web_server, "_get_dashboard_plugins", lambda: [plugin])
    try:
        web_server._mount_plugin_api_routes()
        response = TestClient(test_app).get("/api/plugins/relative-test/probe")
    finally:
        monkeypatch.setattr(web_server, "app", old_app)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"ok": "relative-ok"}
