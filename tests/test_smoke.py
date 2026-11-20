"""Smoke tests for VHS-Sat-Lite.

Run:  .venv/Scripts/python.exe -m unittest discover -s tests -v
Тесты используют временную БД и временную папку HLS, реальные данные не трогают.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="vhs_test_"))
db.DB_PATH = TMP / "test.db"

import server  # noqa: E402  (импорт после подмены БД)

server.DATA_DIR = TMP / "data"
server.app.config["TESTING"] = True


class VhsSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = server.app

    def _client(self, admin: bool = True):
        client = self.app.test_client()
        if admin:
            with client.session_transaction() as sess:
                sess["user"] = {"username": "admin", "role": "admin"}
        return client

    def test_setup_cannot_overwrite_admin(self):
        with patch.object(type(server.config), "has_users", return_value=True), \
             patch.object(type(server.config), "set") as set_mock:
            r = self._client(admin=False).post(
                "/setup",
                data={"username": "HACKER", "password": "secret1",
                      "password2": "secret1"},
                follow_redirects=False,
            )
        self.assertEqual(r.status_code, 302)
        set_mock.assert_not_called()

    def test_events_limit_is_safe(self):
        client = self._client()
        for url in ("/events?limit=abc", "/events?limit=99999", "/events?limit=-5"):
            self.assertEqual(client.get(url).status_code, 200, url)

    def test_create_channel_defaults(self):
        cid = db.create_channel({
            "name": "TEST-DEFAULTS", "source_type": "folder", "folder": "X",
        })
        try:
            ch = db.get_channel(cid)
            self.assertEqual(ch["rotation"], "random")
            self.assertEqual(ch["reshuffle_on_loop"], 0)
            self.assertEqual(ch["hls_enabled"], 1)
            self.assertEqual(ch["enabled"], 1)
        finally:
            db.delete_channel(cid)

    def test_delete_channel_removes_files(self):
        cid = db.create_channel({
            "name": "TEST-DELETE", "source_type": "folder", "folder": "X",
        })
        hls_dir = server.DATA_DIR / "hls" / str(cid)
        hls_dir.mkdir(parents=True, exist_ok=True)
        (hls_dir / "index.m3u8").write_text("#EXTM3U", encoding="utf-8")
        ticker = server.DATA_DIR / "ticker" / f"{cid}.txt"
        ticker.parent.mkdir(parents=True, exist_ok=True)
        ticker.write_text("test", encoding="utf-8")

        r = self._client().post(f"/channels/{cid}/delete",
                                follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(db.get_channel(cid))
        self.assertFalse(hls_dir.exists())
        self.assertFalse(ticker.exists())

    def test_pages_render(self):
        client = self._client()
        for url in ("/", "/channels", "/settings", "/about", "/events"):
            self.assertEqual(client.get(url).status_code, 200, url)
        self.assertEqual(self._client(admin=False).get("/login").status_code, 200)


if __name__ == "__main__":
    unittest.main()
