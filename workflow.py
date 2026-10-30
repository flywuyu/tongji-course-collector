"""Single scheduled entry: login -> discover -> audio -> PPT -> lecture -> report."""
from hidden_process import run as run_hidden
from hidden_process import call as call_hidden
import argparse
import atexit
import ctypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import psutil
import config as C
from pipeline_common import (STATE, STOP, atomic_json, atomic_text, audio_done, items,
                             lecture_path, lock, manual_gold, now, read_json, redact,
                             settings, window)
from course_inventory import artifacts, generate, run_report
from process_job import ProcessJob


class PauseRun(RuntimeError):
    def __init__(self, message, code='resource', retryable=True):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def kill_tree(process):
    try:
        children = process.children(recursive=True)
        for child in reversed(children):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        process.kill()
        psutil.wait_procs(children + [process], timeout=5)
    except psutil.NoSuchProcess:
        pass


def temperature():
    try:
        r = run_hidden(['nvidia-smi', '--query-gpu=temperature.gpu', '--format=csv,noheader,nounits'],
                           capture_output=True, encoding='utf-8', timeout=10,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        return max(int(x.strip()) for x in r.stdout.splitlines() if x.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def gpu_free_gb():
    try:
        result=run_hidden(['nvidia-smi','--query-gpu=memory.free','--format=csv,noheader,nounits'],
            capture_output=True,encoding='utf-8',timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
        values=[float(x.strip())*1024*1024/1e9 for x in result.stdout.splitlines() if x.strip()]
        return min(values) if result.returncode==0 and values else None
    except (OSError,ValueError,subprocess.TimeoutExpired):
        return None


class Runner:
    def __init__(self, enforce_window=True, honour_stop=True):
        self.enforce_window = enforce_window
        self.honour_stop = honour_stop
        self.cfg = settings()
        self.events = []
        self.metrics = []
        self.child = None
        self.prefetch = None
        self.deadline = None

    def event(self, message):
        message = redact(message)
        self.events.append({'at': now().isoformat(), 'message': message})
        print(message, flush=True)

    def allowed(self):
        if self.prefetch is not None:
            self.prefetch.poll()
        if self.deadline is not None and now()>=self.deadline:
            raise PauseRun('本次准备时段结束，保留断点', 'window', False)
        if self.honour_stop and STOP.exists():
            raise PauseRun('收到停止请求，保留已完成课次和缓存', 'stop', False)
        if self.enforce_window:
            inside, end = window()
            if not inside or (end - now()).total_seconds() <= 15:
                raise PauseRun('到达本次运行窗口边界，未完成课次进入下一补跑窗口', 'window', False)
        battery = psutil.sensors_battery()
        if self.cfg['require_ac_power'] and battery and not battery.power_plugged:
            raise PauseRun('电脑已断开电源，暂停任务')
        if shutil.disk_usage(C.DATA).free / 1e9 < self.cfg['min_free_disk_gb']:
            raise PauseRun('磁盘空间不足，暂停任务')
        if psutil.virtual_memory().available / 1e9 < self.cfg['min_available_ram_gb']:
            raise PauseRun('可用内存不足，暂停并释放本次任务资源')

    def command(self, script, args=(), gpu=False, timeout=7200, _decoder_retries=None):
        self.allowed()
        if gpu:
            temp = temperature()
            if temp is None:
                raise PauseRun('无法读取GPU温度，本次不启动转写')
            while temp >= self.cfg['gpu_resume_c']:
                self.event('GPU降温等待：%d°C' % temp)
                for _ in range(15):
                    self.allowed()
                    time.sleep(2)
                temp = temperature()
                if temp is None:
                    raise PauseRun('GPU温度读取失败')
            free=gpu_free_gb()
            required=float(self.cfg.get('asr_min_gpu_free_gb',4.5))
            if free is None:
                raise PauseRun('无法读取独立显存余量，稍后再尝试；不使用共享内存强行加载')
            while free<required:
                self.event('等待独立显存：当前%.2fGB，至少需要%.2fGB；不会关闭其他软件' % (free,required))
                for _ in range(15):
                    self.allowed();time.sleep(2)
                free=gpu_free_gb()
                if free is None:
                    raise PauseRun('独立显存读取失败，稍后恢复')
        STATE.mkdir(exist_ok=True)
        cmd = [sys.executable, '-X', 'utf8', str(Path(C.PROJ) / script), *map(str, args)]
        stage_log = STATE / 'stage.log'
        env = dict(os.environ, PYTHONUTF8='1', OMP_NUM_THREADS='2', MKL_NUM_THREADS='2',
                   OPENBLAS_NUM_THREADS='2', NUMEXPR_NUM_THREADS='2')
        started = time.monotonic()
        hot_since, last_sensor = None, 0
        peak_ram, peak_temp = 0, None
        min_available = psutil.virtual_memory().available / 1e9
        step_name = now().strftime('%Y%m%d_%H%M%S_%f') + '_' + Path(script).stem
        step_log = STATE / 'steps' / (step_name + '.log')
        step_log.parent.mkdir(exist_ok=True)
        with open(stage_log, 'w', encoding='utf-8', newline='\r\n') as log:
            child = subprocess.Popen(cmd, cwd=C.PROJ, env=env, stdout=log, stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS)
            self.child = psutil.Process(child.pid)
            try:
                job = ProcessJob(child)
            except Exception:
                kill_tree(self.child)
                raise
            atomic_json(STATE / 'active_child.json', {'pid': child.pid,
                        'created': self.child.create_time(), 'script': script, 'at': now().isoformat()})
            try:
                while child.poll() is None:
                    min_available = min(min_available, psutil.virtual_memory().available / 1e9)
                    self.allowed()
                    if time.monotonic() - started > timeout:
                        raise RuntimeError('步骤超时：' + script)
                    if time.monotonic() - last_sensor > 10:
                        last_sensor = time.monotonic()
                        try:
                            processes = [self.child] + self.child.children(recursive=True)
                            ram = sum(p.memory_info().rss for p in processes if p.is_running()) / 1e9
                            if self.prefetch is not None:
                                ram += self.prefetch.memory_gb()
                        except psutil.NoSuchProcess:
                            ram = 0
                        peak_ram = max(peak_ram, ram)
                        if ram > self.cfg['max_worker_ram_gb']:
                            raise PauseRun('课次进程内存超限，已终止并释放资源，下次续跑')
                        if gpu:
                            temp = temperature()
                            if temp is None:
                                raise PauseRun('转写期间GPU温度读取失败')
                            peak_temp = max(peak_temp or temp, temp)
                            if temp >= self.cfg['gpu_stop_c']:
                                hot_since = hot_since or time.monotonic()
                                if time.monotonic() - hot_since >= 20:
                                    raise PauseRun('GPU持续高温（%d°C），暂停本次任务' % temp)
                            else:
                                hot_since = None
                        with open(STATE/'resource_history.jsonl','a',encoding='utf-8') as samples:
                            samples.write(json.dumps({'at':now().isoformat(),'script':script,'pid':child.pid,
                                'available_ram_gb':round(min_available,2),'worker_rss_gb':round(ram,2),
                                'gpu_c':peak_temp},ensure_ascii=False)+'\n')
                    time.sleep(1)
            finally:
                if child.poll() is None:
                    kill_tree(self.child)
                job.close()
                child.wait(timeout=15)
                metric_args=list(map(str,args))
                metric_subid=next((value for flag,value in zip(metric_args,metric_args[1:]) if flag=='--sub-id'),'')
                self.metrics.append({'script': script,'sub_id':metric_subid, 'elapsed_sec': round(time.monotonic()-started,1),
                                     'peak_ram_gb': round(peak_ram,2), 'peak_gpu_c': peak_temp,
                                     'min_available_ram_gb':round(min_available,2),'log':str(step_log),
                                     'exit_code': child.returncode})
                self.child = None
                (STATE / 'active_child.json').unlink(missing_ok=True)
                log.flush()
                shutil.copyfile(stage_log, step_log)
        tail = stage_log.read_text(encoding='utf-8')[-1500:]
        from asr_guard import RETRY_CHUNK, DECODER_EXHAUSTED
        if script == 'transcribe_one.py' and gpu:
            if child.returncode == RETRY_CHUNK:
                retries = set(_decoder_retries or ())
                try:
                    it = next(it for it in items() if str(it['sub_id']) == metric_subid)
                    progress = read_json(lecture_path(it) / 'audio' / 'asr_progress.json')
                    failed = progress['timeouts'][-1]['chunk']
                    valid = (str(progress['binding']['sub_id']) == metric_subid and
                             sum(x['chunk'] == failed for x in progress['timeouts']) == 1 and
                             failed not in retries)
                except (OSError, ValueError, KeyError, IndexError, StopIteration, TypeError):
                    valid = False
                if valid:
                    retries.add(failed)
                    self.event('单块解码超时：课次进程已退出；复用已完成片段，仅用贪心重试超时块一次')
                    return self.command(script, args, gpu=True, timeout=timeout, _decoder_retries=retries)
            if child.returncode in (RETRY_CHUNK, DECODER_EXHAUSTED):
                self.event('解码重试仍未完成，保留片段与音频并列入待处理：' + tail)
                return DECODER_EXHAUSTED, tail
        if child.returncode:
            if child.returncode == 30:
                raise PauseRun('转写已按夜间窗口停止，保留缓存')
            self.event('步骤失败：%s（退出码%d） %s' % (script, child.returncode, tail))
        return child.returncode, tail


def state(it, status, error=''):
    path = STATE / 'lecture_states.json'
    states = read_json(path)
    states[str(it['sub_id'])] = {'status': status, 'error': redact(error), 'at': now().isoformat()}
    atomic_json(path, states)


def run(resume=False):
    C.require_initialized()
    inside, _ = window()
    if not inside:
        generate()
        print('当前不在运行窗口：周五00:00–08:00主运行、周六00:00–08:00补跑；已更新清单，没有启动采集')
        return 30
    with lock():
        import runlock
        if runlock.peek()[0]:
            print('旧转写仍在运行，整合任务不重复占用GPU')
            return 31
        if STOP.exists():
            from weekly_supervisor import window_identity
            identity=window_identity()
            if identity is None or STOP.stat().st_mtime>=identity['start'].timestamp():
                print('本窗口已有停止请求，不在子进程启动时清除；下个窗口再恢复')
                return 30
            STOP.unlink(missing_ok=True)
        atomic_json(STATE / 'active.json', {'pid': os.getpid(), 'created': psutil.Process().create_time(),
                                          'started': now().isoformat()})
        runner = Runner()
        before = generate()
        result = {'started': now().isoformat(), 'kind': '自动更新', 'result': '完成',
                  'events': runner.events, 'resource_samples': runner.metrics, 'pid':os.getpid(),
                  'created':psutil.Process().create_time()}
        # Keep the system awake only while a permitted batch is active; let the display sleep.
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
        try:
            if not resume:
                rc, _ = runner.command('platform_session.py', ['discover'], timeout=1200)
                if rc:
                    result['requires_login']=rc==20
                    result['result']='平台发现失败，尚不能确认本周新课是否齐全'
                    result['reason'] = '课程发现失败，保留旧索引；已缓存课次继续处理，详情见步骤日志'
                else:
                    old_ids = {x['sub_id'] for x in before['items']}
                    result['discovery'] = {'new_count': sum(str(x['sub_id']) not in old_ids for x in items())}
            targets = items()
            from audio_prefetch import AudioPrefetch
            runner.prefetch = AudioPrefetch(runner)
            for i, it in enumerate(targets, 1):
                runner.allowed()
                tag = '%d/%d W%02d %s %s' % (i, len(targets), __import__('naming').week_of(it['date']),
                                           it['date'], it['title'])
                if artifacts(it, deep=True)['ready'] and audio_done(it):
                    state(it, 'verified')
                    continue
                runner.event('处理 ' + tag)
                state(it, 'running')
                try:
                    from pipeline_common import subtitle_status
                    if subtitle_status(it).startswith('待核对'):
                        raise RuntimeError('字幕存在外部修订或格式变化，已保留原件，不自动重录覆盖；详见验收报告')
                    if not it.get('playurl'):
                        raise RuntimeError('平台回放地址尚未就绪')
                    if not audio_done(it):
                        while runlock.peek()[0]:
                            runner.allowed()
                            time.sleep(10)
                        if audio_done(it):
                            runner.event('并行字幕队列已经完成本课，复用成果：' + tag)
                        else:
                            runner.prefetch.claim(it['sub_id'])
                            # Launch before current ASR so its GPU work overlaps next download.
                            runner.prefetch.start_next(targets[i:])
                            rc, _ = runner.command('transcribe_one.py', ['--sub-id', it['sub_id']], gpu=True)
                            if rc == 21:
                                runner.event('媒体源暂时不可用，刷新平台地址后重试当前课：'+tag)
                                runner.prefetch.close()
                                refresh_rc,_=runner.command('platform_session.py',['discover'],timeout=180)
                                if refresh_rc:
                                    result['requires_login']=refresh_rc==20
                                    raise PauseRun('回放地址刷新未成功，窗口内稍后恢复', 'media')
                                fresh={str(row['sub_id']):row for row in items()}
                                for target in targets:
                                    target.update(fresh.get(str(target['sub_id']),{}))
                                runner.prefetch.start_next(targets[i:])
                                rc,_=runner.command('transcribe_one.py',['--sub-id',it['sub_id']],gpu=True)
                            if rc:
                                raise RuntimeError('片段解码两次超时，已保留断点，需核对解码记录' if rc == 43 else
                                                   '字幕未完成，保留音频等待下次重试')
                    if manual_gold(it):
                        rc, _ = runner.command('manual_merge.py', ['--sub-id', it['sub_id']], timeout=120)
                        if rc:
                            raise RuntimeError('手工讲义自动更新版尚未合成')
                        qa = artifacts(it, deep=True)
                        if not qa['ready']:
                            raise RuntimeError('手工手工讲义验收待处理：' + '；'.join(qa['issues']))
                        state(it, 'manual_preserved')
                        runner.event('字幕已归档，保留历史手工讲义：' + tag)
                        continue
                    root = lecture_path(it)
                    from ppt_download import complete_download
                    if not complete_download(str(root), it):
                        probes = {str(x['sub_id']): x for x in read_json(Path(C.PROJ) / 'ppt_清单.json').get('items', [])}
                        row = probes.get(str(it['sub_id']))
                        if row is None or row.get('status') in ('probe_error', 'login_required', 'blocked'):
                            rc, _ = runner.command('platform_session.py', ['probe', '--sub-id', it['sub_id']], timeout=180)
                            if rc:
                                if rc==20:result['requires_login']=True
                                raise RuntimeError('PPT探测失败，不能误判成没有UUID')
                            row = next(x for x in read_json(Path(C.PROJ) / 'ppt_清单.json')['items']
                                       if str(x['sub_id']) == str(it['sub_id']))
                        route = 'ppt_download.py' if row.get('ppt_base') and not row.get('download_unavailable') else 'ppt_extract.py'
                        args = ['--date', it['date'], '--title', it['title'], '--sub-id', it['sub_id']]
                        rc, _ = runner.command(route, args)
                        if rc == 6:
                            raise PauseRun('平台限流或下载异常，停止本批次，保留已完成内容')
                        if rc == 4 and route == 'ppt_download.py':
                            runner.event('平台有UUID但没有可用图片，自动使用视频画面：'+tag)
                            probe_path=Path(C.PROJ)/'ppt_清单.json'
                            probe_data=read_json(probe_path)
                            for record in probe_data.get('items',[]):
                                if str(record['sub_id'])==str(it['sub_id']):
                                    record.update(download_unavailable=True,download_checked_at=now().isoformat())
                            atomic_json(probe_path,probe_data)
                            rc,_=runner.command('ppt_extract.py',args)
                            if rc == 6:
                                raise PauseRun('视频源拒绝访问，停止批次并保留进度')
                        if rc:
                            raise RuntimeError('画面采集未完成')
                    rc, _ = runner.command('ppt_build.py', ['--date', it['date'], '--title', it['title'],
                            '--sub-id', it['sub_id'], '--ocr-workers', settings()['ocr_workers']])
                    if rc:
                        raise RuntimeError('OCR或讲义生成失败')
                    qa = artifacts(it, deep=True)
                    if not qa['ready']:
                        raise RuntimeError('成品验收未通过：' + '；'.join(qa['issues']))
                    state(it, 'verified')
                    runner.event('已验收 ' + tag)
                except PauseRun:
                    state(it, 'pending', '窗口结束或资源保护暂停，下次续跑')
                    raise
                except Exception as exc:
                    state(it, 'failed', str(exc))
                    runner.event('待重试 ' + tag + '：' + str(exc))
                    if '成品验收未通过' in str(exc):
                        manifest_path=lecture_path(it)/'ppt清单.json'
                        manifest=read_json(manifest_path)
                        if manifest and any(word in str(exc) for word in ('无法校验','原图已变化','数量与清单')):
                            manifest['partial']=True
                            atomic_json(manifest_path,manifest)
                inventory=generate()
        except PauseRun as exc:
            result.update(result='暂停并保留进度', reason=str(exc), pause_code=exc.code, retryable=exc.retryable)
            runner.event(str(exc))
        except Exception as exc:
            result.update(result='发生异常，已保留进度', reason=redact(exc))
        finally:
            if runner.prefetch is not None:
                runner.prefetch.close()
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            after = generate(deep=True)
            if result['result'] == '完成' and after['ready'] < after['total']:
                result['result'] = '批次结束，仍有待处理课次'
            result['finished'] = now().isoformat()
            result['inventory'] = {'ready': after['ready'], 'total': after['total']}
            result['exit_code'] = (0 if result['result']=='完成' else
                                  30 if result.get('retryable') is False else 40)
            atomic_json(STATE/'last_run.json', result)
            report = run_report(result, before, after)
            print('已写报告：' + report, flush=True)
            (STATE / 'active.json').unlink(missing_ok=True)
        return result['exit_code']


def preflight():
    with lock():
        before = generate()
        runner = Runner(enforce_window=False, honour_stop=False)
        run_data = {'started': now().isoformat(), 'kind': '登录预检', 'events': runner.events}
        rc=40
        try:
            rc, _ = runner.command('platform_session.py', ['check'], timeout=180)
            if rc==0:
                rc,_=runner.command('healthcheck.py',timeout=150)
            run_data['result'] = ('登录可用，今晚00:00–08:00自动更新，无需额外操作' if rc == 0 else
                                  '需要提前登录' if rc == 20 else '运行预检存在待恢复条件，夜间自动复查；详情见报告')
            if rc == 20:
                notify_login()
            elif rc == 0:
                notify_message('课程采集登录预检通过。今晚00:00–08:00自动整理课程，无需额外操作。请保持开机、接电和联网，锁屏可以；结果见课程资料中的运行报告。')
            else:
                notify_message('课程采集运行预检仍有待恢复条件，程序会在今晚复查资源和回放，详情已写入课程资料中的运行报告。')
        except Exception as exc:
            run_data.update(result='预检未通过', reason=redact(exc))
            notify_message('课程采集预检暂未通过，详情已写入课程资料中的运行报告。')
        atomic_json(STATE / 'preflight.json', run_data)
        print(run_report(run_data, before, generate()))
        return 0 if run_data['result'].startswith('登录可用') else 20 if rc==20 else 40


def notify_login():
    notify_message('同济课程采集需要登录。请在周四23:30前双击工程目录中的“提前登录.bat”，完成验证码或二次认证。')


def notify_message(message):
    # Notify only when a user action is needed, never on every successful check.
    try:
        run_hidden([sys.executable, '-X', 'utf8', str(Path(C.PROJ)/'notify_user.py'), message],
                        timeout=125, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        atomic_text(STATE/'notification_error.log', now().isoformat()+' '+redact(exc)+'\n')


def stop():
    STATE.mkdir(exist_ok=True)
    STOP.touch()
    active = read_json(STATE / 'active_child.json')
    if active.get('pid'):
        try:
            process = psutil.Process(active['pid'])
            if abs(process.create_time() - active['created']) < .1:
                kill_tree(process)
        except psutil.NoSuchProcess:
            pass
    import runlock
    busy, holder = runlock.peek()
    if busy:
        try:
            process = psutil.Process(holder['pid'])
            scripts = {'transcribe_one.py', '_one_off_transcribe.py', 'collect_v2.py', 'update.py'}
            if any(Path(arg).name in scripts and Path(arg).resolve().parent == Path(C.PROJ).resolve()
                   for arg in process.cmdline()):
                kill_tree(process)
        except psutil.NoSuchProcess:
            pass
    print('已停止本系统的任务；已完成课次保留')


def main():
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--run', action='store_true')
    modes.add_argument('--resume', action='store_true')
    modes.add_argument('--preflight', action='store_true')
    modes.add_argument('--login', action='store_true')
    modes.add_argument('--stop', action='store_true')
    modes.add_argument('--report', action='store_true')
    args = parser.parse_args()
    if args.login:
        return call_hidden([sys.executable, '-X', 'utf8', str(Path(C.PROJ) / 'platform_session.py'), 'login'])
    if args.stop:
        stop()
        return 0
    if args.preflight:
        return preflight()
    if args.run or args.resume:
        return run(args.resume)
    payload = generate(deep=args.report)
    print('完整讲义已验收 %d / %d；周五00:00–08:00主运行，周六00:00–08:00补跑' % (payload['ready'], payload['total']))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(redact(exc), flush=True)
        raise SystemExit(1)
