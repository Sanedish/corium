"""Stacked drives: folder drives that answer the same URLs (a hostname in common, one prefix at or above the other's).

    python tests/stacked_test.py        (run_all.py runs it)

The first drive that has the path answers, the last one answers for what none has. Before, the first drive answered
everything and a second drive on the same address was unreachable: its files, their ?w= variants and their link
previews were all 404.

Part 1 needs nothing installed. In throwaway instances it checks that a set-up without stacked drives gets exactly
the config it had, where the drive pick sits in the config and which steps follow it, and what the URL tester says.

Part 2 starts a real Caddy on the generated config, in front of a stub Corium Transform, and asks for files on each
drive, on both hostnames, with link modifiers and as a link-preview crawler. Without a Caddy (STRATA_CADDY, or caddy
on the PATH) it is skipped.
"""
import http.client
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_all as ra  # noqa: E402

CODE = ra.CODE
CADDY = os.environ.get("STRATA_CADDY") or shutil.which("caddy")
HOST, ALT = "cdn.dev.example.com", "alt.dev.example.com"
CRAWLER = "Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)"
FAILS = []


def check(ok, label, detail=""):
    print(("PASS " if ok else "FAIL ") + label + ("" if ok or detail == "" else " - %s" % (detail,)))
    if not ok:
        FAILS.append(label)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


SEED = '''
import json, os, sys
import accounts, core, rules
core.init_db(); db = core.connect(); rules.seed(db); db.commit(); db.close()
accounts.has = lambda *a, **k: True  # the seed acts as the owner (the test folders are outside the storage roots)
for body in json.loads(sys.argv[1]):
    rules.save_mount("dev", body)
db = core.connect()
with open(os.path.join(core.HOME, "generated", "cdn.caddy"), "w", encoding="utf-8", newline="\\n") as f:
    f.write(rules.render(db))  # also without a save (the plain instance): the config as Corium would write it
db.close()
out = {"ids": {m["name"]: m["id"] for m in core.mounts(refresh=True)}, "urls": {}}
for u in json.loads(sys.argv[2]):
    out["urls"][u] = rules.test_url(u)
print(json.dumps(out))
'''


def put(folder, name, text):
    p = os.path.join(folder, *name.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


def instance(root, stacked, ports=None, urls=()):
    """A throwaway instance (run_all's): its own folder drive at /, and with `stacked` a second folder on the same
    address (and on one more hostname) plus a third one at /docs."""
    home, first = ra.make_home(root)
    second, deep = os.path.join(root, "second"), os.path.join(root, "deep")
    if ports:
        cfgp = os.path.join(home, "strata.json")
        cfg = json.load(open(cfgp))
        cfg["instance"]["cdn_listen"] = ":%d" % ports["cdn"]
        cfg["transform_listen"] = "127.0.0.1:%d" % ports["transform"]
        json.dump(cfg, open(cfgp, "w"), indent=2)
    for folder, name, text in ((first, "one.txt", "one on first"), (first, "both.txt", "both on first"),
                               (first, "pic1.png", "PNG first"), (first, "docs/inner.txt", "inner on first"),
                               (first, "folder/a.txt", "a"),
                               (second, "two.txt", "two on second"), (second, "both.txt", "both on second"),
                               (second, "pic2.png", "PNG second"), (second, "docs/low.txt", "low on second"),
                               (deep, "top.txt", "top on deep"), (deep, "both.txt", "both on deep")):
        put(folder, name, text)
    drives = [{"name": "Second", "kind": "files", "path": second, "prefix": "/", "hosts": [HOST, ALT]},
              {"name": "Deep", "kind": "files", "path": deep, "prefix": "/docs", "hosts": [HOST]}] if stacked else []
    env = dict(os.environ, STRATA_HOME=home, PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-c", SEED, json.dumps(drives), json.dumps(list(urls))], cwd=CODE, env=env,
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit("seeding failed:\n" + p.stdout + p.stderr)
    gen = os.path.join(home, "generated", "cdn.caddy")
    out = json.loads(p.stdout.strip().splitlines()[-1])
    return {"home": home, "gen": gen, "text": open(gen, encoding="utf-8").read(), "ids": out["ids"], "urls": out["urls"]}


def block(text, name):
    """The lines of matcher @name."""
    lines = text.splitlines()
    for i, l in enumerate(lines):
        if l.strip().startswith("@%s " % name):
            if not l.rstrip().endswith("{"):
                return [l.strip()]
            j = i + 1
            while lines[j].strip() != "}" or lines[j].index("}") != l.index("@"):
                j += 1
            return [x.strip() for x in lines[i:j + 1]]
    return []


def part1(root):
    plain = instance(os.path.join(root, "plain"), False)
    t = plain["text"]
    check("strata_drive" not in t and "# stacked drives" not in t, "one drive per address: no drive pick in the config")
    first = [i for n, i in plain["ids"].items() if n not in ("Second", "Deep")]
    check(any(block(t, "m%d_1" % i) == ["@m%d_1 host `%s`" % (i, HOST)] for i in first),
          "one drive per address: the drive's matcher is the hostname alone, as before")

    urls = ["https://%s/%s" % (HOST, p) for p in ("one.txt", "two.txt", "both.txt", "none.txt", "docs/top.txt",
                                                  "docs/inner.txt", "docs/low.txt")] + ["https://%s/one.txt" % ALT]
    st = instance(os.path.join(root, "stacked"), True, urls=urls)
    t = st["text"]
    two, deep = st["ids"]["Second"], st["ids"]["Deep"]
    one = next(i for n, i in st["ids"].items() if n not in ("Second", "Deep") and "@m%d_1" % i in t and "@tf%d_1" % i in t)
    pos = lambda s: t.index(s)  # noqa: E731
    check("# stacked drives" in t and pos("# access rules") < pos("# stacked drives") < pos("# link modifiers") < pos("# drives")
          and ("# short links" not in t or pos("# short links") < pos("# stacked drives")),
          "order: access rules, short links, the drive pick, then link modifiers and drives")
    pick = t.split("# stacked drives")[1].split("\n\t\t# ", 1)[0]  # up to the next step's comment
    order = [pick.index("strata_drive d%d" % i) for i in (two, one, deep)]
    check(order == sorted(order), "the pick is written from the last drive to the first: an earlier drive that has the "
          "path overwrites it")
    check("vars strata_drive" not in pick, "the pick never tests what an earlier line picked (Caddy merges routes with "
          "the same matcher)")
    check(block(t, "st%d_1" % two) == ["@st%d_1 host `%s` `%s`" % (two, HOST, ALT)],
          "the last drive takes its hostnames without a look at the disk")
    b = block(t, "st%d_1" % one)
    check("host `%s`" % HOST in b and "try_files {path} {path}/" in b and ALT not in " ".join(b),
          "an earlier drive takes a request when it has the file or folder, on the shared hostname only")
    check("vars_regexp st%d_1 {path} `(?i)^/docs(/.*)?$`" % deep in pick and "try_files {re.st%d_1.1} {re.st%d_1.1}/" % (deep, deep) in pick,
          "a drive at a prefix looks for its drive-relative path")
    for i, name in ((one, "first"), (two, "second"), (deep, "prefixed")):
        want = "vars strata_drive d%d" % i
        check(all(want in block(t, n % i) for n in ("m%d_1", "dl%d_1", "tf%d_1", "ver%d_1", "pvp%d_1", "pvc%d_1")),
              "%s drive: the drive, ?dl=1, image modifiers, ?ver= and link previews all follow the pick" % name)
    res = {u.split("/", 3)[3] if HOST in u else "alt:" + u.split("/", 3)[3]: r for u, r in st["urls"].items()}
    said = lambda k: " | ".join([res[k]["result"]] + res[k]["steps"])  # noqa: E731
    check(res["one.txt"]["result"].startswith("200") and "Drive “Second”" not in said("one.txt")
          and "share this address" in said("one.txt"),
          "URL tester: a file on the first drive is served by it", said("one.txt"))
    check(res["two.txt"]["result"].startswith("200") and "Drive “Second”" in said("two.txt"),
          "URL tester: a file only the second drive has is served by the second drive", said("two.txt"))
    check(res["both.txt"]["result"].startswith("200") and "Drive “Second”" not in said("both.txt"),
          "URL tester: a file on both drives comes from the first", said("both.txt"))
    check(res["none.txt"]["result"].startswith("404"), "URL tester: a file on no drive is a 404", said("none.txt"))
    check("Drive “Deep”" in said("docs/top.txt") and "Drive “Second”" in said("docs/low.txt")
          and res["docs/inner.txt"]["result"].startswith("200") and "Drive “Deep”" not in said("docs/inner.txt"),
          "URL tester: under the prefix, the prefixed drive first, then the drives below it")
    check(res["alt:one.txt"]["result"].startswith("404"), "URL tester: a hostname only the second drive has never "
          "reaches the first drive", said("alt:one.txt"))


class Transform(http.server.BaseHTTPRequestHandler):
    """Stub Corium Transform: says which drive Caddy asked it for."""

    def do_GET(self):
        body = ("transform mount=%s embed=%s" % (self.headers.get("X-Strata-Mount"), self.headers.get("X-Strata-Embed") or "")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def part2(root):
    ports = {"cdn": free_port(), "transform": free_port()}
    inst = instance(os.path.join(root, "live"), True, ports)
    one = next(i for n, i in inst["ids"].items() if n not in ("Second", "Deep") and "@tf%d_1" % i in inst["text"])
    two, deep = inst["ids"]["Second"], inst["ids"]["Deep"]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", ports["transform"]), Transform)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    main = os.path.join(root, "Caddyfile.test")
    with open(main, "w") as f:
        f.write("{\n\tadmin off\n\tauto_https off\n}\nimport %s\n" % inst["gen"].replace("\\", "/"))
    env = dict(os.environ, APPDATA=root, XDG_CONFIG_HOME=root, HOME=root)  # Caddy's autosave stays in the test folder
    v = subprocess.run([CADDY, "validate", "--adapter", "caddyfile", "--config", main], capture_output=True, text=True, env=env)
    check(v.returncode == 0, "caddy validate accepts the config with stacked drives", (v.stderr or v.stdout)[-1200:])
    if v.returncode:
        srv.shutdown()
        return
    proc = subprocess.Popen([CADDY, "run", "--adapter", "caddyfile", "--config", main], env=env,
                            stdout=subprocess.DEVNULL, stderr=open(os.path.join(root, "caddy.log"), "w"))

    def get(path, host=HOST, **headers):
        c = http.client.HTTPConnection("127.0.0.1", ports["cdn"], timeout=15)
        try:
            c.request("GET", path, headers=dict({"Host": host}, **headers))
            r = c.getresponse()
            return r.status, r.read().decode("utf-8", "replace")
        finally:
            c.close()
    try:
        for _ in range(60):
            try:
                socket.create_connection(("127.0.0.1", ports["cdn"]), 0.5).close()
                break
            except OSError:
                time.sleep(0.25)
        for path, want, label in (
                ("/one.txt", (200, "one on first"), "a file on the first drive"),
                ("/two.txt", (200, "two on second"), "a file only the second drive has (was 404)"),
                ("/both.txt", (200, "both on first"), "a file on both drives comes from the first"),
                ("/docs/top.txt", (200, "top on deep"), "a file on the drive at /docs"),
                ("/docs/both.txt", (200, "both on deep"), "the drive at /docs answers before the drives under it"),
                ("/docs/inner.txt", (200, "inner on first"), "a /docs file the prefixed drive lacks comes from the first drive"),
                ("/docs/low.txt", (200, "low on second"), "a /docs file only the last drive has"),
                ("/two.txt?dl=1", (200, "two on second"), "?dl=1 on the second drive")):
            got = get(path)
            check(got == want, "real request: %s" % label, "%s -> %s" % (path, got))
        for path in ("/none.txt", "/docs/none.txt", "/folder"):
            st, _ = get(path)
            check(st == 404, "real request: %s on no drive is a 404" % path, st)
        got = get("/none.png?w=10")  # the real Transform answers 404 for it; the stub says who was asked
        check(got == (200, "transform mount=%d embed=" % two), "real request: ?w= of a picture on no drive goes to the "
              "last drive's Transform (which answers 404)", got)
        check(get("/two.txt", host=ALT) == (200, "two on second"), "real request: the second drive's own hostname")
        check(get("/one.txt", host=ALT)[0] == 404, "real request: that hostname never reaches the first drive")
        for path, mid, label in (("/pic1.png?w=10", one, "first"), ("/pic2.png?w=10", two, "second"),
                                 ("/pic2.png?ver=1", two, "second")):
            got = get(path)
            check(got == (200, "transform mount=%d embed=" % mid), "real request: %s goes to Transform for the %s drive"
                  % (path, label), got)
        for path, mid, label in (("/one.txt", one, "first"), ("/two.txt", two, "second"), ("/docs/top.txt", deep, "prefixed")):
            got = get(path, **{"User-Agent": CRAWLER})
            check(got == (200, "transform mount=%d embed=1" % mid), "real request: a crawler's link preview of %s is "
                  "drawn for the %s drive" % (path, label), got)
    finally:
        proc.terminate()
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            proc.kill()
        srv.shutdown()
        srv.server_close()


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # the URL tester's steps use arrows and quotes
    root =tempfile.mkdtemp(prefix="strata-stacked-scratchpad-")
    try:
        part1(root)
        if CADDY:
            part2(root)
        else:
            print("skipped: real requests need a caddy (put it on the PATH or set STRATA_CADDY)")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("\n%d failed" % len(FAILS) if FAILS else "\nall passed")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
