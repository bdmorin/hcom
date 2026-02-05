"""Unit tests for hcom term command (commands/term.py).

Tests screen query, text injection, debug toggle, and formatting.
"""

import json
import socket
import threading
import time
from unittest.mock import patch

from hcom.commands.term import (
    cmd_term,
    _format_screen,
    _get_inject_port,
    _get_pty_instances,
    _inject_raw,
    _inject_text,
    _query_screen,
)


# ==================== _format_screen ====================

class TestFormatScreen:
    def test_basic_format(self):
        data = {
            "lines": ["hello", "", "world"],
            "cursor": [2, 5],
            "size": [24, 80],
            "ready": True,
            "prompt_empty": False,
            "input_text": "foo",
        }
        out = _format_screen(data)
        assert "Screen 24x80" in out
        assert "cursor (2,5)" in out
        assert "ready=True" in out
        assert "prompt_empty=False" in out
        assert "'foo'" in out
        assert "  0: hello" in out
        assert "  2: world" in out
        # Empty line (index 1) should not appear
        assert "  1:" not in out

    def test_empty_screen(self):
        data = {"lines": [], "cursor": [0, 0], "size": [0, 0]}
        out = _format_screen(data)
        assert "Screen 0x0" in out

    def test_missing_fields_use_defaults(self):
        out = _format_screen({})
        assert "Screen 0x0" in out
        assert "cursor (0,0)" in out


# ==================== _flag_path / debug ====================

class TestDebug:
    def test_debug_on_creates_flag(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hcom.commands.term.hcom_path", lambda *parts: tmp_path.joinpath(*parts))
        assert cmd_term(["debug", "on"]) == 0
        assert (tmp_path / ".tmp" / "pty_debug_on").exists()

    def test_debug_off_removes_flag(self, tmp_path, monkeypatch):
        flag = tmp_path / ".tmp" / "pty_debug_on"
        flag.parent.mkdir(parents=True)
        flag.touch()
        monkeypatch.setattr("hcom.commands.term.hcom_path", lambda *parts: tmp_path.joinpath(*parts))
        assert cmd_term(["debug", "off"]) == 0
        assert not flag.exists()

    def test_debug_off_no_flag_ok(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hcom.commands.term.hcom_path", lambda *parts: tmp_path.joinpath(*parts))
        assert cmd_term(["debug", "off"]) == 0

    def test_debug_no_subcommand(self):
        assert cmd_term(["debug"]) == 0

    def test_debug_logs(self, tmp_path, monkeypatch):
        log_dir = tmp_path / ".tmp" / "logs" / "pty_debug"
        log_dir.mkdir(parents=True)
        (log_dir / "test.log").write_text("data")
        monkeypatch.setattr("hcom.commands.term.hcom_path", lambda *parts: tmp_path.joinpath(*parts))
        assert cmd_term(["debug", "logs"]) == 0

    def test_debug_logs_no_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr("hcom.commands.term.hcom_path", lambda *parts: tmp_path.joinpath(*parts))
        assert cmd_term(["debug", "logs"]) == 0


# ==================== _query_screen ====================

class TestQueryScreen:
    def _make_server(self, response: bytes):
        """Start a TCP server that responds to first connection."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def handler():
            conn, _ = srv.accept()
            conn.recv(1024)  # read query
            conn.sendall(response)
            conn.close()
            srv.close()

        t = threading.Thread(target=handler, daemon=True)
        t.start()
        return port

    def test_valid_json_response(self):
        data = {"lines": ["hi"], "size": [24, 80], "cursor": [0, 2], "ready": True, "prompt_empty": True, "input_text": None}
        port = self._make_server(json.dumps(data).encode())
        result = _query_screen(port)
        assert result is not None
        assert result["lines"] == ["hi"]
        assert result["size"] == [24, 80]
        assert result["ready"] is True

    def test_dead_port(self):
        # Use a port nothing is listening on
        result = _query_screen(1, timeout=0.1)
        assert result is None

    def test_invalid_json(self):
        port = self._make_server(b"not json")
        result = _query_screen(port)
        assert result is None

    def test_empty_response(self):
        port = self._make_server(b"")
        result = _query_screen(port)
        assert result is None


# ==================== inject ====================

class TestInject:
    def _make_sink(self):
        """TCP server that captures received data."""
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        port = srv.getsockname()[1]
        received = []

        def handler():
            while True:
                try:
                    conn, _ = srv.accept()
                    data = conn.recv(4096)
                    received.append(data)
                    conn.close()
                except OSError:
                    break

        t = threading.Thread(target=handler, daemon=True)
        t.start()
        return port, received, srv

    def test_inject_raw(self):
        port, received, srv = self._make_sink()
        _inject_raw(port, b"hello")
        time.sleep(0.05)
        srv.close()
        assert received == [b"hello"]

    def test_inject_text_only(self):
        port, received, srv = self._make_sink()
        with patch("hcom.commands.term._get_inject_port", return_value=port):
            rc = _inject_text("test", "hello")
        time.sleep(0.05)
        srv.close()
        assert rc == 0
        assert b"hello" in received

    def test_inject_enter_only(self):
        port, received, srv = self._make_sink()
        with patch("hcom.commands.term._get_inject_port", return_value=port):
            rc = _inject_text("test", "", enter=True)
        time.sleep(0.05)
        srv.close()
        assert rc == 0
        assert b"\r" in received

    def test_inject_text_and_enter(self):
        port, received, srv = self._make_sink()
        with patch("hcom.commands.term._get_inject_port", return_value=port):
            rc = _inject_text("test", "hi", enter=True)
        time.sleep(0.2)  # 100ms delay between connections
        srv.close()
        assert rc == 0
        assert len(received) == 2
        assert received[0] == b"hi"
        assert received[1] == b"\r"

    def test_inject_no_port(self):
        with patch("hcom.commands.term._get_inject_port", return_value=None):
            rc = _inject_text("test", "hi")
        assert rc == 1

    def test_inject_connection_refused(self):
        with patch("hcom.commands.term._get_inject_port", return_value=1):
            rc = _inject_text("test", "hi")
        assert rc == 1


# ==================== cmd_term dispatch ====================

class TestCmdTerm:
    def test_help(self):
        assert cmd_term(["--help"]) == 0

    def test_inject_missing_name(self):
        assert cmd_term(["inject"]) == 1

    def test_inject_nothing_to_inject(self):
        assert cmd_term(["inject", "myname"]) == 1

    def test_screen_no_port(self):
        with patch("hcom.commands.term._get_inject_port", return_value=None):
            assert cmd_term(["myname"]) == 1

    def test_screen_no_instances(self):
        with patch("hcom.commands.term._get_pty_instances", return_value=[]):
            assert cmd_term([]) == 1

    def test_screen_json_flag(self):
        data = {"lines": [], "size": [24, 80], "cursor": [0, 0], "ready": True, "prompt_empty": True, "input_text": None}
        with patch("hcom.commands.term._get_inject_port", return_value=9999), \
             patch("hcom.commands.term._query_screen", return_value=data):
            assert cmd_term(["myname", "--json"]) == 0

    def test_screen_no_response(self):
        with patch("hcom.commands.term._get_inject_port", return_value=9999), \
             patch("hcom.commands.term._query_screen", return_value=None):
            assert cmd_term(["myname"]) == 1

    def test_screen_all_instances(self):
        data = {"lines": ["hi"], "size": [24, 80], "cursor": [0, 0], "ready": True, "prompt_empty": True, "input_text": None}
        with patch("hcom.commands.term._get_pty_instances", return_value=[{"name": "test", "port": 9999}]), \
             patch("hcom.commands.term._query_screen", return_value=data):
            assert cmd_term([]) == 0

    def test_screen_all_none_responding(self):
        with patch("hcom.commands.term._get_pty_instances", return_value=[{"name": "test", "port": 9999}]), \
             patch("hcom.commands.term._query_screen", return_value=None):
            assert cmd_term([]) == 1


# ==================== DB integration ====================

class TestDbLookup:
    def test_get_inject_port_no_db(self):
        """Graceful None when DB unavailable."""
        with patch("hcom.core.db.get_db", side_effect=Exception("no db")):
            assert _get_inject_port("test") is None

    def test_get_pty_instances_no_db(self):
        with patch("hcom.core.db.get_db", side_effect=Exception("no db")):
            assert _get_pty_instances() == []

    def test_get_inject_port_found(self, hcom_env):
        from hcom.core.db import init_db, get_db
        init_db()
        get_db().execute(
            "INSERT INTO notify_endpoints (instance, kind, port, updated_at) VALUES (?, 'inject', ?, ?)",
            ("test", 12345, 0.0),
        )
        assert _get_inject_port("test") == 12345

    def test_get_inject_port_not_found(self, hcom_env):
        from hcom.core.db import init_db
        init_db()
        assert _get_inject_port("nonexistent") is None

    def test_get_pty_instances(self, hcom_env):
        from hcom.core.db import init_db, get_db
        init_db()
        get_db().execute(
            "INSERT INTO notify_endpoints (instance, kind, port, updated_at) VALUES (?, 'inject', ?, ?)",
            ("a", 111, 0.0),
        )
        get_db().execute(
            "INSERT INTO notify_endpoints (instance, kind, port, updated_at) VALUES (?, 'inject', ?, ?)",
            ("b", 222, 0.0),
        )
        result = _get_pty_instances()
        assert len(result) == 2
        names = {r["name"] for r in result}
        assert names == {"a", "b"}
