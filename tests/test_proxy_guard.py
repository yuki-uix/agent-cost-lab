"""Real-socket tests for scripts/proxy_guard.sh (issue #73, gate B).

These do not mock lsof or the socket layer. They bind real TCP sockets and run
the actual guard functions in a bash subprocess, so a broken port check fails
the test instead of passing a stub.
"""
import json
import os
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path

GUARD = Path(__file__).resolve().parents[1] / "scripts" / "proxy_guard.sh"
CAPTURE_SH = GUARD.parent / "capture.sh"
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


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_gone(pid: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.05)


def _run_guard(body: str, *, port: int, pidfile: Path,
               extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PORT"] = str(port)
    env["PIDFILE"] = str(pidfile)
    env["LOG"] = str(pidfile.parent / "proxy.log")
    if extra_env:
        env.update(extra_env)
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
        assert r.returncode == 1, r.stdout + r.stderr
        assert "still in use" in r.stderr
    finally:
        holder.kill()  # SIGKILL cannot be ignored


def test_stop_will_not_kill_a_stale_pidfile(tmp_path):
    """P1a: a stale pidfile names a live, unrelated process. The port is free,
    so the pidfile is the only thing pointing at the bystander — `kill` on the
    stored value would murder it. The whole point of the fix is that the
    bystander survives and stop reports the truth (a warning, exit 2) instead of
    a silent zero."""
    port = _free_port()
    pidfile = tmp_path / "capture.pid"

    innocent = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        pidfile.write_text(str(innocent.pid))
        r = _run_guard("acl_proxy_stop", port=port, pidfile=pidfile)
        # exit 2 is "proxy already gone", a warning — not exit 1 "unsafe to act".
        assert r.returncode == 2, r.stdout + r.stderr
        assert "not killing" in r.stderr
        assert "proxy already gone" in r.stderr
        # the essence of the fix, not just the exit code
        assert innocent.poll() is None, "acl_proxy_stop killed an unrelated process"
    finally:
        innocent.kill()
        innocent.wait()


def test_start_failure_leaves_no_child_and_no_pidfile(tmp_path):
    """P2: when the confirm loop times out (the command starts but never takes
    the port), the failed start must not leave its own child or its pidfile
    behind — both are exactly the unverified residue the next stop would feed
    on."""
    port = _free_port()
    pidfile = tmp_path / "capture.pid"
    marker = tmp_path / "child.pid"

    # A command that stays alive but never listens on PORT, so the 40x0.25s
    # confirm loop times out. It records its own pid before `exec`ing python so
    # the test can assert the child is gone afterwards.
    sleeper = tmp_path / "sleeper.sh"
    sleeper.write_text(
        "#!/usr/bin/env bash\n"
        f"echo $$ > {shlex.quote(str(marker))}\n"
        f"exec {shlex.quote(sys.executable)} -c 'import time; time.sleep(60)'\n")
    sleeper.chmod(0o755)

    r = _run_guard(
        f"acl_proxy_start '{tmp_path / 'cap.jsonl'}'",
        port=port,
        pidfile=pidfile,
        extra_env={"ACL_PYTHON": str(sleeper)},
    )
    assert r.returncode != 0, r.stdout + r.stderr
    assert not pidfile.exists(), "a failed start must not leave a pidfile"
    child = int(marker.read_text())
    _wait_gone(child)
    assert not _pid_alive(child), f"failed start left child {child} running"


# --- capture.sh stop branches on the guard's exit code (issue #73, D16) -------
#
# The guard now distinguishes "proxy already gone" (exit 2) from "unsafe to
# act" (exit 1). capture.sh stop must branch: exit 2 still runs the health gate
# (nothing is writing the capture, so its verdict is trustworthy), exit 1 aborts
# without it (the capture may still be growing). These run the real
# scripts/capture.sh against real sockets and real processes, and assert the
# health gate ran/abstained by looking for its output, never by exit code alone.

def _healthy_capture(path: Path, n: int = 12) -> None:
    """A minimal capture the health gate judges USABLE (mirrors the shape of
    tests/test_capture_health.py's ``healthy()``)."""
    rows = []
    for i in range(n):
        rows.append({
            "status_code": 200,
            "usage": {"input_tokens": 1},
            "error": None,
            "response_id": f"msg_{i}",
            "injected_previous_message_id": f"msg_{i - 1}" if i else None,
            "request_body": {"model": "m",
                             "messages": [{"role": "user", "content": "seed"}]},
        })
    rows[3]["diagnostics"] = {"cache_miss_reason": {"type": "system_changed"}}
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def _run_capture_sh_stop(*, port: int, pidfile: Path) -> subprocess.CompletedProcess:
    """Run the real ``scripts/capture.sh stop`` with a temp port/pidfile/log."""
    env = os.environ.copy()
    env["PORT"] = str(port)
    env["PIDFILE"] = str(pidfile)
    env["LOG"] = str(pidfile.parent / "proxy.log")
    env["ACL_PYTHON"] = sys.executable  # capture_health.py runs under this test's interpreter
    return subprocess.run(
        ["bash", str(CAPTURE_SH), "stop"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        cwd=ROOT,
    )


def _pidfile_file(pidfile: Path) -> Path:
    return Path(str(pidfile) + ".file")


def test_capture_stop_runs_health_gate_on_a_clean_stop(tmp_path):
    """Guard returns 0 -> the health gate runs (its output appears in stdout)."""
    port = _free_port()
    pidfile = tmp_path / "capture.pid"
    capture_file = tmp_path / "cap.jsonl"
    _healthy_capture(capture_file)

    holder = _listener_holder(port, ignore_sigterm=False)
    try:
        _wait_listening(port)
        pidfile.write_text(str(holder.pid))
        _pidfile_file(pidfile).write_text(str(capture_file))
        r = _run_capture_sh_stop(port=port, pidfile=pidfile)
        assert "supports:" in r.stdout, \
            "health gate did not run on a clean stop:\n" + r.stdout + r.stderr
        assert r.returncode == 0, r.stdout + r.stderr
    finally:
        holder.kill()


def test_capture_stop_runs_health_gate_when_proxy_is_gone(tmp_path):
    """Guard returns 2 (port free + stale pidfile) -> the warning is printed and
    the health gate still runs; the bystander named by the pidfile survives."""
    port = _free_port()
    pidfile = tmp_path / "capture.pid"
    capture_file = tmp_path / "cap.jsonl"
    _healthy_capture(capture_file)

    innocent = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        pidfile.write_text(str(innocent.pid))
        _pidfile_file(pidfile).write_text(str(capture_file))
        r = _run_capture_sh_stop(port=port, pidfile=pidfile)
        assert "supports:" in r.stdout, \
            "health gate must run after a stale-pidfile stop:\n" + r.stdout + r.stderr
        assert "not killing" in r.stderr, r.stdout + r.stderr
        assert "proxy already gone" in r.stderr, r.stdout + r.stderr
        # exit 2's health gate is USABLE, and its exit code becomes stop's own
        assert r.returncode == 0, r.stdout + r.stderr
        assert innocent.poll() is None, "capture.sh stop killed an unrelated process"
    finally:
        innocent.kill()
        innocent.wait()


def test_capture_stop_skips_health_gate_when_a_foreign_process_holds_the_port(tmp_path):
    """Guard returns 1 (port held by a process that is not the recorded PID) ->
    capture.sh stop aborts without running the health gate, and neither the
    foreign listener nor the recorded pidfile is touched."""
    port = _free_port()
    pidfile = tmp_path / "capture.pid"
    capture_file = tmp_path / "cap.jsonl"
    _healthy_capture(capture_file)

    holder = _listener_holder(port, ignore_sigterm=False)  # foreign process on the port
    innocent = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_listening(port)
        pidfile.write_text(str(innocent.pid))  # recorded PID != the listener
        _pidfile_file(pidfile).write_text(str(capture_file))
        r = _run_capture_sh_stop(port=port, pidfile=pidfile)
        assert "supports:" not in r.stdout, \
            "health gate must NOT run when a foreign process holds the port:\n" + r.stdout + r.stderr
        assert r.returncode == 1, r.stdout + r.stderr
        assert "not killing" in r.stderr, r.stdout + r.stderr
        assert holder.poll() is None, "capture.sh stop killed the foreign listener"
        # AC4: keep the pidfile so the next stop still names the recorded owner
        assert pidfile.exists(), "the foreign-process stop must keep the pidfile"
    finally:
        holder.kill()
        innocent.kill()
        innocent.wait()
