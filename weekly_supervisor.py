"""Keep recovering within the user's weekly windows; never restart after manual stop."""
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil
import config as C
from pipeline_common import STATE, STOP, atomic_json, lock, now, read_json, settings, window
from process_job import ProcessJob


def window_identity(at=None):
    at=at or now()
    inside,end=window(at)
    if not inside:
        return None
    cfg=settings()
    for candidate in [cfg,*cfg.get('extra_windows',[])]:
        start=at.replace(hour=int(candidate['start'].split(':')[0]),
                         minute=int(candidate['start'].split(':')[1]),second=0,microsecond=0)
        if (start <= at and end.strftime('%H:%M')==candidate['end'] and candidate['weekday']==at.weekday() and
                (not candidate.get('date') or candidate['date']==at.strftime('%Y-%m-%d'))):
            return {'key':end.isoformat(),'start':start,'end':end}
    return None


def manually_stopped(identity):
    return STOP.exists() and STOP.stat().st_mtime >= identity['start'].timestamp()


def live_process(record):
    try:
        return abs(psutil.Process(record['pid']).create_time()-record['created'])<.1
    except (KeyError,TypeError,psutil.Error):
        return False


def main(recover=False):
    identity=window_identity()
    if identity is None or manually_stopped(identity):
        return 0 if recover else 30
    previous=read_json(STATE/'window_supervisor.json')
    if recover and previous.get('window')==identity['key'] and previous.get('status')=='complete':
        return 0
    try:
        with lock('weekly-supervisor'):
            if live_process(read_json(STATE/'active.json')):
                return 0
            if STOP.exists() and not manually_stopped(identity):
                STOP.unlink(missing_ok=True)  # Only a marker from an earlier window.
            return supervise(identity)
    except RuntimeError as exc:
        if str(exc).startswith('已有任务占用：'):
            return 0
        raise


def supervise(identity):
    state={'pid':os.getpid(),'created':psutil.Process().create_time(),
           'window':identity['key'],'started':now().isoformat(),'status':'running','attempts':0}
    path=STATE/'window_supervisor.json'
    prior=read_json(path)
    state['login_notified']=prior.get('login_notified',False) if prior.get('window')==identity['key'] else False
    cooldown=float(settings().get('window_retry_sec',180))
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
    try:
        while window_identity() and now()<identity['end']:
            if manually_stopped(identity):
                state.update(status='stopped',reason='用户停止；本窗口不会自动复活')
                atomic_json(path,state)
                return 30
            state.update(status='running',attempts=state['attempts']+1,at=now().isoformat())
            atomic_json(path,state)
            log_path=STATE/('window_attempt_'+now().strftime('%Y%m%d_%H%M%S')+'.log')
            with open(log_path,'w',encoding='utf-8',newline='\r\n',buffering=1) as log:
                child=subprocess.Popen([sys.executable,'-X','utf8',str(Path(C.PROJ)/'workflow.py'),'--run'],
                    cwd=C.PROJ,stdout=log,stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW|subprocess.BELOW_NORMAL_PRIORITY_CLASS)
                child_created=psutil.Process(child.pid).create_time()
                try:
                    job=ProcessJob(child)
                except Exception:
                    child.kill();child.wait(timeout=10)
                    raise
                try:
                    last_tick=0
                    while child.poll() is None:
                        if manually_stopped(identity) or now()>=identity['end']:
                            job.close()
                            break
                        if time.monotonic()-last_tick>=15:
                            last_tick=time.monotonic()
                            state.update(at=now().isoformat(),child_pid=child.pid,log=str(log_path))
                            atomic_json(path,state)
                        time.sleep(1)
                    child.wait(timeout=15)
                finally:
                    job.close()
            report=read_json(STATE/'last_run.json')
            matching=(report.get('pid')==child.pid and abs(report.get('created',0)-child_created)<.1)
            if child.returncode==0 and matching and report.get('exit_code')==0:
                state.update(status='complete',finished=now().isoformat(),inventory=report.get('inventory'))
                atomic_json(path,state)
                return 0
            if manually_stopped(identity):
                state.update(status='stopped',reason='用户停止；本窗口不会自动复活')
                atomic_json(path,state)
                return 30
            if matching and report.get('requires_login') and not state['login_notified']:
                from workflow import notify_message
                state['login_notified']=True
                atomic_json(path,state)
                notify_message('课程采集需要重新登录或验证。请双击工程目录的“提前登录.bat”完成验证；本窗口会继续等待并自动重试，已完成课程保留。')
            state.update(status='waiting_retry',at=now().isoformat(),last_exit_code=child.returncode,
                         reason=report.get('reason','子进程异常退出或存在待补课次') if matching else '子进程未留下完成报告')
            atomic_json(path,state)
            print('本次未完成；窗口内等待%.0f秒再恢复。原因：%s' % (cooldown,state['reason']),flush=True)
            retry_at=time.monotonic()+cooldown
            while time.monotonic()<retry_at and now()<identity['end']:
                if manually_stopped(identity):
                    state.update(status='stopped',reason='用户停止；本窗口不会自动复活')
                    atomic_json(path,state)
                    return 30
                time.sleep(1)
        state.update(status='window_ended',finished=now().isoformat(),reason='窗口结束；未完成部分已保留')
        atomic_json(path,state)
        return 40
    finally:
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)


if __name__=='__main__':
    raise SystemExit(main('--recover' in sys.argv))
