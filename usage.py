"""Corium usage statistics: opt-in, anonymous, aggregate numbers about how Corium is used.

Off unless the owner says yes (setup.py asks, Settings › System can change it at any time). docs/USAGE-STATISTICS.md
lists every field, why it is collected and what happens to it. In short:

  * once a day, one HTTPS POST of the JSON built by `build()` to `usage.endpoint`. Nothing else, nothing in between.
  * only counts, sums, averages and fixed words (OS family, CPU architecture, file category, feature name).
    Never: IP addresses, host or domain names, paths, file or folder names, user names, e-mail addresses, user agents,
    countries, keys, tokens, passwords, config values, log lines.
  * big numbers are rounded to 3 significant digits and machine sizes to buckets, so a report can't be matched to a
    machine or a site by its exact numbers.
  * the instance is identified by a random id made when usage statistics are switched on (not derived from the
    machine or the site). Switching off with "delete" asks the collector to erase everything sent under that id
    and forgets the id; switching on again starts a new one.
  * DO_NOT_TRACK=1 or CORIUM_NO_USAGE=1 in the environment turns it off for good, whatever the setting says.

    python usage.py --home <instance home> --show     print the report exactly as it would be sent, send nothing
"""
import json
import os
import platform
import re
import secrets
import sys
import time
import urllib.error
import urllib.request

import core
from core import CFG, VERSION, connect, log

SCHEMA = 1
# where reports go. Overridable per instance (strata.json "usage": {"endpoint": ...}); always https.
DEFAULT_ENDPOINT = "https://usage.boykisser.cloud/v1/report"
MIN_GAP = 20 * 3600                      # at most one report per ~day, whatever triggers it
DOC_URL = "https://github.com/Sanedish/corium/blob/main/docs/USAGE-STATISTICS.md"
ID_RE = re.compile(r"^[0-9a-f]{32}$")
SIZE_EDGES = [(100 << 10, "<100KB"), (1 << 20, "<1MB"), (10 << 20, "<10MB"), (100 << 20, "<100MB"),
              (1 << 30, "<1GB"), (None, ">=1GB")]


# ---------------------------------------------------------------- consent state

def env_blocked():
    """DO_NOT_TRACK (consoledonottrack.com) or CORIUM_NO_USAGE: off, and the dashboard says why."""
    for name in ("DO_NOT_TRACK", "CORIUM_NO_USAGE"):
        if str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on"):
            return name
    return ""


def conf():
    return CFG.get("usage") or {}


def endpoint():
    """https only; plain http just for a collector on this machine (testing)."""
    url = str(conf().get("endpoint") or DEFAULT_ENDPOINT)
    if url.startswith("https://") or re.match(r"^http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?/", url):
        return url
    return ""


def active():
    c = conf()
    return bool(c.get("enabled")) and not env_blocked() and bool(endpoint()) and bool(ID_RE.match(c.get("id") or ""))


def status():
    """What the dashboard shows: never the key."""
    db = connect()
    try:
        last = core.get_meta(db, "usage_last") or {}
    finally:
        db.close()
    c = conf()
    return {"enabled": bool(c.get("enabled")), "asked": bool(c.get("asked")), "active": active(),
            "blocked_by": env_blocked(), "endpoint": endpoint(), "id": c.get("id") or "", "doc": DOC_URL,
            "last": {k: last.get(k) for k in ("ts", "ok", "error")}, "last_report": last.get("report")}


def enable():
    c = conf()
    upd = {"enabled": True, "asked": True}
    if not ID_RE.match(c.get("id") or "") or not c.get("key"):
        upd.update(id=secrets.token_hex(16), key=secrets.token_urlsafe(32))
    core.save_config({"usage": upd})


def disable(delete=False):
    """Stop sending. With delete, ask the collector to erase this id's reports, then forget the id (a later
    opt-in starts over as a new, unrelated instance). If the collector can't be reached the id and key are kept,
    so the deletion can be tried again. Returns the collector's answer for delete, else None."""
    c, answer = conf(), None
    has_id = bool(ID_RE.match(c.get("id") or "") and c.get("key"))
    if delete and has_id and endpoint():
        answer = _post(endpoint().rsplit("/", 1)[0] + "/delete", {"id": c["id"], "key": c["key"]})
    upd = {"enabled": False, "asked": True}
    delete = delete and (not has_id or bool((answer or {}).get("ok")))  # nothing sent yet, or erased
    if delete:
        upd.update(id="", key="")
    core.save_config({"usage": upd})
    if delete:
        db = connect()
        try:
            with db:
                db.execute("DELETE FROM meta WHERE k='usage_last'")
        finally:
            db.close()
    return answer


def dismiss():
    """The owner answered "not now" to the dashboard's question: don't ask again, stay off."""
    core.save_config({"usage": {"asked": True}})


# ---------------------------------------------------------------- the report

def r3(n):
    """Round to 3 significant digits (12345 -> 12300). None stays None."""
    if n is None:
        return None
    n = float(n)
    if n == 0:
        return 0
    from math import floor, log10
    d = 2 - int(floor(log10(abs(n))))
    v = round(n, d)
    return int(v) if d <= 0 or v == int(v) else v


def bucket(n, edges):
    """The smallest edge >= n (machine sizes: 7.8 GB RAM -> 8)."""
    if n is None:
        return None
    for e in edges:
        if n <= e:
            return e
    return edges[-1]


def _os():
    sysname = platform.system() or "unknown"
    rel = platform.release() or ""
    if sysname == "Windows":
        try:
            rel = "11" if int(platform.version().split(".")[2]) >= 22000 else rel  # older Pythons say "10" on 11
        except (IndexError, ValueError):
            pass
    elif sysname in ("Linux", "Darwin", "FreeBSD"):
        rel = ".".join(rel.split(".")[:2])  # kernel major.minor, no build or distro strings
    return {"family": sysname if sysname in ("Windows", "Linux", "Darwin", "FreeBSD") else "other",
            "release": re.sub(r"[^0-9.]", "", rel)[:8], "arch": _arch()}


def _arch():
    m = (platform.machine() or "").lower()
    return {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "arm64", "aarch64": "arm64", "x86": "x86",
            "i386": "x86", "i686": "x86"}.get(m, "other")


def _ram_bytes():
    try:
        s = core.host_sample()
        if s:
            return s["mem_total"]
    except Exception:
        pass
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        return None


def _metric(db, key, since):
    row = db.execute("SELECT avg(val), max(val), count(*) FROM metric WHERE key=? AND ts >= ?", (key, since)).fetchone()
    if not row or not row[2]:
        return None
    vals = [v for (v,) in db.execute("SELECT val FROM metric WHERE key=? AND ts >= ? AND val IS NOT NULL "
                                     "ORDER BY val", (key, since))]
    p95 = vals[min(len(vals) - 1, int(len(vals) * 0.95))] if vals else None
    return {"avg": round(row[0], 1) if row[0] is not None else None, "p95": round(p95, 1) if p95 is not None else None}


def _count(db, sql, args=()):
    try:
        return db.execute(sql, args).fetchone()[0] or 0
    except Exception:  # an older database without the table
        return None


def _storage(db):
    """Files Corium serves: how many, how big, which kinds (fixed category words only, never a name)."""
    cats = {c: {"files": 0, "bytes": 0} for c in list(core.CATEGORIES) + ["other"]}
    sizes = {label: 0 for _, label in SIZE_EDGES}
    files = total = 0
    try:
        for cat, n, b in db.execute("SELECT cat, count(*), coalesce(sum(size),0) FROM fidx GROUP BY cat"):
            k = cat if cat in cats else "other"
            cats[k]["files"] += n
            cats[k]["bytes"] += b
            files += n
            total += b
        case = " ".join("WHEN size < %d THEN '%s'" % (e, label) for e, label in SIZE_EDGES if e)
        for label, n in db.execute("SELECT CASE %s ELSE '>=1GB' END k, count(*) FROM fidx GROUP BY k" % case):
            if label in sizes:
                sizes[label] += n
    except Exception:
        pass
    if not files:  # no file index yet: the inventory the compression scan keeps
        inv = core.get_meta(db, "inventory") or {}
        files, total = inv.get("files") or 0, inv.get("bytes") or 0
        for c in inv.get("cats") or []:
            k = c.get("k") if c.get("k") in cats else "other"
            cats[k]["files"] += c.get("c") or 0
            cats[k]["bytes"] += c.get("b") or 0
    return {"files": r3(files), "bytes": r3(total), "avg_file_bytes": r3(total / files) if files else None,
            "categories": {k: {"files": r3(v["files"]), "bytes": r3(v["bytes"])} for k, v in cats.items() if v["files"]},
            "sizes": {k: r3(v) for k, v in sizes.items()}}


def _traffic(db, since):
    """Requests Corium saw in the last 24 hours, from the hourly rollups: totals, file categories, status classes."""
    import rollup
    out = {"requests": None, "bytes": None, "visitors": None}
    try:
        sp = rollup.Span(db, since)
        inner, args = sp.union("*", "host")
        t = db.execute("SELECT coalesce(sum(c),0), coalesce(sum(b),0), coalesce(sum(e4),0), coalesce(sum(e5),0), "
                       "coalesce(sum(bots),0), coalesce(sum(scan),0), coalesce(sum(saved),0), coalesce(sum(enc),0) "
                       "FROM (%s)" % inner, args).fetchone()
        out = {"requests": r3(t[0]), "bytes": r3(t[1]), "errors_4xx": r3(t[2]), "errors_5xx": r3(t[3]),
               "bot_requests": r3(t[4]), "probe_requests": r3(t[5]), "compression_saved_bytes": r3(t[6]),
               "compressed_responses": r3(t[7]), "visitors": r3(rollup.hll_count(sp.hll(db, "*")))}
        inner, args = sp.union("k, c, b", "cat")
        cats = {}
        for k, c, b in db.execute("SELECT k, sum(c), sum(b) FROM (%s) GROUP BY k" % inner, args):
            k = k if k in core.CATEGORIES or k in ("other", "pages") else "other"
            o = cats.setdefault(k, {"requests": 0, "bytes": 0})
            o["requests"] += c or 0
            o["bytes"] += b or 0
        out["categories"] = {k: {"requests": r3(v["requests"]), "bytes": r3(v["bytes"])} for k, v in cats.items()}
        inner, args = sp.union("k, c", "status")
        classes = {}
        for k, c in db.execute("SELECT k, sum(c) FROM (%s) GROUP BY k" % inner, args):
            if str(k).isdigit() and 100 <= int(k) < 600:
                classes[str(k)[0] + "xx"] = classes.get(str(k)[0] + "xx", 0) + (c or 0)
        out["status"] = {k: r3(v) for k, v in sorted(classes.items())}
        hist = sp.lat(db)
        p = lambda q: (lambda v: round(v, 1) if v is not None else None)(rollup.percentile(hist, q))
        out["latency_ms"] = {"p50": p(.5), "p95": p(.95)}
    except Exception as e:
        log("usage: traffic summary unavailable: %s" % e)
    return out


def _features(db):
    """Which parts of Corium are switched on: yes/no and counts, never their settings."""
    c = CFG
    al = c.get("alerts") or {}
    mounts = {}
    try:
        for kind, n in db.execute("SELECT kind, count(*) FROM mount WHERE enabled=1 GROUP BY kind"):
            mounts[kind if kind in ("files", "page", "proxy") else "other"] = n
    except Exception:
        pass
    accounts = _count(db, "SELECT count(*) FROM account WHERE enabled=1")
    enc = str(((c.get("media") or {}).get("video") or {}).get("encoder") or "cpu")
    return {
        "media": bool((c.get("media") or {}).get("enabled")),
        "video_encoder": enc if enc in ("cpu", "nvenc", "qsv", "amf", "auto") else "other",
        "compression": bool((c.get("compression") or {}).get("enabled")),
        "autoban": bool((c.get("autoban") or {}).get("enabled")),
        "bot_challenge": bool((c.get("anubis") or {}).get("enabled")),
        "cloudflare_api": bool((c.get("cloudflare") or {}).get("api_token")),
        "tunnel": os.path.exists(str(c.get("cloudflared_config") or "")),
        # published = a stats.<zone> host (rules.stats_host); telemetry.json alone exists on every started instance
        "public_stats_page": any(str(h).startswith("stats.") for h in (c.get("public") or {}).get("hosts") or []),
        "alerts": bool(al.get("provider") or al.get("resend_api_key") or (al.get("smtp") or {}).get("host")
                       or al.get("webhooks")),
        "two_factor": bool((c.get("auth") or {}).get("totp_secret")),
        "public_hosts": len((c.get("public") or {}).get("hosts") or []),
        "drives": mounts,
        "rules": r3(_count(db, "SELECT count(*) FROM rule WHERE enabled=1 AND coalesce(builtin,0)=0")),
        "share_links": r3(_count(db, "SELECT count(*) FROM link WHERE enabled=1")),
        "accounts": bucket(accounts, [1, 2, 5, 10, 25, 50, 100, 1000, 10 ** 6]) if accounts is not None else None,
    }


def build(now=None):
    """The report, exactly as sent. Every value is a number, a bool, null or one of a fixed set of words."""
    now = now or time.time()
    since = now - 86400
    c = conf()
    ram = _ram_bytes()
    db = connect()
    try:
        # install age: the owner account's creation, else the oldest kept metric
        first = _count(db, "SELECT min(created) FROM account") or _count(db, "SELECT min(ts) FROM metric")
        report = {
            "schema": SCHEMA,
            "id": c.get("id") or "",
            "corium": {"version": re.sub(r"[^0-9A-Za-z.+-]", "", VERSION)[:20],
                       "python": "%d.%d" % sys.version_info[:2],
                       "days_installed": bucket((now - first) / 86400 if first else 0,
                                                [1, 7, 30, 90, 180, 365, 730, 10 ** 6])},
            "machine": dict(_os(), cpu_threads=bucket(os.cpu_count() or 1, [1, 2, 4, 8, 12, 16, 24, 32, 64, 128, 10 ** 4]),
                            ram_gb=bucket(ram / 2 ** 30 if ram else None, [1, 2, 4, 8, 16, 32, 64, 128, 256, 10 ** 4])),
            "resources_24h": {"cpu_pct": _metric(db, "host_cpu", since), "mem_pct": _metric(db, "host_mem", since),
                              "caddy_cpu_pct": _metric(db, "caddy_cpu", since)},
            "features": _features(db),
            "storage": _storage(db),
            "traffic_24h": _traffic(db, since),
        }
    finally:
        db.close()
    return report


# ---------------------------------------------------------------- sending

def _post(url, body):
    data = json.dumps(body, separators=(",", ":")).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "Corium/" + VERSION})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return {"ok": 200 <= r.status < 300, "status": r.status}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "error": "HTTP %d" % e.code}
    except Exception as e:
        return {"ok": False, "error": type(e).__name__}


def send(force=False):
    """Called hourly by Housekeeping; sends at most once per MIN_GAP. Returns what happened (or None: nothing to do)."""
    if not active():
        return None
    db = connect()
    try:
        last = core.get_meta(db, "usage_last") or {}
    finally:
        db.close()
    if not force and time.time() - (last.get("ts") or 0) < MIN_GAP:
        return None
    report = build()
    payload = dict(report, key=conf().get("key") or "")   # the deletion key: the collector keeps only its hash
    res = _post(endpoint(), payload)
    db = connect()
    try:
        with db:
            core.set_meta(db, "usage_last", {"ts": time.time(), "ok": res.get("ok"), "error": res.get("error"),
                                             "report": report})
    finally:
        db.close()
    if not res.get("ok"):
        log("usage statistics: not delivered (%s)" % res.get("error"))
    return res


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Corium usage statistics (see docs/USAGE-STATISTICS.md)")
    ap.add_argument("--home", help="instance home")
    ap.add_argument("--show", action="store_true", help="print the report as it would be sent; send nothing")
    a = ap.parse_args()
    if a.show:
        print(json.dumps(build(), indent=2))
        st = status()
        print("\nusage statistics are %s%s" % ("ON" if st["active"] else "OFF",
                                               " (%s is set)" % st["blocked_by"] if st["blocked_by"] else ""),
              file=sys.stderr)
    else:
        ap.print_help()
