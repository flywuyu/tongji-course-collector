"""Refresh the protected reference lecture in a separate file, preserving every original."""
import argparse
import csv
from pipeline_common import atomic_json, atomic_text, items, lecture_path, manual_gold, read_json, sha
from ppt_build import build_markdown, load_transcript,write_parts


def merge(it):
    if not manual_gold(it):
        raise RuntimeError('此入口仅用于受保护的手工讲义')
    root = lecture_path(it)
    tr = root / 'transcript' / 'transcript.txt'
    if not tr.exists():
        raise RuntimeError('手工讲义新字幕尚未就绪')
    ocr = {r['file']: r for r in read_json(root / 'ocr.json', [])}
    mapping = []
    with open(root / 'slides_去重索引.csv', encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            h, m, s = map(int, row['视频时间'].split(':'))
            mapping.append({'new': row['新文件名'], 'time': row['视频时间'],
                            'sec': h * 3600 + m * 60 + s,
                            'text': ocr.get(row['来源原文件'], {}).get('text', '')})
    text = build_markdown(it, mapping, load_transcript(tr), False)
    text = text.replace('\n---\n', '\n> [历史手工讲义](lecture.md)保留；本文件合并当前字幕。\n\n---\n', 1)
    atomic_text(root / 'lecture_自动更新.md', text)
    write_parts(it,root,mapping,load_transcript(tr),False,full_name='lecture_自动更新.md')
    atomic_json(root / 'manual_merge.json', {'transcript_sha256': sha(tr),
                'original_lecture_sha256': sha(root / 'lecture.md'), 'unique': len(mapping)})


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--sub-id', required=True)
    args = p.parse_args()
    merge(next(x for x in items() if str(x['sub_id']) == args.sub_id))
