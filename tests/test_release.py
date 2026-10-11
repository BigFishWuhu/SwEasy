import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.rune.config_store import load_account, load_config, save_config
from tools.export_rules import export_rules
from tools.prepare_release import prepare, verify_rules
from tools.configure import configure_ocr_model


class ReleaseRulesTests(unittest.TestCase):
    def test_export_excludes_account_rows_and_deleted_private_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "local.sqlite3"
            seed = root / "seed.sqlite3"
            connection = sqlite3.connect(source)
            connection.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
            rules = {"version": 1, "preset": "private-account", "fallback": "keep", "groups": [], "retention_policy": {"account": "PRIVATE_MARKER"}}
            connection.executemany("INSERT INTO app_settings VALUES (?, ?, '')", [
                ("rune_filter_config", json.dumps(rules)),
                ("rune_account_json", json.dumps({"account": {"secret": "PRIVATE_MARKER"}})),
                ("unused", "PRIVATE_MARKER" * 1000),
            ])
            connection.commit()
            connection.close()
            export_rules(source, seed)
            verify_rules(seed)
            self.assertNotIn(b"PRIVATE_MARKER", seed.read_bytes())
            connection = sqlite3.connect(source)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM app_settings").fetchone()[0], 3)
            connection.close()
            config = load_config(db_path=root / "user.sqlite3", legacy_path=root / "missing.json", default_path=root / "default.json", seed_path=seed)
            self.assertEqual(config["preset"], "release-rules")
            self.assertIsNone(load_account(db_path=root / "user.sqlite3"))
            config["fallback"] = "sell"
            save_config(config, db_path=root / "user.sqlite3", legacy_path=root / "backup.json")
            loaded = load_config(db_path=root / "user.sqlite3", legacy_path=root / "missing.json", seed_path=seed)
            self.assertEqual(loaded["fallback"], "sell")

    def test_export_cannot_overwrite_local_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "local.sqlite3"
            with self.assertRaises(ValueError):
                export_rules(path, path)

    def test_committed_seed_is_rules_only(self):
        verify_rules(Path("assets/rune-filter.rules.sqlite3"))

    def test_linux_release_layout_can_be_prepared_repeatedly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "assets").mkdir()
            (root / "assets/rune-filter.rules.sqlite3").write_bytes(Path("assets/rune-filter.rules.sqlite3").read_bytes())
            (root / "MXU").mkdir()
            (root / "MXU/mxu").write_text("frontend", encoding="utf-8")
            (root / "MXU/LICENSE").write_text("MXU license", encoding="utf-8")
            runtime = root / ".python-build/cpython-test/bin"
            runtime.mkdir(parents=True)
            (runtime / "python3").write_text("interpreter", encoding="utf-8")
            prepare(root, "linux")
            prepare(root, "linux")
            self.assertTrue((root / "install/python/bin/python3").is_file())
            self.assertEqual((root / "install/LICENSE-MXU").read_text(), "MXU license")

    def test_complete_ocr_does_not_require_submodule_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ocr = root / "resource/model/ocr"
            ocr.mkdir(parents=True)
            for filename in ("det.onnx", "rec.onnx", "keys.txt"):
                (ocr / filename).write_text("model", encoding="utf-8")
            with patch("tools.configure.assets_dir", root):
                configure_ocr_model()


if __name__ == "__main__":
    unittest.main()
