# Feasibility scripts

One-off scripts behind [docs/FEASIBILITY.md](../../docs/FEASIBILITY.md). Not part of the app.

```sh
uv run --python 3.12 --with httpx python -I sample.py out/sample.json
uv run --python 3.12 --with httpx python -I fetch.py out/sample.json out/files out/fetched.json
uv run --python 3.12 --with openpyxl --with xlrd --with pdfplumber python -I analyze.py out/fetched.json out/analysis.json
```

Downloaded files are untrusted: keep `out/` outside the repo.
