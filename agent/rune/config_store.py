"""SQLite-backed persistence for the local rune filter configuration.

The web editor and the Maa scanner use the same store.  A legacy JSON file is
read once when the database has no configuration row, so existing installs are
migrated without losing custom rules. Fresh installs use the rules-only seed
before falling back to bundled JSON defaults. The JSON file remains an optional
backup written on save for compatibility with older tooling.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "config" / "rune-filter.sqlite3"
LEGACY_CONFIG_PATH = ROOT / "config" / "rune-filter.json"
DEFAULT_CONFIG_PATH = ROOT / "assets" / "rune-filter.default.json"
SEED_DB_PATH = ROOT / "assets" / "rune-filter.rules.sqlite3"
CONFIG_KEY = "rune_filter_config"
ACCOUNT_KEY = "rune_account_json"


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS app_settings ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    connection.commit()
    return connection


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read_seed(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?", (CONFIG_KEY,)
            ).fetchone()
        finally:
            connection.close()
        value = json.loads(row[0]) if row else None
        return value if isinstance(value, dict) else None
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return None


def load_config(
    *,
    db_path: Path = DB_PATH,
    legacy_path: Path = LEGACY_CONFIG_PATH,
    default_path: Path = DEFAULT_CONFIG_PATH,
    seed_path: Path = SEED_DB_PATH,
) -> dict[str, Any]:
    """Load the SQLite config, migrating legacy JSON on first use."""
    from .rules import default_config, merge_default_groups

    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(Path(db_path))
        try:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?", (CONFIG_KEY,)
            ).fetchone()
            if row:
                try:
                    stored = json.loads(row[0])
                except json.JSONDecodeError:
                    stored = None
                if isinstance(stored, dict):
                    merged = merge_default_groups(stored)
                    if merged != stored:
                        _write_row(connection, merged)
                    return merged

            source = _read_json(Path(legacy_path)) or _read_seed(Path(seed_path)) or _read_json(Path(default_path)) or default_config()
            migrated = merge_default_groups(source)
            _write_row(connection, migrated)
            return migrated
        finally:
            connection.close()
    except (OSError, sqlite3.Error, TypeError, ValueError):
        # Read-only or damaged environments can still run from JSON/defaults.
        source = _read_json(Path(legacy_path)) or _read_seed(Path(seed_path)) or _read_json(Path(default_path)) or default_config()
        return merge_default_groups(source)


def _write_row(connection: sqlite3.Connection, config: Mapping[str, Any]) -> None:
    payload = json.dumps(config, ensure_ascii=False, separators=(",", ":"))
    connection.execute(
        "INSERT INTO app_settings(key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (CONFIG_KEY, payload, datetime.now(timezone.utc).isoformat()),
    )
    connection.commit()


def load_account(*, db_path: Path = DB_PATH) -> dict[str, Any] | None:
    """Load the most recently imported account JSON and its display name."""
    connection = _connect(Path(db_path))
    try:
        row = connection.execute(
            "SELECT value, updated_at FROM app_settings WHERE key = ?", (ACCOUNT_KEY,)
        ).fetchone()
    finally:
        connection.close()
    if not row:
        return None
    try:
        stored = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(stored, dict) or not isinstance(stored.get("account"), dict):
        return None
    return {
        "filename": str(stored.get("filename") or "账号符文.json"),
        "account": stored["account"],
        "updated_at": row[1],
    }


def save_account(
    account: Mapping[str, Any],
    filename: str = "账号符文.json",
    *,
    db_path: Path = DB_PATH,
) -> dict[str, Any]:
    """Replace the persisted account JSON used by the local preview."""
    if not isinstance(account, Mapping):
        raise ValueError("账号 JSON 必须是对象")
    safe_name = Path(str(filename or "账号符文.json")).name
    stored = {"filename": safe_name, "account": dict(account)}
    payload = json.dumps(stored, ensure_ascii=False, separators=(",", ":"))
    updated_at = datetime.now(timezone.utc).isoformat()
    connection = _connect(Path(db_path))
    try:
        connection.execute(
            "INSERT INTO app_settings(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (ACCOUNT_KEY, payload, updated_at),
        )
        connection.commit()
    finally:
        connection.close()
    return {"filename": safe_name, "account": dict(account), "updated_at": updated_at}


def save_config(
    config: Mapping[str, Any],
    *,
    db_path: Path = DB_PATH,
    legacy_path: Path = LEGACY_CONFIG_PATH,
) -> dict[str, Any]:
    """Atomically update SQLite and refresh the legacy JSON backup."""
    normalized = dict(config)
    connection = _connect(Path(db_path))
    try:
        _write_row(connection, normalized)
    finally:
        connection.close()

    # Keep older scanner builds/tools usable, but SQLite remains authoritative.
    try:
        legacy = Path(legacy_path)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        temp = legacy.with_suffix(".tmp")
        temp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(legacy)
    except OSError:
        pass
    return normalized
