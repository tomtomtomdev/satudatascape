# satudatascape — Implementation Plan

Companion to [SPEC.md](SPEC.md). The work is split into small vertical slices.
Each slice is completed by **one sub-agent task**, test-first, and ends with a green
build, a progress update in this file, a commit, and a push.

---

## How to run a slice (the loop)

For slice `Sn`, the orchestrator spawns one sub-agent with this brief:

> Implement slice **Sn** from `docs/PLAN.md`, per `docs/SPEC.md`. Work strictly test-first:
> 1. **Red:** write the tests listed under *Tests first*. Run `make test` and confirm the new tests **fail for the right reason** (not import or syntax errors). Paste the failing summary.
> 2. **Green:** write the minimum code to make them pass. Don't change the tests to fit the code unless a test was wrong; if one was, say so.
> 3. **Refactor:** tidy up with the tests still green.
> 4. **Build & test:** run `make check` (ruff + mypy + pytest; from S17 on, also `docker build`). It must exit 0.
> 5. **Progress:** in `docs/PLAN.md`, tick the slice's checkbox, set Status to ✅ with the date, and add a one-line note (anything surprising, follow-ups).
> 6. **Commit & push:** `git add -A && git commit -m "Sn: <slice title>"` and `git push`.
> Report: the red summary, the green summary, the commit SHA, and any deviations from spec.

Rules:
- **One slice per sub-agent, one commit per slice.** Never start Sn+1 in the same task.
- Slices run **sequentially** unless marked ⇄ (parallel-safe: disjoint files, no shared migration).
- No live network calls in tests. Use `respx` + `tests/fixtures/`. Live checks are `pytest -m live` and opt-in only.
- If `make check` can't go green, **don't commit**. Set Status to ⛔ with the reason and stop.
- The orchestrator reviews each report (and the diff) before spawning the next slice.

Definition of done for every slice: new tests written first and passing · `make check` green · PLAN.md updated · pushed to `origin/main`.

---

## Progress

| Slice | Title | Status |
|---|---|---|
| S0 | Project scaffold & CI | ⬜ |
| S1 | Proxy client: envelope & errors | ⬜ |
| S2 | Proxy client: rate limit, retries, UA | ⬜ |
| S3 | Normalisers (format, prioritas, org type, timestamps) | ⬜ |
| S4 | Store: migrations + package upsert w/ hashing | ⬜ |
| S5 | Full crawl with checkpoint/resume | ⬜ |
| S6 | Reconciliation via `package_list` | ⬜ |
| S7 | Incremental crawl (watermark) | ⬜ |
| S8 | Runs table, run lock, drift gates | ⬜ |
| S9 | CLI: crawl / reconcile / show / search / stats | ⬜ |
| S10 | FTS5 search index | ⬜ |
| S11 | Web skeleton, base layout, `/healthz`, basic auth | ⬜ |
| S12 | Datasets list: paging + search | ⬜ |
| S13 | Datasets list: filters + facet counts (HTMX) | ⬜ |
| S14 | Dataset detail + version history | ⬜ |
| S15 | Orgs + Stats pages | ⬜ |
| S16 | Runs page + "Run now" + scheduler | ⬜ |
| S17 | Docker image + deploy docs + backup | ⬜ |
| S18 | Resource downloader | ⬜ |
| S19 | Downloads in UI + CSV preview | ⬜ |
| S20 | Export (JSONL / Parquet) | ⬜ |

Legend: ⬜ todo · 🟨 in progress · ✅ done · ⛔ blocked

---

## Slices

### S0 — Project scaffold & CI
- [ ] Done
**Build:** `uv init` package `satudatascape` (src layout), deps per SPEC §3.0, `ruff`/`mypy`/`pytest` config in `pyproject.toml`, `Makefile` with `test`, `lint`, `typecheck`, `check`, plus a GitHub Actions workflow running `make check` on push.
**Tests first:** `tests/test_smoke.py::test_package_imports_and_has_version`.
**Done when:** CI is green on GitHub.

### S1 — Proxy client: envelope & errors
- [ ] Done
**Build:** `client.ProxyClient.action(name, **params)`: builds the endpoint query string, POSTs the envelope, unwraps `result`. Error types `ProxyError` and `SchemaDrift`.
**Fixtures:** capture once (manually, via `scripts/capture_fixtures.py`) `package_search_rows2.json`, `package_show.json`, `package_list_5.json`, `spring_404.json`.
**Tests first:**
- posts the correct JSON body (`endpoint` with URL-encoded params, `method: GET`, `body: {}`, `token: ""`)
- unwraps `result` for search / show / list
- Spring 404 body → `ProxyError` that includes the `path`
- 200 response without `result` → `SchemaDrift`
- `package_search` result without `count`/`results` → `SchemaDrift`

### S2 — Proxy client: rate limit, retries, UA
- [ ] Done
**Build:** token bucket (rps), semaphore (concurrency), `tenacity` retry on 5xx/429/timeouts with jitter, `Retry-After` support, User-Agent, timeouts.
**Tests first:** retries 3× on 502 then succeeds; gives up after 5 and raises; doesn't retry 404; honours `Retry-After`; never exceeds N concurrent in-flight calls (instrumented mock); UA header present. Use a fake clock so tests take no real time.

### S3 — Normalisers ⇄ (parallel-safe with S1/S2)
- [ ] Done
**Build:** `normalize.py`: `format_norm`, `prioritas_years`, `org_type`, `parse_ckan_ts` → UTC, `solr_ts`.
**Tests first (table-driven):** `xlxs`/`xslx`/`.xlsx`/`xlsx ` → `XLSX`; `csv/xlsx` → `CSV`; `google spreadsheet` → `GSHEET`; unknown → uppercased raw. `"2023, 2024"`, `"2025;2026"`, `"Prioritas 2025"` → year lists; `"1970"` → `[]`. `kota-malang` → `kota`; `kementerian-kesehatan` → `kementerian`; `badan-pusat-statistik` → `badan`. Naive ISO → aware UTC; `solr_ts` → `YYYY-MM-DDTHH:MM:SSZ`.

### S4 — Store: migrations + upsert
- [ ] Done
**Build:** `migrations/0001_init.sql` (schema per SPEC §4), migration runner, `Store.upsert_packages(pkgs) -> UpsertStats`, resources/organizations derived on upsert, `package_versions` written on hash change, WAL enabled.
**Tests first:** migrations are idempotent; inserting a new package counts as inserted; re-upserting the identical package changes nothing and bumps `last_seen_at`; a changed field writes a new version row; resources are replaced when the package's resource list shrinks; org_type and format_norm are populated; a deleted package that reappears clears `deleted_at`.

### S5 — Full crawl with checkpoint/resume
- [ ] Done
**Build:** `crawler.full_crawl(client, store)`: `rows=0` count, pages with `sort=id asc&rows=1000`, bounded concurrency, checkpoint after each contiguous completed page, optional raw JSONL.gz writer.
**Tests first:** fake index of 2,500 packages → 3 pages, all stored; crash injected after page 2 → resume skips pages 0–1; empty page before `start >= count` → raises the drift error; raw JSONL written when enabled.

### S6 — Reconciliation
- [ ] Done
**Build:** `crawler.reconcile`: diff `package_list` against stored names; `package_show` the missing ones; soft-delete the absent ones.
**Tests first:** a fake index that inserts items mid-crawl (skipped by offset paging) → reconcile fetches them; a removed name gets `deleted_at` set; reconcile is a no-op when everything is in sync.

### S7 — Incremental crawl
- [ ] Done
**Build:** `crawler.incremental`: watermark = max(`metadata_modified`) − overlap; `q=metadata_modified:[ts TO *]`, `sort=metadata_modified asc, id asc`.
**Tests first:** builds the correct `q` and `sort` (asserted on the request); only newer packages are upserted; the overlap window doesn't double-count changes; an empty DB falls back to requiring `--full`.

### S8 — Runs, lock, drift gates
- [ ] Done
**Build:** `runs` rows for every crawl (also on failure), a DB-based run lock (with a stale-lock timeout), a startup probe, and the >10% count-drop gate that blocks soft-deletes.
**Tests first:** a run row is written on success and on exception; a second concurrent run is refused; a stale lock is taken over; a 15% count drop aborts reconcile before any soft-delete; a probe against a bad envelope fails fast.

### S9 — CLI
- [ ] Done
**Build:** `typer` app: `crawl [--full]`, `reconcile`, `show`, `search`, `stats`, global flags, config file + `SDS_*` env.
**Tests first (CliRunner + respx):** each command's exit code and key output; env overrides config; `--db` path respected.
**Also:** one manual live run, `satudatascape search penduduk --rows 3`, recorded in the slice note.

### S10 — FTS5 search index
- [ ] Done
**Build:** `migrations/0002_fts.sql`: FTS5 table over title, notes, org title, and tags, kept in sync by triggers; `store.search(q, filters, sort, page)` behind a `search` module.
**Tests first:** matches on title and on notes; title matches rank above notes matches; the index stays updated after an upsert and after a soft-delete; Indonesian words with diacritics and casing are handled (`unicode61 remove_diacritics 2`); paging is stable.

### S11 — Web skeleton
- [ ] Done
**Build:** FastAPI app factory, Jinja2 base layout (Pico.css + HTMX via CDN), nav, `/healthz`, HTTP Basic auth from config (disabled when unset), `satudatascape serve`.
**Tests first (TestClient):** `/healthz` returns JSON with DB ok and last run; pages require auth when configured and return 401 without credentials; the base layout renders the nav.

### S12 — Datasets list: paging + search
- [ ] Done
**Build:** `GET /` with a `q` box, sort options, 50/page, and an HTMX partial for `#results` (served when the `HX-Request` header is present).
**Tests first:** an empty query lists the newest first; `q` filters the results; page 2 offsets correctly; an HTMX request returns the partial only; query params round-trip into pagination links.

### S13 — Filters + facet counts
- [ ] Done
**Build:** org type, organization, harvest source, format, prioritas year, and modified-since filters; facet counts via `GROUP BY` over the filtered set; a 5-minute cache.
**Tests first:** each filter narrows the results correctly; filters combine with AND; facet counts reflect the other active filters; the cache is invalidated after a crawl run finishes.

### S14 — Dataset detail
- [ ] Done
**Build:** `GET /datasets/{name}`: metadata, extras table, origin and data.go.id links, resource table, version history with the changed field names.
**Tests first:** renders the title and notes; 404 for an unknown name; the resource table shows normalised formats; the version list shows which fields changed between two versions; a soft-deleted dataset shows a "removed upstream" banner.

### S15 — Orgs + Stats ⇄ (parallel-safe with S14)
- [ ] Done
**Build:** `GET /orgs` (counts, link to the filtered list); `GET /stats` (counts by org type, org, harvest source, format; totals; last successful run).
**Tests first:** org counts match the store; the stats totals add up; links carry the correct filter params.

### S16 — Runs page, Run now, scheduler
- [ ] Done
**Build:** `GET /runs` table, an auto-refreshing current-run partial (`hx-trigger="every 5s"`), `POST /runs` (incremental|full) launching a background task under the run lock, APScheduler jobs (daily incremental at 02:00 Asia/Jakarta, weekly reconcile on Sunday at 03:00).
**Tests first:** the runs list renders; POST starts a run (crawler stubbed) and a second POST returns 409 while one is running; the scheduler registers both jobs with the correct triggers and timezone.

### S17 — Docker + deploy + backup
- [ ] Done
**Build:** a multi-stage `Dockerfile` (uv, non-root user, `data/` volume), `docker-compose.yml`, `docs/DEPLOY.md` (VM and Fly.io options), and a nightly `sqlite3 .backup` job with retention. `make check` now also runs `docker build`.
**Tests first:** the backup function produces a valid, openable copy and prunes beyond retention; a container smoke test (CI) gets `/healthz` = 200.

### S18 — Resource downloader
- [ ] Done
**Build:** `downloader.py` per SPEC §3.3: selection query, per-host limits, `.part` then rename, sha256, conditional GET, size guard, host circuit breaker, `insecure_hosts`, `download` CLI with `--dry-run`.
**Tests first:** downloads and records hash and size; a 304 skips the rewrite; an over-size file is skipped (via HEAD and via a streaming cutoff); the per-host concurrency is never exceeded; a host is marked down after N failures; the dry run lists files without fetching.

### S19 — Downloads in UI + CSV preview
- [ ] Done
**Build:** download status and a local-file link in the detail page; a "Download selected" action under the run lock; an HTMX CSV preview of the first 50 rows (encoding sniffing and delimiter detection).
**Tests first:** status badges per state; preview handles `;`-delimited and latin-1 files; preview refuses non-CSV files; serving a file stays inside `data/files` (path-traversal test).

### S20 — Export ⇄ (parallel-safe with S18/S19)
- [ ] Done
**Build:** `export --format jsonl|parquet` (`pyarrow`), one flat packages table plus one resources table.
**Tests first:** row counts match the store; the Parquet schema is stable; soft-deleted packages are excluded unless `--include-deleted` is passed.

---

## Notes log
<!-- Each slice appends one line: `YYYY-MM-DD Sn — note` -->
