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
| S18 | Downloader core: URL lint, sniffing, error taxonomy | ⬜ |
| S19 | Best-resource selection + host health/backoff | ⬜ |
| S20 | Table extraction: CSV + JSON | ⬜ |
| S21 | Table extraction: XLSX/XLS (header detection, merged headers) | ⬜ |
| S22 | Data in UI: quality badges, table preview, "has usable data" filter | ⬜ |
| S23 | Link health page | ⬜ |
| S24 | Export (JSONL / Parquet) | ⬜ |
| S25 | *(optional)* PDF table extraction for PDF-only datasets | ⬜ |

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

### S18 — Downloader core: URL lint, sniffing, error taxonomy
- [ ] Done
See [FEASIBILITY.md](FEASIBILITY.md). Expect ~28% of files to fail and ~8% of successful ones to be the wrong type.
**Build:** `downloader.py` per SPEC §3.3, with:
- **Pre-flight URL lint:** dot-less hosts (`api`, `tes`), `localhost`, and private/reserved IPs → `internal_host`, no request made.
- Streaming download to `.part` then rename; sha256; size guard; conditional GET.
- **Magic-byte sniffing** → `actual_type` (XLSX/XLS/PDF/ZIP/DOCX/JSON/XML/HTML/IMG/TEXT/EMPTY).
- **Error taxonomy** in `downloads.error_kind`: `dns`, `connect_timeout`, `read_timeout`, `tls`, `http_4xx`, `http_5xx`, `html_landing`, `ogc_exception`, `internal_host`, `no_url`, `too_large`.
- Per-host concurrency 1–2, global cap, `insecure_hosts` allowlist.
- `download` CLI with `--dry-run`.

**Tests first:** URL lint table (`http://api/x` → internal; `http://10.0.0.1` → internal; `https://data.x.go.id` → ok); sniffing table using tiny fixture files (xlsx zip, xls OLE header, `%PDF`, HTML landing page, OGC `ServiceExceptionReport`, BOM CSV); each error kind is produced by its respx/httpx scenario; a TLS failure on a host outside the allowlist → `tls`, and on an allowlisted host → success flagged insecure; a 304 skips the rewrite; an over-size file is cut off; per-host concurrency is never exceeded.

### S19 — Best-resource selection + host health
- [ ] Done
**Build:**
- `select_best(package)` ranks resources CSV > JSON > XLSX > XLS > TSV > PDF > other, using `format_norm`, and **falls back to the next candidate** when the chosen one fails or sniffs as the wrong type.
- `host_health` table with a rolling success rate and `retry_after`. A dead host is backed off across runs (1d → 3d → 7d).
- `download --best-only` is the default; `--all` is opt-in.

**Tests first:** ranking order; fallback when the CSV is a 404 → the XLSX is used; a dataset with only PDF/WMS selects PDF and skips geo; backoff progresses after consecutive failed runs and resets on success; a host in backoff is skipped without making a request.

### S20 — Table extraction: CSV + JSON ⇄ (parallel-safe with S19)
- [ ] Done
**Build:** `extract/` package that writes `data/tables/<resource_id>.parquet` and a `tables` row (parser, rows, cols, columns JSON, quality A/B/C, warnings).
- CSV: encoding sniff (utf-8-sig → cp1252 fallback), delimiter sniff (`, ; | \t`), `newline=''`, column-count consistency check.
- JSON: record-list locator for `[...]`, `{data}`, `{records}`, `{fields,records}`, `{code,status,message,data}`, `{deskripsi,header,data}`, and GeoJSON `features[].properties`. Nested values are serialised as JSON strings.

**Tests first:** fixtures for each wrapper shape and delimiter; a cp1252 file with Indonesian text; embedded newlines in quoted fields; inconsistent columns → quality C with a warning; a non-tabular JSON object → `no_table`.

### S21 — Table extraction: XLSX/XLS
- [ ] Done
**Build:**
- Header-row detection: first row whose filled-cell count is ≥60% of the widest row.
- Merged-header flattening: forward-fill horizontal merges, then join levels (`Tahun / 2023`).
- Drop title and note rows above the header and trailing note rows below the data.
- First sheet by default; the other sheets are recorded in `tables.sheets`.
- `.xls` is read via `xlrd`.
- Quality: A = tidy as-is, B = header fixed or merges flattened, C = raw grid only.

**Tests first:** small fixture workbooks built in-test with openpyxl: tidy table → A; title in rows 0–2 with the header on row 3 → B with the correct columns; a two-level merged header → flattened names; a trailing "Sumber: …" note row is dropped; a multi-sheet workbook lists its sheets; a corrupt file → quality C / error, not a crash.

### S22 — Data in UI
- [ ] Done
**Build:**
- Detail page: per-resource download status and error kind, `actual_type` vs declared format, quality badge, and an HTMX **table preview** (first 50 rows, from the extracted Parquet, falling back to the raw CSV), plus a raw-file download link.
- Datasets list: a **"has usable data"** filter (quality A/B) and a quality badge in each row.
- "Download selected" action under the run lock.
- WMS/WFS resources render as "open in geoportal" links.

**Tests first:** badges per state; the preview renders from Parquet; the preview falls back when there's no table; the filter narrows correctly; file serving stays inside `data/` (path-traversal test).

### S23 — Link health page ⇄ (parallel-safe with S22)
- [ ] Done
**Build:** `GET /health/links`: overall success rate; breakdown by error kind; per-host table (success %, last ok, backoff state); top failing publishers; a declared-vs-actual format mismatch matrix. CSV export of the per-host table.
**Tests first:** aggregates match the seeded `downloads` / `host_health` rows; sorting by failure rate works; the CSV export has the right columns.

### S24 — Export
- [ ] Done
**Build:** `export --format jsonl|parquet` (`pyarrow`): a flat packages table, a resources table (with download and quality columns), and optionally `--with-tables` to bundle the extracted tables.
**Tests first:** row counts match the store; the Parquet schema is stable; soft-deleted packages are excluded unless `--include-deleted` is passed.

### S25 — *(optional)* PDF table extraction
- [ ] Done
**Build:** `pdfplumber` extraction **only for datasets with no machine-readable alternative** (~7%). Pages ≤ 5 by default. Scanned PDFs (no text layer) → `no_text_layer`; OCR is out of scope. Output goes through the same `tables` pipeline, with quality capped at B.
**Tests first:** a generated 1-page table PDF → correct cells; a multi-table page → first table plus a warning; a PDF with no text layer → `no_text_layer`; skipped when the dataset has a CSV/XLSX.

---

## Notes log
<!-- Each slice appends one line: `YYYY-MM-DD Sn — note` -->
