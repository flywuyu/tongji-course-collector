"""Friendly local setup and maintenance commands for Tongji Course Collector."""
from hidden_process import run as run_hidden
from hidden_process import call as call_hidden
import argparse
import datetime as dt
import getpass
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from console import configure

configure()

VERSION='0.1.0'


def initialize(args):
    import config as C
    from pipeline_common import atomic_json,read_json
    start=dt.date.fromisoformat(args.term_start)
    previous=read_json(C.LOCAL_CONFIG)
    if (previous.get('term_start') and previous['term_start']!=start.isoformat() and
            read_json(C.CAT).get('items')):
        raise RuntimeError('已有学期课程，不能原地改学期日期；请为新学期建立独立项目目录')
    if C.LOCAL_CONFIG.exists() and not args.force:
        raise RuntimeError('本地配置已存在；如要改学期，请先为新学期建立独立目录，或明确使用 --force。')
    output=Path(args.output).expanduser()
    if not output.is_absolute():output=Path(C.PROJ)/output
    output=output.resolve()
    if output==Path(C.PROJ) or output in Path(C.PROJ).parents:
        raise RuntimeError('课程输出目录不能是项目根目录或其上级')
    output.mkdir(parents=True,exist_ok=True)
    atomic_json(C.LOCAL_CONFIG,{'term_start':start.isoformat(),'data_dir':str(output)})
    settings=Path(C.PROJ)/'pipeline_settings.json'
    if not settings.exists():
        atomic_json(settings,read_json(Path(C.PROJ)/'pipeline_settings.example.json'))
    (Path(C.PROJ)/'.pipeline').mkdir(exist_ok=True)
    print('本机配置已创建。下一步：python manage.py doctor，然后 python manage.py login')
    return 0


def doctor():
    import config as C
    packages=('torch','openai-whisper','playwright','psutil','Pillow','numpy',
              'rapidocr-onnxruntime','onnxruntime','reportlab','pypdf')
    errors=[]
    if sys.platform!='win32':errors.append('首版完整采集/定时功能要求Windows 10/11')
    try:C.require_initialized()
    except (RuntimeError,ValueError) as exc:errors.append(str(exc))
    for name in packages:
        try:print(name+': '+importlib.metadata.version(name))
        except importlib.metadata.PackageNotFoundError:errors.append('缺少依赖 '+name)
    if not shutil.which(C.FF) and not Path(C.FF).is_file():errors.append('未找到FFmpeg，请加入PATH或设置TONGJI_FFMPEG')
    else:
        result=run_hidden([C.FF,'-version'],capture_output=True,encoding='utf-8',timeout=15)
        if result.returncode:errors.append('FFmpeg无法运行')
    if not shutil.which('nvidia-smi'):errors.append('未找到NVIDIA驱动/nvidia-smi，GPU自动转写不可用')
    try:
        import torch
        if not torch.cuda.is_available():errors.append('当前PyTorch不能使用CUDA，请按官方安装选择器安装CUDA版本')
    except ImportError:pass
    print('\n'.join('需要处理：'+message for message in errors) if errors else '本机基础检查通过；未下载课程，也未加载识别模型。')
    return 1 if errors else 0


def main(argv=None):
    parser=argparse.ArgumentParser(description='同济课程采集系统：配置、登录、模型准备和凌晨定时')
    parser.add_argument('--version',action='version',version=VERSION)
    sub=parser.add_subparsers(dest='command',required=True)
    init=sub.add_parser('init',help='创建仅保存在本机的配置')
    init.add_argument('--term-start',required=True,help='学期第一天，YYYY-MM-DD')
    init.add_argument('--output',default='data/courses')
    init.add_argument('--force',action='store_true')
    for name,help_text in [('doctor','检查本机依赖'),('login','打开浏览器手动登录'),
                           ('account','可选：加密保存本机账号'),('forget-account','删除保存的账号'),
                           ('prepare-model','显式下载并核验large-v3，不在GPU上加载'),
                           ('report','刷新课程清单'),('run','运行一批，仅在允许时段开始'),
                           ('resume','断点续跑，仅在允许时段开始'),('stop','停止本系统当前任务'),
                           ('schedule','安装本项目的Windows定时任务'),('unschedule','移除本项目的Windows定时任务')]:
        command=sub.add_parser(name,help=help_text)
        if name in ('schedule','unschedule'):command.add_argument('--dry-run',action='store_true')
    args=parser.parse_args(argv)
    if args.command=='init':return initialize(args)
    if args.command=='doctor':return doctor()
    import config as C
    if args.command not in ('forget-account',):C.require_initialized()
    if args.command=='account':
        if sys.platform!='win32':raise RuntimeError('当前加密保存功能仅支持Windows')
        import credentials
        username=input('同济账号（只用于本机登录）：').strip()
        password=getpass.getpass('密码（不会回显，也不写入明文文件）：')
        if not username or not password:raise RuntimeError('账号与密码不能为空')
        credentials.save(username,password)
        print('已使用当前Windows用户的DPAPI加密保存；请勿分享整个浏览器目录或运行数据。')
        return 0
    if args.command=='forget-account':
        import credentials
        credentials.PATH.unlink(missing_ok=True)
        print('已删除本机保存的账号；浏览器登录状态需在浏览器中单独退出。')
        return 0
    if args.command=='prepare-model':
        import whisper
        from model_loader import checkpoint_file
        cache=Path(os.getenv('XDG_CACHE_HOME',str(Path.home()/'.cache')))/'whisper'
        cache.mkdir(parents=True,exist_ok=True)
        whisper._download(whisper._MODELS['large-v3'],str(cache),False)
        checkpoint_file()
        print('large-v3已下载并核验；未在GPU上加载。')
        return 0
    if args.command in ('schedule','unschedule'):
        if sys.platform!='win32':raise RuntimeError('定时任务仅支持Windows')
        script='install_schedule.ps1' if args.command=='schedule' else 'uninstall_schedule.ps1'
        command=['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',str(Path(C.PROJ)/script)]
        if args.command=='schedule':command+=['-PythonPath',sys.executable]
        if args.dry_run:command+=['-WhatIf']
        return call_hidden(command)
    import workflow
    modes={'login':'--login','report':'--report','run':'--run','resume':'--resume','stop':'--stop'}
    sys.argv=['workflow.py',modes[args.command]]
    return workflow.main()


if __name__=='__main__':
    try:raise SystemExit(main())
    except (RuntimeError,ValueError) as exc:
        print(str(exc),file=sys.stderr)
        raise SystemExit(1)
