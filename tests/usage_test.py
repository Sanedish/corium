"""Usage statistics (usage.py, docs/USAGE-STATISTICS.md): off by default, owner only, nothing identifying in the
report, the deletion key stays out of strata.json, DO_NOT_TRACK wins. Throwaway home; nothing is sent anywhere
(the only collector used is a stub on 127.0.0.1)."""
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

HOME = tempfile.TemporaryDirectory(prefix="strata-usage-")
os.environ["STRATA_HOME"] = HOME.name
for v in ("DO_NOT_TRACK", "CORIUM_NO_USAGE"):
    os.environ.pop(v, None)
CODE = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(CODE), str(CODE / "vendor")]
import accounts  # noqa: E402
import core  # noqa: E402
import strata  # noqa: E402
import usage  # noqa: E402

core.init_db()
# things that must never show up in a report: names, paths, hosts, addresses, people
SENSITIVE = ["secret-project", "holiday.jpg", "alice", "alice@example.org", "files.example.org", "203.0.113.9",
             "Mozilla", "/private/"]
WORDS = {"Windows", "Linux", "Darwin", "FreeBSD", "other", "x86_64", "arm64", "x86", "cpu", "nvenc", "qsv", "amf", "auto"}


def seed():
    db = core.connect()
    with db:
        db.execute("INSERT INTO mount(id, name, kind, path, prefix, hosts, enabled) VALUES(1, 'secret-project', "
                   "'files', 'C:/private/secret-project', '/', 'files.example.org', 1)")
        for i, (name, cat, size) in enumerate([("holiday.jpg", "image", 2_345_678), ("song.mp3", "audio", 7_000_000),
                                                ("notes.txt", "text", 1234)]):
            db.execute("INSERT INTO fidx(mount, path, parent, name, ext, cat, size, mtime, depth) VALUES(1,?,?,?,?,?,?,?,1)",
                       ("/private/" + name, "/private", name, name.rsplit(".", 1)[1], cat, size, time.time()))
        db.execute("INSERT INTO req(ts, host, method, path, status, bytes, dur, ip, country, ua, agent, os, kind) "
                   "VALUES(?, 'files.example.org', 'GET', '/private/holiday.jpg', 200, 2345678, 3, '203.0.113.9', 'DE', "
                   "'Mozilla/5.0', 'Firefox', 'Linux', 'browser')", (time.time(),))
        db.execute("INSERT INTO account(user, email, password_hash, role, created, updated) "
                   "VALUES('alice', 'alice@example.org', 'x', 'root', ?, ?)", (time.time() - 40 * 86400, time.time()))


def strings(v, out):
    if isinstance(v, str):
        out.append(v)
    elif isinstance(v, dict):
        for x in v.values():
            strings(x, out)
    elif isinstance(v, list):
        for x in v:
            strings(x, out)
    return out


class Stub(BaseHTTPRequestHandler):
    got = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        Stub.got.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
        self.send_response(204)
        self.end_headers()


class UsageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        seed()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        core.save_config({"usage": {"endpoint": "http://127.0.0.1:%d/v1/report" % cls.srv.server_port}})

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        Stub.got.clear()

    def test_off_by_default_and_sends_nothing(self):
        self.assertFalse(core.DEFAULTS["usage"]["enabled"])
        core.save_config({"usage": {"enabled": False}})
        self.assertFalse(usage.active())
        self.assertIsNone(usage.send(force=True))
        self.assertEqual(Stub.got, [])

    def test_report_has_nothing_identifying(self):
        r = usage.build()
        text = json.dumps(r)
        for s in SENSITIVE:
            self.assertNotIn(s, text)
        for s in strings(r, []):  # every string is a fixed word, a version or the random id
            self.assertTrue(s in WORDS or s == r["id"] or s in (r["corium"]["version"], r["corium"]["python"],
                                                                  r["machine"]["release"]), s)
        self.assertEqual(r["storage"]["files"], 3)
        self.assertEqual(r["storage"]["categories"]["image"], {"files": 1, "bytes": 2_350_000})  # rounded
        self.assertEqual(r["features"]["drives"], {"files": 1})

    def test_rounding(self):
        self.assertEqual(usage.r3(12345), 12300)
        self.assertEqual(usage.r3(999), 999)
        self.assertEqual(usage.r3(0), 0)
        self.assertEqual(usage.bucket(7.8, [4, 8, 16]), 8)

    def test_enable_send_delete(self):
        usage.enable()
        self.assertTrue(usage.active())
        with open(core.CONFIG_PATH, encoding="utf-8") as f:
            on_disk = json.load(f)["usage"]
        self.assertNotIn("key", on_disk)  # the deletion key lives in data/.env only
        self.assertTrue(core.CFG["usage"]["key"])
        self.assertEqual(usage.send(force=True)["ok"], True)
        self.assertIsNone(usage.send())  # at most once a day
        path, body = Stub.got[0]
        self.assertEqual(path, "/v1/report")
        self.assertEqual(body["key"], core.CFG["usage"]["key"])
        self.assertNotIn("key", json.dumps(usage.status()))  # never shown in the dashboard
        usage.disable(delete=True)
        self.assertEqual(Stub.got[-1][0], "/v1/delete")
        self.assertFalse(usage.active())
        self.assertEqual(core.CFG["usage"]["id"], "")

    def test_failed_delete_keeps_the_id(self):
        usage.enable()
        rid = core.CFG["usage"]["id"]
        with mock.patch.object(usage, "endpoint", return_value="http://127.0.0.1:9/v1/report"):  # nothing listens
            self.assertFalse(usage.disable(delete=True)["ok"])
        self.assertEqual(core.CFG["usage"]["id"], rid)  # can be tried again
        self.assertFalse(usage.active())
        usage.disable(delete=True)
        self.assertEqual(core.CFG["usage"]["id"], "")

    def test_do_not_track_wins(self):
        usage.enable()
        with mock.patch.dict(os.environ, {"DO_NOT_TRACK": "1"}):
            self.assertFalse(usage.active())
            self.assertIsNone(usage.send(force=True))
        with mock.patch.dict(os.environ, {"CORIUM_NO_USAGE": "true"}):
            self.assertFalse(usage.active())
        usage.disable()

    def test_endpoint_https_or_loopback_only(self):
        for url, ok in (("http://collector.example/v1/report", False), ("https://collector.example/v1/report", True),
                        ("http://127.0.0.1:9/v1/report", True), ("http://127.0.0.1.evil.example/v1/report", False)):
            with mock.patch.dict(core.CFG["usage"], {"endpoint": url}):
                self.assertEqual(bool(usage.endpoint()), ok, url)

    def test_owner_only(self):
        self.assertIn("system.usage", accounts.ROOT_ONLY)
        for p in ("/api/usage", "/api/usage/preview"):
            self.assertEqual(strata.route_cap(p, "POST", {}, {}), "system.usage")
            self.assertEqual(strata.route_cap(p, "GET", {}, {}), "system.usage")


if __name__ == "__main__":
    unittest.main(verbosity=1)
