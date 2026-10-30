"""Fail publication/CI if tracked files contain runtime data or common credential forms."""
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
PRIVATE_DIRS={'.pipeline','.browser_profile','.venv','data','__pycache__','归档'}
PRIVATE_FILES={'config.local.json','pipeline_settings.json','weekly.json','ppt_清单.json','catalog_playurls.json',
               'catalog_playurls.json.bak','对账报告.md'}
BINARY_SUFFIXES={'.dpapi','.exe','.dll','.wav','.mp4','.mp3','.srt','.pt','.onnx','.sqlite','.db','.pdf','.zip'}
SECRETS=[re.compile(r'gh[pousr]_[A-Za-z0-9_]{25,}'),
         re.compile(r'github_pat_[A-Za-z0-9_]{45,}'),
         re.compile(r'eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}'),
         re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')]


def violations(path,content):
    relative=Path(path)
    errors=[]
    if PRIVATE_DIRS.intersection(relative.parts) or relative.name in PRIVATE_FILES:
        errors.append('private runtime path')
    if relative.suffix.lower() in BINARY_SUFFIXES:errors.append('binary/course artifact')
    try:text=content.decode('utf-8-sig')
    except UnicodeDecodeError:return errors+['non-text file']
    if any(pattern.search(text) for pattern in SECRETS):errors.append('credential-like content')
    if re.search(r'https://look\.tongji\.edu\.cn/[^\s<>\"\']*\?(?:[^\s<>\"\']*&)?token=[A-Za-z0-9_-]{20,}',text):
        errors.append('authenticated URL')
    return errors


def main():
    result=subprocess.run(['git','ls-files','-z'],cwd=ROOT,capture_output=True,check=True)
    files=[name for name in result.stdout.decode('utf-8').split('\0') if name]
    if not files:
        print('No tracked files to inspect; stage the intended release first.',file=sys.stderr)
        return 1
    errors=[]
    for name in files:
        staged=subprocess.check_output(['git','show',':'+name],cwd=ROOT)
        errors.extend(name+': '+problem for problem in violations(name,staged))
    for problem in errors:print(problem,file=sys.stderr)
    print('Inspected %d tracked files; violations=%d'%(len(files),len(errors)))
    return 1 if errors else 0


if __name__=='__main__':raise SystemExit(main())
