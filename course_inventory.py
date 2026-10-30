"""Course/week inventory and substantive delivery checks, without starting GPU work."""
import csv
from collections import defaultdict
import html
import io
from pathlib import Path
import re
from urllib.parse import quote, unquote

import config as C
import naming
from pipeline_common import (atomic_json, atomic_text, items, lecture_path, manual_gold,
                             now, read_json, sha, skip_audio, subtitle_status,safe)


def artifacts(it, deep=False):
    root = lecture_path(it)
    md = root / 'lecture.md'
    if manual_gold(it) and (root / 'lecture_自动更新.md').exists():
        md = root / 'lecture_自动更新.md'
    raw_names = {x.name for x in (root / 'slides').glob('*.jpg')
                 if re.fullmatch(r'\d+_\d\d-\d\d-\d\d\.jpg',x.name,re.I)}
    from ppt_download import complete_download
    pictures_ready = manual_gold(it) or complete_download(str(root), it)
    result = {'ready': False, 'raw': len(raw_names), 'unique': 0,
              'pictures_ready': pictures_ready, 'lecture_file': md.name, 'issues': []}
    if not md.exists():
        return result
    try:
        marker = read_json(root / 'ppt_build.json')
        manifest = read_json(root / 'ppt清单.json')
        text = md.read_text(encoding='utf-8')
        refs = re.findall(r'!\[[^\]]*\]\(([^)]+)\)', text)
        times=[]
        for ref in refs:
            match=re.search(r'_(\d\d)-(\d\d)-(\d\d)\.jpg$',ref)
            if match:
                times.append(int(match[1])*3600+int(match[2])*60+int(match[3]))
        if times != sorted(times):
            result['issues'].append('讲义图片时间戳顺序错误')
        if not refs:
            result['issues'].append('讲义缺少画面')
        for ref in refs:
            image = (root / unquote(ref)).resolve()
            if root.resolve() not in image.parents or not image.is_file():
                result['issues'].append('讲义图片链接无效')
                break
        ocr = read_json(root / 'ocr.json', [])
        if not isinstance(ocr, list) or not ocr:
            result['issues'].append('OCR清单缺失')
            ocr = []
        if any(x.get('err') for x in ocr):
            result['issues'].append('OCR包含错误')
        names = raw_names
        if {x.get('file') for x in ocr} != names:
            result['issues'].append('OCR与原图不配对')
        for key in ('slides_索引.csv', 'slides_去重索引.csv'):
            if not (root / key).exists():
                result['issues'].append(key + '缺失')
        result.update(raw=len(names), unique=len(refs))
        pdf = root / ('PPT讲义_%s.pdf' % it['date'])
        dedup_pdf = root / ('PPT讲义_%s_去重.pdf' % it['date'])
        if manual_gold(it) and dedup_pdf.exists():
            pdf = dedup_pdf
        if not pdf.exists():
            result['issues'].append('PDF缺失')
        else:
            from pdf_utils import page_count
            if page_count(pdf) != len(refs):
                result['issues'].append('PDF页数与讲义画面数不一致')
        if manifest.get('partial'):
            result['issues'].append('试验片段未完成整节')
        transcript = root / 'transcript' / 'transcript.txt'
        if manual_gold(it) and transcript.exists():
            merge_marker = read_json(root / 'manual_merge.json')
            if md.name != 'lecture_自动更新.md' or merge_marker.get('transcript_sha256') != sha(transcript):
                result['issues'].append('新字幕待合并到手工讲义自动更新版')
        no_audio = subtitle_status(it).startswith('免字幕')
        if marker and not no_audio:
            import hashlib
            tr_hash = hashlib.sha1(transcript.read_bytes()).hexdigest() if transcript.exists() else ''
            if marker.get('transcript_sha1') != tr_hash:
                result['issues'].append('讲义尚未合并最新字幕')
        if no_audio and re.search(r'^\[\d\d:\d\d:\d\d\]', text, re.M):
            result['issues'].append('静音课程讲义混入旧字幕')
        if deep:
            from PIL import Image
            for name in names:
                with Image.open(root / 'slides' / name) as image:
                    image.verify()
            if manifest.get('count') and manifest.get('count') != len(names):
                result['issues'].append('原图数量与清单不一致')
            if marker.get('source_sha256'):
                import hashlib
                signature = hashlib.sha256(''.join(n + ':' + sha(root / 'slides' / n)
                                                    for n in sorted(names)).encode()).hexdigest()
                if signature != marker['source_sha256']:
                    result['issues'].append('原图已变化，讲义需重建')
        result['ready'] = not result['issues']
    except Exception as exc:
        result['issues'].append('成品无法校验：' + type(exc).__name__)
    return result


def snapshot(deep=False):
    probes = {str(x['sub_id']): x for x in read_json(Path(C.PROJ) / 'ppt_清单.json').get('items', [])}
    states = read_json(Path(C.PROJ) / '.pipeline' / 'lecture_states.json')
    rows = []
    for it in items():
        root = lecture_path(it)
        p = probes.get(str(it['sub_id']), {})
        a = artifacts(it, deep)
        source = read_json(root / 'ppt清单.json').get('source', '')
        route = '二级·视频画面' if source=='video' or p.get('download_unavailable') else '一级·平台PPT' if p.get('ppt_base') or source == 'platform' or manual_gold(it) else (
                '二级·视频画面' if p and p.get('status') not in ('probe_error', 'login_required', 'blocked') else '待探测')
        subtitle = subtitle_status(it)
        warnings = list(a['issues'])
        txt = root / 'transcript' / 'transcript.txt'
        if txt.exists() and not skip_audio(it):
            from clean_halluc import read_txt, is_halluc
            count = sum(is_halluc(t) for _, t in read_txt(txt))
            if count:
                warnings.append('字幕有%d条疑似噪音待复核' % count)
            unclear=txt.read_text(encoding='utf-8').count('[识别不清]')
            if unclear:warnings.append('字幕有%d处识别不清，已明确标记'%unclear)
        state = states.get(str(it['sub_id']), {})
        subtitle_marker=read_json(root/'transcript'/'done.json')
        build_marker=read_json(root/'ppt_build.json')
        rows.append({'sub_id': str(it['sub_id']), 'course': it['title'], 'date': it['date'],
                     'week': naming.week_of(it['date']), 'slot': naming.slot_of(it.get('sub_title', '')),
                     'dir': str(root.relative_to(Path(C.DATA))), 'subtitle': subtitle,
                     'ppt_route': route, 'ppt': ('已提取' if a['pictures_ready'] else
                                                '部分，待续跑' if a['raw'] else '待提取'),
                     'raw': a['raw'], 'unique': a['unique'],
                     'lecture_file': a['lecture_file'],
                     'has_parts': (root/'lecture_parts'/'INDEX.md').is_file(),
                     'subtitle_revision':subtitle_marker.get('hashes',{}).get('txt') or (str(txt.stat().st_mtime_ns) if txt.exists() else ''),
                     'lecture_revision':build_marker.get('built_at') or read_json(root/'manual_merge.json').get('transcript_sha256',''),
                     'lecture': '已验收' if a['ready'] and (subtitle == '已提取' or subtitle.startswith('免字幕')) else '待完成',
                     'warnings': warnings, 'last_state': state.get('status', ''),
                     'last_error': state.get('error', '')})
    return rows


def table(rows, root_prefix=''):
    text = ['| 周次 | 日期/节次 | 字幕 | PPT方式 | 原图/成品画面 | 讲义 | 备注 |',
            '|---|---|---|---|---|---|---|']
    for row in rows:
        path = quote(root_prefix + row['dir'].replace('\\', '/') + '/' + row['lecture_file'], safe='/')
        lecture = '[已验收](%s)' % path if row['lecture'] == '已验收' else '待完成'
        if row['lecture']=='已验收' and row.get('has_parts'):
            lecture+=' · [分段](%s)'%quote(root_prefix+row['dir'].replace('\\','/')+'/lecture_parts/INDEX.md',safe='/')
        warning = '；'.join(row['warnings']) or row['last_error'] or '—'
        text.append('| W%02d | %s %s | %s | %s | %s | %s | %s |' % (
            row['week'], row['date'], row['slot'], row['subtitle'], row['ppt_route'],
            '%d / %d' % (row['raw'], row['unique']) if row['raw'] else '待提取', lecture,
            warning.replace('|', '／').replace('\n', ' ')))
    return '\n'.join(text)


def generate(deep=False):
    rows = snapshot(deep)
    grouped = defaultdict(list)
    for row in rows:
        grouped[row['course']].append(row)
    ready = sum(x['lecture'] == '已验收' for x in rows)
    subtitles = sum(x['subtitle'] == '已提取' for x in rows)
    skipped = sum(x['subtitle'].startswith('免字幕') for x in rows)
    heading = '# 课程资料总目录\n\n更新：%s（北京时间）\n\n%d 门课，%d 节；字幕已提取 %d 节，免字幕 %d 节，完整讲义已验收 %d 节。\n\n' % (
            now().strftime('%Y-%m-%d %H:%M:%S'), len(grouped), len(rows), subtitles, skipped, ready)
    discovery=read_json(Path(C.PROJ)/'.pipeline'/'discovery_status.json')
    if discovery.get('status')=='failed':
        heading+='> ⚠️ 最近一次未能刷新平台，当前数量仅代表已发现课次；不能据此认定本周新课全部完成。请看运行报告中的登录或网络处理要求。\n\n'
    summary = ['| 课程 | 已发现周次 | 课次 | 字幕已完成 | 免字幕 | PPT已提取 | 讲义已验收 |', '|---|---|---|---|---|---|---|']
    full = []
    for course, course_rows in grouped.items():
        summary.append('| [%s](%s/_course.md) | %s | %d | %d | %d | %d | %d |' % (
             course, quote(safe(course), safe=''), '、'.join('W%02d' % w for w in sorted({r['week'] for r in course_rows})), len(course_rows),
             sum(x['subtitle'] == '已提取' for x in course_rows),
             sum(x['subtitle'].startswith('免字幕') for x in course_rows),
             sum(x['ppt'] == '已提取' for x in course_rows),
             sum(x['lecture'] == '已验收' for x in course_rows)))
        full.extend(['\n## ' + course, '', table(course_rows), ''])
        local = [{**r, 'dir': Path(r['dir']).name} for r in course_rows]
        atomic_text(Path(C.DATA) / safe(course) / '_course.md', '# ' + course + '\n\n' + table(local) + '\n')
    text = heading + '\n'.join(summary) + '\n\n' + '\n'.join(full)
    text += '\n\n日常：周五00:00–08:00（周四深夜）主运行，周六00:00–08:00补跑；08:00停止，周四21:00通知预检结果。\n'
    text += '\n[运行报告](运行报告/) · [验收报告](验收报告.md) · [CSV清单](课次状态总表.csv)\n'
    atomic_text(Path(C.DATA) / 'INDEX.md', text)
    atomic_text(Path(C.DATA) / '课次状态总表.md', text)
    ppt_lines = ['# PPT清单与处理状态', '', '更新：' + now().isoformat(), '',
                 '| # | 课程 | 周次/日期 | 目录 | 分级 | 图片状态 | 讲义 |',
                 '|---|---|---|---|---|---|---|']
    for i, r in enumerate(rows, 1):
        ppt_lines.append('| %d | %s | W%02d %s | %s | %s | %s | %s |' %
                         (i, r['course'], r['week'], r['date'], Path(r['dir']).name,
                          r['ppt_route'], r['ppt'], r['lecture']))
    atomic_text(Path(C.DATA) / '_PPT清单表.md', '\n'.join(ppt_lines) + '\n')
    payload = {'generated': now().isoformat(), 'total': len(rows), 'ready': ready,
               'subtitle_done': subtitles, 'audio_skipped': skipped, 'items': rows}
    atomic_json(Path(C.DATA) / 'index.json', payload)
    buf = io.StringIO(newline='')
    writer = csv.writer(buf)
    writer.writerow(['课程', '周次', '日期', '节次', '字幕', 'PPT方式', '原图', '成品画面', '讲义', '备注'])
    for r in rows:
        writer.writerow([r['course'], 'W%02d' % r['week'], r['date'], r['slot'], r['subtitle'],
                         r['ppt_route'], r['raw'], r['unique'], r['lecture'], '；'.join(r['warnings'])])
    atomic_text(Path(C.DATA) / '课次状态总表.csv', buf.getvalue(), bom=True)
    audit = '# 课程资料验收报告\n\n时间：%s\n\n已验收 %d / %d 节。未完成项会在夜间窗口续跑。\n\n' % (
             now().isoformat(), ready, len(rows))
    for r in rows:
        if r['lecture'] != '已验收' or r['warnings']:
            audit += '- W%02d %s %s：%s；%s；%s\n' % (
                r['week'], r['date'], r['course'], r['subtitle'], r['lecture'],
                '；'.join(r['warnings']) or r['last_error'] or '等待夜间处理')
    atomic_text(Path(C.DATA) / '验收报告.md', audit)
    atomic_text(C.REPORT,audit)
    return payload


def run_report(run, before, after):
    out = Path(C.DATA) / '运行报告'
    out.mkdir(exist_ok=True)
    stamp = now()
    name = 'W%02d_%s_%s_%s' % (naming.week_of(stamp.strftime('%Y-%m-%d')),
              stamp.strftime('%Y-%m-%d'), stamp.strftime('%H-%M-%S'), run.get('kind', '自动更新'))
    old = {r['sub_id']: r for r in before.get('items', [])}
    fields = ('subtitle', 'ppt', 'lecture', 'raw', 'unique','subtitle_revision','lecture_revision')
    changed = [r for r in after['items'] if any(old.get(r['sub_id'], {}).get(k) != r.get(k) for k in fields)]
    if run.get('include_all_courses'):
        changed=after['items']
    text = '# 本次课程更新报告\n\n开始：%s\n\n结束：%s\n\n结果：%s\n\n' % (
             run.get('started', ''), stamp.isoformat(), run.get('result', ''))
    text += '本次新增发现 %d 节；开始时待补 %d 节；完整讲义 %d/%d → %d/%d 节。\n\n' % (
            run.get('discovery', {}).get('new_count', 0),
            before.get('total',0)-before.get('ready',0),before.get('ready', 0),before.get('total',0),
            after.get('ready', 0),after.get('total',0))
    if run.get('reason'):
        text += run['reason'] + '\n\n'
    samples = run.get('resource_samples', [])
    if samples:
        ram = max(x.get('peak_ram_gb',0) for x in samples)
        temps = [x['peak_gpu_c'] for x in samples if x.get('peak_gpu_c') is not None]
        text += '步骤进程组实测内存峰值 %.2f GB；GPU温度峰值 %s。\n\n' % (
                ram, ('%d°C' % max(temps)) if temps else '本次未运行GPU')
        by_sub=defaultdict(float)
        for sample in samples:
            if sample.get('sub_id'):by_sub[sample['sub_id']]+=sample.get('elapsed_sec',0)
        if by_sub:
            text+='| 课程 | 日期 | 本次新增步骤耗时（含重试） |\n|---|---|---|\n'
            for row in after['items']:
                if row['sub_id'] in by_sub:
                    text+='| %s | %s | %.1f 分钟 |\n'%(row['course'],row['date'],by_sub[row['sub_id']]/60)
            text+='\n已存在字幕、图片和OCR会复用，此表不把复用内容当作本次重新运行。\n\n'
    for r in changed:
        text += '- W%02d %s %s：字幕 %s；PPT %s；讲义 %s\n' % (
             r['week'], r['date'], r['course'], r['subtitle'], r['ppt'], r['lecture'])
    text += '\n[全部课程及历史周次](../课次状态总表.md)\n\n'
    for event in run.get('events', []):
        text += '- %s %s\n' % (event.get('at', ''), event.get('message', ''))
    atomic_text(out / (name + '.md'), text)
    atomic_json(out / (name + '.json'), run)
    atomic_text(out / '最新报告.md', text)
    atomic_json(out / '最新报告.json', {**run, 'written_at': stamp.isoformat(),
                                      'updated_courses': changed, 'verified': after['ready'],
                                      'total': after['total']})
    return str(out / (name + '.md'))


if __name__ == '__main__':
    import sys
    result = generate('--deep' in sys.argv)
    print('课程 %d 节；完整讲义 %d 节；字幕已完成 %d 节；免字幕 %d 节' %
          (result['total'], result['ready'], result['subtitle_done'], result['audio_skipped']))
