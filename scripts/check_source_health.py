"""
Plain HTTP reachability probe for every registered web source.

No LLM, no DB writes, no crawling — one GET per registered primary_url, so
it is cheap enough to run any time and safe to run while the API is up.

Built 16 Sep 2026 after finding that 6 of 25 registered sources had been
unfetchable for weeks with nothing anywhere reporting it: two university
sites had reorganised their URLs into 404s, two had certificates valid only
for a hostname other than the one registered, and one was a literal
`https://example.com/portfolio` placeholder added through the dashboard and
never filled in. A dead source is silent — the crawl simply yields nothing —
so this check exists to make that visible on demand.

IMPORTANT — read before acting on the output
--------------------------------------------
A failing primary_url does NOT always mean a dead source, because the
crawler works from pages it discovers, not only from the registered root:

  * startupsucht.com's homepage 403s under Cloudflare while its
    /startup-liste-verzeichnis-<city> pages are healthy and have produced
    620 startups. Probing only the root would call a working source dead.

So before repointing anything, check what the source has actually produced:

    SELECT source_url, count(*), max(extracted_at) FROM startups
     WHERE source_url ILIKE '%<domain>%' GROUP BY 1 ORDER BY 2 DESC;

A TLS error is reported with a second unverified attempt, which separates
"certificate is wrong for this hostname" (retry succeeds — usually means the
apex domain or a new host is the right target) from "host is genuinely gone"
(retry also fails).

Usage
-----
    python scripts/check_source_health.py
"""
import concurrent.futures as cf
import ssl, sys, yaml, httpx

srcs = yaml.safe_load(open("config/sources.yaml"))["web_sources"]

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

def probe(s):
    url = s["primary_url"]
    row = {"id": s["source_id"], "url": url, "prio": s.get("priority", "?")}
    try:
        with httpx.Client(follow_redirects=True, timeout=25,
                          headers={"User-Agent": UA}) as c:
            r = c.get(url)
        row.update(status=r.status_code, bytes=len(r.content),
                   final=str(r.url) if str(r.url) != url else "")
    except Exception as e:
        row.update(status="ERR", err=f"{type(e).__name__}: {str(e)[:90]}")
        # distinguish a cert problem from a dead host: retry unverified
        try:
            with httpx.Client(follow_redirects=True, timeout=25, verify=False,
                              headers={"User-Agent": UA}) as c:
                r = c.get(url)
            row["noverify"] = f"{r.status_code} ({len(r.content)}B)"
        except Exception as e2:
            row["noverify"] = f"also fails: {type(e2).__name__}"
    return row

with cf.ThreadPoolExecutor(max_workers=8) as ex:
    rows = list(ex.map(probe, srcs))

ok = [r for r in rows if r["status"] == 200 and r["bytes"] > 2000]
thin = [r for r in rows if r["status"] == 200 and r["bytes"] <= 2000]
bad = [r for r in rows if r["status"] != 200]

print(f"registered web sources: {len(rows)}")
print(f"  fetch OK  : {len(ok)}")
print(f"  thin body : {len(thin)}")
print(f"  FAILING   : {len(bad)}\n")
for r in bad + thin:
    print(f"  [{r['prio']:6}] {r['id']:28} {r['status']}")
    print(f"           {r['url']}")
    if r.get("err"):      print(f"           err      : {r['err']}")
    if r.get("noverify"): print(f"           no-verify: {r['noverify']}")
    if r.get("final"):    print(f"           final    : {r['final']}")
    if r.get("bytes") is not None and r["status"] == 200:
        print(f"           bytes    : {r['bytes']}")
