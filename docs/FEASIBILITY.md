# Data Fetch Feasibility: Resource Files

Can we actually get the *data* (XLSX, CSV, PDF, JSON, … files), not just the catalogue?

Empirical check run 2026-10-06. Scripts: [`scripts/feasibility/`](../scripts/feasibility/).

## Method

1. **Sampling:** 800 random datasets (40 random offsets × 20 rows over `sort=id asc`) gave 1,416 resources. From those I drew a **stratified sample of 390 resources**: XLSX 90, CSV 90, PDF 50, JSON 50, XLS 40, WMS 15, WFS 15, other 40.
2. **Download:** each file was downloaded with ≤2 connections per host, a 30 s timeout and a 25 MB cap, following redirects. If TLS verification failed, I retried without it and flagged the file.
3. **Classification:** each body was classified by **magic bytes**, not by the declared format.
4. **Parsing:** each file was parsed with `openpyxl` / `xlrd` / `csv` / `json` / `pdfplumber` and scored for structure.
5. **Coverage check:** a separate 1,000-dataset sample measured coverage per dataset.

The sample is small (390 files, 111 hosts), so the percentages are **±5–8 pp**. They're fine for go/no-go and design decisions, not for reporting.

## Verdict

| Layer | Feasible? | Expected yield |
|---|---|---|
| Catalogue metadata | ✅ Yes, fully | ~100% (see SPEC §1) |
| Raw file mirror | ✅ Yes, with expected losses | **~70% of files** download as the declared type |
| Usable table per dataset (auto) | 🟨 Partly | **~55–65% of datasets** give a clean table without manual work |
| PDF → table | 🟨 Possible, low priority | PDFs are mostly 1-page generated table exports; only ~7% of datasets are PDF-only |
| WMS/WFS geodata | ⛔ Not in v1 | URLs usually lack the parameters needed to return data; keep as links |
| Harmonising indicators across regions | ⛔ Out of scope | Same indicator, different shapes per kabupaten; needs manual curation |

**Bottom line:** fetching files is feasible and cheap (~10–35 GB total). The hard
parts are **link rot** (~28% of files fail) and **messy spreadsheets** (about half of
the XLSX files need header detection). Design for best-effort extraction with an
explicit quality score, always keep the raw file, and never promise 100%.

## Findings

### 1. Reachability: 72% of files return HTTP 200

Of 390 files, 280 returned 200. The failures:

| Failure | Count | Cause |
|---|---:|---|
| DNS failure | 30 | Mostly **`http://api/bigdata/...`**: West Java's harvester leaked an *internal* hostname (Prov. Jabar, Cianjur, Bogor, Majalengka, Bekasi, …). Also junk like `http://tes`. |
| Connect timeout | 20 | Dead or firewalled regional servers, raw IPs (`103.170.104.187:8080`) |
| 403 | 17 | WAF / bot blocks |
| 404 | 11 | Files removed at the origin |
| 5xx (500/502/503/522/523) | 21 | Flaky origins, broken Cloudflare origins |
| Read timeout / protocol errors | 7 | Slow or broken servers |
| No URL | 4 | Empty `url` |

- **The West Java internal-host bug is systematic.** In the 1,000-dataset check, 99 resources pointed at `http://api/...`. Rewriting them to `data.jabarprov.go.id` / `opendata.jabarprov.go.id` returns a 403 block page. **Not recoverable automatically.** Flag these as `broken_url: internal_host`. Worth reporting to the Jabar Open Data team or to Satu Data.
- Some resource URLs point at `dev-data.*` and other staging hosts.
- **TLS:** 12 of 280 successful files (4%) needed `verify=False` (broken certificate chains on `.go.id` hosts). The `insecure_hosts` allowlist in SPEC §3.3 is necessary.
- **Speed is not a problem:** median 0.26 s per file, p90 0.74 s. No file exceeded 25 MB.

Success rate by declared format: **PDF 92%, XLSX 79%, JSON 64%, XLS 55%, CSV 51%**.
CSV/XLS are often generated on demand by small regional portals, and those are the flakiest.

### 2. Declared format ≠ actual content (~8% of 200s)

| Declared | Actually got |
|---|---|
| XLSX (71) | 66 XLSX, 3 **HTML**, 1 XLS, 1 CSV |
| CSV (46) | 40 CSV-like text, 4 **XLSX**, 2 **HTML** |
| PDF (46) | 43 PDF, 2 HTML, 1 ZIP |
| JSON (32) | 31 JSON, 1 HTML |
| Format empty / "other" (33) | 13 CSV-like text, 7 HTML, 3 XLSX, … |

The HTML responses are **landing pages** (Satu Data portals, geoportals, Google Drive).
Of the CSV-like text files, 1 was actually HTML (a page with a `<link>` tag).
**Always sniff magic bytes and store `actual_type`.** Never trust `format` or `Content-Type`.

### 3. Spreadsheets: they parse, but half aren't tidy

- **XLSX:** 74 of 74 parsed with `openpyxl`. **XLS:** 23 of 23 parsed with `xlrd`.
- Tables are **tiny**: median **13 rows** (max 839). These are aggregate tables, not microdata.
- **Only 50% are tidy** (single sheet, header on row 0, no merged cells):
  - header row not at row 0: 49% (rows 1–4 are typical, up to row 11, with a title or notes above it)
  - merged cells: 41% (multi-level headers such as `Tahun → 2022 | 2023`)
  - multiple sheets: 11%
- **CSV:** 53 of 55 parsed with stdlib `csv` (the 2 failures had embedded newlines; fix by opening with `newline=''`).
  - Encoding: 91% UTF-8, 9% cp1252.
  - Delimiter: `,` 70%, `;` 26%, plus `|` and tab.
  - 87% have a consistent column count.
  - Headers are usually readable (`Kode Wilayah, Wilayah, Tahun, …`).

### 4. JSON: half are flat record lists

36 of 36 parsed, and 19 (53%) are flat record lists. The wrappers vary by portal:
`{fields, records}` (CKAN DataStore-style), `{data}`, `{code, status, message, data}`,
`{dataset_id, tahun, jumlah_data, records}`, `{deskripsi, header, data}`, and GeoJSON
`FeatureCollection`. A small "find the record list" locator covers these.

### 5. PDF: mostly generated table exports, not reports

- 44 of 44 opened. Median **1 page**. Only 2 were scanned (no text layer).
- `pdfplumber` found tables in 40 of 44, e.g. Kota Malang's per-dataset PDF exports and Pemalang's `export/pdf/data-sektoral`.
- These PDFs are usually a **rendering of a table the portal also offers as XLSX/CSV**.

### 6. Dataset-level coverage (1,000 datasets)

| | Share |
|---|---:|
| Has ≥1 machine-readable resource (CSV/XLSX/XLS/JSON/TSV) | **87.3%** |
| PDF-only | 6.9% |
| Geo-only (WMS/WFS/SHP) | 1.7% |
| No resources | 1.7% |

- Median **1 resource per dataset**. Common combinations: XLSX only (41%), CSV only (22%), CSV+XLSX (9%), PDF only (7%).
- **223 distinct hosts across 1,000 datasets.** The mirror talks to hundreds of servers, each with its own failure mode.
- `accesslevel`: 81% unset, 18% `public`, plus a few `terbatas` (restricted).

Combining the numbers: 87% have a machine-readable resource, ~75% of those download as the right type, and ~85–100% of those parse. That gives **≈55–65% of datasets with an automatically usable table**. Another ~10% are reachable but need PDF extraction or manual header fixes.

### 7. Volume and time

| Type | Resources (catalogue) | Mean size | Est. total |
|---|---:|---:|---:|
| XLSX | ~413k | 18 KB | ~7 GB |
| CSV | ~244k | 8 KB | ~2 GB |
| PDF | ~105k | 197 KB | ~20 GB |
| XLS | ~63k | 36 KB | ~2 GB |
| JSON | ~43k | 18 KB | ~1 GB |
| **All** | ~950k | | **~33 GB** (~12 GB excluding PDFs) |

With a **best resource per dataset** policy (one file each): about **~9 GB**.

**Time:** the cost is set by politeness per host, not bandwidth. The largest
single host (Kota Malang, ~80k datasets) at 1 request/s takes **~1 day**. Other hosts run in
parallel, so a full first pass takes **1–2 days**. After that, refreshes are incremental:
only resources whose `metadata_modified` changed, ~13k datasets/week.

## Design consequences (applied to SPEC/PLAN)

1. **Sniff, don't trust.** Store `actual_type` from magic bytes. Flag `html_instead_of_file` and `ogc_exception`.
2. **Error taxonomy** in `downloads.error_kind`: `dns`, `connect_timeout`, `read_timeout`, `tls`, `http_4xx`, `http_5xx`, `html_landing`, `internal_host`, `no_url`, `ogc_exception`, `too_large`.
3. **Pre-flight URL lint:** skip hosts without a dot (`api`, `tes`), `localhost`, and private IP ranges. Mark them `internal_host` without making a request.
4. **Best resource per dataset:** CSV > JSON (flat) > XLSX > XLS > TSV > PDF > other. On failure, fall back to the next candidate. By default, download only the best one.
5. **Host health:** a per-host rolling success rate. Back off dead hosts *across runs* (e.g. retry after 1d, 3d, 7d) instead of hammering them every night.
6. **Table extraction is best-effort with a quality score:**
   - CSV: encoding + delimiter sniff, `newline=''`
   - JSON: record-list locator
   - XLSX/XLS: header-row detection, merged-header flattening (`Tahun / 2023`), first sheet by default with other sheets listed
   - Output a normalised table (Parquet) plus a `quality` grade: `A` tidy, `B` header fixed, `C` raw grid only
7. **Always keep the raw file.** Extraction can improve later without refetching.
8. **PDF extraction** (`pdfplumber`) is a later, optional slice, applied only to PDF-only datasets (~7%).
9. **WMS/WFS:** store as links. Show "open in geoportal". No fetching in v1.
10. **UI:** a "has usable data" filter, quality badges, preview from the *extracted* table, and a **Link health** page (per-host success, top failing publishers). That last one is useful in itself as a data-quality report on the portal.

## Open questions raised

- Report the West Java `http://api/...` URLs upstream? They break about 10% of resources in the Jabar region.
- Is a ~28% dead-link rate acceptable to surface openly in the UI (Link health page), given the repo is public?
- Do we need manual curation for a shortlist of high-value indicators (e.g. population, poverty, health per kabupaten)? That would be the only way to get comparable cross-region tables.
