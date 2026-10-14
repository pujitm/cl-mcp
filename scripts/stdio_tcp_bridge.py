#!/usr/bin/env python3
"""
Stdio<->TCP line bridge for MCP, with an optional shared-daemon mode.

Plain mode forwards newline-delimited JSON between stdin/stdout and a TCP
MCP server that is already running:

  python3 scripts/stdio_tcp_bridge.py --host 127.0.0.1 --port 12345

With --ensure-daemon the bridge first makes sure the shared cl-mcp daemon
(scripts/daemon.lisp: one multi-client TCP server with the worker pool
enabled) is running, starting it detached when it is not.  Every agent
then gets its own session, worker and project root inside one warm SBCL
image, instead of paying a cold Quicklisp load per agent -- and instead
of failing outright when that load hits a snag (for example cl+ssl
without libcrypto on PATH).  Concurrent launches race on a lock file;
only one daemon is ever started.

Usage:
  python3 scripts/stdio_tcp_bridge.py --ensure-daemon

Environment variables (fallbacks):
  LISP_MCP_HOST, LISP_MCP_PORT   plain-mode endpoint
  CL_MCP_HOME                    daemon registry dir (default ~/.cl-mcp)
  CL_MCP_SBCL                    sbcl executable (default: PATH lookup)
  CL_MCP_QUICKLISP_SETUP         quicklisp setup.lisp (default ~/quicklisp/setup.lisp)
  CL_MCP_DAEMON_COMMAND          JSON array: verbatim daemon command (tests)
  CL_MCP_DAEMON_START_TIMEOUT    seconds to wait for the daemon (default 120)

Notes:
  - Only forwards single-line messages (newline-delimited JSON).
  - Exits when stdin hits EOF or the TCP connection closes.
  - Keep stdout strictly for protocol lines; logs go to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import atexit
import signal


def eprint(*a, **k):
    print(*a, file=sys.stderr, **k)


# ---------------------------------------------------------------------------
# Shared-daemon registry (~/.cl-mcp/daemon.json + daemon.port + daemon.lock)
# ---------------------------------------------------------------------------

def daemon_home() -> str:
    home = os.environ.get("CL_MCP_HOME") or os.path.join(os.path.expanduser("~"), ".cl-mcp")
    os.makedirs(home, exist_ok=True)
    return home


def _registry_path(home: str) -> str:
    return os.path.join(home, "daemon.json")


def read_registry(home: str):
    try:
        with open(_registry_path(home), encoding="utf-8") as f:
            data = json.load(f)
        port = int(data["port"])
        pid = int(data.get("pid") or 0)
        return port, pid
    except Exception:
        return None, 0


def write_registry(home: str, port: int, pid: int) -> None:
    tmp = _registry_path(home) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"port": port, "pid": pid}, f)
    os.replace(tmp, _registry_path(home))


def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def health_check(port: int, timeout: float = 2.0) -> bool:
    """True when a cl-mcp daemon answers an MCP ping on PORT."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(b'{"jsonrpc":"2.0","id":0,"method":"ping"}\n')
            buf = b""
            while b"\n" not in buf and len(buf) < 65536:
                chunk = sock.recv(4096)
                if not chunk:
                    return False
                buf += chunk
            return b'"id":0' in buf or b'"id": 0' in buf
    except OSError:
        return False


class _FileLock:
    """Blocking exclusive lock on a file (msvcrt on Windows, flock else)."""

    def __init__(self, path: str):
        self.path = path
        self.fh = None

    def __enter__(self):
        self.fh = open(self.path, "a+")
        if os.name == "nt":
            import msvcrt
            deadline = time.time() + 180
            while True:
                try:
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.time() > deadline:
                        raise TimeoutError(f"timed out locking {self.path}")
                    time.sleep(0.2)
        else:
            import fcntl
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        finally:
            self.fh.close()
        return False


def find_openssl_dir():
    """Directory containing libcrypto-3-x64.dll, or None (Windows only).

    cl-mcp loads hunchentoot, which loads cl+ssl, which needs OpenSSL on
    PATH.  Git for Windows ships it; scoop's sbcl does not bring it.
    """
    if os.name != "nt":
        return None
    if shutil.which("libcrypto-3-x64.dll"):
        return None  # already reachable via PATH
    candidates = []
    git = shutil.which("git")
    if git:  # <git>/cmd/git.exe or <git>/bin/git.exe -> <git>/mingw64/bin
        candidates.append(os.path.join(os.path.dirname(os.path.dirname(git)), "mingw64", "bin"))
    candidates += [
        r"C:\Program Files\Git\mingw64\bin",
        r"C:\Program Files (x86)\Git\mingw64\bin",
        os.path.join(os.path.expanduser("~"), "scoop", "apps", "openssl", "current", "bin"),
    ]
    for d in candidates:
        if os.path.isfile(os.path.join(d, "libcrypto-3-x64.dll")):
            return d
    return None


def build_daemon_command():
    """Command that starts the shared daemon (see scripts/daemon.lisp)."""
    override = os.environ.get("CL_MCP_DAEMON_COMMAND")
    if override:
        return json.loads(override)
    sbcl = os.environ.get("CL_MCP_SBCL") or shutil.which("sbcl") or "sbcl"
    scripts_dir = os.path.dirname(os.path.abspath(__file__))
    daemon_lisp = os.path.join(scripts_dir, "daemon.lisp")
    setup = os.environ.get("CL_MCP_QUICKLISP_SETUP") or os.path.join(
        os.path.expanduser("~"), "quicklisp", "setup.lisp")
    cmd = [sbcl, "--noinform", "--non-interactive"]
    if os.path.isfile(setup):
        cmd += ["--load", setup, "--eval", "(ql:quickload :cl-mcp :silent t)"]
    else:
        cmd += ["--eval", "(require :asdf)", "--eval", "(asdf:load-system :cl-mcp)"]
    cmd += ["--load", daemon_lisp]
    return cmd


def _spawn_daemon(home: str, port_file: str) -> int:
    env = dict(os.environ)
    env["CL_MCP_DAEMON_PORT_FILE"] = port_file
    env.setdefault("MCP_LOG_LEVEL", "warn")
    # The daemon must not inherit a per-agent pinned root or a disabled
    # worker pool: sessions bring their own root, and the pool is what
    # isolates concurrent sessions from each other.
    env.pop("MCP_NO_WORKER_POOL", None)
    env.pop("MCP_PROJECT_ROOT", None)
    ssl_dir = find_openssl_dir()
    if ssl_dir and ssl_dir.lower() not in env.get("PATH", "").lower():
        env["PATH"] = ssl_dir + os.pathsep + env.get("PATH", "")
    cmd = build_daemon_command()
    log_path = os.path.join(home, "daemon.log")
    kwargs = {}
    if os.name == "nt":
        # Detached: the daemon must outlive this agent's MCP client.
        kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=log, **kwargs)
    return proc.pid


def ensure_daemon():
    """Return the shared daemon's port, starting the daemon if needed."""
    home = daemon_home()
    port, pid = read_registry(home)
    if port and pid_alive(pid) and health_check(port):
        return port
    lock_path = os.path.join(home, "daemon.lock")
    port_file = os.path.join(home, "daemon.port")
    with _FileLock(lock_path):
        port, pid = read_registry(home)
        if port and pid_alive(pid) and health_check(port):
            return port
        # A live pid that does not answer yet is a daemon still starting:
        # wait for it instead of spawning a second one.
        deadline = time.time() + float(os.environ.get("CL_MCP_DAEMON_START_TIMEOUT", "120"))
        if port and pid_alive(pid):
            while time.time() < deadline:
                if health_check(port):
                    return port
                time.sleep(0.5)
        try:
            os.remove(port_file)
        except OSError:
            pass
        pid = _spawn_daemon(home, port_file)
        while time.time() < deadline:
            try:
                with open(port_file, encoding="utf-8") as f:
                    parts = f.read().split()
                if parts:
                    bound_port = int(parts[0])
                    file_pid = int(parts[1]) if len(parts) > 1 else 0
                    bound_pid = file_pid or pid
                    if health_check(bound_port):
                        write_registry(home, bound_port, bound_pid)
                        return bound_port
            except (OSError, ValueError):
                pass
            if not pid_alive(pid):
                break
            time.sleep(0.5)
        raise RuntimeError(
            f"cl-mcp daemon did not become healthy; see {os.path.join(home, 'daemon.log')}")


# ---------------------------------------------------------------------------
# Bridge proper
# ---------------------------------------------------------------------------

def bridge(host: str, port: int, connect_timeout: float = 5.0) -> int:
    cleanup_called = threading.Event()

    def cleanup(sock, sock_file_r, sock_file_w):
        # Idempotent cleanup to ensure EOF is sent and descriptors close.
        if cleanup_called.is_set():
            return
        cleanup_called.set()
        try:
            if sock_file_w:
                try:
                    sock_file_w.flush()
                except Exception:
                    pass
                try:
                    sock.shutdown(socket.SHUT_WR)
                except Exception:
                    pass
        finally:
            for f in (sock_file_r, sock_file_w):
                try:
                    f.close()
                except Exception:
                    pass
            try:
                sock.close()
            except Exception:
                pass

    try:
        sock = socket.create_connection((host, port), timeout=connect_timeout)
        # Disable read timeouts after connect: MCP connections can idle.
        try:
            sock.settimeout(None)
        except Exception:
            pass
    except Exception as e:
        eprint(f"[bridge] connect failed: {e}")
        return 2

    sock_file_r = sock.makefile('r', encoding='utf-8', newline='')
    sock_file_w = sock.makefile('w', encoding='utf-8', newline='\n')
    atexit.register(lambda: cleanup(sock, sock_file_r, sock_file_w))

    def handle_signal(signum, _frame):
        eprint(f"[bridge] signal {signum}, closing socket")
        cleanup(sock, sock_file_r, sock_file_w)
        sys.exit(128 + signum)

    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig is not None:
            try:
                signal.signal(sig, handle_signal)
            except (ValueError, OSError):
                pass

    stop = threading.Event()

    def pump_stdin_to_tcp():
        try:
            for line in sys.stdin:
                if not line.endswith('\n'):
                    line += '\n'
                sock_file_w.write(line)
                sock_file_w.flush()
        except Exception as e:
            eprint(f"[bridge] stdin->tcp error: {e}")
        finally:
            cleanup(sock, sock_file_r, sock_file_w)
            stop.set()

    def pump_tcp_to_stdout():
        try:
            for line in sock_file_r:
                # Structured log lines ({"ts":...) go to stderr, never stdout.
                if line.strip().startswith('{"ts":'):
                    sys.stderr.write(line)
                    try:
                        sys.stderr.flush()
                    except Exception:
                        pass
                else:
                    sys.stdout.write(line)
                    try:
                        sys.stdout.flush()
                    except Exception:
                        pass
        except Exception as e:
            eprint(f"[bridge] tcp->stdout error: {e}")
        finally:
            cleanup(sock, sock_file_r, sock_file_w)
            stop.set()

    t1 = threading.Thread(target=pump_stdin_to_tcp, name="stdin->tcp", daemon=True)
    t2 = threading.Thread(target=pump_tcp_to_stdout, name="tcp->stdout", daemon=True)
    t1.start(); t2.start()
    try:
        while not stop.is_set():
            time.sleep(0.05)
    finally:
        cleanup(sock, sock_file_r, sock_file_w)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="MCP stdio<->TCP bridge")
    ap.add_argument("--host", default=os.environ.get("LISP_MCP_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("LISP_MCP_PORT", "0") or 0))
    ap.add_argument("--ensure-daemon", action="store_true",
                    help="start the shared cl-mcp daemon if needed and bridge to it")
    ap.add_argument("--connect-timeout", type=float, default=5.0)
    args = ap.parse_args()
    if args.ensure_daemon and not args.port:
        try:
            args.port = ensure_daemon()
        except Exception as e:
            eprint(f"[bridge] ensure-daemon failed: {e}")
            return 2
    if not args.port:
        args.port = 12345
    eprint(f"[bridge] connecting tcp://{args.host}:{args.port}")
    rc = bridge(args.host, args.port, args.connect_timeout)
    eprint(f"[bridge] exit {rc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
