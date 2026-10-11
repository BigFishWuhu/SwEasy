"""Small localhost-only web configuration server for rune rules.

It intentionally uses Python's standard library so the installed Maa package
is enough to run it; no npm build step is required for the first version.
"""

from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from .rune.efficiency import calculate_efficiency, stat_name
    from .rune.json_import import iter_account_runes, normalize_account_rune
    from .rune.rules import KEY_SPEED_SETS, default_config, evaluate_rune, merge_default_groups, normalize_config, validate_config
    from .rune.config_store import DB_PATH, DEFAULT_CONFIG_PATH, LEGACY_CONFIG_PATH, load_account as load_stored_account, load_config as load_stored_config, save_account as save_stored_account, save_config as save_stored_config
except ImportError:
    from rune.efficiency import calculate_efficiency, stat_name
    from rune.json_import import iter_account_runes, normalize_account_rune
    from rune.rules import KEY_SPEED_SETS, default_config, evaluate_rune, merge_default_groups, normalize_config, validate_config
    from rune.config_store import DB_PATH, DEFAULT_CONFIG_PATH, LEGACY_CONFIG_PATH, load_account as load_stored_account, load_config as load_stored_config, save_account as save_stored_account, save_config as save_stored_config

ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = ROOT / "web"
CONFIG_PATH = DB_PATH
RUNTIME_PATH = WEB_ROOT / "runtime.json"
INSTANCE_ID = uuid.uuid4().hex
SPEED_THRESHOLDS = (30, 25, 20)


def _publish_runtime(port: int) -> None:
    """Publish the active port where even a legacy static server can read it."""
    payload = {
        "service": "SwEasyRuneWeb",
        "port": port,
        "instance": INSTANCE_ID,
        "pid": os.getpid(),
        "started_at": int(time.time()),
    }
    temporary = RUNTIME_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, RUNTIME_PATH)


def _restart_process(server: ThreadingHTTPServer) -> None:
    """Close this listener and start a detached successor on the same port."""
    time.sleep(0.25)
    port = server.server_address[1]
    server.shutdown()
    server.server_close()
    launcher = Path(__file__).resolve().with_name("web_launcher.py")
    command = [sys.executable, str(launcher), "--port", str(port), "--no-browser"]
    kwargs: dict[str, Any] = {
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
    subprocess.Popen(command, **kwargs)
    # The main thread waits on an Event and owns no further state. Exiting here
    # avoids leaving one socket-less process behind after every Web restart.
    os._exit(0)


def load_config() -> dict[str, Any]:
    return load_stored_config(
        db_path=DB_PATH,
        legacy_path=LEGACY_CONFIG_PATH,
        default_path=DEFAULT_CONFIG_PATH,
    )


def save_config(config: dict[str, Any]) -> None:
    errors = validate_config(config)
    if errors:
        raise ValueError("；".join(errors))
    save_stored_config(config, db_path=DB_PATH, legacy_path=LEGACY_CONFIG_PATH)


def _base_substat_speed(rune: dict[str, Any]) -> float | None:
    """Return the ungrinded speed substat; main speed and grinds do not count."""
    for item in rune.get("substats", []) or []:
        if not isinstance(item, dict):
            continue
        try:
            if stat_name(item.get("type", item.get("stat"))) == "SPD":
                return float(item.get("value", 0) or 0)
        except (ValueError, TypeError):
            continue
    return None


def _speed_tier_stats(runes: list[dict[str, Any]]) -> dict[str, Any]:
    """Build cumulative 30+/25+/20+ ungrinded speed counts."""
    key_sets = set(KEY_SPEED_SETS)

    def bucket(items: list[dict[str, Any]]) -> dict[str, Any]:
        speeds = [speed for item in items if (speed := _base_substat_speed(item)) is not None]
        return {
            "runes": len(items),
            "with_speed": len(speeds),
            "tiers": {str(threshold): sum(speed >= threshold for speed in speeds) for threshold in SPEED_THRESHOLDS},
        }

    by_name: dict[str, list[dict[str, Any]]] = {}
    for rune in runes:
        by_name.setdefault(str(rune.get("set") or "Unknown"), []).append(rune)
    order = {name: index for index, name in enumerate(KEY_SPEED_SETS)}
    by_set = [
        {"set": name, "key": name in key_sets, **bucket(items)}
        for name, items in sorted(
            by_name.items(),
            key=lambda pair: (0 if pair[0] in key_sets else 1, order.get(pair[0], 999), pair[0]),
        )
    ]
    key_runes = [rune for rune in runes if rune.get("set") in key_sets]
    other_runes = [rune for rune in runes if rune.get("set") not in key_sets]
    return {
        "basis": "base_substat",
        "thresholds": list(SPEED_THRESHOLDS),
        "key_sets": list(KEY_SPEED_SETS),
        "all": bucket(runes),
        "key_total": bucket(key_runes),
        "off_set": bucket(other_runes),
        "by_set": by_set,
    }


def evaluate_account(account: Any, config: dict[str, Any], include_equipped: bool = True) -> dict[str, Any]:
    """Predict keep/sell decisions for every imported rune without touching the game.

    Equipment and lock state remain in the result as annotations, but are
    deliberately cleared only for this read-only prediction. The live scanner
    still protects equipped/locked runes through ``evaluate_rune``.
    """
    if not isinstance(account, dict):
        raise ValueError("账号 JSON 必须是对象")
    locked_ids = {
        int(item["rune_id"])
        for item in account.get("rune_lock_list", []) or []
        if isinstance(item, dict) and str(item.get("rune_id", "")).isdigit()
    }
    results: list[dict[str, Any]] = []
    seen: set[Any] = set()
    skipped = 0
    for raw in iter_account_runes(account):
        rune_id = raw.get("rune_id")
        if rune_id in seen:
            continue
        seen.add(rune_id)
        try:
            rune = normalize_account_rune(raw, locked_ids=locked_ids)
            if not include_equipped and rune.get("equipped"):
                continue
            efficiency = calculate_efficiency(rune)
            decision = evaluate_rune({
                **rune,
                "equipped": False,
                "locked": False,
                "efficiency": efficiency,
            }, config)
            results.append({
                "rune_id": rune.get("rune_id"), "set": rune.get("set"), "slot": rune.get("slot"),
                "quality": rune.get("quality"), "stars": rune.get("stars"), "level": rune.get("level"),
                "ancient": rune.get("ancient"), "equipped": rune.get("equipped"), "locked": rune.get("locked"),
                "main_stat": rune.get("main_stat"), "innate": rune.get("innate"),
                "substats": rune.get("substats", []), "efficiency": efficiency,
                "action": decision.action, "reason": decision.reason, "rule_id": decision.rule_id,
                "uncertain": decision.uncertain,
            })
        except (ValueError, TypeError, KeyError):
            skipped += 1
    from collections import Counter
    action_counts = Counter(item["action"] for item in results)
    rule_counts = Counter(item["rule_id"] or "fallback" for item in results)
    equipped_results = [item for item in results if item["equipped"]]
    return {
        "summary": {
            "total": len(results), "keep": action_counts.get("KEEP", 0),
            "sell": action_counts.get("SELL", 0), "uncertain": action_counts.get("KEEP_UNCERTAIN", 0),
            "skipped": skipped, "equipped": sum(bool(item["equipped"]) for item in results),
            "equipped_keep": sum(item["action"] != "SELL" for item in equipped_results),
            "equipped_sell": sum(item["action"] == "SELL" for item in equipped_results),
        },
        "rules": [{"rule_id": key, "count": value} for key, value in rule_counts.most_common()],
        "speed_tiers": _speed_tier_stats(results),
        "runes": results,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "SwEasyRuneWeb/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[web] {self.address_string()} - {format % args}")

    def _json(self, status: int, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> Any:
        size = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(size).decode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/config":
            return self._json(200, load_config())
        if path == "/api/account":
            stored = load_stored_account(db_path=DB_PATH)
            return self._json(200, {"saved": stored is not None, **(stored or {})})
        if path == "/api/health":
            return self._json(200, {
                "ok": True,
                "service": "SwEasyRuneWeb",
                "instance": INSTANCE_ID,
                "port": self.server.server_address[1],
                "config": str(CONFIG_PATH),
            })
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        file_path = (WEB_ROOT / relative).resolve()
        if WEB_ROOT not in file_path.parents and file_path != WEB_ROOT:
            return self._json(404, {"error": "not found"})
        if not file_path.is_file():
            return self._json(404, {"error": "not found"})
        payload = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(file_path.name)[0] or "application/octet-stream")
        # The editor is a local development tool. Avoid stale CSS/JS after a
        # code update so users do not need to restart the server or clear the
        # browser cache just to see the latest layout.
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self._body()
            if path == "/api/restart":
                self._json(200, {"ok": True, "restarting": True, "instance": INSTANCE_ID})
                threading.Thread(
                    target=_restart_process,
                    args=(self.server,),
                    daemon=True,
                ).start()
                return
            if path == "/api/config":
                save_config(body)
                return self._json(200, {"ok": True, "config": body})
            if path == "/api/account":
                stored = save_stored_account(
                    body.get("account"),
                    str(body.get("filename") or "账号符文.json"),
                    db_path=DB_PATH,
                )
                return self._json(200, {
                    "ok": True,
                    "filename": stored["filename"],
                    "updated_at": stored["updated_at"],
                })
            if path == "/api/evaluate":
                rune = body.get("rune", {})
                config = body.get("config") or load_config()
                efficiency = calculate_efficiency(rune)
                decision = evaluate_rune({**rune, "efficiency": efficiency}, config)
                return self._json(200, {"efficiency": efficiency, "decision": decision.as_dict()})
            if path == "/api/evaluate_account":
                account = body.get("account")
                config = body.get("config") or load_config()
                return self._json(200, evaluate_account(account, config, body.get("include_equipped", True)))
            return self._json(404, {"error": "not found"})
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            return self._json(400, {"error": str(exc)})


def serve(port: int = 8765, open_browser: bool = False) -> ThreadingHTTPServer:
    """Start the local service without opening a browser.

    ``open_browser`` remains as a compatibility argument for older callers,
    but is intentionally ignored. The launcher prints the access URL instead.
    """
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    _publish_runtime(port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/"
    print(f"[web] 符文筛选网页已启动，访问地址：{url}", flush=True)
    print(f"[web] 服务不会自动打开浏览器，请复制地址访问（当前端口：{port}）。", flush=True)
    return server


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SwEasy rune filter web editor")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    server = serve(args.port)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()
