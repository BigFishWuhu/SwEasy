"""Start or safely replace the localhost rune-filter web service."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_PATH = ROOT / "web" / "runtime.json"
SERVICE_NAME = "SwEasyRuneWeb"


def _listener_pids(port: int, output: str | None = None) -> set[int]:
    """Return Windows PIDs listening on one exact TCP port."""
    if output is None:
        completed = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            check=False,
            capture_output=True,
            text=True,
            encoding="mbcs" if os.name == "nt" else "utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = completed.stdout
    result: set[int] = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0].upper() != "TCP":
            continue
        local = parts[1].rsplit(":", 1)
        if len(local) != 2 or local[1] != str(port):
            continue
        if "LISTEN" not in parts[-2].upper() or not parts[-1].isdigit():
            continue
        result.add(int(parts[-1]))
    return result


def _health(port: int) -> dict | None:
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _is_sweasy_health(payload: dict | None) -> bool:
    return bool(
        payload
        and payload.get("ok") is True
        and (
            payload.get("service") == SERVICE_NAME
            # Compatibility with the first Web version, which did not expose
            # a service marker yet.
            or str(payload.get("config", "")).replace("\\", "/").endswith("config/rune-filter.sqlite3")
        )
    )


def _is_sweasy_server(port: int) -> bool:
    return _is_sweasy_health(_health(port))


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


def _terminate_pid(pid: int) -> None:
    """Terminate one already validated listener, with a Windows fallback."""
    try:
        os.kill(pid, signal.SIGTERM)
        return
    except (PermissionError, OSError) as first_error:
        completed = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            capture_output=True,
            text=True,
            encoding="mbcs" if os.name == "nt" else "utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode == 0:
            return
        detail = (completed.stderr or completed.stdout).strip()
        raise PermissionError(f"无法终止旧 Web 服务 PID {pid}：{detail or first_error}") from first_error


def replace_existing_server(port: int) -> None:
    pids = _listener_pids(port)
    if not pids:
        return
    if not _is_sweasy_server(port):
        raise RuntimeError(f"端口 {port} 被非 SwEasy 服务占用，已安全停止")
    for pid in pids:
        if pid != os.getpid():
            _terminate_pid(pid)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if _port_is_free(port):
            return
        time.sleep(0.1)
    raise RuntimeError(f"旧 Web 服务未能释放端口 {port}")


def _next_available_port(preferred: int, limit: int = 20) -> int:
    for port in range(preferred + 1, preferred + limit + 1):
        if _port_is_free(port):
            return port
    raise RuntimeError(f"端口 {preferred + 1}-{preferred + limit} 均被占用，无法启动备用 Web 服务")


def _runtime_port() -> int | None:
    try:
        payload = json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
        port = int(payload.get("port", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError, AttributeError):
        return None
    health = _health(port) if 0 < port < 65536 else None
    return port if health and health.get("service") == SERVICE_NAME else None


def _request_graceful_restart(port: int) -> bool:
    """Ask a current server to replace itself, then wait for a new instance."""
    before = _health(port)
    if not before or before.get("service") != SERVICE_NAME:
        return False
    request = Request(
        f"http://127.0.0.1:{port}/api/restart",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=2) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (OSError, HTTPError, URLError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(result, dict) or not result.get("restarting"):
        return False
    previous_instance = before.get("instance")
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        time.sleep(0.2)
        current = _health(port)
        if current and current.get("service") == SERVICE_NAME:
            current_instance = current.get("instance")
            if previous_instance is None or current_instance != previous_instance:
                return True
    raise RuntimeError(f"Web 服务已接受重启请求，但端口 {port} 未在 12 秒内恢复")


def _start_detached_server(port: int) -> None:
    """Start the long-lived server, wait for health, then let this launcher exit."""
    server_script = Path(__file__).resolve().with_name("web_server.py")
    command = [
        os.sys.executable,
        str(server_script),
        "--port",
        str(port),
        "--no-browser",
    ]
    kwargs = {
        "cwd": str(ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **kwargs)
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        health = _health(port)
        if health and health.get("service") == SERVICE_NAME:
            return
        if process.poll() is not None:
            raise RuntimeError(f"Web 服务进程提前退出，退出码 {process.returncode}")
        time.sleep(0.2)
    raise RuntimeError(f"Web 服务已启动进程，但端口 {port} 未在 12 秒内就绪")


def _print_access_address(port: int, restarted: bool = False) -> None:
    state = "已重启" if restarted else "已启动"
    url = f"http://127.0.0.1:{port}/"
    print(f"[web] 符文筛选网页{state}", flush=True)
    print(f"[web] 访问地址：{url}", flush=True)
    print(f"[web] 当前端口：{port}；服务不会自动打开浏览器，请复制地址访问。", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="启动或重启 SwEasy 符文筛选网页")
    parser.add_argument("--port", type=int, default=8765)
    # Kept for compatibility with old Maa tasks and restart commands. Browser
    # opening has been removed entirely; the launcher only prints the URL.
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()
    if args.restart:
        # A fallback instance may already be active because an elevated legacy
        # listener could not be stopped. Restart that exact instance first.
        current_port = _runtime_port()
        candidates = dict.fromkeys(port for port in (current_port, args.port) if port is not None)
        for port in candidates:
            try:
                restarted = _request_graceful_restart(port)
            except RuntimeError as exc:
                # The accepted restart may have closed the old socket but
                # failed to launch its successor. Continue with a clean start.
                print(f"[web] {exc}")
                restarted = False
            if restarted:
                _print_access_address(port, restarted=True)
                return
        try:
            replace_existing_server(args.port)
        except PermissionError as exc:
            fallback = _next_available_port(args.port)
            print(f"[web] {exc}")
            print(f"[web] 使用备用端口 {fallback}；旧地址会自动跳转到新服务。")
            args.port = fallback
    _start_detached_server(args.port)
    _print_access_address(args.port)


if __name__ == "__main__":
    main()
