"""The public statistics page (Corium Telemetry) on a new install: setup.py's question, host list, tunnel rules,
service names and port; rules.ensure_stats_mount for a stats host added after the first run; usage.py's feature flag.
Throwaway home, no services, no network, no Caddy."""
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

HOME = tempfile.TemporaryDirectory(prefix="strata-stats-")
os.environ["STRATA_HOME"] = HOME.name
CODE = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(CODE), str(CODE / "vendor")]
import core  # noqa: E402
import rules  # noqa: E402
import setup  # noqa: E402
import usage  # noqa: E402

core.init_db()


def answers(*lines):
    """setup.ask answering `lines` in order, then Enter (the default) like a closed stdin."""
    it = iter(lines)
    return mock.patch.object(setup, "ask", lambda q, default="": next(it, "") or default)


class SetupQuestion(unittest.TestCase):
    def test_off_by_default(self):
        with answers(""), mock.patch.object(setup, "say"):
            self.assertEqual(setup.step_stats_host("example.org", "cdn.example.org", []), "")

    def test_yes_gives_stats_domain(self):
        with answers("y"), mock.patch.object(setup, "say"):
            self.assertEqual(setup.step_stats_host("example.org", "cdn.example.org", []), "stats.example.org")

    def test_rerun_keeps_it_on(self):
        hosts = ["cdn.example.org", "example.org", "stats.example.org"]
        with answers(""), mock.patch.object(setup, "say"):
            self.assertEqual(setup.step_stats_host("example.org", "cdn.example.org", hosts), "stats.example.org")

    def test_new_domain_gets_new_stats_host(self):
        with answers("y"), mock.patch.object(setup, "say"):
            self.assertEqual(setup.step_stats_host("example.net", "cdn.example.net", ["stats.example.org"]),
                             "stats.example.net")

    def test_never_the_cdn_host(self):
        with answers("y"), mock.patch.object(setup, "say"):
            self.assertEqual(setup.step_stats_host("example.org", "stats.example.org", []), "")


class SetupSite(unittest.TestCase):
    def site(self, raw, *lines):
        cfg = {"_home": HOME.name, "update": {}}
        with answers(*lines), mock.patch.object(setup, "say"):
            setup.step_site(cfg, raw, HOME.name)
        return cfg

    def test_new_install_with_page(self):
        cfg = self.site({}, "example.org", "", os.path.join(HOME.name, "files"), "y")
        self.assertEqual(cfg["update"]["public"]["hosts"], ["cdn.example.org", "example.org", "stats.example.org"])
        self.assertEqual(cfg["site"]["stats"], "stats.example.org")

    def test_rerun_turning_it_off_keeps_hand_added_hosts(self):
        raw = {"instance": {"name": "example.org", "id": "example"},
               "public": {"hosts": ["cdn.example.org", "example.org", "stats.example.org", "img.example.org"]}}
        cfg = self.site(raw, "", "", os.path.join(HOME.name, "files"), "n")
        self.assertEqual(cfg["update"]["public"]["hosts"], ["cdn.example.org", "example.org", "img.example.org"])
        self.assertEqual(cfg["site"]["stats"], "")


class SetupPieces(unittest.TestCase):
    def test_ingress_for_both_hosts(self):
        base = "tunnel: abc\ningress:\n  - hostname: old.example.org\n    service: http://localhost:1\n  - service: http_status:404\n"
        new = setup.add_ingress(setup.add_ingress(base, "cdn.example.org", 8080), "stats.example.org", 8080)
        self.assertLess(new.index("stats.example.org"), new.index("http_status:404"))
        self.assertIn("  - hostname: stats.example.org\n    service: http://localhost:8080\n", new)

    def test_service_names(self):
        names = setup.service_names(setup.CODE, "example")
        self.assertEqual(names[2], "Strata Telemetry" if setup.WIN else "strata-telemetry")  # deploy.ps1 restarts it
        other = setup.service_names(os.path.join(HOME.name, "other"), "example")
        self.assertEqual(other, ("Strata example", "Strata Tunnel example", "Strata Telemetry example") if setup.WIN
                         else ("strata-example", "strata-tunnel-example", "strata-telemetry-example"))

    def test_port_offset_and_existing_port(self):
        self.assertEqual(setup.BASE_PORTS["telemetry"], 8082)
        home = tempfile.mkdtemp(dir=HOME.name)
        self.assertEqual(setup.ports_for({"listen": "127.0.0.1:8190"}, "", home)["telemetry"], 8182)
        os.makedirs(os.path.join(home, "telemetry"))
        with open(os.path.join(home, "telemetry", "telemetry.json"), "w") as f:
            json.dump({"listen": "127.0.0.1:9123", "feed_url": "http://127.0.0.1:8181"}, f)
        self.assertEqual(setup.ports_for({"listen": "127.0.0.1:8190"}, "", home)["telemetry"], 9123)

    def test_no_service_mode_only_prints(self):
        cfg = {"_home": HOME.name, "site": {"stats": "stats.example.org"}, "ports": {"telemetry": 8082}}
        said = []
        with mock.patch.object(setup, "say", said.append), mock.patch.object(setup, "install_task") as it, \
                mock.patch.object(setup, "install_unit") as iu, mock.patch.object(setup, "remove_service") as rm:
            setup.step_stats_service(cfg, {"instance": {"id": "example", "name": "example.org"}}, False)
            cfg["site"]["stats"] = ""
            setup.step_stats_service(cfg, {"instance": {"id": "example", "name": "example.org"}}, False)
        self.assertFalse(it.called or iu.called or rm.called)
        self.assertTrue(any("telemetry.py" in s for s in said), said)


class StatsMount(unittest.TestCase):
    def setUp(self):
        self.hosts = list(core.CFG["public"]["hosts"])
        db = core.connect()
        with db:
            db.execute("DELETE FROM mount")
            db.execute("DELETE FROM meta WHERE k IN ('stats_mount', 'site')")
            db.execute("INSERT INTO mount(id, name, kind, path, prefix, hosts, enabled) "
                       "VALUES(1, 'Storage', 'files', ?, '/', ?, 1)", (HOME.name, json.dumps(["cdn.example.org"])))
            rules.set_meta(db, "site", dict(rules.site_default(), quiet=[]))
        db.close()

    def tearDown(self):
        core.CFG["public"]["hosts"] = self.hosts

    def run_once(self):
        db = core.connect()
        try:
            with db:
                return rules.ensure_stats_mount(db)
        finally:
            db.close()

    def stats_mounts(self):
        db = core.connect()
        try:
            return [(r[0], r[1], json.loads(r[2])) for r in
                    db.execute("SELECT kind, path, hosts FROM mount WHERE name='Public statistics'")]
        finally:
            db.close()

    def test_nothing_without_stats_host(self):
        core.CFG["public"]["hosts"] = ["cdn.example.org"]
        self.assertFalse(self.run_once())
        self.assertEqual(self.stats_mounts(), [])

    def test_created_once_and_owner_changes_stick(self):
        core.CFG["public"]["hosts"] = ["cdn.example.org", "stats.example.org"]
        self.assertTrue(self.run_once())
        m = self.stats_mounts()
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0][0], "proxy")
        self.assertTrue(m[0][1].startswith("127.0.0.1:"))
        self.assertEqual(m[0][2], ["stats.example.org"])
        db = core.connect()
        site = rules.get_meta(db, "site")
        db.close()
        self.assertIn({"host": "stats.example.org", "paths": ["/api/live", "/api/stats"]}, site["quiet"])
        self.assertFalse(self.run_once())          # second start: nothing new
        db = core.connect()
        with db:
            db.execute("DELETE FROM mount WHERE name='Public statistics'")
        db.close()
        self.assertFalse(self.run_once())          # the owner deleted it: stays deleted
        self.assertEqual(self.stats_mounts(), [])

    def test_existing_drive_is_left_alone(self):
        core.CFG["public"]["hosts"] = ["cdn.example.org", "stats.example.org"]
        db = core.connect()
        with db:
            db.execute("INSERT INTO mount(name, kind, path, prefix, hosts, enabled) VALUES('Mine', 'proxy', "
                       "'127.0.0.1:9999', '/', ?, 1)", (json.dumps(["stats.example.org"]),))
        db.close()
        self.assertFalse(self.run_once())
        self.assertEqual(self.stats_mounts(), [])


class UsageFlag(unittest.TestCase):
    def test_flag_follows_the_stats_host(self):
        saved = list(core.CFG["public"]["hosts"])
        db = core.connect()
        try:
            core.CFG["public"]["hosts"] = ["cdn.example.org"]
            self.assertFalse(usage._features(db)["public_stats_page"])
            core.CFG["public"]["hosts"] = ["cdn.example.org", "stats.example.org"]
            self.assertTrue(usage._features(db)["public_stats_page"])
        finally:
            core.CFG["public"]["hosts"] = saved
            db.close()


if __name__ == "__main__":
    unittest.main()
