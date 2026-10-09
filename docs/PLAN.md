# satudatascape — Implementation Plan

Status: S2 done — next: S3
Goal: A local mirror of the Satu Data Indonesia catalogue (and best-effort data files) with a small internal web UI, built slice by slice, test-first.

Companion to [SPEC.md](SPEC.md). The work is split into small vertical slices.
Each slice is completed by **one sub-agent task**, test-first, and ends with a green
build, a progress update in this file, a commit, and a push.

---

## Why
data.go.id holds ~614k dataset records, but only through an undocumented proxy, with dirty
metadata and files spread over ~400 regional hosts. Internal users (1–10) need a local copy
they can search, filter and inspect, kept fresh automatically, and a usable table where the
file allows one. Details: SPEC §1–2, FEASIBILITY.md.

Success criteria (each covered by the slices named in brackets):
1. A full crawl stores every package `package_search` returns and survives a crash by resuming from its checkpoint. [S5]
2. Reconciliation against `package_list` catches packages skipped during paging and soft-deletes removed ones. [S6, S8]
3. An incremental crawl fetches only packages modified since the watermark (minus a 1 h overlap), and the upsert is idempotent. [S4, S7]
4. Proxy drift (bad envelope, missing `count`/`results`, a >10% count drop, an empty page before the end) fails loudly and never soft-deletes. [S1, S5, S8]
5. The client is polite: ≤4 concurrent requests, ≤2 req/s, retries with backoff, and an identifying User-Agent. [S2]
6. Users can search (FTS5) and filter datasets, open a detail page with version history, see orgs, stats and runs, and trigger a run, all behind optional Basic auth. [S10–S16]
7. Crawls run on a schedule (daily incremental, weekly reconcile) without overlapping. [S8, S16]
8. Resource files download best-effort, with URL lint, magic-byte sniffing, an error taxonomy and per-host limits/backoff; the raw file is always kept. [S18a–S19, S21b]
9. CSV/JSON/XLSX/XLS files are extracted to Parquet with an A/B/C quality grade, and that grade is visible and filterable in the UI. [S20–S22b]
10. The catalogue exports to JSONL/Parquet, and the app ships as one Docker image with nightly backups. [S17, S24]

## What
In scope: slices S0–S24 below (S25 is optional): proxy client, normalisers, SQLite store with FTS5, full/incremental/reconcile crawler, drift gates, config + CLI, FastAPI+HTMX UI, scheduler, Docker + backup, downloader, best-resource selection + host health, CSV/JSON/XLSX/XLS extraction (+ `extract` CLI), data in the UI, link-health page, export (incl. `--with-tables`, `--include-deleted`).

**Out of scope** (SPEC §2 non-goals plus): harmonising indicators across regions; WMS/WFS data fetching (links only); OCR; a public API, multi-tenant accounts, horizontal scaling; scraping Next.js HTML (plan B is documented, not built); Litestream; charts on the Stats page; answering SPEC §9 open questions (not blocking v1); any deploy to a real host (S17 ships the image and docs — VM only, no Fly.io walkthrough); committing crawled data to git (only tiny trimmed test fixtures).

## Who
- **Users:** 1–10 internal users through the web UI; an operator using the CLI.
- **Owner/reviewer:** Tommy (repo owner).
- **Agents:** one fresh sub-agent per slice; this orchestrating session dispatches, verifies and relays.

## Where
Push target: remote `origin` → `https://github.com/tomtomtomdev/satudatascape.git`, branch `feat/satudatascape-v1` (`main` is the default branch, so slices go on a feature branch; resolved from the only remote). First push: `git push -u origin feat/satudatascape-v1`; then `git push origin feat/satudatascape-v1`. Never force-push, never push `main`.

Repo layout (SPEC §3.0 tree is updated to match):
```
pyproject.toml  uv.lock  .python-version  Makefile  .github/workflows/ci.yml
Dockerfile  docker-compose.yml  docs/DEPLOY.md  scripts/capture_fixtures.py
src/satudatascape/
  __init__.py  config.py  client.py  ratelimit.py  normalize.py  store.py
  crawler.py  runs.py  search.py  cli.py  downloader.py  urllint.py  sniff.py
  select.py  pipeline.py  export.py  backup.py  scheduler.py
  extract/{__init__,csv_,json_,xlsx}.py
  migrations/0001_init.sql … (loaded via importlib.resources, ships in the wheel)
  web/app.py  web/routes/{datasets,detail,orgs,stats,runs,links}.py
  web/templates/  web/static/app.css
tests/  tests/fixtures/  tests/web/
```
Runtime: local macOS (dev), GitHub Actions (CI, ubuntu), a Docker container on a small VM (documented only). Runtime data lives in `data/` (gitignored).

**Migrations:** each table is added by the slice that first needs it.
| File | Slice | Content |
|---|---|---|
| `0001_init.sql` | S4 | `packages`, `resources`, `organizations`, `package_versions`, `runs` (SPEC §4, plus `runs.status`, `runs.error`) |
| `0002_runlock.sql` | S8 | `run_lock(id INTEGER PRIMARY KEY CHECK(id=1), run_id, holder, acquired_at, heartbeat_at)` |
| `0003_fts.sql` | S10 | FTS5 table `packages_fts(name UNINDEXED, title, notes, org_title, tags)` |
| `0004_files.sql` | S18b | `downloads` (SPEC §4 + `package_id`, `final_url`, `content_type`), `host_health` |
| `0005_tables.sql` | S20 | `tables` (SPEC §4 + `status` = ok/no_table/error/no_text_layer) |

## When
Strictly sequential, in the order of the Progress table. No parallel slices. Milestones:
- **M1 catalogue** — S0–S10: CLI mirror with search.
- **M2 UI** — S11–S17: web UI, scheduler, Docker image.
- **M3 files** — S18a–S24: downloads, extraction, data in the UI, export.
- **M4 optional** — S25.
No deadline.

## How

### Tech stack
New project: there's nothing to detect yet, so every row is proposed in SPEC §3.0 (written by the owner) and pinned in S0. **S0 adds every dependency below except pdfplumber**, so later slices never touch `pyproject.toml` deps (S25 adds pdfplumber).

| Layer | Choice | Version | Source |
|-------|--------|---------|--------|
| Language / toolchain | Python, uv | 3.12 / uv ≥0.11 | SPEC §3.0; `.python-version` (S0) |
| HTTP | httpx (async) + tenacity | locked in `uv.lock` | SPEC §3.0 |
| DB | SQLite (WAL) + FTS5 (`unicode61 remove_diacritics 2`), stdlib `sqlite3` | bundled SQLite (3.51 under uv's 3.12) | SPEC §3.0; reviewer verified |
| Web | FastAPI + uvicorn + Jinja2 + python-multipart; HTMX + Pico.css via CDN | locked | SPEC §3.0; uvicorn/multipart added by review |
| CLI / config | typer; stdlib `tomllib` + `SDS_*` env | locked | SPEC §3.0, §6 |
| Scheduling | APScheduler + tzdata | `>=3.10,<4` | SPEC §3.0; tzdata added by review |
| Files / tables | openpyxl, xlrd, pyarrow; pdfplumber (S25 only) | locked | SPEC §3.4, FEASIBILITY |
| Tests | pytest, pytest-asyncio (`asyncio_mode=auto`), respx, FastAPI TestClient (httpx) | locked | SPEC §3.0, §8 |
| Lint / types | ruff (lint + format), mypy `strict = true` on `src/` (+ `types-openpyxl`; `ignore_missing_imports` for xlrd, pyarrow) | locked | SPEC §3.0 |
| Build / CI | GNU Make, GitHub Actions (`astral-sh/setup-uv`, `uv sync --locked`), Docker | — | SPEC §3.0 |

**New dependencies:** all of the above. They're approved by being in the owner's SPEC/PLAN; uvicorn, python-multipart, tzdata and types-openpyxl are plain runtime/typing necessities of the approved stack. Anything not listed here is a block to report, not a choice to make. No freezegun/time-machine: time is injected (see Approach).

### Approach
See SPEC §3 (architecture, client, crawler, downloader, extractor), §4 (schema), §5/§5b (CLI, UI), §6 (config) and §7 (drift). Key decisions:
- One process and one SQLite file; hand-written SQL; migrations in the package.
- **Injectable time:** `ratelimit.TokenBucket(rps, clock, sleep)` and tenacity's `sleep=` take callables, so tests use a fake clock and take no real time.
- Offset paging on `id asc`, plus a `package_list` reconcile; idempotent hash-based upsert. The checkpoint lives in `runs.checkpoint` (from S5).
- FTS lives only in `search.py`; it's synced in application code inside `Store.upsert_packages` and soft-delete (no triggers, since tags/org title live in `raw_json`). Title weighted with `bm25(packages_fts, 0, 10, 1, 2, 2)`.
- CLI `search` is a **live Solr passthrough** (SPEC §5); the UI search is local FTS.
- Facet cache key = `(filter tuple, max(runs.id) where finished_at not null)`, so a crawl from any process invalidates it.
- Web: one `APIRouter` per page under `web/routes/`; nav defined once in the S11 base template. Scheduler is gated by `scheduler.enabled` (off in tests); run as a single uvicorn worker.
- "Run now" launches the crawl with `asyncio.create_task` under the run lock (not `BackgroundTasks`).
- Downloader: errors are classified by exception type (`ConnectError` caused by `socket.gaierror` → `dns`; `ssl.SSLError` cause → `tls`; `ConnectTimeout`; `ReadTimeout`; status → `http_4xx`/`http_5xx`). Per-host concurrency is 1 with ≥1 s spacing, global cap 8; a host is marked down for the run after 3 consecutive connection errors. Paths are sanitised and must stay inside `data/files`.
- Pipeline: download → extract; extraction failure keeps the raw file and writes `tables.status='error'`. Download runs write `runs(kind='download')`.
- Quality grades. CSV/JSON: A = consistent columns with a header on row 0; B = encoding/delimiter recovered or a wrapper unwrapped; C = inconsistent column counts. XLSX: per S21a.
- Backup: Python `sqlite3.Connection.backup()` as a nightly APScheduler job, with retention; the raw JSONL is rotated by the same job.

Risks and how each is retired:
- Proxy drift → probe and gates (S8), offline fixtures, opt-in `pytest -m live`.
- Paging skips → reconcile (S6).
- 616k-name `package_list` in memory → S6 streams names into a temp table and diffs in SQL; a `slow`-marked test with 600k synthetic names keeps reconcile under 10 s / 300 MB.
- FTS/facet latency at 614k rows → `slow`-marked perf tests in S10 (search < 300 ms) and S13b (facets < 500 ms) on synthetic data.
- Link rot → error taxonomy and host backoff (S18b–S19).
- Messy spreadsheets → quality grades (S21a).
- Disk (≈10–35 GB files + 2–3 GB DB + backups) → disk budget and retention in DEPLOY.md (S17).
- Public repo / terms of use (SPEC §9.2) → only trimmed fixtures are committed; `data/`, `out/` and config are gitignored.
- No local Docker daemon → `make check` skips the docker build locally; CI builds it and runs the container smoke test.

### Commands
- **Test:** `make test` (= `uv run pytest`; `addopts = "-m 'not live and not slow'"`; `make test-slow` / `make test-live` opt in)
- **Build / check:** `make check` = `lint` (`ruff check` + `ruff format --check`) + `typecheck` (`mypy src`) + `test` + `docker-maybe` (builds only if a `Dockerfile` exists **and** `docker info` succeeds, else prints `skip docker: no Dockerfile or no daemon`; amended in S0: CI runners have a daemon, so the original daemon-only guard would fail CI until S17 adds the Dockerfile). Ruff excludes `scripts/feasibility/` (throwaway research scripts); pytest runs with `filterwarnings = error` and `--strict-markers`.
- **Run:** `uv run satudatascape <cmd>` (CLI, from S9b); `uv run satudatascape serve` (UI, from S11); before S9b, `uv run python -c …` as each slice's Run line says. Never run a full live crawl from a slice.

---

## Slice protocol

Each sub-agent, for exactly one slice:
1. **Red:** write the slice's tests; run them; they must fail for the right reason (not import/syntax).
2. **Green:** the least code to pass; refactor while green.
3. **Build:** `make check` exits 0, with no new warnings.
4. **Run:** the slice's Run step; note what it showed.
5. **Full suite:** `make test` green (pre-existing reds are reported, not hidden).
6. **Record:** fill the slice's Progress row (status `done`, date, tests, run result); update `Status:`; append to the Notes log; amend any plan section the slice proved wrong and say so in the row.
7. **Commit** code + tests + plan together: `feat(<module>): s<n> <lowercase description>` (the repo owner's convention; use `chore(...)`/`ci(...)` where apt). **No AI-attribution trailers.** `git add` only intended files (never `data/`, `out/`, `.env`, `satudatascape.toml`).
8. **Push** to `origin feat/satudatascape-v1`. From S0 on, wait for CI: `gh run watch --exit-status $(gh run list -b feat/satudatascape-v1 -L1 --json databaseId -q '.[0].databaseId')`. A red CI is fixed in this slice.
9. Fill the Commit column in a follow-up commit `plan(satudatascape): s<n> hash` and push.

No live network in tests (`respx` + `tests/fixtures/`). If `make check` can't go green, don't commit: report the block.

---

## Slices

### S0 — Project scaffold & CI
Depends on: none
Red: `tests/test_smoke.py::test_package_imports_and_has_version` (fails: no package).
Green: `uv init --package` (src layout), `.python-version` = 3.12, every stack dep except pdfplumber; ruff/mypy(strict)/pytest config (`asyncio_mode="auto"`, markers `live`, `slow`, addopts); `Makefile` (`test`, `test-slow`, `test-live`, `lint`, `fmt`, `typecheck`, `docker-maybe`, `check`); `.github/workflows/ci.yml` on push + pull_request (setup-uv, `uv sync --locked`, `make check`); extend `.gitignore` (`out/`, `satudatascape.toml`, `.env`, `*.part`, `.mypy_cache/`, `.ruff_cache/`, `.pytest_cache/`). Update the SPEC §3.0 tree to match *Where*.
Run: `uv run python -c "import satudatascape; print(satudatascape.__version__)"`.
Done when: `make check` is green locally and the CI run on the branch is green.

### S1 — Proxy client: envelope & errors
Depends on: S0
Red: `tests/test_client.py`: posts the correct JSON body (`endpoint` with URL-encoded params, `method: GET`, `body: {}`, `token: ""`); unwraps `result` for search/show/list; Spring 404 → `ProxyError` including `path`; 200 without `result` → `SchemaDrift`; `package_search` without `count`/`results` → `SchemaDrift`.
Green: `client.ProxyClient.action(name, **params)` per SPEC §3.1; `ProxyError`, `SchemaDrift`. `scripts/capture_fixtures.py` captures `package_search_rows2.json`, `package_show.json`, `package_list_5.json`, `spring_404.json` into `tests/fixtures/` (trimmed: rows=2, list limit=5). If the network is unavailable, hand-write them from the SPEC §1.1 shapes and say so.
Run: `uv run python -c "import asyncio; from satudatascape.client import ProxyClient; print(asyncio.run(ProxyClient().action('package_search', rows=0))['count'])"` → ~614k.
Done when: all five behaviours pass offline (criterion 4, envelope part).

### S2 — Proxy client: rate limit, retries, UA
Depends on: S1
Red: `tests/test_client_resilience.py`: retries on 502 then succeeds; gives up after **5 attempts in total** and raises; doesn't retry 404, nor an HTTP 500 whose envelope message says `404 NOT FOUND` (how `package_show` answers an unknown id — S1 live finding; amended in S2), nor `SchemaDrift`; honours `Retry-After`; never exceeds 4 concurrent in-flight calls (instrumented mock); 10 calls at rps=2 advance the fake clock ≥4.5 s; UA header matches SPEC §3.1; timeouts are connect 10 s / read 90 s.
Green: `ratelimit.TokenBucket` with injected `clock`/`sleep`; semaphore; tenacity with jitter and injected sleep; UA; timeouts.
Run: the S1 Run command with `rps=1`, three sequential calls, showing ~2 s elapsed.
Done when: criterion 5 is covered by tests that take <1 s of real time.

### S3 — Normalisers
Depends on: S0
Red: `tests/test_normalize.py` (table-driven): `xlxs`/`xslx`/`.xlsx`/`xlsx ` → `XLSX`; `csv/xlsx` → `CSV`; `google spreadsheet` → `GSHEET`; unknown → uppercased raw. `"2023, 2024"`, `"2025;2026"`, `"Prioritas 2025"` → year lists; `"1970"` → `[]`. `kota-malang` → `kota`; `kabupaten-demak` → `kabupaten`; `provinsi-…` → `provinsi`; `kementerian-kesehatan` → `kementerian`; `badan-pusat-statistik` → `badan`; anything else → `other`. Naive ISO → aware UTC; `solr_ts` → `YYYY-MM-DDTHH:MM:SSZ`.
Green: `normalize.py`: `format_norm`, `prioritas_years`, `org_type`, `parse_ckan_ts`, `solr_ts`.
Run: `uv run python -c "from satudatascape.normalize import format_norm as f; print([f(x) for x in ['xlxs','csv/xlsx','PDF ']])"`.
Done when: the table passes.

### S4 — Store: migrations + upsert
Depends on: S3
Red: `tests/test_store.py`: migrations are idempotent and WAL is on; a new package counts as inserted; an identical re-upsert changes nothing but bumps `last_seen_at`; a changed field writes a `package_versions` row; resources are replaced when the list shrinks; `org_type` and `format_norm` are populated; a soft-deleted package that reappears clears `deleted_at`.
Green: `migrations/0001_init.sql`, a runner (`importlib.resources`, `schema_migrations` table), `Store.upsert_packages(pkgs) -> UpsertStats`, derived resources/organizations, canonical-JSON sha256.
Run: `uv run python -c` upserting `tests/fixtures/package_show.json` into `/tmp/sds.db` twice → `inserted=1` then `unchanged=1`.
Done when: criterion 3's idempotence part passes.

### S5 — Full crawl with checkpoint/resume
Depends on: S2, S4
Red: `tests/test_crawler_full.py` (fake index): 2,500 packages → 3 pages, all stored; a crash injected after page 2 → resume skips pages 0–1 (checkpoint read from the `runs` row); an empty page before `start >= count` → `SchemaDrift`; raw JSONL.gz written when enabled, and only the last N run directories kept.
Green: `crawler.full_crawl(client, store, run_id)`: `rows=0` count, `sort=id asc&rows=1000` pages through the client's limits, a contiguous checkpoint in `runs.checkpoint`, an optional raw writer with rotation.
Run: `uv run python -c` running `full_crawl` against a respx fake of 2,500 → prints the stored count.
Done when: criterion 1 passes.

### S6 — Reconciliation
Depends on: S5
Red: `tests/test_crawler_reconcile.py`: a fake index that inserts items mid-crawl → reconcile fetches them via `package_show`; a removed name gets `deleted_at`; in sync → no-op; `slow`: 600k synthetic names diff in < 10 s.
Green: `crawler.reconcile`: stream `package_list` names into a temp table, diff in SQL, `package_show` the missing ones, soft-delete the absent ones.
Run: as S5, plus reconcile on a fake with one inserted and one removed → prints `fetched=1 deleted=1`.
Done when: criterion 2 passes.

### S7 — Incremental crawl
Depends on: S6
Red: `tests/test_crawler_incremental.py`: correct `q` and `sort` (asserted on the request); only newer packages are upserted; the overlap window doesn't double-count `changed`; an empty DB raises `NeedsFullCrawl`.
Green: `crawler.incremental` per SPEC §3.2.
Run: `uv run python -c` printing the built `q` for a seeded watermark.
Done when: criterion 3 passes.

### S8 — Runs, lock, drift gates, run_full
Depends on: S7
Red: `tests/test_runs.py`: a run row (status, error) written on success and on exception; a second concurrent run is refused; a stale lock (no heartbeat for 30 min) is taken over; a 15% count drop aborts reconcile before any soft-delete; a startup probe against a bad envelope fails fast; `run_full()` with an empty-page drift mid-crawl → reconcile not called, no `deleted_at` set.
Green: `migrations/0002_runlock.sql`; `runs.py` (`start_run`, `finish_run`, `RunLock` context manager with heartbeat); `crawler.probe`; `crawler.run_full()` / `run_incremental()` / `run_reconcile()` wiring lock + runs + gates.
Run: `uv run python -c` that holds the lock in one connection and tries a second → prints the refusal.
Done when: criteria 2, 4 and 7 (lock part) pass.

### S9a — Config
Depends on: S8
Red: `tests/test_config.py`: defaults match SPEC §6; the TOML file overrides the defaults; `SDS_*` env overrides the file; `"1h"` → timedelta, `"500MB"` → bytes; an unknown key is an error.
Green: `config.py` (`Settings` dataclass, `load(path, env)`), including `scheduler.enabled`, `auth.user`/`auth.password`, `backup.retention`.
Run: `SDS_RPS=1 uv run python -c "from satudatascape.config import load; print(load(None).rps)"` → 1.
Done when: all config tests pass.

### S9b — CLI
Depends on: S9a
Red: `tests/test_cli.py` (CliRunner + respx): `crawl`, `crawl --full`, `reconcile`, `show`, `search` (live passthrough), `stats` exit codes and key output; `NeedsFullCrawl` → exit 2 with a hint; `--db` respected; `--log-format json` emits parseable JSON lines.
Green: `cli.py` (typer) wired to `crawler.run_*`, global flags, `[project.scripts] satudatascape`.
Run: `uv run satudatascape search penduduk --rows 3` (one live request), recorded in the row.
Done when: all commands are tested offline and the live search printed 3 titles.

### S10 — FTS5 search index
Depends on: S9b
Red: `tests/test_search.py`: matches on title and notes; title matches rank above notes matches; the index is updated after an upsert and after a soft-delete; diacritics and casing handled; paging is stable; `slow`: 600k synthetic rows, query < 300 ms.
Green: `migrations/0003_fts.sql`; `search.query(conn, q, sort, page)`; sync calls inside `Store.upsert_packages`/soft-delete; a backfill on migrate.
Run: `uv run python -c` searching the S4 demo DB → prints the hit.
Done when: the search part of criterion 6 passes.

### S11 — Web skeleton
Depends on: S10
Red: `tests/web/test_app.py` (TestClient): `/healthz` JSON shows DB ok and the last run's status/age; pages return 401 without credentials when auth is configured; the base layout renders the nav (all six page links).
Green: `web/app.py` factory, `web/routes/`, base Jinja2 template (Pico + HTMX CDN), Basic auth middleware, `satudatascape serve` (uvicorn, 1 worker).
Run: `uv run satudatascape serve --port 8765 &` then `curl -s localhost:8765/healthz`.
Done when: tests pass and the curl returns JSON.

### S12 — Datasets list: paging + search
Depends on: S11
Red: `tests/web/test_datasets.py`: an empty query lists the newest first; `q` filters; page 2 offsets correctly; `HX-Request` returns the `#results` partial only; query params round-trip into pagination links.
Green: `GET /` in `routes/datasets.py`, 50/page, sort options.
Run: serve on a seeded DB, then `curl -s 'localhost:8765/?q=penduduk' | grep -c '<tr'`.
Done when: tests pass and the curl shows rows.

### S13a — Datasets list: filters
Depends on: S12
Red: `tests/web/test_filters.py`: each filter (org type, organization, harvest source, format, prioritas year via `json_each`, modified-since) narrows the results; filters combine with AND; the URL stays shareable.
Green: a filter builder in `search.py`, wired into the route.
Run: curl with `?org_type=kota&format=CSV` → fewer rows.
Done when: tests pass.

### S13b — Facet counts + cache
Depends on: S13a
Red: `tests/web/test_facets.py`: facet counts reflect the other active filters; the cache is hit on a repeated request; a new finished `runs` row changes the cache key; `slow`: facets over 600k rows < 500 ms.
Green: `GROUP BY` facets, a 5-minute TTL cache keyed per *Approach*, an HTMX filter sidebar.
Run: curl twice and print the timings.
Done when: tests pass.

### S14 — Dataset detail
Depends on: S13b
Red: `tests/web/test_detail.py`: title and notes render; an unknown name → 404; the resource table shows normalised formats; versions list the changed field names; a soft-deleted dataset shows the "removed upstream" banner.
Green: `routes/detail.py` per SPEC §5b (no download/quality columns yet).
Run: curl `/datasets/<fixture name>`.
Done when: tests pass.

### S15 — Orgs + Stats
Depends on: S14
Red: `tests/web/test_orgs_stats.py`: org counts match the store; the stats totals add up; links carry the correct filter params.
Green: `routes/orgs.py`, `routes/stats.py`.
Run: curl `/orgs` and `/stats`.
Done when: tests pass.

### S16 — Runs page, Run now, scheduler
Depends on: S15
Red: `tests/web/test_runs.py`: the runs list renders; POST starts a run (stub crawler blocking on an `Event`) and a second POST returns 409 while it runs; the partial refreshes. `tests/test_scheduler.py`: three jobs registered — daily incremental 02:00, weekly reconcile Sunday 03:00, nightly backup 03:30, all `Asia/Jakarta`; a scheduled job skips and logs when the lock is held; the scheduler doesn't start when `scheduler.enabled` is false. The backup job's body is stubbed until S17.
Green: `routes/runs.py`; `scheduler.py` (APScheduler started in the app lifespan when enabled).
Run: serve, POST `/runs` with kind=incremental against a respx-free stub mode, or just render `/runs` on the seeded DB.
Done when: criterion 7 passes.

### S17 — Docker + deploy + backup
Depends on: S16
Red: `tests/test_backup.py`: `backup.run(db, dir, retention)` produces an openable copy with the same row counts, prunes beyond retention, and rotates raw JSONL beyond N runs.
Green: `backup.py` (wired into the S16 nightly job); multi-stage `Dockerfile` (uv, non-root, `data/` volume, single worker); `docker-compose.yml`; `docs/DEPLOY.md` (VM, disk budget, retention, restore); a CI job that builds the image, runs it and `curl --retry 10 /healthz` = 200.
Run: `make check` (the docker build is skipped locally without a daemon) and the CI container job green.
Done when: criterion 10's deploy/backup part passes.

### S18a — URL lint + sniffing
Depends on: S17
Red: `tests/test_urllint.py`: `http://api/x`, `http://tes`, `localhost`, `10.0.0.1`, `192.168.x`, `127.x` → `internal_host`; empty → `no_url`; `https://data.x.go.id` → ok. `tests/test_sniff.py`: tiny fixtures (xlsx zip, docx zip, xls OLE header, `%PDF`, HTML landing, OGC `ServiceExceptionReport`, BOM CSV, JSON, empty) → `actual_type`.
Green: `urllint.py`, `sniff.py` (pure functions).
Run: `uv run python -c` linting 3 sample URLs from FEASIBILITY.
Done when: the tables pass.

### S18b — Fetch + error taxonomy + downloads row
Depends on: S18a
Red: `tests/test_downloader.py`: each error kind from its respx scenario/exception type; TLS failure off the allowlist → `tls`, on it → success with `insecure=1`; a 304 skips the rewrite; an over-size body is cut off → `too_large`; `.part` then rename; sha256 recorded; HTML landing → `html_landing`; a filename with `../` stays inside `data/files`.
Green: `migrations/0004_files.sql`; `downloader.fetch_one(resource) -> DownloadResult` and the `downloads` upsert.
Run: `uv run python -c` fetching one known-good small public file into `/tmp`.
Done when: every error kind is tested.

### S18c — Host limits + download CLI
Depends on: S18b
Red: `tests/test_download_run.py`: per-host concurrency 1 with ≥1 s spacing (fake clock); global cap 8; a host is down for the run after 3 consecutive connection errors; selection by `--org/--format/--harvest-source/--since`; `--dry-run` makes no requests; a `runs(kind='download')` row is written.
Green: `downloader.run(selection)`, `download` CLI.
Run: `uv run satudatascape download --dry-run --org kota-malang` on a seeded DB.
Done when: tests pass.

### S19 — Best-resource selection + host health
Depends on: S18c
Red: `tests/test_select.py`: ranking CSV > JSON > XLSX > XLS > TSV > PDF > other; CSV 404 → falls back to the XLSX; CSV sniffing as HTML → falls back; PDF/WMS only → PDF, geo skipped; backoff 1d → 3d → 7d across failed runs and reset on success; a host in backoff is skipped without a request.
Green: `select.py`; `host_health` updates; `--best-only` default, `--all` opt-in.
Run: dry-run download showing the chosen resource per dataset.
Done when: criterion 8 passes (excluding extraction).

### S20 — Table extraction: CSV + JSON
Depends on: S19
Red: `tests/test_extract_csv_json.py`: each JSON wrapper shape and each delimiter; cp1252 Indonesian text; embedded newlines in quoted fields; inconsistent columns → C with a warning; grades A/B per *Approach*; non-tabular JSON → `status='no_table'`; nested values serialised as JSON strings.
Green: `migrations/0005_tables.sql`; `extract/` CSV + JSON → `data/tables/<resource_id>.parquet` + `tables` row.
Run: `uv run python -c` extracting a fixture → prints rows/cols/quality.
Done when: tests pass.

### S21a — Table extraction: XLSX/XLS
Depends on: S20
Red: `tests/test_extract_xlsx.py` (workbooks built in-test with openpyxl; an `.xls` fixture): tidy → A; title rows 0–2 with header on row 3 → B with the right columns; two-level merged header → `Tahun / 2023`; a trailing "Sumber: …" row dropped; multi-sheet lists `sheets`; corrupt → `status='error'`, no crash.
Green: `extract/xlsx.py` per the S21 rules (≥60% header heuristic, forward-filled merges, first sheet, xlrd for `.xls`).
Run: extract a sample workbook → print the columns.
Done when: tests pass.

### S21b — Pipeline + extract CLI
Depends on: S21a
Red: `tests/test_pipeline.py`: download → extract runs on success; an extraction failure keeps the raw file and writes `tables.status='error'`; `satudatascape extract --resource <id>` re-extracts from the kept file without network; a skipped type (PDF, IMG) gets no `tables` row.
Green: `pipeline.py`, `extract` CLI; `download` calls the pipeline.
Run: dry-run → real run on 1 fixture-served file via respx in `uv run python -c`, or `extract --resource` on a file in `data/files`.
Done when: criteria 8 and 9 (extraction) pass.

### S22a — Data on the detail page
Depends on: S21b
Red: `tests/web/test_detail_data.py`: download status / error kind / declared vs actual type / quality badge per state; the preview renders the first 50 rows from Parquet; it falls back to the raw CSV, then to a "no preview" message; raw-file serving rejects path traversal; WMS/WFS rows render geoportal links.
Green: detail template + `GET /datasets/{name}/preview/{resource_id}` + `GET /files/{resource_id}`.
Run: curl a preview on a seeded DB with one extracted table.
Done when: tests pass.

### S22b — Data in the list + Download selected
Depends on: S22a
Red: `tests/web/test_list_data.py`: the "has usable data" filter (quality A/B) narrows the results and appears as a facet; each row has a quality badge; POST "Download selected" starts a `download` run under the lock (stubbed) and returns 409 while one runs.
Green: datasets route + template changes; download launch in `routes/runs.py`.
Run: curl `/?usable=1`.
Done when: criterion 9 (UI part) passes.

### S23 — Link health page
Depends on: S22b
Red: `tests/web/test_links.py`: aggregates match seeded `downloads`/`host_health`; breakdown by error kind; sort by failure rate; top failing publishers; the declared-vs-actual matrix; the CSV export has the right columns.
Green: `routes/links.py` + template + CSV endpoint.
Run: curl `/health/links` and `/health/links.csv`.
Done when: tests pass.

### S24 — Export
Depends on: S23
Red: `tests/test_export.py`: row counts match the store for JSONL and Parquet; the Parquet schema is stable (asserted field list); soft-deleted rows excluded unless `--include-deleted`; `--with-tables` copies the extracted Parquet files.
Green: `export.py`, `export` CLI.
Run: `uv run satudatascape export --format parquet --out /tmp/sds-export` on a seeded DB.
Done when: criterion 10 (export part) passes.

### S25 — *(optional)* PDF table extraction
Depends on: S24
Red: `tests/test_extract_pdf.py`: a generated 1-page table PDF → correct cells; a multi-table page → first table + warning; no text layer → `status='no_text_layer'`; skipped when the dataset has a CSV/XLSX; quality capped at B.
Green: add pdfplumber; `extract/pdf.py` (≤5 pages), only for PDF-only datasets.
Run: extract the generated PDF → print the cells.
Done when: tests pass.

---

## Progress

| Slice | Status | Date | Commit | Tests | Run showed |
|-------|--------|------|--------|-------|------------|
| S0 Project scaffold & CI | done | 2026-10-09 | f4fceb3 (+ ci fixes 9c9c81d, ff5071e) | 1 passed (smoke); `make check` green, docker skipped | `0.1.0`; uv 0.11.26, Python 3.12.12, SQLite 3.51.3. Amended *Commands* (`docker-maybe` also requires a Dockerfile) |
| S1 Proxy client: envelope & errors | done | 2026-10-09 | 9ce1b85 (CI 37893209335 green) | 16 new (`tests/test_client.py`) + 1 live; suite 17 passed, 1 deselected; `make check` green, docker skipped | `622223` (live `package_search rows=0`; plan said ~614k). Fixtures captured live, not hand-written |
| S2 Proxy client: rate limit, retries, UA | done | 2026-10-09 | | 13 new (`tests/test_client_resilience.py`); suite 30 passed, 1 deselected in 0.27 s; `make check` green, docker skipped | Live at rps=1: 3 sequential `package_search rows=0` → `622223` at 0.18 / 1.34 / 2.15 s; unknown-id `package_show` → `ProxyError` 500 `404 NOT FOUND` once at 3.11 s, not retried. Amended S2 *Red* (500-wrapped 404 is non-retryable) |
| S3 Normalisers | todo | | | | |
| S4 Store: migrations + upsert | todo | | | | |
| S5 Full crawl with checkpoint/resume | todo | | | | |
| S6 Reconciliation | todo | | | | |
| S7 Incremental crawl | todo | | | | |
| S8 Runs, lock, drift gates, run_full | todo | | | | |
| S9a Config | todo | | | | |
| S9b CLI | todo | | | | |
| S10 FTS5 search index | todo | | | | |
| S11 Web skeleton | todo | | | | |
| S12 Datasets list: paging + search | todo | | | | |
| S13a Datasets list: filters | todo | | | | |
| S13b Facet counts + cache | todo | | | | |
| S14 Dataset detail | todo | | | | |
| S15 Orgs + Stats | todo | | | | |
| S16 Runs page, Run now, scheduler | todo | | | | |
| S17 Docker + deploy + backup | todo | | | | |
| S18a URL lint + sniffing | todo | | | | |
| S18b Fetch + error taxonomy | todo | | | | |
| S18c Host limits + download CLI | todo | | | | |
| S19 Best-resource selection + host health | todo | | | | |
| S20 Extraction: CSV + JSON | todo | | | | |
| S21a Extraction: XLSX/XLS | todo | | | | |
| S21b Pipeline + extract CLI | todo | | | | |
| S22a Data on the detail page | todo | | | | |
| S22b Data in the list + Download selected | todo | | | | |
| S23 Link health page | todo | | | | |
| S24 Export | todo | | | | |
| S25 *(optional)* PDF extraction | todo | | | | |

---

## Notes log
<!-- Each slice appends one line: `YYYY-MM-DD Sn — note` -->
2026-10-09 S0 — uv package scaffold, all stack deps except pdfplumber locked (fastapi 0.143, httpx 0.28, pyarrow 25, apscheduler 3.x, mypy 2.4, ruff 0.16); `[project.scripts]` left for S9b; `docker-maybe` guard now also checks for a Dockerfile so CI stays green before S17; ruff excludes `scripts/feasibility/`.
2026-10-09 S0 — CI: bumped to node24 action majors (checkout@v7, setup-uv@v10.2.0; setup-uv publishes no major tags, so it is pinned to the full tag). CI run 37892951137 green.
2026-10-09 S1 — `ProxyClient.action` + `ProxyError(status, path)`; `SchemaDrift` subclasses `ProxyError`, so SPEC §3.1 ("ProxyError on a body without result") and S1 ("SchemaDrift") both hold. Params encoded with `urlencode(quote_via=quote)` (space → `%20`). Without an injected `httpx.AsyncClient` each call opens its own (S2 owns timeouts/UA/limits). Live findings: search count is now 622,223; `package_show` for an unknown id returns **HTTP 500** with envelope `{"status":"500 INTERNAL_SERVER_ERROR","message":"404 NOT FOUND: …"}` and no `result` — S2's retry-on-5xx would retry it 5 times, so S2/S6 should treat a `404 NOT FOUND` message as non-retryable. Fixture `package_search_rows2.json` is 28 KB (public metadata only).
2026-10-09 S2 — `ratelimit.TokenBucket` (burst 1, one slot per 1/rps, injected clock/sleep); `ProxyClient(rps=2, concurrency=4, clock, sleep)`: semaphore held only around the HTTP call (bucket acquired inside it, never during backoff); tenacity `AsyncRetrying` 5 attempts, `wait_exponential_jitter(1, max 60)`, or `Retry-After` (seconds or HTTP date) when present. Retries 5xx/429/`httpx.TimeoutException`; never 4xx, `SchemaDrift`, or a 500 whose message holds `404 NOT FOUND` (S6 can rely on one request per missing id). UA and `Timeout(90, connect=10)` set per request, so they also apply to an injected `AsyncClient`. S1's 502 test now injects a no-op sleep (it retried with real backoff, ~17 s). Connection errors (non-timeout) are not retried, per SPEC §3.1.

---

## Review
Independent review, 2026-10-09: 25 findings.
1. DoD pushed to `origin/main` → fixed (Where, Slice protocol: feature branch, `-u` first push).
2. "CI green" uncheckable → fixed (protocol step 8: `gh run watch` before the hash commit).
3. Commit style ambiguous → fixed (`feat(<module>): s<n> …`, no AI trailers, per owner rule).
4. Migration numbering undefined; missing lock table, `runs.status/error`, `tables.status`, download columns → fixed (Where: migrations table).
5. Migrations location / module list inconsistent with SPEC → fixed (package migrations via importlib.resources; S0 updates SPEC tree).
6. Missing uvicorn, python-multipart, tzdata, mypy stubs; APScheduler pin → fixed (Tech stack).
7. Deps added per slice would conflict → fixed (S0 adds all except pdfplumber).
8. ⇄ markers would collide → fixed (dropped; strictly sequential; one router per page anyway).
9. docker in `make check` without a daemon → fixed (`docker-maybe`; CI container job in S17).
10. ruff/mypy/pytest config unspecified; gitignore gaps → fixed (S0, Tech stack).
11. Live fixture capture in a sub-agent / public repo → decided: S1 captures trimmed fixtures, hand-written fallback; public metadata only.
12. Fake clock, "5" ambiguity, no rate test → fixed (injected clock, 5 attempts total, rps test).
13. Checkpoint storage before runs; no full→reconcile wiring or drift-no-delete test → fixed (S5 uses `runs.checkpoint`; S8 `run_full`).
14. `--full` referenced before CLI → fixed (`NeedsFullCrawl`).
15. CLI search semantics, log format, S9 size → fixed (live passthrough; JSON log test; split S9a/S9b).
16. FTS triggers impossible over raw_json; `filters` in S10 → fixed (app-level sync, bm25 weights, filters moved to S13a).
17. Cross-process cache invalidation; S13 size → fixed (key on last finished run id; split S13a/S13b).
18. TestClient BackgroundTasks can't show 409; scheduler under tests; lock vs schedule → fixed (create_task + Event stub, `scheduler.enabled`, skip-when-locked test).
19. Backup needs sqlite3 binary; no nightly job → fixed (`Connection.backup()`, third job).
20. S18 too large; per-host 1–2 vs 1; in-run host-down untested; error classification → fixed (split S18a/b/c, 1 per host, exception-type classification).
21. No download→extract wiring, no extract CLI, raw-kept untested, download runs → fixed (S21b, runs kind=download).
22. CSV/JSON grades undefined; `no_table` placement → fixed (Approach grades; `tables.status`).
23. S22 deps and size; traversal placement → fixed (split S22a/b; traversal test also in S18b).
24. Missing Run / Done when lines → fixed (every slice).
25. Unretired risks (package_list memory, FTS/facet latency, JSONL rotation, disk, ToS) → fixed (Risks list; slow tests in S6/S10/S13b; rotation in S5/S17; DEPLOY disk budget).
