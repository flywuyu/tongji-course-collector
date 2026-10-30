# -*- coding: utf-8 -*-
"""Level-2 fallback: sample video frames when the platform has no PPT images.

This is deliberately separate from ppt_download.py. It streams the public MP4
through ffmpeg without storing the video, then writes the same slides/index
shape consumed by ppt_build.py. One lecture at a time, after Whisper finishes.
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time

import config as C
import naming
from ppt_download import complete_download, load_item, safe

STEP_SEC = 20


def hms(sec):
    return '%02d-%02d-%02d' % (sec // 3600, (sec % 3600) // 60, sec % 60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', required=True)
    ap.add_argument('--title', required=True)
    ap.add_argument('--sub-id', help='exact lecture identity')
    ap.add_argument('--out', default='')
    ap.add_argument('--step', type=int, default=STEP_SEC)
    ap.add_argument('--input', default='', help='local video for an isolated test (requires --out)')
    ap.add_argument('--seconds', type=int, default=0, help='short isolated test (requires --out)')
    ap.add_argument('--force', action='store_true')
    a = ap.parse_args()
    if a.step < 1:
        ap.error('--step must be at least 1 second')
    if (a.input or a.seconds) and not a.out:
        ap.error('--input and --seconds require --out')
    it = load_item(a.date, a.title, a.sub_id)
    outdir = a.out or os.path.join(C.DATA, safe(it['title']), naming.lecture_dir(it))
    if not a.force and os.path.exists(os.path.join(outdir, 'lecture.md')) and \
            os.path.exists(os.path.join(outdir, 'ocr.json')):
        print('已有 PPT 成品，默认跳过：%s' % outdir)
        return 0
    if not a.force and complete_download(outdir, it):
        print('原图和清单已完整，跳过：%s' % outdir)
        return 0

    duration = float(it.get('duration') or 0)
    if duration <= 0 or not it.get('playurl'):
        print('缺少视频直链或时长')
        return 2
    target_duration = min(duration, a.seconds) if a.seconds else duration
    slides = os.path.join(outdir, 'slides')
    os.makedirs(slides, exist_ok=True)
    from pipeline_common import atomic_json
    atomic_json(os.path.join(outdir, 'ppt清单.json'),
                {'sub_id': it['sub_id'], 'partial': True, 'count': 0, 'items': []})
    for name in os.listdir(slides):
        if re.fullmatch(r'_video_frame_\d{5}\.jpg', name):
            os.remove(os.path.join(slides, name))
    pattern = os.path.join(slides, '_video_frame_%05d.jpg')
    cmd = [C.FF, '-y', '-hide_banner', '-loglevel', 'info', '-threads', '2']
    if not a.input:
        cmd += ['-rw_timeout','10000000','-user_agent', 'Mozilla/5.0']
    cmd += ['-i', a.input or it['playurl'],
            '-t', str(int(target_duration if a.seconds else duration + 60)),
            '-an', '-vf', "fps=1,select='isnan(prev_selected_t)+gte(t-prev_selected_t,%d)+gt(scene,0.008)',showinfo" % a.step,
            '-fps_mode', 'vfr', '-q:v', '3', '-start_number', '0', pattern]
    print('从视频按 %d 秒抽帧：%s %s' % (a.step, it['date'], it['title']), flush=True)
    started = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding='utf-8', errors='strict')
    if proc.returncode:
        print('ffmpeg 失败：%s' % (proc.stderr or '')[-400:])
        return 6 if re.search(r'\b(403|429)\b', proc.stderr or '') else 3
    frames = sorted(x for x in os.listdir(slides) if re.fullmatch(r'_video_frame_\d{5}\.jpg', x))
    points = [float(x) for x in re.findall(r'\bn:\s*\d+.*?pts_time:([\d.]+)', proc.stderr)]
    if len(points) != len(frames) or not points or points[-1] < target_duration - max(a.step + 2, 3):
        print('抽帧不完整或时间戳不匹配：%d 帧 / %d 时间点，预期 %.0f 秒' %
              (len(frames), len(points), target_duration))
        return 4

    rows = []
    for i, old in enumerate(frames):
        sec = int(round(points[i]))
        name = '%03d_%s.jpg' % (i + 1, hms(sec))
        os.replace(os.path.join(slides, old), os.path.join(slides, name))
        rows.append({'ms': sec * 1000, 'bytes': os.path.getsize(os.path.join(slides, name)),
                     'file': name})
    expected = {r['file'] for r in rows}
    for name in os.listdir(slides):
        if re.fullmatch(r'\d+_\d\d-\d\d-\d\d\.jpg', name) and name not in expected:
            os.remove(os.path.join(slides, name))

    with open(os.path.join(outdir, 'slides_索引.csv'), 'w',
              encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['序号', '视频时间', '文件', '大小KB', '原始地址'])
        for i, r in enumerate(rows, 1):
            sec = r['ms'] // 1000
            w.writerow([i, hms(sec).replace('-', ':'), r['file'],
                        round(r['bytes'] / 1024), 'video@' + hms(sec).replace('-', ':')])
    manifest = {'sub_id': it['sub_id'], 'duration_ms': int(target_duration * 1000),
                'partial': bool(a.seconds),
                'source': 'video', 'step_sec': a.step, 'scene_threshold': 0.008,
                'strategy': '每秒检测变化，同时每20秒保留一帧', 'count': len(rows),
                'total_bytes': sum(r['bytes'] for r in rows), 'items': rows}
    tmp = os.path.join(outdir, 'ppt清单.json.tmp')
    with open(tmp, 'w', encoding='utf-8', newline='\r\n') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(outdir, 'ppt清单.json'))
    print('完成：%d 帧，%.1f MB，用时 %.1f 分钟' %
          (len(rows), manifest['total_bytes'] / 1e6, (time.time() - started) / 60), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
