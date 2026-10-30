# -*- coding: utf-8 -*-
"""单实例锁：保证同一时间只有一个采集进程在跑

用法：
    import runlock
    ok, holder = runlock.acquire()
    if not ok:
        print('已有进程在跑：', holder)
        sys.exit(1)
    atexit.register(runlock.release)      # 正常退出自动释放

锁文件：项目根目录 .collector.lock（内容：pid + 启动时间）
崩溃留下的锁：acquire() 会检查 PID 是否还活着，死了就直接接管。
"""
from hidden_process import run as run_hidden
import os, sys, json, time, subprocess, atexit

import config as C

LOCK = C.LOCK
_held = False


def _pid_alive(pid):
    if not pid:
        return False
    try:
        import psutil
        return psutil.pid_exists(int(pid))
    except (ImportError, ValueError):
        pass
    try:
        out = run_hidden(['tasklist', '/FI', 'PID eq %s' % pid],
                             capture_output=True, text=True, errors='replace', timeout=10).stdout
        return str(pid) in (out or '')
    except Exception:
        # 查不到就当活着，宁可不动
        return True


def peek():
    """不抢锁，只看现在有没有人在跑。返回 (是否被占用, 信息dict)"""
    if not os.path.exists(LOCK):
        return False, {}
    try:
        with open(LOCK, encoding='utf-8') as f:
            info = json.load(f)
    except Exception:
        return False, {}
    if not _pid_alive(info.get('pid')):
        return False, info
    try:
        import psutil
        process = psutil.Process(int(info['pid']))
        created = info.get('created')
        if created is None and info.get('start'):
            created = time.mktime(time.strptime(info['start'], '%Y-%m-%d %H:%M:%S'))
        if created is not None and abs(process.create_time() - float(created)) > 2:
            return False, info
    except psutil.NoSuchProcess:
        return False, info
    except (ValueError, KeyError, psutil.AccessDenied):
        return True, info
    return True, info


def acquire():
    global _held
    from pipeline_common import lock, atomic_json, window
    with lock('collector-claim'):
        if not window()[0]:
            return False, {'reason': 'outside_window', 'message': '只能在配置的凌晨运行窗口启动GPU采集'}
        busy, info = peek()
        if busy:
            return False, info
        import psutil
        from pipeline_common import settings
        if psutil.virtual_memory().available/1e9 < settings()['min_available_ram_gb']:
            return False, {'reason':'memory','message':'可用内存不足，未启动新转写'}
        from workflow import gpu_free_gb
        free=gpu_free_gb()
        if free is None or free<settings().get('asr_min_gpu_free_gb',4.5):
            return False, {'reason':'vram','message':'独立显存不足或读数不可用，不以共享内存强行加载模型'}
        data = {'pid': os.getpid(), 'created': psutil.Process().create_time(),
                'start': time.strftime('%Y-%m-%d %H:%M:%S')}
        atomic_json(LOCK, data)
        _held = True
        return True, data


def release():
    global _held
    if not _held:
        return
    try:
        if os.path.exists(LOCK):
            info = json.load(open(LOCK, encoding='utf-8'))
            if str(info.get('pid')) == str(os.getpid()):
                os.remove(LOCK)
    except Exception:
        pass
    _held = False
