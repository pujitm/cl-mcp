#!/usr/bin/env python3
"""Tests for the shared-daemon mode of scripts/stdio_tcp_bridge.py.

Stdlib unittest only; a fake MCP daemon (a small Python TCP server that
answers ping and writes the bridge's port file, exactly like
scripts/daemon.lisp does) stands in for SBCL, so these run anywhere:

  python -m unittest scripts.test_stdio_tcp_bridge   # from repo root, or
  python scripts/test_stdio_tcp_bridge.py
"""

import json
import os
import socket
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stdio_tcp_bridge as bridge  # noqa: E402

FAKE_DAEMON = r"""
import os, socket, threading
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", 0))
srv.listen(16)
port = srv.getsockname()[1]
with open(os.environ["CL_MCP_DAEMON_PORT_FILE"], "w") as f:
    f.write(f"{port} {os.getpid()}\n")
def handle(conn):
    buf = b""
    try:
        while True:
            data = conn.recv(4096)
            if not data:
                return
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    conn.sendall(b'{"jsonrpc":"2.0","id":0,"result":{}}\n')
    except OSError:
        pass
    finally:
        conn.close()
while True:
    c, _ = srv.accept()
    threading.Thread(target=handle, args=(c,), daemon=True).start()
"""


class BridgeDaemonTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.home = tempfile.mkdtemp(prefix="cl-mcp-test-")
        self._env = dict(os.environ)
        os.environ["CL_MCP_HOME"] = self.home
        os.environ["CL_MCP_DAEMON_START_TIMEOUT"] = "30"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def _start_fake_server(self):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]

        def serve():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                threading.Thread(target=self._answer, args=(conn,), daemon=True).start()

        threading.Thread(target=serve, daemon=True).start()
        self.addCleanup(srv.close)
        return port

    @staticmethod
    def _answer(conn):
        try:
            data = conn.recv(4096)
            if data:
                conn.sendall(b'{"jsonrpc":"2.0","id":0,"result":{}}\n')
        except OSError:
            pass
        finally:
            conn.close()

    def test_registry_roundtrip(self):
        bridge.write_registry(self.home, 4321, 999)
        self.assertEqual(bridge.read_registry(self.home), (4321, 999))

    def test_health_check(self):
        port = self._start_fake_server()
        self.assertTrue(bridge.health_check(port))
        self.assertFalse(bridge.health_check(1))  # nothing listens on port 1

    def test_ensure_daemon_reuses_healthy_daemon(self):
        port = self._start_fake_server()
        bridge.write_registry(self.home, port, os.getpid())
        self.assertEqual(bridge.ensure_daemon(), port)

    def test_ensure_daemon_spawns_when_registry_stale(self):
        bridge.write_registry(self.home, 1, 0)  # dead port, no pid
        os.environ["CL_MCP_DAEMON_COMMAND"] = json.dumps(
            [sys.executable, "-c", FAKE_DAEMON])
        port = bridge.ensure_daemon()
        self.assertTrue(bridge.health_check(port))
        reg_port, reg_pid = bridge.read_registry(self.home)
        self.assertEqual(reg_port, port)
        self.assertTrue(bridge.pid_alive(reg_pid))
        # Second call must reuse, not respawn.
        self.assertEqual(bridge.ensure_daemon(), port)

    def test_find_openssl_dir_contract(self):
        result = bridge.find_openssl_dir()
        if result is not None:
            self.assertTrue(os.path.isfile(os.path.join(result, "libcrypto-3-x64.dll")))


if __name__ == "__main__":
    unittest.main()
