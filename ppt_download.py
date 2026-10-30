# -*- coding: utf-8 -*-
"""一级采集：下载平台「AI 识别(PPT)」里切好的画面。

流程：
    1. 拿 ppt_base（形如 .../ppt_extraction_image_file/<uuid>/ppt/），由 ppt_probe.py 提供；
       没有就现场探测一次。
    2. 按 5 秒网格试 PPT_{毫秒}_0.jpg（模板里的时间点是不规则的：5~60 秒都出现过，
       所以只能逐点试，返回 200 且 >5KB 才算命中）。
    3. 存到 <课程目录>/slides/NNN_HH-MM-SS.jpg，并写 slides_索引.csv。

只使用当前用户已授权课程页面返回的UUID；服务端权限和限流始终优先。

用法：
    python -X utf8 ppt_download.py --date 2026-10-21 --title 高级语言 --out "D:\\tmp\\ppt试跑"
    python -X utf8 ppt_download.py --date 2026-10-21 --title 高级语言        # 直接写进课程目录
"""
import argparse
import csv
import hashlib
import json
import os
import random
import re
import sys
import time
from pathlib import Path
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as C
import naming

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'}
MIN_BYTES = 5 * 1024
STEP_MS = 5000          # 写死 5 秒：金标清单里 110000/215000 都不是 20000 的倍数，
                        # 按 20 秒枚举会漏近一半；1 秒密度扫过，5 秒网格无盲区（2026-10-25 实测）
ABORT = {'flag': False, 'reason': ''}


def safe(s):
    from pipeline_common import safe as normalize
    return normalize(s)


def hms(sec):
    return '%02d-%02d-%02d' % (sec // 3600, (sec % 3600) // 60, sec % 60)


def load_item(date, title_kw, sub_id=None):
    items = json.loads(Path(C.CAT).read_text(encoding='utf-8'))['items']
    for it in items:
        if (sub_id is not None and str(it['sub_id']) == str(sub_id)) or (sub_id is None and it['date'] == date and title_kw in it['title']):
            return it
    raise SystemExit('找不到 %s %s' % (date, title_kw))


def cached_base(it):
    """Use the completed probe result so batch downloads do not contend for Chromium."""
    path = os.path.join(C.PROJ, 'ppt_清单.json')
    if not os.path.exists(path):
        return ''
    try:
        rows = json.loads(Path(path).read_text(encoding='utf-8')).get('items', [])
        for row in rows:
            if str(row.get('sub_id')) == str(it['sub_id']):
                base = row.get('ppt_base') or ''
                prefix = 'https://look.tongji.edu.cn/play/ai/clip/ppt_extraction/'
                return base if base.startswith(prefix) and base.endswith('/ppt/') else ''
    except (OSError, ValueError):
        pass
    return ''


def complete_download(outdir, it):
    path = os.path.join(outdir, 'ppt清单.json')
    if not os.path.exists(path) or not os.path.exists(os.path.join(outdir, 'slides_索引.csv')):
        return False
    try:
        manifest = json.loads(Path(path).read_text(encoding='utf-8'))
        rows = manifest.get('items') or []
        min_size = 128 if manifest.get('source') == 'video' else MIN_BYTES
        return (not manifest.get('partial') and
                str(manifest.get('sub_id')) == str(it['sub_id']) and
                len(rows) == manifest.get('count') and len(rows) > 0 and
                all(os.path.getsize(os.path.join(outdir, 'slides', r['file'])) == r['bytes'] and r['bytes'] >= min_size
                    for r in rows))
    except (OSError, KeyError, TypeError, ValueError):
        return False


def probe_uuid(it, timeout=30):
    from pipeline_common import lock
    from platform_session import login, probe
    from playwright.sync_api import sync_playwright
    with lock('browser'), sync_playwright() as p:
        ctx=p.chromium.launch_persistent_context(C.PROFILE,headless=True,args=['--mute-audio'])
        try:
            login(ctx)
            result=probe(ctx,it)
            if result['status'] in ('blocked','login_required','probe_error'):
                raise RuntimeError('PPT探测失败；保留进度，不当成没有PPT')
            return result.get('ppt_base','')
        finally:
            ctx.close()



def fetch_one(base, ms, retries=2):
    """试一个时间点。命中返回图片字节，没命中返回 None。

    注意 404 是常态（1195 个点里约 980 个没有图），**不能重试**，否则一节要跑十几分钟。
    只有 403/429 才退避，并且立刻全局停下——被限流了就该收手，别硬刚。
    """
    if ABORT['flag']:
        return ms, None, ''
    url = '%sPPT_%d_0.jpg' % (base, ms)
    for attempt in range(retries):
        try:
            time.sleep(random.uniform(0.03, 0.08))       # 抖动，别把对方打爆
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25) as r:
                data = r.read()
                if r.status == 200 and len(data) >= MIN_BYTES:
                    if not data.startswith(b'\xff\xd8') or not data.rstrip().endswith(b'\xff\xd9'):
                        ABORT.update(flag=True, reason='返回内容不是完整JPEG，停止并保留进度')
                        return ms, None, url
                    return ms, data, url
                ABORT.update(flag=True, reason='图片响应异常或体积过小，未提交清单')
                return ms, None, url
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                ABORT['flag'] = True
                ABORT['reason'] = 'HTTP %d（被限流/封禁）' % e.code
                return ms, None, url
            if e.code == 404:
                return ms, None, url
            if attempt + 1 == retries:
                ABORT.update(flag=True, reason='HTTP %d：无法区分缺图与网络错误' % e.code)
        except Exception:
            time.sleep(0.5 * (attempt + 1))               # 网络抖动才重试
    ABORT.update(flag=True, reason=ABORT.get('reason') or '图片网络重试失败，未提交清单')
    return ms, None, url


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', required=True)
    ap.add_argument('--title', required=True)
    ap.add_argument('--sub-id', help='exact lecture identity')
    ap.add_argument('--out', default='')
    ap.add_argument('--step', type=int, default=STEP_MS)
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--force', action='store_true', help='已有手工版产物也强行重下（默认跳过）')
    a = ap.parse_args()

    it = load_item(a.date, a.title, a.sub_id)
    dur_s = int(float(it.get('duration') or 0))
    print('%s %s｜时长 %d 分钟' % (it['date'], it['title'], dur_s // 60), flush=True)

    outdir = a.out or os.path.join(C.DATA, safe(it['title']), naming.lecture_dir(it))
    # 金标那节（W06_10-21）已经有手工版 lecture.md / ocr.json / PDF，默认不碰，别覆盖。
    # 这一步放在开浏览器之前：先判断该不该干，再花 5 秒去起浏览器。
    if not a.force and os.path.exists(os.path.join(outdir, 'ocr.json')) \
            and os.path.exists(os.path.join(outdir, 'lecture.md')):
        print('✗ 这节已经有手工版产物（lecture.md + ocr.json），默认跳过以免覆盖。')
        print('  真要重下就加 --force。目录：%s' % outdir)
        return 5

    if not a.force and complete_download(outdir, it):
        print('✓ 图片和清单已经完整，跳过下载：%s' % outdir)
        return 0

    base = cached_base(it) or probe_uuid(it)
    if not base:
        print('✗ 探不到 PPT。注意两种可能：')
        print('   a) 这节课平台真的没有 AI 识别画面；')
        print('   b) 已经有一个 Playwright 在用 .browser_profile（例如 ppt_probe.py 正在跑），')
        print('      第二个 Chromium 起不来 —— 这种情况会误判成"没有"，等那边跑完再试。')
        return 3
    print('PPT 前缀:', base[:120], flush=True)

    slides = os.path.join(outdir, 'slides')
    os.makedirs(slides, exist_ok=True)
    from pipeline_common import atomic_json
    atomic_json(os.path.join(outdir, 'ppt清单.json'),
                {'sub_id': it['sub_id'], 'partial': True, 'count': 0, 'items': []})
    print('输出:', slides, flush=True)

    grid = list(range(0, dur_s * 1000 + 1, a.step))
    print('按 %d 秒网格试 %d 个时间点…' % (a.step // 1000, len(grid)), flush=True)

    results = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, (ms, data, url) in enumerate(ex.map(lambda m: fetch_one(base, m), grid), 1):
            if data is None:
                continue
            sec = ms // 1000
            name = '%03d_%s.jpg' % (len(results) + 1, hms(sec))
            with open(os.path.join(slides, name + '.tmp'), 'wb') as f:
                f.write(data)
            os.replace(os.path.join(slides, name + '.tmp'), os.path.join(slides, name))
            results.append({'idx': len(results) + 1, 'ms': ms, 'sec': sec, 'file': name,
                            'bytes': len(data), 'sha1': hashlib.sha1(data).hexdigest(), 'url': url})
            if i % 100 == 0:
                print('  已试 %d/%d，命中 %d，%.0fs' % (i, len(grid), len(results), time.time() - t0), flush=True)

    print('\n网格试完：%d 点 → 命中 %d 张，用时 %.1f 分钟' % (
        len(grid), len(results), (time.time() - t0) / 60), flush=True)
    if ABORT['flag']:
        print('⚠ 已按规则中止：%s' % ABORT['reason'])
        print('  命中了 %d 张但**没有写清单**（避免半截数据）。过几小时再跑，或换节先做。' % len(results))
        return 6
    if not results:
        print('✗ 一张都没下到')
        return 4

    # A previous interrupted run may have left numbered files that this run did not hit.
    expected = {r['file'] for r in results}
    for name in os.listdir(slides):
        if re.fullmatch(r'\d+_\d{2}-\d{2}-\d{2}\.jpg', name) and name not in expected:
            os.remove(os.path.join(slides, name))

    total = sum(r['bytes'] for r in results)
    uniq = len({r['sha1'] for r in results})
    print('合计 %.1f MB｜sha1 完全重复 %d 张（去重后 %d 张，交给 _build 按 OCR 文字去重）' % (
        total / 1e6, len(results) - uniq, uniq))

    with open(os.path.join(outdir, 'slides_索引.csv'), 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['序号', '视频时间', '文件', '大小KB', '原始地址'])
        for r in results:
            w.writerow([r['idx'], '%02d:%02d:%02d' % (r['sec'] // 3600, (r['sec'] % 3600) // 60, r['sec'] % 60),
                        r['file'], round(r['bytes'] / 1024), r['url']])

    atomic_json(os.path.join(outdir, 'ppt清单.json'),
              {'sub_id': it['sub_id'], 'duration_ms': dur_s * 1000, 'source': 'platform',
               'step_sec': 5, 'count': len(results),
               'total_bytes': total, 'items': [{'ms': r['ms'], 'bytes': r['bytes'], 'file': r['file']}
                                               for r in results]})
    print('清单：slides_索引.csv / ppt清单.json')
    return 0


if __name__ == '__main__':
    sys.exit(main())
