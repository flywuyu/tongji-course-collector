"""Portable local paths. Private settings and course outputs are never source files."""
import json
import os
from pathlib import Path
import shutil

PROJ=str(Path(__file__).resolve().parent)
LOCAL_CONFIG=Path(PROJ)/'config.local.json'
USER_CONFIG=json.loads(LOCAL_CONFIG.read_text(encoding='utf-8-sig')) if LOCAL_CONFIG.exists() else {}
def resolved(value):
    path=Path(value).expanduser()
    return str((Path(PROJ)/path).resolve()) if not path.is_absolute() else str(path.resolve())

DATA=resolved(USER_CONFIG.get('data_dir','data/courses'))
MATERIAL=resolved(USER_CONFIG.get('reference_dir','data/reference'))
TERM_START=USER_CONFIG.get('term_start')
CAT=str(Path(PROJ)/'catalog_playurls.json')
CAT_BAK=CAT+'.bak'
WEEKLY=str(Path(PROJ)/'weekly.json')
LOG=str(Path(PROJ)/'collect_log.txt')
REPORT=str(Path(PROJ)/'对账报告.md')
LOCK=str(Path(PROJ)/'.collector.lock')
PROFILE=str(Path(PROJ)/'.browser_profile')
FF=os.environ.get('TONGJI_FFMPEG') or USER_CONFIG.get('ffmpeg_path') or shutil.which('ffmpeg') or 'ffmpeg'
FFDIR=str(Path(FF).resolve().parent) if Path(FF).is_file() else ''
SKIP_TITLES=set()  # Configure confirmed silent courses in pipeline_settings.json, never assume.


def require_initialized():
    import datetime
    if not TERM_START:
        raise RuntimeError('请先执行 python manage.py init --term-start 学期首日YYYY-MM-DD')
    datetime.date.fromisoformat(TERM_START)
    Path(DATA).mkdir(parents=True,exist_ok=True)
