# satudatascape — Fetcher Spec

Fetcher that mirrors the dataset catalogue (and, optionally, resource files) of
**Satu Data Indonesia** (`data.go.id`), the national open data portal.

Status: draft v0.1 · 2026-10-06 · all API facts below verified live on 2026-10-06.

---

## 1. Background: what the portal actually exposes

The portal *is* CKAN-backed, but it is **not** a stock CKAN site anymore:

| Assumption | Reality (verified) |
|---|---|
| `https://data.go.id/api/3/action/...` works | **404.** `data.go.id` is now a Next.js frontend. |
| `katalog.data.go.id` CKAN host | **Does not resolve** (DNS NXDOMAIN). |
| CKAN API reachable directly | Only through the frontend's proxy: `POST https://data.go.id/api/proxy`, which forwards to a Spring Boot backend that re-exposes a subset of CKAN `/api/3/action/*`. |

### 1.1 The proxy

```http
POST https://data.go.id/api/proxy
Content-Type: application/json

{"endpoint": "/api/3/action/package_search?rows=1000&start=0&sort=id%20asc",
 "method": "GET", "body": {}, "token": ""}
```

Response wraps the CKAN result in its own envelope:

```json
{"status": "200 OK", "message": "Sucess", "result": { ...CKAN result... }}
```

Unknown paths return a Spring-style error with HTTP 404:
`{"timestamp": "...", "status": 404, "error": "Not Found", "path": "/api/3/action/group_list"}`.

### 1.2 Supported actions

| Action | Works | Notes |
|---|---|---|
| `package_search` | ✅ | Main workhorse. See quirks below. |
| `package_show?id=` | ✅ | Full package dict incl. `resources`, `extras`, `organization`, `tags`. |
| `package_list` | ✅ | Names only; `limit`/`offset` honored; full list (no limit) returns 616,056 names in one response. |
| `status_show`, `organization_list`, `group_list` | ❌ 404 | Organizations must be derived from package dicts. |

### 1.3 `package_search` behaviour (the parts that matter)

| Param | Behaviour |
|---|---|
| `rows` | **Capped at 1000** (2000 → 1000 rows returned). ~3.4 MB / ~1.7 s per 1000-row page. |
| `start` | Deep paging works (tested `start=200000`). |
| `sort` | Honored. **Default is `prioritas_tahun desc` — not unique, unsafe for paging.** Use `sort=id asc` for full crawls. |
| `q` | Honored, **including Solr field syntax**: `organization:<name>`, `res_format:CSV`, `metadata_modified:[NOW-7DAYS TO *]`. |
| `fq` | ⚠️ **Silently ignored** (count unchanged). Put all filters in `q`. |
| `facet.field`, `fl` | ⚠️ Ignored. `facets` always returns `{"": {".": 5}}`. |
| `include_private` | No effect (good — public only). |
| Top-level filter params (`organization=`) | Ignored. |

### 1.4 Data shape and scale

- `package_search` count: **613,739** datasets. `package_list`: **616,056** names.
  The website shows **708,117**. The gap is unexplained (see §9 open questions);
  the fetcher reports all three numbers and does not assume they match.
- Records are **harvested** from ministry and regional CKAN/DCAT portals. `extras`
  carries `harvest_source_id`, `harvest_source_title` (e.g. `"BSSN - CKAN"`),
  `harvest_object_id`, `guid`, `dcat_issued`, `dcat_modified`,
  `dcat_publisher_name`, `accesslevel` (e.g. `"Terbatas"`), `prioritas_tahun`, `language`.
- `resources[].url` points at the **origin portal**, not data.go.id
  (e.g. `satudata.jenepontokab.go.id/dataset-to-excel/<uuid>`,
  `api-data.bssn.go.id/data-ckan/download/...`, `data.badanpangan.go.id/download/...`).
  Downloading files means hitting hundreds of different government hosts of varying quality.
- `resources[].format` is free text and dirty (`xlxs`, `xslx`, `xlxx` seen in facets). Normalise.
- `datastore_active` is `false` on sampled resources, so there's no DataStore API to lean on.
- `notes` may be truncated with `............` in search results; `package_show` returns the same text, so treat that as source data.

### 1.5 What data is available

Snapshot taken 2026-10-06 from the facets the `/dataset` page renders server-side.
These are **UI-side counts** (total 708,117). For the same filter, the API returns
roughly 85–90% of these: `kota-malang` 92,941 in the UI vs 80,659 in the API; `XLSX`
403,387 vs 367,877. Treat this table as a map of the catalogue, not as exact totals.

**What a "dataset" is here:** a catalogue record (CKAN package) holding metadata and
links to 1+ files on the publisher's own portal. data.go.id is an **aggregator**: it
hosts metadata, not the data. Content is overwhelmingly **aggregated statistical
tables** (counts and percentages per district, per year), not microdata or live APIs.

**Who publishes (445 organizations).** It's dominated by local government:

| Publisher type | Orgs | Datasets | Share |
|---|---:|---:|---:|
| Kabupaten (regency) | 261 | 382,649 | 54% |
| Kota (city) | 71 | 226,994 | 32% |
| Provinsi (province) | 31 | 81,778 | 12% |
| Badan / lembaga (national agencies: BPS, BMKG, BNPB, BIG, BSSN, Badan Pangan, BPOM, BRIN …) | 38 | 9,111 | 1.3% |
| Kementerian (ministries: Kesehatan, Keuangan, PUPR, ESDM, KKP, Perhubungan, Pendidikan …) | 39 | 5,992 | 0.8% |
| Other | 5 | 1,593 | 0.2% |

The largest single publishers are Kota Malang (92,941), Kota Semarang (31,117),
Kab. Demak (29,112), Kab. Karanganyar (27,428), Kab. Musi Banyuasin (21,566),
Kab. Bantul (15,299), DI Yogyakarta (15,290), and Kalimantan Barat (14,605).
The top 10 publishers hold ~38% of everything. Coverage across the country is
**very uneven**: many regencies publish nothing.

**Resource formats (192 distinct raw values, ~947k resources):**

| Format | Resources | Notes |
|---|---:|---|
| XLSX (+ `.xlsx`, `xlxs`, `xslx` …) | ~413k | The bulk; tables, often with merged-cell headers |
| CSV (+ `.csv`) | ~244k | Most machine-friendly |
| PDF | ~105k | Reports and scanned tables; not machine-readable |
| XLS | ~63k | Legacy Excel |
| JSON | ~43k | Often the origin portal's per-dataset API export |
| WMS / WFS | ~18k / ~16k | Geospatial services (mostly BIG / provincial geoportals) |
| XML, DOCX, HTML, TSV, JPEG, PNG, TXT, RDF, SHP … | <10k each | Long tail |

**Topics.** Tags are free text (500+ distinct). Dominant themes:
health (kesehatan, puskesmas, posyandu), education (pendidikan, sekolah),
population (penduduk, kependudukan), agriculture/livestock/fisheries,
regional finance (`SIPD`: 46k, from the Home Affairs regional-government info system),
village SDGs (`SDGS_DESA_CANTIK`), social welfare, employment, infrastructure.
The `kategori` facet (sectoral classification) covers only ~1k datasets and has
inconsistent casing (`HUKUM` vs `Hukum`), so it's not useful for browsing.

**Priority data.** `prioritas_tahun` marks national "Data Prioritas" for
2022–2026 on ~3.5k datasets. Values are dirty (`"2023, 2024"`, `"2025;2026"`,
`"Prioritas 2025"`, `"1970"`). Parse them into a set of years.

**Temporal range.** Records were created from 2022-10 onward (`metadata_created`).
~13k datasets were modified in the last 7 days, which is the expected size of a
weekly incremental run.

**Per-record metadata available** (CKAN package + harvest extras): title, notes
(description, Indonesian), organization, tags, `url` (origin page), license (mostly empty),
created/modified timestamps, `accesslevel` (e.g. `Terbatas` = restricted; the file
may need a login on the origin portal), `dcat_publisher_name`, `dcat_issued`/`dcat_modified`,
harvest source, and per-resource name/format/url/timestamps. Size and mimetype are almost always null.

**What's *not* there:** no DataStore (`datastore_active: false`), so no row-level
query API; no consistent license; no update-frequency or temporal-coverage field
(the year is usually only in the title or tags); no file sizes.

**Implications for the fetcher/UI**
- The format, `prioritas_tahun`, and kategori normalisers are worth building; the raw values are noisy.
- Org type (kabupaten/kota/provinsi/kementerian/badan) is derivable from the org `name` prefix. Add it as a UI filter.
- Expect many duplicate-looking titles across regions (the same indicator per kabupaten). Search ranking should boost title matches and allow an org filter.
- Downloading "everything" means ~950k files from ~400 origin hosts. Default to metadata only, with downloads by explicit selection.

### 1.6 Operational

- No auth required. No rate-limit headers. 12 concurrent requests → all 200.
- `robots.txt`: `Allow: /`, `Disallow: /private/`; sitemap at `/sitemap.xml`.
- The proxy is an **undocumented internal endpoint**. It can change without notice;
  the fetcher must detect drift loudly (§7).

---

## 2. Goals / non-goals

**Goals**
1. Full mirror of catalogue metadata (all package dicts) to local storage.
2. Cheap incremental refresh (only changed packages), scheduled.
3. Download resource files (best resource per dataset by default; filters by org, format, size) and **best-effort extraction into tables** with a quality grade. See [FEASIBILITY.md](FEASIBILITY.md): ~70% of files are reachable, and ~55–65% of datasets yield a usable table automatically.
4. A web UI for **1–10 internal users** to browse, search, filter, and inspect the mirror, and to see fetch-run status.
5. Be a polite client of a government service.

**Non-goals (v1)**
- Harmonising the same indicator across regions into one comparable table (needs manual curation).
- Fetching WMS/WFS geodata (kept as links), OCR of scanned PDFs.
- A public API, multi-tenant accounts, or horizontal scaling.
- Scraping the Next.js HTML; the proxy API is enough.

---

## 3. Architecture

```
          ┌──────────────┐   POST /api/proxy    ┌───────────────────────┐
 CLI ───► │ ProxyClient  │ ───────────────────► │ data.go.id (Spring→CKAN) │
          └──────┬───────┘                      └───────────────────────┘
                 │ package dicts
          ┌──────▼───────┐        ┌──────────────┐      GET resource.url
          │ CatalogCrawler│──────►│  Store (SQLite│◄──── ResourceDownloader ───► origin hosts
          │ full / incr   │       │  + raw JSONL) │       (per-host limits)
          └──────────────┘        └──────────────┘
```

### 3.0 Tech stack (sized for 1–10 users)

At this scale the right shape is **one process, one file-based DB, one container**.
There's no need for Postgres, a queue, a separate SPA, or Kubernetes.

| Concern | Choice | Why |
|---|---|---|
| Language / tooling | **Python 3.12**, `uv` (deps + venv), `ruff` (lint + format), `mypy` | One language for fetcher and UI; `uv` keeps builds fast and reproducible. |
| HTTP client | `httpx` (async) + `tenacity` | Async paging with retries; `respx` mocks it cleanly in tests. |
| Database | **SQLite** (WAL) + **FTS5** full-text index on title/notes/org/tags | ~614k rows and ~2–3 GB fit easily. FTS5 gives fast Indonesian keyword search with no extra service. WAL lets the UI read while the crawler writes. |
| DB access | stdlib `sqlite3` + hand-written SQL, numbered `migrations/*.sql` | The schema is small. An ORM would add more than it saves. |
| Web | **FastAPI** + **Jinja2** server-rendered templates + **HTMX** | Server-rendered pages with partial updates (filters, paging, run status) and no JS build step. One codebase, one deploy. |
| CSS | **Pico.css** (classless) via CDN, plus a small `app.css` | Decent default look with zero build tooling. |
| Scheduling | **APScheduler** inside the web process (daily incremental at 02:00 WIB, weekly reconcile) + a "Run now" button | No cron or worker infra. A DB lock row prevents overlapping runs. |
| CLI | `typer` | Same code paths as the scheduler, for ops and backfills. |
| Auth | Single shared login via HTTP Basic, or put it behind Tailscale / Cloudflare Access | 1–10 trusted users. The data is public anyway; auth just keeps the "Run now" button private. |
| Tests | `pytest`, `pytest-asyncio`, `respx`, FastAPI `TestClient`, recorded JSON fixtures | Fully offline test suite. |
| Packaging / deploy | Single **Docker** image; run on a small VM (1 vCPU / 1–2 GB RAM, 20 GB disk) or Fly.io/Railway **with a persistent volume** for `data/` | SQLite needs a real disk, so use a volume. Back up with a nightly `sqlite3 .backup`, or Litestream to S3/R2. |
| CI | GitHub Actions: `ruff check`, `mypy`, `pytest`, `docker build` | Same commands as the local `make check`. |

**When to outgrow this:** more than ~10 concurrent writers, multiple app instances, or analytical queries over file contents. At that point, move to Postgres (keep SQL portable now: no SQLite-only types except FTS5, which is isolated behind a `search` module) and a separate worker process.

Single-process layout (kept in sync with PLAN.md *Where*):

```
pyproject.toml  uv.lock  .python-version  Makefile  .github/workflows/ci.yml
Dockerfile  docker-compose.yml  docs/DEPLOY.md  scripts/capture_fixtures.py
src/satudatascape/
  __init__.py      # __version__
  config.py        # Settings, TOML + SDS_* env
  client.py        # ProxyClient
  ratelimit.py     # TokenBucket (injected clock)
  normalize.py     # format / org type / timestamps
  store.py         # sqlite access, upsert, migrations runner
  crawler.py       # full / incremental / reconcile
  runs.py          # runs rows + run lock
  search.py        # FTS5 + filters + facets (only SQLite-specific module)
  cli.py           # typer
  downloader.py  urllint.py  sniff.py  select.py  pipeline.py
  export.py  backup.py  scheduler.py
  extract/{__init__,csv_,json_,xlsx}.py
  migrations/0001_init.sql …   # loaded via importlib.resources
  web/
    app.py         # FastAPI factory
    routes/{datasets,detail,orgs,stats,runs,links}.py
    templates/     # Jinja2 (+ HTMX partials)
    static/app.css
tests/ (unit, fixtures/, web/)
```

### 3.1 `ProxyClient`
- `action(name: str, **params) -> dict`: builds `endpoint`, posts the envelope, unwraps `result`.
- Raises `ProxyError` on non-200, on a body without `result`, or when the 404
  Spring shape comes back. Raises `SchemaDrift` when `package_search.result`
  lacks `count`/`results`.
- Sends `User-Agent: satudatascape/<version> (+https://github.com/tomtomtomdev/satudatascape)`.
- Global concurrency limit (default **4**) and a token bucket (default **2 req/s**).
- Retries on 5xx, 429, timeouts: exponential backoff with jitter, max 5 tries. Honors `Retry-After` if one ever appears.
- Timeouts: connect 10 s, read 90 s (1000-row pages are 3.4 MB).

### 3.2 `CatalogCrawler`

**Full crawl**
1. `package_search?rows=0` → record `count_at_start`.
2. Page with `sort=id asc`, `rows=1000`, `start=0,1000,…` (~614 pages).
   Pages are independent, so run them through the client's concurrency limit.
3. Upsert each package (§4). Checkpoint the last completed `start` so a crash resumes.
4. Finish with a **reconciliation pass**: diff `package_list` (names) against stored names.
   - Names in the list but not stored → `package_show` each one (catches items that shifted during paging).
   - Names stored but no longer listed → mark `deleted_at` (soft delete).
5. Record a run summary: counts from search / list / stored, pages, errors, duration.

Why reconcile: offset paging over a live index can skip or duplicate rows when
items are inserted mid-crawl. `id asc` keeps that window small; the
`package_list` diff closes it.

**Incremental crawl** (default mode after the first full run)
1. `watermark` = max `metadata_modified` stored, minus a **1-hour overlap**.
2. `q=metadata_modified:[<watermark>Z TO *]`, `sort=metadata_modified asc, id asc`, page through.
3. Upsert. The upsert is idempotent, so the overlap is harmless.
4. Run the `package_list` reconciliation **weekly** (not on every incremental run) to pick up deletions.

Timestamps: CKAN returns naive ISO strings (`2026-06-17T08:23:12.600209`), which are UTC
in CKAN. Store them as UTC and format Solr ranges as `YYYY-MM-DDTHH:MM:SSZ`.

### 3.3 `ResourceDownloader` (opt-in)

Feasibility-driven rules (details in [FEASIBILITY.md](FEASIBILITY.md) §Design consequences):
- **Pre-flight URL lint**: dot-less hosts (the West Java harvest leaks `http://api/...`), `localhost` and private IPs → `internal_host`, never requested.
- **Sniff magic bytes** → `actual_type`; never trust the declared `format` or the `Content-Type` (~8% mismatch, mostly HTML landing pages).
- **Error taxonomy** recorded per resource (`dns`, `connect_timeout`, `read_timeout`, `tls`, `http_4xx`, `http_5xx`, `html_landing`, `ogc_exception`, `internal_host`, `no_url`, `too_large`).
- **Best resource per dataset** (CSV > JSON > XLSX > XLS > TSV > PDF), falling back to the next candidate on failure; `--all` downloads everything.
- **Host health** with cross-run backoff (1d → 3d → 7d) for dead hosts.
- Extraction (§3.4) runs after download and always keeps the raw file.

General behaviour:
- Input: a selection query over the store (`--org`, `--format`, `--harvest-source`, `--since`).
- Per-host concurrency **1** and ≥1 s spacing; global cap 8. Origin hosts are small regional servers.
- Streams to `data/files/<package_id>/<resource_id>/<sanitised filename>`. Writes to `*.part`, then renames.
- Records HTTP status, final URL, `Content-Type`, size, sha256, `ETag`/`Last-Modified`.
- Re-downloads only when resource `metadata_modified` or `last_modified` changed, or with a conditional GET (`If-None-Match` / `If-Modified-Since`).
- Size guard: skip > 500 MB by default (`--max-size`); `HEAD` first when the host supports it.
- Dead links are expected. Record them, don't fail the run. Mark a host as down after N consecutive connection errors and skip it for the rest of the run.
- TLS: some `.go.id` hosts have broken chains (4% of successful downloads in the sample). Default is verify on. `--insecure-hosts` is an explicit allowlist, logged in the run record.

### 3.4 `TableExtractor` (best-effort)
- CSV: encoding sniff (utf-8-sig → cp1252), delimiter sniff (`, ; | \t`), `newline=''`.
- JSON: record-list locator (`[...]`, `{data}`, `{records}`, `{fields,records}`, `{code,status,message,data}`, `{deskripsi,header,data}`, GeoJSON `features[].properties`).
- XLSX/XLS: header-row detection, merged-header flattening (`Tahun / 2023`), dropping title and note rows, first sheet by default.
- PDF (optional, PDF-only datasets): `pdfplumber`, ≤5 pages, no OCR.
- Output: `data/tables/<resource_id>.parquet` + a `tables` row with quality **A** (tidy as-is), **B** (header fixed or merges flattened), or **C** (raw grid only).

---

## 4. Storage

`data/satudata.db` (SQLite, WAL mode):

```sql
packages(
  id TEXT PRIMARY KEY, name TEXT UNIQUE, title TEXT,
  org_id TEXT, org_name TEXT,
  org_type TEXT,                     -- kabupaten|kota|provinsi|kementerian|badan|other (from name prefix)
  prioritas_years TEXT,              -- normalised JSON array, e.g. [2023,2024]
  harvest_source_id TEXT, harvest_source_title TEXT,
  metadata_created TEXT, metadata_modified TEXT,
  num_resources INT, private INT, state TEXT,
  raw_json TEXT NOT NULL,            -- full package dict, verbatim
  content_hash TEXT NOT NULL,        -- sha256 of canonical raw_json
  first_seen_at TEXT, last_seen_at TEXT, deleted_at TEXT
);
resources(
  id TEXT PRIMARY KEY, package_id TEXT REFERENCES packages(id),
  name TEXT, url TEXT, url_host TEXT,
  format_raw TEXT, format_norm TEXT,  -- 'xlxs' → 'XLSX', etc.
  metadata_modified TEXT, last_modified TEXT
);
organizations(id TEXT PRIMARY KEY, name TEXT, title TEXT, raw_json TEXT);  -- derived
downloads(resource_id TEXT PRIMARY KEY, status INT, path TEXT, bytes INT,
          sha256 TEXT, etag TEXT, last_modified TEXT, fetched_at TEXT,
          actual_type TEXT, error_kind TEXT, error TEXT, insecure INT DEFAULT 0);
host_health(host TEXT PRIMARY KEY, ok INT, fail INT, last_ok_at TEXT,
            last_error_kind TEXT, consecutive_failed_runs INT, retry_after TEXT);
tables(resource_id TEXT PRIMARY KEY, parser TEXT, path TEXT, n_rows INT, n_cols INT,
       columns TEXT, sheets TEXT, quality TEXT, warnings TEXT, extracted_at TEXT);
package_versions(package_id TEXT, content_hash TEXT, raw_json TEXT, seen_at TEXT,
                 PRIMARY KEY(package_id, content_hash));   -- history, only when the hash changes
runs(id INTEGER PRIMARY KEY, kind TEXT, started_at TEXT, finished_at TEXT,
     count_search INT, count_list INT, count_stored INT, upserts INT,
     changed INT, errors INT, checkpoint TEXT, notes TEXT);
```

The upsert compares `content_hash`. It only writes `package_versions` and bumps
`changed` when the hash differs, and it always updates `last_seen_at`.

Also writes `data/raw/YYYY-MM-DD/page-<start>.jsonl.gz` (one package per line)
during full crawls, so a run can be replayed without hitting the API. Size is ~2 GB raw / ~200 MB gzipped per full crawl. Rotate these.

Dataset files themselves stay out of git (`data/` in `.gitignore`).

---

## 5. CLI

```
satudatascape crawl --full                 # full mirror + reconcile
satudatascape crawl                        # incremental (default)
satudatascape reconcile                    # package_list diff only
satudatascape show <id|name>               # live package_show, pretty-printed
satudatascape search "<q>" [--rows N]      # passthrough, Solr syntax in q
satudatascape download [--org X] [--format CSV,XLSX] [--harvest-source "BSSN - CKAN"]
                       [--since 2026-01-01] [--max-size 500MB] [--dry-run]
satudatascape export --format parquet|jsonl [--out PATH]
satudatascape stats                        # counts by org / harvest source / format
```

Global flags: `--db`, `--concurrency`, `--rps`, `--log-format json|text`.

Plus `satudatascape serve [--host 0.0.0.0 --port 8000]`, which starts the web UI and the scheduler.

---

## 5b. Web UI

Server-rendered, HTMX for partial updates. Every list is paginated server-side
(50/page), so no page ever loads more than one page of rows.

| Page | Route | Content |
|---|---|---|
| Datasets | `GET /` | Search box (FTS5), filters: has usable data (quality A/B), org type, organization, harvest source, resource format, `prioritas_tahun`, modified-since. Sort: relevance / recently modified / title. Result rows show title, org, formats, modified date, # resources. Filters and paging swap `#results` via HTMX and keep the URL query string shareable. |
| Dataset detail | `GET /datasets/{name}` | Title, notes, org, tags, extras table, link to the origin portal (`url`) and to data.go.id. Resource table: name, normalised format, origin host, download status, local file link. Each resource also shows its download status and error kind, declared vs actual type, and a quality badge. A "Preview first 50 rows" HTMX panel reads from the extracted table. WMS/WFS resources render as "open in geoportal" links. Version history from `package_versions` (date + changed fields). |
| Organizations | `GET /orgs` | Org list with dataset counts; click through to a filtered Datasets view. |
| Runs | `GET /runs` | Table of `runs` (kind, start/finish, search/list/stored counts, upserts, changed, errors). The current run auto-refreshes every 5 s via `hx-trigger="every 5s"`. "Run incremental now" and "Run full now" buttons (POST, guarded by the run lock). |
| Stats | `GET /stats` | Counts by org / harvest source / format, plus totals and last successful run. Plain tables first; charts are optional later. |
| Link health | `GET /health/links` | Overall file success rate, breakdown by error kind, per-host success/backoff table, top failing publishers, declared-vs-actual format matrix. CSV export. |
| Health | `GET /healthz` | JSON: DB reachable, last run status and age. For uptime checks. |

Facet counts are computed with SQL `GROUP BY` over the filtered set and cached for
5 minutes keyed by the filter tuple. That is fine at this size.

---

## 6. Configuration

`satudatascape.toml` with env overrides (`SDS_*`):

```toml
base_url    = "https://data.go.id/api/proxy"
concurrency = 4
rps         = 2
page_size   = 1000
overlap     = "1h"
user_agent  = "satudatascape/0.1 (+https://github.com/tomtomtomdev/satudatascape)"
[download]
per_host_concurrency = 1
max_size = "500MB"
insecure_hosts = []
```

---

## 7. Robustness & drift detection

Because the proxy is undocumented:

- **Startup probe**: `package_search?rows=1`. Fail fast with a clear message if the envelope or `result.count` is missing.
- **Sanity gates** on a full run: abort without soft-deleting anything if
  `count_search` drops more than 10% against the previous run, or if a page returns 0 results before `start >= count`.
- **Fallback documented, not built**: if the proxy goes away, the Next.js `/dataset`
  page server-renders the same `package_search` payload in its RSC stream
  (`self.__next_f.push`). That is the plan B; it is not in v1 scope.
- Structured JSON logs; each run writes its `runs` row even on failure.

---

## 8. Testing

- **Unit**: envelope unwrapping, error shapes, Solr range formatting, format normalisation, content hashing, upsert idempotence.
- **Recorded fixtures** (`tests/fixtures/*.json`): real responses captured once —
  `package_search` page, `package_show`, `package_list` slice, the 404 Spring body.
  Run them through `respx`.
- **Crawler simulation**: a fake index that inserts items mid-crawl, to prove that the reconciliation catches skipped rows.
- **Live smoke test** (opt-in, `pytest -m live`): one `rows=1` search, one `package_show`. Never the full crawl in CI.

---

## 9. Open questions

1. **Count mismatch**: UI shows 708,117, `package_search` returns 613,739, `package_list` returns 616,056.
   Does the UI count include something the API hides, for example a second source or private and draft items?
   We should ask the Satu Data secretariat (Bappenas) or compare facet totals.
2. **Terms of use / attribution** for bulk mirroring and for republishing (the repo is public).
   Datasets carry mostly empty `license_id`. Decide whether the repo ships only code, or also derived metadata.
3. Is there a sanctioned public API key or endpoint (`api.data.go.id` answers **403**, which suggests something gated exists)?
4. Should organization metadata be enriched from somewhere, given `organization_list` is unavailable?
5. Scope of v1 downloads: everything, or a curated subset (e.g. `prioritas_tahun` datasets only)?

---

## 10. Delivery plan

The implementation plan, sliced into small TDD increments with progress tracking,
lives in **[PLAN.md](PLAN.md)**.

Rough cost of a full metadata crawl at 2 req/s with 4 concurrent requests: ~614 pages × ~1.7 s each, which comes to **~5–10 min** and ~2 GB of transfer.
