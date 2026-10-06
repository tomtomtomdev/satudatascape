import asyncio, json, sys, time, collections, httpx, os
from urllib.parse import urlparse
inp, outdir, outjson = sys.argv[1:4]
S = json.load(open(inp))["sample"]
CAP = 25 * 2**20
hostsem = collections.defaultdict(lambda: asyncio.Semaphore(2)); gsem = asyncio.Semaphore(16)
UA = {"User-Agent": "satudatascape-feasibility/0.1 (+https://github.com/tomtomtomdev/satudatascape)"}
async def get(c, item, verify_note):
    t = time.time()
    async with c.stream("GET", item["url"], headers=UA) as r:
        buf = bytearray(); trunc = False
        async for ch in r.aiter_bytes():
            buf += ch
            if len(buf) > CAP: trunc = True; break
        return dict(status=r.status_code, ctype=r.headers.get("content-type"), cdisp=r.headers.get("content-disposition"),
                    final=str(r.url), bytes=len(buf), trunc=trunc, secs=round(time.time()-t,2), tls=verify_note), bytes(buf)
async def one(cs, i, item):
    u = item.get("url") or ""
    if not u.startswith("http"): item.update(err="no-url"); return item
    h = urlparse(u).hostname; item["host"] = h
    async with gsem, hostsem[h]:
        for verify, c in cs:
            try:
                meta, body = await get(c, item, verify); item.update(meta)
                p = os.path.join(outdir, f"{i:04d}.bin"); open(p, "wb").write(body); item["path"] = p
                item.pop("err", None); return item
            except httpx.ConnectError as e:
                item["err"] = "connect:" + str(e)[:120]
                if "CERTIFICATE" in str(e).upper() or "SSL" in str(e).upper(): continue
                return item
            except Exception as e:
                item["err"] = type(e).__name__ + ":" + str(e)[:120]; return item
    return item
async def main():
    lim = httpx.Timeout(30, connect=15)
    async with httpx.AsyncClient(follow_redirects=True, timeout=lim) as a, httpx.AsyncClient(follow_redirects=True, timeout=lim, verify=False) as b:
        res = await asyncio.gather(*[one([("ok", a), ("insecure", b)], i, it) for i, it in enumerate(S)])
    json.dump(res, open(outjson, "w"))
    print(collections.Counter((r["fmt"], r.get("status") or r.get("err","")[:25]) for r in res).most_common(60))
asyncio.run(main())
