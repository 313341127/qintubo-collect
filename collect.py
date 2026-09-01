#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub Actions 云端采集：苹果CMS源站 -> Turso (SQLite 云)
- 多源全量采集 + 去重（id=title|year 哈希，INSERT OR IGNORE）
- 多线路（同片多源都写 play_urls）
- 断点续传（进度存 collect_progress）
- 封面直接用源站 URL，过滤默认图
"""
import requests, urllib3, json, os, time, re, sys
from concurrent.futures import ThreadPoolExecutor, as_completed

urllib3.disable_warnings()

TURSO_URL = os.environ.get('TURSO_URL', '')
TURSO_TOKEN = os.environ.get('TURSO_TOKEN', '')
TH = {'Authorization': f'Bearer {TURSO_TOKEN}', 'Content-Type': 'application/json'}
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'

SOURCES = [
    {'name': '爱奇艺', 'base': 'https://www.iqiyizyapi.com'},
    {'name': '魔都', 'base': 'https://www.mdzyapi.com'},
    {'name': '最大', 'base': 'https://zuidazy.com'},
    {'name': '索尼', 'base': 'http://suonizy.com'},
    {'name': '无尽', 'base': 'https://api.wujinapi.com'},
    {'name': '速博', 'base': 'https://subocaiji.com'},
    {'name': '暴风', 'base': 'https://bfzyapi.com'},
    {'name': '非凡', 'base': 'http://cj.ffzyapi.com'},
]

DEFAULT_COVERS = [
    "f8b245592640f76bc8e6bca0db4b8aa6", "f107f53f18c87d287c0f07f9aff00aaa",
    "5161ed49852f560e85cd52a1f7f995b7", "e85e5a693c6382ea3181d621e9c6fd6e",
    "863b4c3fbdee183907d1d16ad67c0cd0", "default", "nopic", "no_pic", "placeholder",
]

START = time.time()
TIME_LIMIT = int(os.environ.get('TIME_LIMIT', '3000'))  # 默认 50 分钟

WRITE_FAIL = 0  # 连续写入失败计数（容错）


def _turso_args(args):
    if not args:
        return None
    return [{'type': 'text', 'value': '' if a is None else str(a)} for a in args]


def db(sql, params=None):
    """执行 SQL，返回 dict 列表（SELECT）或 []"""
    global WRITE_FAIL
    req = {'type': 'execute', 'stmt': {'sql': sql}}
    if params is not None:
        req['stmt']['args'] = _turso_args(params)
    for _ in range(3):
        try:
            r = requests.post(f'{TURSO_URL}/v2/pipeline', headers=TH,
                              json={'requests': [req]}, timeout=45)
            j = r.json()
            results = j.get('results')
            if results and not j.get('error'):
                res0 = results[0]
                if res0.get('type') == 'ok':
                    if sql.strip().upper().startswith('INSERT'):
                        WRITE_FAIL = 0
                    out = res0.get('response', {}).get('result', {})
                    cols = [c['name'] for c in out.get('cols', [])]
                    rows = out.get('rows', [])
                    # Turso 返回类型化值 {'type':..., 'value':...}，解包为纯值
                    def unwrap(v):
                        if isinstance(v, dict) and 'type' in v:
                            return v.get('value')
                        return v
                    return [{c: unwrap(r[i]) for i, c in enumerate(cols)} for r in rows]
                return []
            # 错误处理
            err = json.dumps(j.get('error') or results, ensure_ascii=False)
            if 'already exists' in err.lower() or 'unique constraint' in err.lower():
                return []
            if sql.strip().upper().startswith('INSERT'):
                WRITE_FAIL += 1
            print('DB错误:', err[:200], 'SQL前80:', sql[:80], flush=True)
        except Exception as e:
            if sql.strip().upper().startswith('INSERT'):
                WRITE_FAIL += 1
            print('DB重试:', str(e)[:80], flush=True)
        time.sleep(1)
    return None


def get_json(url):
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=15, verify=False, headers={
                'User-Agent': UA,
                'Referer': url.split('/api.php')[0] + '/',
                'Accept': 'application/json, text/plain, */*',
                'Accept-Language': 'zh-CN,zh;q=0.9'
            })
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        time.sleep(0.8)
    return None


def clean_title(t):
    if not t:
        return ''
    t = t.strip()
    for s in ["（预告片）", "(预告片)", "（预告）", "(预告)", "[电影解说]", "【电影解说】", "（电影解说）", "（枪版）", "(枪版)", "（TC）", "(TC)"]:
        t = t.replace(s, '')
    return re.sub(r'\s+', ' ', t).strip()


def make_id(title, year):
    key = clean_title(title) + '|' + (year or '')
    h = 0
    for ch in key:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
        if h >= 0x80000000:
            h -= 0x100000000
    h = abs(h)
    return 'm:' + format(h, 'x').rjust(12, '0')[-12:]


def classify(type_id, title, genres):
    tid = int(type_id) if str(type_id).isdigit() else 0
    g = (genres or '').lower()
    # AI 漫剧/短剧优先识别：源站常把短剧 type_id 标为电影(1)，按 genres 归为短剧
    if any(k in g for k in ['AI漫', '漫剧', '短剧', '微短剧', '竖屏']):
        return '短剧'
    if tid in (1, 2, 3, 4):
        return {1: '电影', 2: '剧集', 3: '综艺', 4: '动漫'}[tid]
    t = (title or '').lower()
    if any(k in g for k in ['动画', '动漫', '卡通']):
        return '动漫'
    if any(k in g for k in ['综艺', '脱口秀', '真人秀']):
        return '综艺'
    if '短剧' in g:
        return '短剧'
    if '直播' in t:
        return '直播'
    if '短剧' in t:
        return '短剧'
    if re.search(r'第[一二三四五六七八九十百千\d]+季|第[一二三四五六七八九十百千\d]+部|S\d{2}', t):
        return '剧集'
    if any(k in t for k in ['综艺', '真人秀', '脱口秀', '晚会', '选秀', '挑战', '奔跑', '爸爸', '妈妈', '姐姐', '哥哥', '侦探', '推理', '密室', '王牌', '欢乐']):
        return '综艺'
    if any(k in t for k in ['动漫', '动画', '番剧', 'ova', 'oad', '剧场版', '动态漫画', '漫剧']):
        return '动漫'
    if 'vs' in t or any(k in t for k in ['联赛', '锦标赛', '杯赛', '世预赛', '中超', '中甲', 'cba', 'nba', '英超', '西甲', '意甲', '德甲', '法甲', '赛事', '体育', '足球', '篮球', '排球', '网球']):
        return '体育'
    return '电影'


def parse_eps(play_url):
    if not play_url:
        return []
    eps = []
    main_line = play_url.split('$$$')[0] or ''
    for part in main_line.split('#'):
        if '$' in part:
            parts = part.split('$', 1)
            if len(parts) == 2:
                title = parts[0].strip()
                u = parts[1].strip()
                if u:
                    if re.search(r'play/', u) and '.m3u8' not in u:
                        u = u.rstrip('/') + '/index.m3u8'
                    eps.append({'title': title, 'url': u})
    return eps


def is_default_cover(cover):
    if not cover:
        return True
    l = cover.lower()
    return any(f in l for f in DEFAULT_COVERS)


def _esc(v):
    if v is None:
        return "''"
    return "'" + str(v).replace("'", "''") + "'"


def d1_batch_insert_movies(rows):
    if not rows:
        return True
    cols = ['id', 'title', 'year', 'rate', 'duration', 'genres', 'plot', 'cover_url',
            'detail_url', 'type', 'actors', 'director', 'area', 'remark', 'fetched_at', 'play_count']
    n = 500
    ok = True
    for i in range(0, len(rows), n):
        chunk = rows[i:i + n]
        vals = ','.join('(' + ','.join(_esc(r[c]) for c in cols) + ')' for r in chunk)
        sql = f"INSERT OR IGNORE INTO movies ({','.join(cols)}) VALUES {vals}"
        if db(sql) is None:
            ok = False
            break
    return ok


def d1_batch_insert_playurls(rows):
    if not rows:
        return True
    cols = ['movie_id', 'source', 'ep_title', 'play_url']
    n = 700
    ok = True
    for i in range(0, len(rows), n):
        chunk = rows[i:i + n]
        vals = ','.join('(' + ','.join(_esc(r[c]) for c in cols) + ')' for r in chunk)
        sql = f"INSERT OR IGNORE INTO play_urls ({','.join(cols)}) VALUES {vals}"
        if db(sql) is None:
            ok = False
            break
    return ok


MAX_EPS = 300  # 每片最多保留的集数（控制存储，超出截断）
MAX_SOURCE_LINES = 3  # 每片最多保留的源线路数（多线路=多个源）


def collect_source(src, start_page, max_pages):
    """采集单个源，返回 (collected, skipped, noeps, detailfail, pages_done)"""
    base = src['base']
    name = src['name']
    collected = skipped = noeps = detailfail = 0
    page = start_page
    pages_done = 0
    try:
        j = get_json(f'{base}/api.php/provide/vod/?ac=list&pg={page}')
        if not j:
            return (collected, skipped, noeps, detailfail, 0, 'list请求失败')
        pagecount = int(j.get('pagecount') or 0)
    except Exception:
        return (collected, skipped, noeps, detailfail, 0, 'list解析失败')

    while pages_done < max_pages and (time.time() - START) < TIME_LIMIT:
        if page > pagecount:
            break
        j = get_json(f'{base}/api.php/provide/vod/?ac=list&pg={page}')
        if not j or not j.get('list'):
            # 尝试下一页
            page += 1
            pages_done += 1
            continue
        lst = j.get('list') or []

        def fetch_detail(m):
            vid = m.get('vod_id')
            if not vid:
                return None
            d = get_json(f'{base}/api.php/provide/vod/?ac=detail&ids={vid}')
            if not d or not d.get('list'):
                return None
            return d['list'][0]

        items = []
        with ThreadPoolExecutor(max_workers=16) as ex:
            futs = [ex.submit(fetch_detail, m) for m in lst]
            for f in as_completed(futs):
                d = f.result()
                if d:
                    items.append(d)

        movie_rows = []
        play_rows = []
        for d in items:
            title = d.get('vod_name') or ''
            ct = clean_title(title)
            if not ct:
                skipped += 1
                continue
            eps = parse_eps(d.get('vod_play_url') or '')
            if not eps:
                noeps += 1
                continue
            year = d.get('vod_year') or ''
            mid = make_id(title, year)
            mtype = classify(d.get('type_id'), title, d.get('vod_class') or '')
            if mtype == '电影':
                remark = (d.get('vod_remarks') or '')
                if re.search(r'全集|集全|全\d+集|完结', remark):
                    mtype = '短剧'
                elif len(eps) > 20:
                    mtype = '剧集'
                elif len(eps) > 1:
                    mtype = '短剧'
            raw_cover = d.get('vod_pic') or ''
            if is_default_cover(raw_cover):
                raw_cover = ''
            rate = d.get('vod_score') or d.get('vod_douban_score') or ''
            now = time.strftime('%Y-%m-%d %H:%M:%S')
            movie_rows.append({
                'id': mid, 'title': ct, 'year': str(year), 'rate': str(rate)[:8],
                'duration': '', 'genres': d.get('vod_class') or '', 'plot': d.get('vod_content') or '',
                'cover_url': raw_cover, 'detail_url': '', 'type': mtype,
                'actors': d.get('vod_actor') or '', 'director': d.get('vod_director') or '',
                'area': d.get('vod_area') or '', 'remark': d.get('vod_remarks') or '',
                'fetched_at': now, 'play_count': 0,
            })
            for ep in eps[:MAX_EPS]:
                play_rows.append({
                    'movie_id': mid, 'source': name, 'ep_title': ep['title'],
                    'play_url': ep['url'],
                })
            collected += 1

        if movie_rows:
            if not d1_batch_insert_movies(movie_rows):
                return (collected, skipped, noeps, detailfail, pages_done, f'页{start_page}->{page-1} 写入失败停止')
        if play_rows:
            if not d1_batch_insert_playurls(play_rows):
                return (collected, skipped, noeps, detailfail, pages_done, f'页{start_page}->{page-1} 写入失败停止')

        page += 1
        pages_done += 1

    # 返回是否完成该源
    done = page > pagecount
    return (collected, skipped, noeps, detailfail, pages_done, f'页{start_page}->{page-1}' + (' 完成' if done else ' 未完'))


def main():
    if not (TURSO_URL and TURSO_TOKEN):
        print('缺少 Turso 环境变量')
        sys.exit(1)

    # 确保 play_urls 唯一索引（3 列精简索引，多线路去重）
    db("CREATE UNIQUE INDEX IF NOT EXISTS idx_play_uniq ON play_urls(movie_id, source, ep_title)")

    # 读进度
    rows = db('SELECT source_index, source_pages, total_collected, total_skipped FROM collect_progress WHERE id=1')
    if not rows:
        db("INSERT INTO collect_progress (id, source_index, source_pages, total_collected, total_skipped) VALUES (1, 0, '{}', 0, 0)")
        rows = db('SELECT source_index, source_pages, total_collected, total_skipped FROM collect_progress WHERE id=1')
    prog = rows[0]
    try:
        source_pages = json.loads(prog.get('source_pages') or '{}')
    except Exception:
        source_pages = {}
    si = int(prog.get('source_index') or 0)
    total_collected = int(prog.get('total_collected') or 0)
    total_skipped = int(prog.get('total_skipped') or 0)

    # 每次运行：从当前源开始，最多采 MAX_ROUNDS 个源，每源最多 MAX_PAGES 页
    # 全部源采完一轮后进入增量模式（每源只采最新 2 页）
    done_all = db("SELECT 1 AS x FROM collect_progress WHERE last_result LIKE 'ALLDONE%'")
    incr_mode = bool(done_all)
    MAX_ROUNDS = len(SOURCES)
    MAX_PAGES = 2 if incr_mode else int(os.environ.get('MAX_PAGES', '30'))
    rounds = 0
    done_count = 0
    while rounds < MAX_ROUNDS and (time.time() - START) < TIME_LIMIT:
        src = SOURCES[si % len(SOURCES)]
        page = int(source_pages.get(str(si), 1))
        print(f'[{src["name"]}] 模式{"增量" if incr_mode else "全量"} page={page}', flush=True)
        collected, skipped, noeps, detailfail, pdone, msg = collect_source(src, page, MAX_PAGES)
        total_collected += collected
        total_skipped += skipped + noeps
        is_done = '完成' in msg
        new_page = page + pdone
        if is_done:
            # 该源采完，重置到第 1 页（下轮从头采最新）
            new_page = 1
            done_count += 1
        source_pages[str(si)] = new_page
        print(f'[{src["name"]}] {msg} 采{collected} 跳过{skipped+noeps} (累计{total_collected})', flush=True)
        si = (si + 1) % len(SOURCES)
        rounds += 1
        # 更新进度
        last_result = f'源{src["name"]} {msg} 采{collected}'
        if done_count == len(SOURCES):
            last_result = 'ALLDONE 全量第一轮完成，进入增量模式'
        db('UPDATE collect_progress SET source_index=?, source_pages=?, total_collected=?, total_skipped=?, last_run=?, last_result=? WHERE id=1',
           [si, json.dumps(source_pages), total_collected, total_skipped,
            time.strftime('%Y-%m-%d %H:%M:%S'), last_result])
        # 容错：写入失败累计>=3 或本次返回"存储满"则停止本轮
        if WRITE_FAIL >= 3 or '写入失败' in msg:
            print('!! 写入持续失败，停止本轮采集', flush=True)
            break

    print(f'=== 本次完成：累计采集 {total_collected} 部 ===', flush=True)

    # ===== 云端自接力（关机也可持续全量采集） =====
    # 若未全量完成(无 ALLDONE)，自动 dispatch 触发下一轮，直到全量完成
    try:
        fin = db('SELECT last_result FROM collect_progress WHERE id=1')
        if fin and not str(fin[0].get('last_result') or '').startswith('ALLDONE'):
            tok = os.environ.get('GH_PAT') or os.environ.get('GITHUB_TOKEN') or ''
            repo = os.environ.get('GH_REPO', '313341127/qintubo-collect')
            if tok:
                r = requests.post(
                    f'https://api.github.com/repos/{repo}/actions/workflows/collect.yml/dispatches',
                    headers={'Authorization': f'token {tok}', 'Accept': 'application/vnd.github+json',
                             'User-Agent': 'qintubo-collect', 'Content-Type': 'application/json'},
                    json={'ref': 'main'}, timeout=30)
                print(f'=== 云端自接力：未全量完成，已触发下一轮 (HTTP {r.status_code}) ===', flush=True)
            else:
                print('=== 无 GITHUB_TOKEN，跳过自接力 ===', flush=True)
        else:
            print('=== 全量采集完成(ALLDONE)，停止自接力 ===', flush=True)
    except Exception as e:
        print('云端自接力失败:', str(e)[:150], flush=True)


if __name__ == '__main__':
    main()
