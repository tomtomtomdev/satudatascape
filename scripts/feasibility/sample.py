import json, random, sys, collections, httpx
random.seed(42)
out = sys.argv[1]
def act(ep):
    r = httpx.post("https://data.go.id/api/proxy", json={"endpoint": ep, "method": "GET", "body": {}, "token": ""}, timeout=90)
    return r.json()["result"]
total = act("/api/3/action/package_search?rows=0")["count"]
pkgs = []
for off in random.sample(range(0, total - 20), 40):
    pkgs += act(f"/api/3/action/package_search?rows=20&start={off}&sort=id%20asc")["results"]
NORM = {"XLSX":"XLSX",".XLSX":"XLSX","XLXS":"XLSX","XSLX":"XLSX","CSV":"CSV",".CSV":"CSV","PDF":"PDF",".PDF":"PDF","XLS":"XLS","JSON":"JSON",".JSON":"JSON","WMS":"WMS","WFS":"WFS"}
by = collections.defaultdict(list)
for p in pkgs:
    for r in p.get("resources", []):
        f = NORM.get((r.get("format") or "").strip().upper(), "OTHER")
        by[f].append({"pkg": p["name"], "org": (p.get("organization") or {}).get("name"), "fmt_raw": r.get("format"), "fmt": f, "url": r.get("url"), "res_id": r["id"],
                      "harvest": next((e["value"] for e in p.get("extras", []) if e["key"] == "harvest_source_title"), None)})
quota = {"XLSX":90,"CSV":90,"PDF":50,"XLS":40,"JSON":50,"WMS":15,"WFS":15,"OTHER":40}
pop = {k: len(v) for k, v in by.items()}
sample = []
for f, v in by.items():
    random.shuffle(v); sample += v[:quota.get(f, 20)]
json.dump({"total": total, "pkgs": len(pkgs), "res_population": pop, "sample": sample}, open(out, "w"))
print(len(pkgs), "pkgs", pop, "sample", len(sample))
print(collections.Counter(s["org"].split("-")[0] if s["org"] else None for s in sample).most_common())
