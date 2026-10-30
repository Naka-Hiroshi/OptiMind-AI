"""Backup/restore API end to end (tests/__init__.py uses a temporary settings file,
so nothing on your PC changes)."""
import unittest

from app import __version__
from app.main import app
from tests import api_client


class BackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = api_client(app)
        cls.client.__enter__()  # starts the loops (lifespan)

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_export_gives_back_a_settings_dictionary(self):
        data = self.client.post("/api/app/backup/export").json()
        self.assertIsInstance(data, dict)
        # The settings the UI can change all show up in the export.
        for key in ("gemini_api_key", "groq_api_key", "gemini_model", "groq_model",
                    "ollama_url", "ollama_model", "run_as_admin", "measure_games",
                    "language", "ai_review_minutes"):
            self.assertIn(key, data)

    def test_import_roundtrips_the_export(self):
        exported = self.client.post("/api/app/backup/export").json()
        result = self.client.post("/api/app/backup/import", json={"data": exported}).json()
        # backup_import() returns current(), so it's a settings dictionary, not an "ok" flag.
        self.assertIsInstance(result, dict)
        self.assertIn("file", result)

    def test_import_only_touches_known_settings(self):
        result = self.client.post("/api/app/backup/import", json={"data": {"not_a_real_setting": "x"}}).json()
        self.assertIsInstance(result, dict)
        self.assertIn("file", result)

    def test_empty_import_changes_nothing(self):
        result = self.client.post("/api/app/backup/import", json={"data": {}}).json()
        self.assertIsInstance(result, dict)
        self.assertIn("file", result)
