"""One bounded, CPU-only audio download owned by the workflow parent."""
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil
import config as C
from pipeline_common import STATE, atomic_json, audio_done, lecture_path, now, subtitle_status
from process_job import ProcessJob


class AudioPrefetch:
    def __init__(self, runner):
        self.runner = runner
        self.child = self.job = self.log = None
        self.sub_id = None
        self.started = 0
        self.state_path = STATE / 'active_prefetch.json'
        self.timeout = float(runner.cfg.get('audio_prefetch_timeout_sec', 900))
        if not 0 < self.timeout <= 3600:
            raise ValueError('音频预取超时必须在0到3600秒内')

    def _event(self, text):
        self.runner.event(text)

    def _finish(self, reason=None):
        child = self.child
        if child is None:
            if self.log:
                self.log.close()
                self.log = None
            return
        try:
            try:
                if self.job:
                    self.job.close()  # Also kills a blocked probe or ffmpeg descendant.
            finally:
                if child.poll() is None:
                    try:
                        proc = psutil.Process(child.pid)
                        for descendant in reversed(proc.children(recursive=True)):
                            try:
                                descendant.kill()
                            except psutil.NoSuchProcess:
                                pass
                    except psutil.NoSuchProcess:
                        pass
                    if child.poll() is None:
                        try:
                            child.kill()
                        except OSError:
                            if child.poll() is None:
                                raise
            child.wait(timeout=15)
            self._event(('音频预取已结束：' + str(reason) if reason else
                        '音频预取已核验，可供转写复用' if child.returncode == 0 else
                        '音频预取未完成，轮到本课时正常下载') + '（课次%s）' % self.sub_id)
        finally:
            try:
                if self.log:
                    self.log.close()
            finally:
                self.child = self.job = self.log = None
                self.sub_id = None
                self.state_path.unlink(missing_ok=True)

    def poll(self):
        if self.child is not None:
            try:
                if self.child.poll() is not None:
                    self._finish()
                elif time.monotonic() - self.started >= self.timeout:
                    self._finish('预取超时；主流程继续，后续正常下载')
            except (OSError, subprocess.SubprocessError, psutil.Error) as exc:
                self._event('预取回收异常，不影响本课转写：' + type(exc).__name__)

    def claim(self, sub_id):
        """Foreground owns this lecture next; never race two writers of lecture.wav.tmp."""
        self.poll()
        if self.child is not None and self.sub_id == str(sub_id):
            try:
                self._finish('本课接管下载；未完成的预取已停止')
            except (OSError, subprocess.SubprocessError, psutil.Error) as exc:
                self._event('预取交接异常，转写使用独占下载锁核验：' + type(exc).__name__)

    def start_next(self, targets):
        self.poll()
        if self.child is not None:
            return
        try:
            for it in targets:
                if audio_done(it) or subtitle_status(it).startswith('待核对') or not it.get('playurl'):
                    continue
                # Only the next ASR lesson. An existing WAV is left to its normal validator;
                # do not run ahead and accumulate multiple extra complete audio files.
                if (lecture_path(it) / 'audio' / 'lecture.wav').exists():
                    return
                self.runner.allowed()
                STATE.mkdir(exist_ok=True)
                self.sub_id = str(it['sub_id'])
                self.log = open(STATE / ('prefetch_%s.log' % self.sub_id),
                                'w', encoding='utf-8', newline='\r\n')
                self.child = subprocess.Popen([sys.executable, '-X', 'utf8',
                    str(Path(C.PROJ) / 'transcribe_one.py'), '--sub-id', self.sub_id, '--download-only'],
                    cwd=C.PROJ, env=dict(os.environ, PYTHONUTF8='1', OMP_NUM_THREADS='2'),
                    stdout=self.log, stderr=self.log,
                    creationflags=subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS)
                self.started = time.monotonic()
                try:
                    self.job = ProcessJob(self.child)
                except Exception:
                    # Fail closed if process-tree supervision could not be established.
                    self.child.kill()
                    self.child.wait(timeout=10)
                    raise
                atomic_json(self.state_path, {'pid': self.child.pid,
                    'created': psutil.Process(self.child.pid).create_time(), 'sub_id': self.sub_id,
                    'started': now().isoformat(), 'timeout_sec': self.timeout})
                self._event('后台预取下一节音频（课次%s，深度1；不加载模型）' % self.sub_id)
                return
        except Exception as exc:
            # Prefetch is an optimization. A failed start may never fail the foreground lesson.
            self._event('音频预取未启动，主流程照常：' + type(exc).__name__)
            self.close()

    def memory_gb(self):
        if self.child is None or self.child.poll() is not None:
            return 0
        try:
            proc = psutil.Process(self.child.pid)
            return sum(p.memory_info().rss for p in [proc, *proc.children(recursive=True)]) / 1e9
        except psutil.Error:
            return 0

    def close(self):
        try:
            self._finish('批次退出，保留已核验音频；取消未完成预取')
        except (OSError, subprocess.SubprocessError, psutil.Error) as exc:
            self._event('预取关闭异常：' + type(exc).__name__)
