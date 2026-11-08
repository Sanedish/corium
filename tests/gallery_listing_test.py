"""Gallery listings and folder cards show only what the drive serves.

    python tests/gallery_listing_test.py        (run_all.py runs it)

Caddy's browse puts the whole folder listing (names, sizes, dates) into the gallery page source; the page used to hide
dot-files only in the browser, and files covered by hide or forbid rules were not left out at all. gallery.html now
filters the listing on the server with the rules Caddy hands it (rules.listing_filter). A link-preview crawler on a
gallery folder gets the folder's card (render_folder_cards, before the gallery route, which used to answer it with
the listing page), and the card draws the folder's pictures: the same filter applies, and a picture the scan gate
holds is not drawn (cards.gate_filter).

Part 1 needs nothing installed: the path-pattern translation follows Caddy's `path` matcher, the scan gate settings,
and a folder card drawn with a held picture in the folder. Part 2 starts a real Caddy on a throwaway instance's
generated config, reads the listing JSON in the gallery page, and opens every entry the listing left out, which must
still answer 404 / 403 (so the regexes and Caddy's own matchers agree); with the real Transform behind it, a crawler
on the folder gets the card. Without a Caddy (STRATA_CADDY, or caddy on the PATH) the requests are skipped.
"""
import http.client
import http.server
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_all as ra  # noqa: E402
from anubis_test import instance, free_port, HOST, BROWSER, CRAWLER  # noqa: E402

sys.path[:0] = [ra.CODE, os.path.join(ra.CODE, "vendor"), os.path.join(ra.CODE, "transform")]
CADDY = os.environ.get("STRATA_CADDY") or shutil.which("caddy")
FAILS = []

RULES = [
    {"name": "Gallery", "action": "gallery", "patterns": ["/gal/*"]},
    {"name": "Hide secret", "action": "hide", "patterns": ["/gal/secret/*"]},
    {"name": "No PSD", "action": "deny", "patterns": ["/gal/*.psd"]},
    {"name": "Hide drafts", "action": "hide", "patterns": ["re:^/gal/draft-[0-9]{2}\\."]},  # braces reach Caddy escaped
    {"name": "Hide tmp", "action": "hide", "patterns": ["/gal/*.tmp"]},  # path.Match: * stays in one segment
    {"name": "One PSD is fine", "action": "allow", "patterns": ["/gal/cover.psd"]},
]
FILES = ["gal/.env", "gal/Thumbs.db", "gal/secret/x.txt", "gal/art.PSD", "gal/cover.psd", "gal/draft-01.txt",
         "gal/draft-1.txt", "gal/notes.tmp", "gal/sub/notes.tmp", "gal/weird {path}.txt", "gal/System Volume Information/x"]
LISTED = {"a.txt", "pic.jpg", "cover.psd", "draft-1.txt", "sub/", "weird {path}.txt"}  # Caddy names folders "name/"
LEFT_OUT = {".env": 404, "Thumbs.db": None, "secret/x.txt": 404, "art.PSD": 403, "draft-01.txt": 404, "notes.tmp": 404,
            "System Volume Information/": None}


def check(ok, label, detail=""):
    print(("PASS " if ok else "FAIL ") + label + ("" if ok or detail == "" else " - %s" % (detail,)))
    if not ok:
        FAILS.append(label)


def picture(path, rgb):
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new("RGB", (320, 240), rgb).save(path)


def red_pixels(jpeg):
    """How many clearly red pixels a card has (a held picture in the tests is pure red, the others blue)."""
    import io
    from PIL import Image
    im = Image.open(io.BytesIO(jpeg)).convert("RGB")
    px = im.tobytes()
    return sum(1 for i in range(0, len(px), 3) if px[i] > 90 and px[i] > 2 * px[i + 2] and px[i] > 2 * px[i + 1])


class Gate(http.server.BaseHTTPRequestHandler):
    """A stub of Corium Media's /_scan: held.png is held, everything else may be served."""
    asked = []

    def do_GET(s):
        Gate.asked.append((s.headers.get("X-Strata-Mount"), s.headers.get("X-Forwarded-Uri")))
        s.send_response(403 if s.headers.get("X-Forwarded-Uri", "").endswith("/held.png") else 200)
        s.send_header("Content-Length", "0")
        s.end_headers()

    def log_message(*a):
        pass


def part1(root):
    import rules
    cases = [("/gal/secret/*", "/gal/secret/x.txt", True), ("/gal/secret/*", "/gal/secretx", False),
             ("*.psd", "/gal/ART.PSD", True), ("*.psd", "/gal/a.psdx", False),
             ("/gal/*.tmp", "/gal/n.tmp", True), ("/gal/*.tmp", "/gal/sub/n.tmp", False),
             ("*draft*", "/x/my-draft/y", True), ("/a/b", "/A/B", True), ("/a/b", "/a/b/c", False),
             ("/a/?.txt", "/a/1.txt", True), ("/a/?.txt", "/a/12.txt", False), ("/a.b", "/axb", False)]
    for pat, path, want in cases:
        got = bool(re.search(rules.caddy_path_rx(pat), path))
        check(got == want, "path pattern %s %s %s" % (pat, "matches" if want else "does not match", path))
    check(rules.caddy_path_rx("/caf%C3%A9/*") is None, "a %-escaped pattern is left out (Caddy compares it escaped)")

    hide, allow = rules.listing_filter([dict(r, mount=None, id=i) for i, r in enumerate(RULES)], {"id": 1})
    check(bool(re.search(hide, "/gal/secret/")) and not re.search(hide, "/gal/pic.jpg"), "the hide filter covers hide and forbid rules")
    check(bool(re.search(allow, "/gal/cover.psd")), "allow rules are the exceptions")
    mixed = [{"id": 1, "mount": None, "action": "hide", "patterns": ["/x/*"]},
             {"id": 2, "mount": 7, "action": "allow", "patterns": ["/x/ok"]}]
    check(rules.listing_filter(mixed, {"id": 7})[1] == "", "a drive's allow doesn't except a rule for every drive")
    check(rules.listing_filter([], {"id": 1}) == ("", ""), "no rules, no filter")

    # the scan gate settings a folder card gets (Corium Media faked: the test instance runs without it)
    real = rules.media_rules

    class Media:
        gate = {"enabled": True, "categories": ["image"]}
        fail = False

        @staticmethod
        def enabled():
            if Media.fail:
                raise RuntimeError("media settings unreadable")
            return True

        @staticmethod
        def _m():
            return {"gate": Media.gate}

        @staticmethod
        def listen():
            return "127.0.0.1:18184"
        gated_regex = staticmethod(real.gated_regex) if real else staticmethod(lambda c: r"(?i)\.(?:png|jpg)$")
    try:
        rules.media_rules = Media
        url, rx = rules.scan_gate({"kind": "files"})
        check(url == "http://127.0.0.1:18184/_scan" and re.search(rx, "a.PNG") and not re.search(rx, "a.txt"),
              "with the scan gate on, folder cards ask media's /_scan about gated pictures", (url, rx))
        Media.gate = {"enabled": False, "categories": ["image"]}
        check(rules.scan_gate({"kind": "files"}) == ("", ""), "with the gate off, nothing to ask")
        Media.fail = True
        url, rx = rules.scan_gate({"kind": "files"})
        check(url and re.search(rx, "a.png") and re.search(rx, "a.txt"), "unreadable media settings: every picture counts as held")
    finally:
        rules.media_rules = real

    # a folder card with a held picture: drawn without it, and the gate's answer is part of the cache key
    import cards
    folder = os.path.join(root, "pics")
    picture(os.path.join(folder, "a.png"), (20, 40, 230))
    picture(os.path.join(folder, "held.png"), (240, 0, 0))
    picture(os.path.join(folder, "b.png"), (20, 40, 230))
    port = free_port()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Gate)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        cache = os.path.join(root, "tcache")
        plain, _ = cards.card(folder, None, cache, True, "/", {})
        check(red_pixels(plain) > 1000, "control: without the gate the held (red) picture is in the card", red_pixels(plain))
        gate = cards.gate_filter("http://127.0.0.1:%d/_scan" % port, 3, "/gal/pics/", r"(?i)\.png$")
        gated, _ = cards.card(folder, None, cache, True, "/", {}, None, gate)
        check(red_pixels(gated) == 0, "with the gate, the held picture is not drawn", red_pixels(gated))
        check(("3", "/gal/pics/held.png") in Gate.asked, "the gate is asked with the drive and the picture's URL", Gate.asked)
        closed = cards.gate_filter("http://127.0.0.1:1/_scan", 3, "/gal/pics/", r"(?i)\.png$")
        none, _ = cards.card(folder, None, cache, True, "/", {}, None, closed)
        check(red_pixels(none) == 0, "a gate that doesn't answer means no pictures")
    finally:
        srv.shutdown()
        srv.server_close()


def get(port, path, ua=BROWSER):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    c.request("GET", path, headers={"Host": HOST, "User-Agent": ua, "Accept": "text/html", "Sec-Fetch-Dest": "document",
                                    "X-Strata-Gallery-Hide": "nothing", "X-Strata-Gallery-Allow": ".*",  # a client's own
                                    "X-Strata-Scan": "", "X-Strata-Scan-Ext": "nothing"})                # are replaced
    r = c.getresponse()
    return r.status, r.getheader("Content-Type", ""), r.read()


def wait_port(port):
    for _ in range(80):
        try:
            socket.create_connection(("127.0.0.1", port), 0.5).close()
            return True
        except OSError:
            time.sleep(0.25)
    return False


def part2(root):
    ports = {"cdn": free_port(), "transform": free_port()}
    inst = instance(root, None, RULES, ports)
    files = os.path.join(inst["home"], "files")
    for name in FILES:
        os.makedirs(os.path.dirname(os.path.join(files, name)), exist_ok=True)
        open(os.path.join(files, name), "w").write("x")
    picture(os.path.join(files, "gal", "pic.jpg"), (20, 40, 230))
    t = inst["text"]
    check(re.search(r"request_header X-Strata-Gallery-Path \{http\.request\.uri\.path\}\n\s+request_header X-Strata-Gallery-Hide", t)
          is not None, "the gallery route sets the path, then the filters, before anything rewrites the path")
    check("[0-9]\\{2\\}" in t, "braces in a rule's regex reach the header escaped, so Caddy doesn't read them as placeholders")
    check(t.index("# access rules") < t.index("# gallery folder cards") < t.index("# gallery folders") < t.index("# link previews"),
          "order: access rules, gallery folder cards, galleries, then file link previews")
    # the folder card gets the same filter (header_up, escaped like request_header; both go through Caddy's header
    # placeholders), which cards.py applies to its listing
    gal_hdr = set(re.findall(r"request_header (X-Strata-Gallery-(?:Hide|Allow)) `([^`]*)`", t))
    card_hdr = set(re.findall(r"header_up (X-Strata-Gallery-(?:Hide|Allow)) `([^`]*)`", t))
    check(len(gal_hdr) == 2 and card_hdr == gal_hdr, "the folder card gets the same filter as the listing", (gal_hdr, card_hdr))
    check("header_up -X-Strata-Scan\n" in t, "media off: no scan gate for folder cards, and a client's own header is dropped")
    import cards
    sent = {k: v.replace(r"\{", "{").replace(r"\}", "}") for k, v in gal_hdr}  # what Caddy's replacer makes of it
    keep = cards.rule_filter("/gal/", sent.get("X-Strata-Gallery-Hide", ""), sent.get("X-Strata-Gallery-Allow", ""))
    shown, dirs = cards.listing(os.path.join(files, "gal"))
    shown = {f[0] for f in shown if keep(f[0], False)} | {d + "/" for d in dirs if keep(d, True)}
    check(shown == LISTED, "the folder card leaves out the same entries as the listing", sorted(shown ^ LISTED))
    if not CADDY:
        print("skipped the request tests: no caddy found (put it on the PATH or set STRATA_CADDY)")
        return
    main = os.path.join(root, "Caddyfile.test")
    with open(main, "w") as f:
        f.write("{\n\tadmin off\n\tauto_https off\n}\nimport %s\n" % inst["gen"].replace("\\", "/"))
    env = dict(os.environ, APPDATA=root, XDG_CONFIG_HOME=root, HOME=root)
    v = subprocess.run([CADDY, "validate", "--adapter", "caddyfile", "--config", main], capture_output=True, text=True, env=env)
    check(v.returncode == 0, "caddy validate accepts the gallery routes", (v.stderr or v.stdout)[-800:])
    if v.returncode:
        return
    procs = [subprocess.Popen([CADDY, "run", "--adapter", "caddyfile", "--config", main], env=env, cwd=root,
                              stdout=subprocess.DEVNULL, stderr=open(os.path.join(root, "caddy.log"), "w")),
             subprocess.Popen([sys.executable, os.path.join(ra.CODE, "transform", "transform.py"), "--home", inst["home"]],
                              env=inst["env"], cwd=ra.CODE, stdout=subprocess.DEVNULL,
                              stderr=open(os.path.join(root, "transform.log"), "w"))]
    try:
        check(wait_port(ports["cdn"]) and wait_port(ports["transform"]), "Caddy and Transform are up")
        st, _, page = get(ports["cdn"], "/gal/")
        m = re.search(r'<script id="listing" type="application/json">(.*?)</script>', page.decode("utf-8", "replace"), re.S)
        check(st == 200 and m is not None, "the gallery page renders", (st, page[:300]))
        if m:
            names = {i["name"] for i in json.loads(m.group(1))}
            check(names == LISTED, "the listing names exactly what the drive serves", sorted(names ^ LISTED))
        for name, want in LEFT_OUT.items():
            if want:
                st, _, _ = get(ports["cdn"], "/gal/" + name)
                check(st == want, "%s still answers %d when opened" % (name, want), st)
        for name in ("cover.psd", "draft-1.txt"):
            st, _, _ = get(ports["cdn"], "/gal/" + name)
            check(st == 200, "%s is listed and served" % name, st)

        # a crawler on the folder gets its card (Open Graph page, then ?card=1), never the listing
        st, ctype, page = get(ports["cdn"], "/gal/", ua=CRAWLER)
        text = page.decode("utf-8", "replace")
        check(st == 200 and 'property="og:image"' in text and 'id="listing"' not in text,
              "a crawler on a gallery folder gets the Open Graph page, not the listing", (st, ctype, text[:300]))
        st, ctype, img = get(ports["cdn"], "/gal/?card=1", ua=CRAWLER)
        check(st == 200 and ctype.startswith("image/jpeg") and len(img) > 1000, "?card=1 on the folder is the card image",
              (st, ctype, len(img)))
        st, _, _ = get(ports["cdn"], "/gal/secret/", ua=CRAWLER)
        check(st == 404, "a hidden folder gets no card", st)
        st, _, page = get(ports["cdn"], "/gal/")
        check(st == 200 and b'id="listing"' in page, "a browser on the folder still gets the gallery", st)
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()


def main():
    root = tempfile.mkdtemp(prefix="strata-gallery-")
    try:
        part1(root)
        part2(os.path.join(root, "inst"))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print("\n%d failed" % len(FAILS) if FAILS else "\nall passed")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
