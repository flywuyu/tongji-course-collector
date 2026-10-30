# -*- coding: utf-8 -*-
"""清理 Whisper 幻觉行：静音段被识别成「字幕志愿者」「由 Amara.org 社群提供的字幕」这类废话。

仅列出疑似模板噪音，默认不改字幕。需要结合原录音人工核对；--apply是用户明确选择的清理操作。

用法：
    python -X utf8 clean_halluc.py                  # 试运行，只报告，一个字都不改
    python -X utf8 clean_halluc.py --apply          # 真改：先写 .bak 备份，再重写 txt 和 srt
    python -X utf8 clean_halluc.py --apply 某节目录   # 只处理指定目录（可给多个）

判定规则（保守：只删铁定是幻觉的）：
    1. 文本命中已知幻觉模板；
    2. 连续 3 条及以上完全相同的文本（静音循环的特征）。
不删：正常的课堂用语（"谢谢大家"、"我们下课"这类真人会说的话不在模板里）。

改完 txt 和 srt 会保持一一对应（srt 序号重排）。原有 .bak 不会被覆盖，重复跑安全。
"""
import os
import re
import sys
import glob
from pathlib import Path

import config as C

# 已知幻觉模板。全部按"归一化后的整行文本"匹配，避免误伤。
PATTERNS = [
    r'^请不吝点赞',
    r'^请不吝點讚',
    r'^请大家不吝点赞',
    r'^点个?(赞|订阅)',
    r'^订阅(我们的)?频道',
    r'^由\s*amara',
    r'^amara\.org',
    r'^字幕由\s*amara',
    r'^字幕志愿者',
    r'^中文版字幕',
    r'^中文字幕',            # 含「中文字幕——YK」「中文字幕志愿者 李宗盛」
    r'^字幕(提供|制作)',
    r'^本字幕',
    r'^響鐘$',
    r'^响钟$',
    r'^敲鐘$',
    r'^ming\s*pao',
    r'^明镜与点点$',
    r'^谢谢(观看|收看)$',
    r'^感谢(您的?)?(观看|收看)$',
    r'^谢谢大家观看$',
]
PAT = [re.compile(p, re.IGNORECASE) for p in PATTERNS]

RE_TXT = re.compile(r'^\[(\d\d):(\d\d):(\d\d)\]\s*(.*)$')


def norm(s):
    """归一化：去空白和标点，只留文字和数字，再做模板匹配。"""
    return re.sub(r'[\s\u3000·—\-–_.,，。、！!？?：:；;\"\'“”‘’()（）\[\]【】]', '', s).lower()


def is_halluc(text):
    n = norm(text)
    if not n:
        return False
    for p in PAT:
        if p.search(n):
            return True
    return False


def read_txt(path):
    rows = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            m = RE_TXT.match(line.rstrip('\r\n'))
            if m:
                rows.append((int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)),
                             m.group(4).strip()))
    return rows


def read_srt(path):
    blocks = []
    with open(path, encoding='utf-8') as f:
        raw = f.read()
    for b in re.split(r'\r?\n\r?\n', raw.strip()):
        lines = [x for x in b.splitlines() if x.strip()]
        if len(lines) >= 3:
            blocks.append((lines[1], '\n'.join(lines[2:])))
    return blocks


def mark(rows):
    """返回 keep 掩码：False = 删掉。"""
    keep = [True] * len(rows)
    texts = [r[1] for r in rows]

    for i, t in enumerate(texts):
        if is_halluc(t):
            keep[i] = False

    # 连续 3 条以上完全相同的文本
    i = 0
    n = len(texts)
    while i < n:
        j = i + 1
        while j < n and texts[j] == texts[i]:
            j += 1
        if j - i >= 3 and norm(texts[i]):
            for k in range(i, j):
                keep[k] = False
        i = j
    return keep


def process(txt_path, apply=False):
    tdir = os.path.dirname(txt_path)
    srt_path = os.path.join(tdir, 'transcript.srt')
    if not os.path.exists(srt_path):
        return None

    rows = read_txt(txt_path)
    blocks = read_srt(srt_path)
    if len(rows) != len(blocks):
        print('  ! 跳过（txt %d 句 / srt %d 块 对不上，不动）: %s' % (len(rows), len(blocks), tdir))
        return None

    keep = mark(rows)
    removed = [rows[i][1] for i in range(len(rows)) if not keep[i]]
    n_keep = sum(keep)

    # whisper 偶尔吐出替换字符 U+FFFD（无法解码的字节），会让 encoding_guard 报"乱码"，一并清掉。
    fffd = sum(r[1].count('\ufffd') for r in rows)
    if fffd:
        rows = [(s, t.replace('\ufffd', '')) for s, t in rows]
        blocks = [(tl, tx.replace('\ufffd', '')) for tl, tx in blocks]

    changed = bool(removed) or fffd > 0

    if apply and changed:
        from pipeline_common import atomic_json,atomic_text,atomic_bytes,read_json,sha,now
        txt_bak = txt_path + '.bak'
        srt_bak = srt_path + '.bak'
        if not os.path.exists(txt_bak):
            atomic_bytes(txt_bak,Path(txt_path).read_bytes())
        if not os.path.exists(srt_bak):
            atomic_bytes(srt_bak,Path(srt_path).read_bytes())

        atomic_json(Path(tdir)/'commit.pending',{'operation':'approved_hallucination_cleanup','at':now().isoformat()})
        txt_content=''.join('[%02d:%02d:%02d] %s\n'%(sec//3600,sec//60%60,sec%60,text)
                            for i,(sec,text) in enumerate(rows) if keep[i])
        kept_blocks=[block for i,block in enumerate(blocks) if keep[i]]
        srt_content=''.join('%d\n%s\n%s\n\n'%(n,timeline,text)
                            for n,(timeline,text) in enumerate(kept_blocks,1))
        atomic_text(txt_path,txt_content)
        atomic_text(srt_path,srt_content)
        marker=Path(tdir)/'done.json'
        meta=read_json(marker)
        if meta:
            meta.update(segments=n_keep,last_cleaned_at=now().isoformat(),cleaned_removed=len(removed))
            if meta.get('schema')==2:
                meta['hashes']={'txt':sha(txt_path),'srt':sha(srt_path)}
            if isinstance(meta.get('segment_meta'),list):
                if len(meta['segment_meta'])==len(rows):
                    meta['segment_meta']=[s for i,s in enumerate(meta['segment_meta']) if keep[i]]
                else:
                    meta.pop('segment_meta')
            atomic_json(marker,meta)
        (Path(tdir)/'commit.pending').unlink(missing_ok=True)

    return {'dir': tdir, 'total': len(rows), 'kept': n_keep,
            'removed': removed, 'fffd': fffd, 'applied': bool(apply and changed)}


def main():
    args = [a for a in sys.argv[1:] if a != '--apply']
    apply = '--apply' in sys.argv[1:]

    if args:
        targets = []
        for a in args:
            p = os.path.join(C.DATA, a, 'transcript', 'transcript.txt') if not a.endswith('.txt') else a
            targets.append(p)
    else:
        targets = sorted(glob.glob(os.path.join(C.DATA, '*', '*', 'transcript', 'transcript.txt')))

    print('模式：%s' % ('真改（会写 .bak 备份）' if apply else '试运行（不改任何文件）'))
    print('目标：%d 节\n' % len(targets))

    tot = 0
    tot_fffd = 0
    for t in targets:
        r = process(t, apply=apply)
        if not r:
            continue
        rel = os.path.relpath(r['dir'], C.DATA)
        tot_fffd += r.get('fffd', 0)
        if not r['removed'] and not r.get('fffd'):
            print('-- %s：干净（%d 句）' % (rel, r['total']))
            continue
        tot += len(r['removed'])
        extra = ''
        if r.get('fffd'):
            extra = '，清替换字符 %d 个' % r['fffd']
        print('-- %s：%d 句 → 保留 %d，删 %d%s%s' % (
            rel, r['total'], r['kept'], len(r['removed']), extra,
            '，已落盘' if r['applied'] else '（未落盘）'))
        seen = {}
        for x in r['removed']:
            seen[x] = seen.get(x, 0) + 1
        for x, c in sorted(seen.items(), key=lambda kv: -kv[1]):
            print('     %3d× %s' % (c, x))

    print('\n合计幻觉行：%d｜替换字符：%d' % (tot, tot_fffd))
    if not apply:
        print('这是试运行。确认无误后加 --apply 真改（会自动留 .bak）。')


if __name__ == '__main__':
    main()
