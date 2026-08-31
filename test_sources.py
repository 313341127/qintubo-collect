import requests, urllib3, time
from concurrent.futures import ThreadPoolExecutor, as_completed
urllib3.disable_warnings()

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0"}

SOURCES = [
    ("爱奇艺", "https://www.iqiyizyapi.com"),
    ("魔都", "https://www.mdzyapi.com"),
    ("最大", "https://zuidazy.com"),
    ("最大2", "https://www.zuidazy.co"),
    ("飘零", "https://p2100.net"),
    ("百度", "https://api.apibdzy.com"),
    ("无尽", "https://api.wujinapi.com"),
    ("速博", "https://subocaiji.com"),
    ("索尼", "http://suonizy.com"),
    ("非凡", "http://cj.ffzyapi.com"),
    ("暴风", "https://bfzyapi.com"),
]

def test(name, base):
    try:
        url = f"{base}/api.php/provide/vod/?ac=list"
        r = requests.get(url, timeout=15, verify=False, headers=HEADERS)
        if r.status_code != 200:
            return (name, base, f"HTTP {r.status_code}", "")
        j = r.json()
        lst = j.get("list", [])
        pc = j.get("pagecount", 0)
        if not lst:
            return (name, base, f"list空 页{pc}", "")
        vid = lst[0].get("vod_id")
        if not vid:
            return (name, base, f"无vod_id 页{pc}", "")
        dr = requests.get(f"{base}/api.php/provide/vod/?ac=detail&ids={vid}", timeout=15, verify=False, headers=HEADERS)
        dj = dr.json()
        if not dj.get("list"):
            return (name, base, f"detail空 页{pc}", "")
        dd = dj["list"][0]
        pu = dd.get("vod_play_url") or ""
        eps = len([x for x in pu.split("#") if "$" in x])
        flag = "OK" if eps > 0 else "NO-EPS"
        return (name, base, f"{flag} list页{pc} detail集数{eps}", str(dd.get("vod_name", ""))[:14])
    except Exception as e:
        return (name, base, f"ERR {type(e).__name__} {str(e)[:60]}", "")

print("=== 源站放行测试 (GitHub Actions IP) ===")
with ThreadPoolExecutor(max_workers=8) as ex:
    futs = {ex.submit(test, n, b): n for n, b in SOURCES}
    for f in as_completed(futs):
        r = f.result()
        print(f"[{r[0]}] {r[1]}\n    {r[2]} {r[3]}")
