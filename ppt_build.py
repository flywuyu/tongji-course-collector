# -*- coding: utf-8 -*-
"""Build OCR, deduplicated slides, PDF, and lecture.md for one lecture.

The grouping and Markdown layout follow the archived _final_merge.py prototype.
Run this after the Whisper queue finishes so OCR does not compete for CPU.
"""
import argparse
import csv
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from multiprocessing import Pool

import numpy as np
from PIL import Image, ImageFilter, ImageStat

import config as C
import naming
from ppt_download import load_item, safe

TH_TEXT = 0.72
TH_VIS = 10.0
BUILDER_VERSION = 3
_eng = None
_src = None


def init_ocr(src):
    global _eng, _src
    from rapidocr_onnxruntime import RapidOCR
    import rapidocr_onnxruntime.utils as ort_utils
    from onnxruntime import SessionOptions
    import cv2
    cv2.setNumThreads(1)
    def bounded_options():
        options = SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        return options
    ort_utils.SessionOptions = bounded_options
    _src = src
    _eng = RapidOCR()


def sec_of(name):
    m = re.match(r'\d+_(\d\d)-(\d\d)-(\d\d)\.jpg$', name, re.I)
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) if m else 0


def ocr_one(name):
    try:
        res, _ = _eng(os.path.join(_src, name))
        lines = [x[1] for x in (res or []) if x and len(x) > 1 and x[1]]
        return {'file': name, 'sec': sec_of(name), 'text': ' '.join(lines), 'lines': lines}
    except Exception as e:
        return {'file': name, 'sec': sec_of(name), 'text': '', 'lines': [], 'err': str(e)}


def write_json(path, value):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\r\n') as f:
        json.dump(value, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def run_ocr(src, names, out, workers):
    started = time.time()
    from pipeline_common import read_json, sha
    checkpoint = out + '.progress.json'
    cached = {row['file']: row for row in read_json(checkpoint, []) if not row.get('err')}
    hashes = {n: sha(os.path.join(src, n)) for n in names}
    cached = {n: r for n, r in cached.items() if n in hashes and r.get('source_sha256') == hashes[n]}
    pending = [n for n in names if n not in cached]
    if pending:
        with Pool(processes=workers, initializer=init_ocr, initargs=(src,), maxtasksperchild=200) as pool:
            for row in pool.imap(ocr_one, pending, chunksize=1):
                row['source_sha256'] = hashes[row['file']]
                cached[row['file']] = row
                if len(cached) % 25 == 0:
                    write_json(checkpoint, list(cached.values()))
                    print('OCR进度 %d/%d' % (len(cached), len(names)), flush=True)
    rows = [cached[n] for n in names]
    failures = [r for r in rows if r.get('err')]
    if failures:
        raise RuntimeError('OCR 失败 %d/%d 张；首个错误：%s' %
                           (len(failures), len(rows), failures[0]['err']))
    write_json(out, rows)
    if os.path.exists(checkpoint):
        os.remove(checkpoint)
    print('OCR %d 张，用时 %.1f 分钟' % (len(rows), (time.time() - started) / 60), flush=True)
    return rows


def norm(text):
    return re.sub(r'[\s\.,;:!?\'"()\[\]{}<>_\-—…、。，；：！？“”‘’（）【】《》]', '', text or '').lower()


def display_boxes(gray):
    import cv2
    cv2.setNumThreads(1)
    arr = np.asarray(gray, dtype=np.uint8)
    _, binary = cv2.threshold(cv2.GaussianBlur(arr, (5, 5), 0), 0, 255,
                              cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    found = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        area = cv2.contourArea(contour)
        if .06 * arr.size < area < .85 * arr.size and 1.35 < w / h < 2.3 and area / (w*h) > .72:
            found.append((x, y, x+w, y+h))
    return found


def overlap(a, b):
    inter = max(0, min(a[2],b[2])-max(a[0],b[0])) * max(0, min(a[3],b[3])-max(a[1],b[1]))
    union = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter
    return inter / max(1, union)


def stable_displays(src, names):
    samples = []
    for name in names[:12]:
        with Image.open(os.path.join(src, name)) as im:
            samples.append(display_boxes(im.convert('L')))
    if len(samples) < 3:
        return []
    candidates = next((x for x in samples if x), [])
    return [box for box in candidates
            if sum(any(overlap(box, other) > .85 for other in sample) for sample in samples) >= len(samples)*.75]


def image_stats(path, boxes=None):
    with Image.open(path) as im:
        gray = im.convert('L')
        sharpness = ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).var[0]
        use_focus = bool(boxes) and all(any(overlap(box, other) > .85 for other in display_boxes(gray))
                                      for box in boxes)
        regions = [gray.crop(box) for box in boxes] if use_focus else [gray]
        vector = np.stack([np.asarray(region.resize((128,72), Image.Resampling.LANCZOS),
                                     dtype=np.float32) for region in regions])
    return sharpness, vector, use_focus


def dedupe(src, names, ocr):
    from pipeline_common import read_json
    manifest = read_json(Path(src).parent / 'ppt清单.json')
    boxes = stable_displays(src, names) if manifest.get('source') == 'video' else []
    by_file = {row['file']: row for row in ocr}
    rows = []
    for name in names:
        rec = by_file.get(name, {})
        sharpness, vector, focused = image_stats(os.path.join(src, name), boxes)
        rows.append({'file': name, 'sec': sec_of(name), 'text': rec.get('text', ''),
                     'nt': norm(rec.get('text', '')), 'sharp': sharpness, 'v': vector, 'focus': focused})
    groups = [[rows[0]]]
    for row in rows[1:]:
        prev = groups[-1][-1]
        if row['focus'] != prev['focus'] or row['v'].shape != prev['v'].shape:
            groups.append([row])
            continue
        diff = np.abs(row['v'] - prev['v'])
        visual_same = (float(np.mean(diff)) < TH_VIS and
                       float(diff.reshape(-1, 9, 8, 16, 8).mean(axis=(2, 4)).max()) < 25)
        if len(row['nt']) >= 12 and len(prev['nt']) >= 12:
            same = visual_same and difflib.SequenceMatcher(None, prev['nt'], row['nt']).ratio() >= TH_TEXT
        else:
            same = visual_same
        if same:
            groups[-1].append(row)
        else:
            groups.append([row])
    kept = []
    for group in groups:
        best = max(group, key=lambda r: (len(r['text']), r['sharp']))
        best['merged'] = len(group)
        best['sec'] = group[0]['sec']
        kept.append(best)
    return kept


def format_time(sec):
    return '%02d:%02d:%02d' % (sec // 3600, (sec % 3600) // 60, sec % 60)


def load_transcript(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding='utf-8') as f:
        for line in f:
            m = re.match(r'\[(\d\d):(\d\d):(\d\d)\]\s*(.+)', line.strip())
            if m:
                out.append((int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)),
                            m.group(4)))
    return out


def append_slide(md, row, prefix=''):
    md.extend(['', '## 画面 %s　（视频 %s）' % (row['new'][:3], row['time']), '',
               '![%s](%sslides_unique/%s)' % (row['new'], prefix,row['new']), ''])
    if row['text']:
        md.extend(['**画面文字（OCR）**：', '', '```', row['text'], '```'])
    md.append('')


def build_markdown(it, mapping, segments, no_transcript,prefix=''):
    slot = naming.slot_of(it.get('sub_title') or '')
    title = '# %s　%s%s' % (it['title'], it['date'], ('　' + slot) if slot else '')
    md = [title, '', '> 自动生成：课堂字幕（ASR）+ 关键画面（按时间采样，按画面文字去重）。',
          '> 字幕和 OCR 可能有识别错误；代码、公式与术语请结合原画面核对。',
          '> [全部原始画面及时间戳](%sslides_索引.csv) · [去重画面索引](%sslides_去重索引.csv)'%(prefix,prefix),
          '> [按10分钟分段阅读](%slecture_parts/INDEX.md)'%prefix]
    if it.get('course_id') and it.get('sub_id'):
        md.append('> [课程回放](https://look.tongji.edu.cn/livingroom?course_id=%s&sub_id=%s&tenant_code=222)'%
                  (it['course_id'],it['sub_id']))
    if no_transcript:
        md.append('> 本课目前没有可用字幕，以下仅按视频时间排列画面。')
    md.extend(['', '---', ''])
    si = 0
    for sec, text in segments:
        while si < len(mapping) and mapping[si]['sec'] <= sec:
            append_slide(md, mapping[si],prefix)
            si += 1
        md.append('[%s] %s' % (format_time(sec), text))
    while si < len(mapping):
        append_slide(md, mapping[si],prefix)
        si += 1
    return '\n'.join(md) + '\n'


def write_parts(it,outdir,mapping,segments,no_transcript,full_name='lecture.md'):
    from pipeline_common import atomic_text,atomic_json
    parts=Path(outdir)/'lecture_parts'
    parts.mkdir(exist_ok=True)
    duration=max(float(it.get('duration')or 0),max((r['sec'] for r in mapping),default=0)+1,
                 max((s for s,t in segments),default=0)+1)
    index=['# '+it['title']+'　'+it['date']+' · 分段阅读','',
           '[完整讲义](../'+full_name+')；每段保留原视频时间，便于AI逐段读取。','',
           '| 段 | 视频范围 | 画面 | 字幕行 |','|---|---|---|---|']
    files=[]
    for i,start in enumerate(range(0,int(duration)+1,600),1):
        end=min(start+600,duration)
        if end<=start:continue
        slides=[r for r in mapping if start<=r['sec']<end]
        prior=[r for r in mapping if r['sec']<start]
        if prior:slides=[prior[-1]]+slides
        lines=[(s,t)for s,t in segments if start<=s<end]
        if not slides and not lines:continue
        name='part_%02d_%s.md'%(i,format_time(start).replace(':','-'))
        text=build_markdown(it,slides,lines,no_transcript,prefix='../')
        text=text.replace('\n---\n','\n> 本段视频范围：%s–%s；必要时带入上一段末张画面。\n\n---\n'%
                          (format_time(start),format_time(int(end))),1)
        atomic_text(parts/name,text)
        files.append(name)
        index.append('| [%02d](%s) | %s–%s | %d | %d |'%
                     (i,name,format_time(start),format_time(int(end)),len(slides),len(lines)))
    atomic_text(parts/'INDEX.md','\n'.join(index)+'\n')
    atomic_json(parts/'manifest.json',{'files':files,'part_seconds':600,'full_lecture':full_name})


def make_pdf(paths, dst):
    from pdf_utils import make_image_pdf
    make_image_pdf(paths,dst)


def cached_outputs_complete(outdir, pdf, expected):
    from pdf_utils import page_count
    try:
        root=Path(outdir).resolve()
        text=(root/'lecture.md').read_text(encoding='utf-8')
        refs=re.findall(r'!\[[^\]]*\]\(([^)]+)\)',text)
        if len(refs)!=expected or not refs:
            return False
        if not all((root/ref).is_file() and root in (root/ref).resolve().parents for ref in refs):
            return False
        from pipeline_common import read_json
        parts=read_json(root/'lecture_parts'/'manifest.json')
        if not (root/'lecture_parts'/'INDEX.md').is_file() or not parts.get('files') or not all(
                (root/'lecture_parts'/f).is_file() for f in parts['files']):
            return False
        return page_count(pdf)==expected
    except (OSError,ValueError,RuntimeError):
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', required=True)
    ap.add_argument('--title', required=True)
    ap.add_argument('--sub-id', help='exact lecture identity')
    ap.add_argument('--out', default='')
    ap.add_argument('--ocr-workers', type=int, default=4)
    ap.add_argument('--skip-ocr', action='store_true')
    ap.add_argument('--allow-no-transcript', action='store_true')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--prepare-ocr', action='store_true',help='只做CPU OCR缓存，不提前生成缺字幕讲义')
    a = ap.parse_args()
    it = load_item(a.date, a.title, a.sub_id)
    outdir = a.out or os.path.join(C.DATA, safe(it['title']), naming.lecture_dir(it))
    src = os.path.join(outdir, 'slides')
    if not os.path.isdir(src):
        print('缺少 slides：%s' % src)
        return 2
    names = sorted((n for n in os.listdir(src) if re.fullmatch(r'\d+_\d\d-\d\d-\d\d\.jpg', n, re.I)),
                   key=lambda n: (sec_of(n), int(n.split('_')[0])))
    if not names:
        print('slides 目录没有采集画面')
        return 2
    if a.prepare_ocr:
        from pipeline_common import read_json,sha,atomic_json
        source_hash=hashlib.sha256(''.join(n+':'+sha(os.path.join(src,n)) for n in sorted(names)).encode()).hexdigest()
        prepared=read_json(Path(outdir)/'ocr_prepared.json')
        out=os.path.join(outdir,'ocr.json')
        rows=read_json(out,[]) if prepared.get('source_sha256')==source_hash else []
        if {r.get('file') for r in rows}!=set(names) or any(r.get('err') for r in rows):
            rows=run_ocr(src,names,out,max(1,a.ocr_workers))
        atomic_json(Path(outdir)/'ocr_prepared.json',{'sub_id':str(it['sub_id']),
                    'source_sha256':source_hash,'count':len(rows)})
        print('CPU OCR缓存已准备；未将缺字幕课次标记为完整讲义',flush=True)
        return 0
    tr = os.path.join(outdir, 'transcript', 'transcript.txt')
    from pipeline_common import audio_done, subtitle_status, sha
    no_audio = subtitle_status(it).startswith('免字幕')
    no_transcript = no_audio or not os.path.exists(tr)
    if no_audio:
        tr = os.path.join(outdir, '__no_audio_transcript__')
    if no_transcript and not audio_done(it) and not a.allow_no_transcript:
        print('字幕尚未完成，保留原图，稍后再生成 lecture.md：%s' % outdir)
        return 3
    pdf = os.path.join(outdir, 'PPT讲义_%s.pdf' % it['date'])
    marker = os.path.join(outdir, 'ppt_build.json')
    tr_sha1 = hashlib.sha1(Path(tr).read_bytes()).hexdigest() if not no_transcript else ''
    source_hash = hashlib.sha256(''.join(n + ':' + sha(os.path.join(src, n)) for n in sorted(names)).encode()).hexdigest()
    if not a.force and os.path.exists(marker):
        try:
            old = json.loads(Path(marker).read_text(encoding='utf-8'))
            if (old.get('sub_id') == it['sub_id'] and old.get('slides') == len(names) and
                    old.get('source_sha256') == source_hash and old.get('builder_version') == BUILDER_VERSION and
                    old.get('transcript_sha1') == tr_sha1 and
                    cached_outputs_complete(outdir,pdf,old.get('unique')) and
                    all(os.path.exists(os.path.join(outdir, n)) for n in
                        ('ocr.json', 'lecture.md', 'slides_去重索引.csv')) and os.path.exists(pdf)):
                print('✓ PPT 成品已完整且字幕未变，跳过：%s' % outdir)
                return 0
        except (OSError, ValueError):
            pass
    if not a.force and os.path.exists(os.path.join(outdir, 'ocr.json')) and \
            os.path.exists(os.path.join(outdir, 'lecture.md')) and not os.path.exists(marker):
        print('已有手工成品，默认不覆盖：%s' % outdir)
        return 0

    ocr_path = os.path.join(outdir, 'ocr.json')
    old_marker = {}
    if os.path.exists(marker):
        old_marker = json.loads(Path(marker).read_text(encoding='utf-8'))
    from pipeline_common import read_json
    prepared=read_json(Path(outdir)/'ocr_prepared.json')
    reuse_ocr = source_hash in (old_marker.get('source_sha256'),prepared.get('source_sha256')) and os.path.exists(ocr_path)
    if a.skip_ocr or reuse_ocr:
        if not os.path.exists(ocr_path):
            print('--skip-ocr 需要已有 ocr.json')
            return 4
        ocr = json.loads(Path(ocr_path).read_text(encoding='utf-8'))
    else:
        ocr = run_ocr(src, names, ocr_path, max(1, a.ocr_workers))
    if {r.get('file') for r in ocr} != set(names):
        print('OCR 清单与 slides 不一致，停止构建')
        return 4

    kept = dedupe(src, names, ocr)
    uniq = os.path.join(outdir, 'slides_unique')
    os.makedirs(uniq, exist_ok=True)
    mapping = []
    for i, row in enumerate(kept, 1):
        sec = row['sec']
        name = '%03d_%s.jpg' % (i, format_time(sec).replace(':', '-'))
        shutil.copy2(os.path.join(src, row['file']), os.path.join(uniq, name))
        mapping.append({'new': name, 'sec': sec, 'from': row['file'],
                        'merged': row['merged'], 'text': row['text'], 'time': format_time(sec)})
    expected = {r['new'] for r in mapping}
    for name in os.listdir(uniq):
        if re.fullmatch(r'\d+_\d\d-\d\d-\d\d\.jpg', name, re.I) and name not in expected:
            os.remove(os.path.join(uniq, name))

    csv_path = os.path.join(outdir, 'slides_去重索引.csv')
    with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(['新文件名', '视频时间', '来源原文件', '合并帧数', '画面文字（前80字）'])
        for row in mapping:
            w.writerow([row['new'], row['time'], row['from'], row['merged'], row['text'][:80]])

    make_pdf([os.path.join(uniq, row['new']) for row in mapping], pdf)
    segments = load_transcript(tr)
    md_path = os.path.join(outdir, 'lecture.md')
    with open(md_path + '.tmp', 'w', encoding='utf-8', newline='\r\n') as f:
        f.write(build_markdown(it, mapping, segments, no_transcript))
    os.replace(md_path + '.tmp', md_path)
    write_parts(it,outdir,mapping,segments,no_transcript)
    write_json(marker, {'sub_id': it['sub_id'], 'slides': len(names),
                        'source_sha256': source_hash, 'builder_version': BUILDER_VERSION,
                        'unique': len(mapping), 'transcript_sha1': tr_sha1,
                        'built_at': time.strftime('%Y-%m-%d %H:%M:%S')})
    print('完成：%d 原图 → %d 去重｜%d 行字幕｜PDF %.1f MB' %
          (len(names), len(mapping), len(segments), os.path.getsize(pdf) / 1e6), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
