from unittest.mock import patch

from plugins.kanban.dashboard import plugin_api


def test_dashboard_conn_does_not_force_init_db(monkeypatch, tmp_path):
    """Dashboard requests must not bust kanban_db's per-process init cache.

    connect() already creates/migrates the schema on first open. Calling
    init_db() on every dashboard API request deliberately clears that cache and
    re-runs migrations against a hot board, racing the gateway dispatcher and
    workers.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    init_calls = []

    def _init_db_spy(*args, **kwargs):
        init_calls.append((args, kwargs))
        raise AssertionError("dashboard _conn must use connect() only")

    with patch.object(plugin_api.kanban_db, "init_db", side_effect=_init_db_spy):
        conn = plugin_api._conn(board="default")
        try:
            assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            conn.close()

    assert init_calls == []
