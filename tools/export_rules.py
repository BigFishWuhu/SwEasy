"""Export only rule settings into a new, account-free release database."""

import argparse
import json
import os
import sqlite3
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_KEY = "rune_filter_config"
RULE_FIELDS = {"version", "algorithm", "mode", "preset", "fallback", "groups", "retention_policy"}


def export_rules(source: Path, destination: Path) -> None:
    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        raise ValueError("The release seed must not overwrite the local database")
    connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT value FROM app_settings WHERE key = ?", (CONFIG_KEY,)
        ).fetchone()
    finally:
        connection.close()
    if not row:
        raise ValueError("No rune rules found in the source database")
    rules = json.loads(row[0])
    if not isinstance(rules, dict) or not isinstance(rules.get("groups"), list):
        raise ValueError("Invalid rule configuration")
    if set(rules) - RULE_FIELDS:
        raise ValueError("Unknown top-level settings: review them before publishing")
    rules["preset"] = "release-rules"
    if isinstance(rules.get("retention_policy"), dict):
        rules["retention_policy"].pop("account", None)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=destination.parent, suffix=".sqlite3")
    os.close(descriptor)
    try:
        connection = sqlite3.connect(temporary)
        try:
            connection.execute(
                "CREATE TABLE app_settings (key TEXT PRIMARY KEY, "
                "value TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO app_settings VALUES (?, ?, ?)",
                (CONFIG_KEY, json.dumps(rules, ensure_ascii=False, separators=(",", ":")), ""),
            )
            connection.commit()
        finally:
            connection.close()
        os.replace(temporary, destination)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "config/rune-filter.sqlite3")
    parser.add_argument("--output", type=Path, default=ROOT / "assets/rune-filter.rules.sqlite3")
    arguments = parser.parse_args()
    export_rules(arguments.source, arguments.output)
    print(f"Exported rules only: {arguments.output}")
