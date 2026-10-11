"""Maa custom action that starts the rune Web service with visible output."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction


ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = Path(__file__).resolve().with_name("web_launcher.py")


def run_web_launcher(*, port: int = 8765, restart: bool = True) -> bool:
    """Run the short-lived launcher and relay its result to the Agent log."""
    command = [sys.executable, str(LAUNCHER), "--port", str(port), "--no-browser"]
    if restart:
        command.append("--restart")
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    kwargs: dict[str, Any] = {
        "cwd": str(ROOT),
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 30,
        "env": environment,
        "check": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    print(f"[web] 正在启动或重启符文筛选网页（首选端口：{port}）……", flush=True)
    try:
        completed = subprocess.run(command, **kwargs)
    except subprocess.TimeoutExpired:
        print("[web] 启动失败：30 秒内未完成健康检查", flush=True)
        return False
    except OSError as exc:
        print(f"[web] 启动失败：无法运行启动器：{exc}", flush=True)
        return False

    output = (completed.stdout or "").strip()
    error = (completed.stderr or "").strip()
    if output:
        print(output, flush=True)
    if error:
        print(f"[web] 启动器错误输出：{error}", flush=True)
    if completed.returncode != 0:
        print(f"[web] 启动失败：启动器退出码 {completed.returncode}", flush=True)
        return False
    if "访问地址：http://127.0.0.1:" not in output:
        print("[web] 启动失败：服务未返回经过健康检查的访问地址", flush=True)
        return False
    return True


@AgentServer.custom_action("start_rune_web")
class StartRuneWebAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        del context
        try:
            params = json.loads(argv.custom_action_param or "{}") or {}
            port = int(params.get("port", 8765))
            restart = bool(params.get("restart", True))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            print(f"[web] 启动失败：参数无效：{exc}", flush=True)
            return False
        if not 1 <= port <= 65535:
            print(f"[web] 启动失败：端口 {port} 超出有效范围", flush=True)
            return False
        return run_web_launcher(port=port, restart=restart)
