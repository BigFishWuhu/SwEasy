"""Assemble the MXU frontend and a relocatable uv-managed Python runtime."""

import argparse
import shutil
import sqlite3
from pathlib import Path


def verify_rules(path: Path) -> None:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        keys = connection.execute("SELECT key FROM app_settings").fetchall()
        if tables != [("app_settings",)] or keys != [("rune_filter_config",)]:
            raise ValueError("Release database must contain only rune rules")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Release database is corrupt")
    finally:
        connection.close()


def prepare(root: Path, platform: str) -> None:
    verify_rules(root / "assets/rune-filter.rules.sqlite3")
    install = root / "install"
    install.mkdir(exist_ok=True)
    executable = "mxu.exe" if platform == "win" else "mxu"
    candidates = list((root / "MXU").rglob(executable))
    if len(candidates) != 1:
        raise ValueError(f"Expected one MXU executable, found {candidates}")
    shutil.copytree(candidates[0].parent, install, dirs_exist_ok=True)
    if (install / "LICENSE").exists():
        (install / "LICENSE").replace(install / "LICENSE-MXU")
    interpreter = "python.exe" if platform == "win" else "bin/python3"
    runtimes = sorted({path.resolve() for path in (root / ".python-build").iterdir() if (path / interpreter).exists()})
    if len(runtimes) != 1:
        raise ValueError("Expected exactly one standalone Python runtime")
    shutil.copytree(runtimes[0], install / "python", symlinks=True, dirs_exist_ok=True)
    if platform == "linux":
        (install / executable).chmod(0o755)
    (install / "THIRD-PARTY-NOTICES.txt").write_text(
        "MXU v2.7.1: AGPL-3.0, unmodified binary; source: https://github.com/MistEO/MXU/tree/v2.7.1\n"
        "MaaFramework v5.14.3: LGPL-3.0; source: https://github.com/MaaXYZ/MaaFramework/tree/v5.14.3\n"
        "Python: PSF license; source: https://github.com/python/cpython\n"
        "Standalone Python: https://github.com/astral-sh/python-build-standalone\n",
        encoding="utf-8",
    )
    for filename in ("LICENSE", "LICENSE.md", "LICENSE.txt"):
        if (root / "deps" / filename).is_file():
            shutil.copy2(root / "deps" / filename, install / f"LICENSE-MaaFramework-{filename}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", choices=("win", "linux"))
    arguments = parser.parse_args()
    prepare(Path(__file__).resolve().parents[1], arguments.platform)
