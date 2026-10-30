"""Shared durable files, validation and Windows process locks. No GPU imports."""
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import sys

import config as C
import naming

LOCAL = dt.timezone(dt.timedelta(hours=8))
SETTINGS = Path(C.PROJ) / 'pipeline_settings.json'
STATE = Path(C.PROJ) / '.pipeline'
STOP = STATE / 'stop'


def now():
    return dt.datetime.now(LOCAL)


def settings():
    defaults=read_json(Path(__file__).parent/'pipeline_settings.example.json')
    local=read_json(SETTINGS)
    return {**defaults,**local}



def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return {} if default is None else default


def atomic_text(path, text, bom=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8-sig' if bom else 'utf-8', newline='\r\n') as f:
            f.write(text.replace('\r\n', '\n'))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def atomic_json(path, obj):
    atomic_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


def atomic_bytes(path,data):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=path.name+'.',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(data);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.remove(tmp)


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def safe(text):
    value=re.sub(r'[\x00-\x1f\\/:*?"<>|]', '_', text).strip(' .')
    if not value:
        raise ValueError('课程名称缺失或仅包含路径控制字符')
    if re.match(r'^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$',value,re.I):
        value='_'+value
    return value


def items():
    return sorted(read_json(C.CAT).get('items', []),
                  key=lambda x: (x.get('date', ''), x.get('start_at') or 0, str(x['sub_id'])))


def lecture_path(it):
    base = Path(C.DATA).resolve()
    path = (base / safe(it['title']) / naming.lecture_dir(it)).resolve()
    if base not in path.parents:
        raise ValueError('课次路径越出资料根目录')
    return path


def course_rule(it):
    return settings().get('course_rules', {}).get(it['title'], {})


def skip_audio(it):
    return it['title'] in C.SKIP_TITLES or course_rule(it).get('audio') == 'skip'


def window(at=None):
    at = at or now()
    cfg = settings()
    windows = [cfg] + cfg.get('extra_windows', [])
    for candidate in windows:
        h, m = map(int, candidate['start'].split(':'))
        eh, em = map(int, candidate['end'].split(':'))
        start = at.replace(hour=h, minute=m, second=0, microsecond=0)
        end = at.replace(hour=eh, minute=em, second=0, microsecond=0)
        if (at.weekday() == candidate['weekday'] and start <= at < end and
                (not candidate.get('date') or candidate['date'] == at.strftime('%Y-%m-%d'))):
            return True, end
    return False, at.replace(hour=8, minute=0, second=0, microsecond=0)








@contextlib.contextmanager
def lock(name='workflow'):
    STATE.mkdir(exist_ok=True)
    path = STATE / (name + '.lock')
    with open(path, 'a+b') as f:
        f.seek(0, 2)
        if f.tell() == 0:
            f.write(b'0')
            f.flush()
        f.seek(0)
        import msvcrt
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError('已有任务占用：' + name) from None
        try:
            yield
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


def subtitle_status(it):
    if skip_audio(it):
        return '免字幕（静音课程）'
    root = lecture_path(it)
    media = read_json(root / 'media.json')
    if media.get('audio_status') == 'silent' and str(media.get('sub_id')) == str(it['sub_id']):
        return '免字幕（实测无声）'
    tdir = root / 'transcript'
    txt, srt = tdir / 'transcript.txt', tdir / 'transcript.srt'
    interrupted=any(p.exists() for p in (tdir/'commit.pending',Path(str(txt)+'.tmp'),Path(str(srt)+'.tmp')))
    if not txt.exists() or not srt.exists():
        return '待提取'
    try:
        rows = re.findall(r'^\[\d\d:\d\d:\d\d\] .+', txt.read_text(encoding='utf-8'), re.M)
        blocks = re.findall(r'^\d+\s*\n\d\d:\d\d:\d\d,\d{3} -->',
                            srt.read_text(encoding='utf-8'), re.M)
        if not rows or len(rows) != len(blocks):
            return '需修复（字幕不配对）' if interrupted else '待核对（字幕外部修改或不配对，已保留）'
        marker = read_json(tdir / 'done.json')
        if (tdir / 'commit.pending').exists() and marker.get('schema') != 2:
            return '需修复（提交未完成）'
        if marker.get('schema') == 2:
            if str(marker.get('sub_id')) != str(it['sub_id']) or marker.get('hashes') != {
                    'txt': sha(txt), 'srt': sha(srt)}:
                return '需修复（提交未完成）' if interrupted else '待核对（字幕外部修改，未自动覆盖）'
        return '已提取'
    except (UnicodeError, OSError, ValueError):
        return '需修复（无法读取）'


def audio_done(it):
    return subtitle_status(it) in ('已提取', '免字幕（静音课程）', '免字幕（实测无声）')


def manual_gold(it):
    root=lecture_path(it)
    return ((root/'lecture.md').is_file() and (root/'ocr.json').is_file()
            and not (root/'ppt_build.json').exists())



def redact(message):
    text = str(message)
    text = re.sub(r'https?://[^\s\"\']+', '[地址已隐藏]', text)
    return re.sub(r'eyJ[\w-]+\.[\w-]+\.[\w-]+', '[凭证已隐藏]', text)
