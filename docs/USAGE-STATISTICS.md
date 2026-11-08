# Usage statistics

Corium can send anonymous usage statistics to its developers. **It is off unless the owner of an instance says
yes.** This page lists everything that is sent, why, and what happens to it. The code is [`usage.py`](../usage.py);
it is short and meant to be read.

> Not to be confused with **Corium Telemetry** (`telemetry/`): that is the optional public statistics page
> (`stats.<your domain>`) an instance can publish about *its own* traffic. It never sends anything to the Corium
> developers.

## Why

To know how many Corium instances exist, what they run on and how they are used, so that work goes where it
matters: which operating systems and Python versions to support, which features are used, what file sizes and
amounts of traffic Corium has to handle well.

## How you are asked

* `setup.py` asks once: *Send anonymous usage statistics? (y/N)*. Pressing Enter means **no**.
* Instances installed or upgraded without that question: the owner account is asked once in the dashboard
  (**Yes** / **No thanks** / **Preview report**). Closing the question without answering keeps it off and asks again
  on the next visit.
* Only the owner (root) account can see or change the setting: **System › Usage statistics**. Administrators can't.
* `DO_NOT_TRACK=1` or `CORIUM_NO_USAGE=1` in the environment of the Corium process turns it off for good, whatever
  the setting says.

## What is sent

Once a day (at most once every 20 hours), one HTTPS `POST` with a JSON body to the collector at
`https://usage.boykisser.cloud/v1/report` (run by the Corium developer; an instance can point `usage.endpoint` in
`strata.json` elsewhere). To see the exact
report your instance would send, without sending it:

* **System › Usage statistics › Preview report** in the dashboard, or
* `python usage.py --home <instance home> --show`

| Field | Example | What it is |
|---|---|---|
| `schema` | `1` | Report format version |
| `id` | `9d5f…cc25` | Random id, made when you switch usage statistics on. Not derived from the machine, the site or anything else. |
| `key` | — | Random deletion key (sent, never shown). The collector stores only its SHA-256 and uses it so that only you can replace or delete your reports. Kept in `data/.env`. |
| `corium.version` | `0.5.1` | Corium version |
| `corium.python` | `3.12` | Python major.minor |
| `corium.days_installed` | `30` | Age of the install, bucketed (1, 7, 30, 90, 180, 365, 730, more) |
| `machine.family` | `Windows` | Windows, Linux, Darwin, FreeBSD or other |
| `machine.release` | `11` | Windows 10/11, or the kernel's major.minor (`6.8`). No build numbers, no distribution name. |
| `machine.arch` | `x86_64` | x86_64, arm64, x86 or other |
| `machine.cpu_threads` | `8` | Logical CPUs, bucketed (1, 2, 4, 8, 12, 16, 24, 32, 64, 128, more) |
| `machine.ram_gb` | `16` | RAM, bucketed (1, 2, 4, 8, 16, 32, 64, 128, 256, more) |
| `resources_24h.cpu_pct`, `.mem_pct`, `.caddy_cpu_pct` | `{"avg": 4.8, "p95": 11.3}` | Machine CPU and memory load and Caddy's CPU over the last 24 hours (average, 95th percentile) |
| `features.*` | `true` / `false` | Which parts are switched on: media pipeline, precompression, auto-ban, bot challenge, Cloudflare API, Cloudflare Tunnel, public stats page, alerts, two-factor sign-in. `video_encoder`: cpu, nvenc, qsv, amf, auto or other |
| `features.public_hosts`, `.drives`, `.rules`, `.share_links`, `.accounts` | `3`, `{"files": 2, "proxy": 1}` | How many public host names, drives per kind, custom rules and share links there are (counts only). Accounts bucketed (1, 2, 5, 10, 25, 50, 100, 1000, more). |
| `storage.files`, `.bytes`, `.avg_file_bytes` | `3540`, `10200000000` | Files Corium serves: how many, total size, average size |
| `storage.categories` | `{"image": {"files": 2890, "bytes": 7300000000}}` | The same per file type (image, video, audio, 3d, web, text, font, archive, document, other) |
| `storage.sizes` | `{"<100KB": 2460, …}` | How many files fall into each size class (<100 KB, <1 MB, <10 MB, <100 MB, <1 GB, ≥1 GB) |
| `traffic_24h.requests`, `.bytes`, `.visitors` | `2520`, `164000000`, `55` | Requests, bytes served and distinct visitors (an estimate) in the last 24 hours |
| `traffic_24h.errors_4xx`, `.errors_5xx`, `.bot_requests`, `.probe_requests` | `19` | Counts of client errors, server errors, bot requests and vulnerability probes |
| `traffic_24h.compression_saved_bytes`, `.compressed_responses` | `2950000` | What precompression saved |
| `traffic_24h.categories`, `.status` | `{"image": {"requests": 31, "bytes": 95700000}}`, `{"2xx": 2490}` | Requests and bytes per file type; responses per status class |
| `traffic_24h.latency_ms` | `{"p50": 0.9, "p95": 2.2}` | Response time percentiles |

Every number of 1000 or more is **rounded to 3 significant digits** (12 345 → 12 300), so a report can't be matched
to a site by its exact figures.

## What is never sent

No IP addresses (of visitors or of your machine - the collector does not store the address a report came from), no
host or domain names, no URLs or paths, no file or folder names, no user names, e-mail addresses or account details,
no user agents, countries or anything else about visitors, no keys, tokens or passwords, no configuration values,
no log lines. Every value is a number, `true`/`false`, `null`, or one of the fixed words listed above; the
collector rejects any report that contains anything else.

## What happens to it

* The collector keeps one report per instance per day, the hour it arrived and the hash of the deletion key. It
  keeps no IP address, no request headers and no access log. Rate limits use the sender's address in memory only.
* Reports are deleted after 400 days.
* They are read only by the Corium developers, as totals and distributions (how many instances, which versions,
  median traffic, …). They are not sold or shared, and not combined with other data.
* The collector has no read API: the data can only be viewed on the machine it is stored on.

## Switching off, deleting

**System › Usage statistics**:

* **Switch off** stops sending. Reports already sent stay until they expire.
* **Switch off and delete sent data** (or **Delete sent data** once it is off) asks the collector to erase every
  report sent under your id, then forgets the id. Switching on again later starts a new, unrelated id. If the
  collector can't be reached, the id is kept so you can try again.

Uninstalling Corium simply stops the reports; to also erase what was sent, use *Switch off and delete* first.
