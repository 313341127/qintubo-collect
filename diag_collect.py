# -*- coding: utf-8 -*-
"""GitHub runner 上的采集网络复现诊断：打印每阶段耗时"""
import os, time, json
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests

print('pid', os.getpid(), flush=True)
for k in ('HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy','ALL_PROXY','NO_PROXY','REQUESTS_CA_BUNDLE'):
    if os.environ.get(k):
        print('PROXY_ENV', k, '=', os.environ.get(k), flush=True)
print('no proxy env done', flush=True)

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'

def get_json(url):
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=15, verify=False, headers={
                'User-Agent': UA,
                'Referer': url.split('/api.php')[0] + '/',
                'Accept': 'application/json, text/plain, */*',
                'Accept-Language': 'zh-CN,zh;q=0.9'})
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            print('get_json err', str(e)[:120], flush=True)
        time.sleep(0.8)
    return None

base = 'https://subocaiji.com'
# 1) ac=list 单请求
t = time.time()
j = get_json(f'{base}/api.php/provide/vod/?ac=list&pg=2907')
print('STEP list pg2907:', 'ok' if j else 'FAIL', 'records', len(j.get('list') or []) if j else 0, 'sec', round(time.time()-t, 1), flush=True)
if not j:
    sys.exit(1)
lst = j.get('list') or []
print('list len', len(lst), flush=True)

def fetch_detail(m):
    vid = m.get('vod_id')
    if not vid:
        return None
    d = get_json(f'{base}/api.php/provide/vod/?ac=detail&ids={vid}')
    return (vid, bool(d and d.get('list')))

# 2) 并发 detail（模拟 collect_source）
t = time.time()
items = []
with ThreadPoolExecutor(max_workers=16) as ex:
    futs = [ex.submit(fetch_detail, m) for m in lst]
    for f in as_completed(futs):
        items.append(f.result())
print('STEP 16-concurrent detail:', len(items), 'ok', sum(1 for x in items if x and x[1]), 'sec', round(time.time()-t, 1), flush=True)

# 3) 串行 detail（对比）
t = time.time()
okc = 0
for m in lst[:5]:
    d = fetch_detail(m)
    if d and d[1]:
        okc += 1
print('STEP serial 5 detail: ok', okc, 'sec', round(time.time()-t, 1), flush=True)
print('DIAG DONE', flush=True)
