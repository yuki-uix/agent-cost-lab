"""Real-socket tests for scripts/proxy_guard.sh (issue #73, gate B).

These do not mock lsof or the socket layer. They bind real TCP sockets and run
the actual guard functions in a bash subprocess, so a broken port check fails
the test instead of passing a stub.
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

GUARD = Path(__file__).resolve().parents[1] / "scripts" / "proxy_guard.sh"
ROOT = GUARD.parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_listening(port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"nothing listened on 127.0.0.1:{port} in {timeout}s")


def _run_guard(body: str, *, port: int, pidfile: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PORT"] = str(port)
    env["PIDFILE"] = str(pidfile)
    env["LOG"] = str(pidfile.parent / "proxy.log")
    return subprocess.run(
        ["bash", "-c", f"source {GUARD}\n{body}\n"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=ROOT,
    )


def _listener_holder(port: int, ignore_sigterm: bool) -> subprocess.Popen:
    """A background process holding ``port`` in LISTEN for 60s."""
    code = (
        "import socket, time"
        + (", signal; signal.signal(signal.SIGTERM, signal.SIG_IGN)" if ignore_sigterm else "")
        + f"\ns = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        f"s.bind(('127.0.0.1', {port})); s.listen(1)\n"
        "time.sleep(60)"
    )
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def test_start_fails_when_port_is_already_in_use(tmp_path):
    port = _free_port()
    pidfile = tmp_path / "capture.pid"

    holder = _listener_holder(port, ignore_sigterm=False)
    try:
        _wait_listening(port)
        r = _run_guard(
            f"acl_proxy_start '{tmp_path / 'cap.jsonl'}'",
            port=port,
            pidfile=pidfile,
        )
        assert r.returncode != 0, r.stdout + r.stderr
        assert "already in use" in r.stderr
        assert str(port) in r.stderr
        # the guard must refuse before starting anything: no pidfile is written
        assert not pidfile.exists()
    finally:
        holder.kill()


def test_stop_times_out_when_the_process_ignores_sigterm(tmp_path):
    port = _free_port()
    pidfile = tmp_path / "capture.pid"

    # a proxy that ignores SIGTERM keeps the port open after `kill` returns
    holder = _listener_holder(port, ignore_sigterm=True)
    try:
        _wait_listening(port)
        pidfile.write_text(str(holder.pid))
        r = _run_guard("acl_proxy_stop", port=port, pidfile=pidfile)
        assert r.returncode != 0, r.stdout + r.stderr
        assert "still in use" in r.stderr
    finally:
        holder.kill()  # SIGKILL cannot be ignored
