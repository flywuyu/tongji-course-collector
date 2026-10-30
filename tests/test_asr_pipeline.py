"""Prefetch and hard decoder deadlines. Runs real workers without loading a GPU model."""
import contextlib
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import psutil

import config as C
import pipeline_common as P
from asr_guard import (ChunkProgress, DecoderExhausted, chunk_deadline,
                       decode_options, decoder_policy)


PROJECT = str(Path(__file__).resolve().parents[1])


class ASRPipelineTests(unittest.TestCase):
    def setUp(self):
        # Synthetic fixture rules; public defaults never assume any course is silent.
        self.rules=mock.patch.object(C,'SKIP_TITLES',{'马克思主义基本原理'})
        self.term=mock.patch.object(C,'TERM_START','2026-09-14')
        self.rules.start();self.term.start()
        self.addCleanup(self.rules.stop);self.addCleanup(self.term.stop)

    def test_policy_is_real_greedy_without_hidden_temperature_sampling(self):
        policy = decoder_policy({})
        self.assertEqual(policy['beam_size'], 1)
        self.assertIsNone(decode_options('zh', 1)['beam_size'])
        self.assertEqual(decode_options(None, 5)['beam_size'], 5)
        self.assertEqual(decode_options('zh', 1)['temperature'], 0.0)
        self.assertIsNone(decode_options('zh', 1)['best_of'])
        self.assertEqual(chunk_deadline(180, policy), 1440)
        self.assertEqual(chunk_deadline(900, policy), 1800)
        self.assertEqual(chunk_deadline(1, policy), 60)
        with self.assertRaises(ValueError):
            decoder_policy({'asr_chunk_timeout_factor': float('inf')})

    def test_checkpoints_bound_to_audio_and_limit_only_the_failed_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'progress.json'
            progress = ChunkProgress(path, {'sha': 'first', 'ranges': [[0, 1], [1, 2]]})
            progress.completed(0, 0, 1, [{'text': '保留'}], [], 5, 1, 2)
            self.assertEqual(progress.timed_out(1, 1, 2, 5, 8), 42)
            resumed = ChunkProgress(path, progress.data['binding'])
            self.assertEqual(resumed.record(0)['segments'][0]['text'], '保留')
            self.assertEqual(resumed.beam_for(0, 5), 5)
            self.assertEqual(resumed.beam_for(1, 5), 1)
            self.assertEqual(resumed.timed_out(1, 1, 2, 1, 8), 43)
            with self.assertRaises(DecoderExhausted):
                resumed.beam_for(1, 5)
            replaced = ChunkProgress(path, {'sha': 'different'})
            self.assertIsNone(replaced.record(0))
            self.assertEqual(replaced.beam_for(1, 5), 5)

    def test_download_only_uses_real_ffmpeg_and_never_imports_gpu(self):
        # A local media file exercises the production probe/downloader/atomic promotion.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.wav'
            subprocess.run([C.FF, '-y', '-v', 'error', '-f', 'lavfi', '-i',
                'sine=frequency=440:duration=7:sample_rate=16000', str(source)],
                check=True, capture_output=True)
            class QuietHandler(SimpleHTTPRequestHandler):
                def log_message(self, *args):
                    pass
            server = ThreadingHTTPServer(('127.0.0.1', 0),
                functools.partial(QuietHandler, directory=tmp))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            it = {'sub_id': 'test', 'title': '测试课', 'date': '2026-10-21',
                  'course_id': '1', 'duration': 7,
                  'playurl': 'http://127.0.0.1:%d/source.wav' % server.server_port}
            code = f'''
import builtins,sys
sys.path.insert(0,{PROJECT!r})
from pathlib import Path
import config as C
import pipeline_common as P
C.DATA={str(root/'data')!r}
C.CAT={str(root/'catalog.json')!r}
P.STATE=Path({str(root/'state')!r})
P.atomic_json(C.CAT,{{'items':[{it!r}]}})
import transcribe_one as T
T.window=lambda:(True,None)
real_import=builtins.__import__
def no_gpu(name,*args,**kwargs):
    if name.split('.')[0] in ('torch','whisper'):
        raise AssertionError('download-only imported GPU library')
    return real_import(name,*args,**kwargs)
builtins.__import__=no_gpu
sys.argv=['transcribe_one.py','--sub-id','test','--download-only']
assert T.main()==0
assert (P.lecture_path({it!r})/'audio'/'lecture.wav').exists()
assert 'torch' not in sys.modules and 'whisper' not in sys.modules
'''
            try:
                child = subprocess.run([sys.executable, '-X', 'utf8', '-c', code],
                                       capture_output=True, encoding='utf-8', timeout=20)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertFalse(list(root.rglob('*.tmp')))

    def _prefetch_fixture(self, tmp, stack):
        import audio_prefetch as A
        import workflow as W
        state = Path(tmp) / 'state'
        state.mkdir()
        stack.enter_context(mock.patch.object(A, 'STATE', state))
        stack.enter_context(mock.patch.object(C, 'DATA', str(Path(tmp) / 'data')))
        runner = W.Runner(enforce_window=False)
        stack.enter_context(mock.patch.object(runner, 'allowed'))
        prefetch = A.AudioPrefetch(runner)
        self.addCleanup(prefetch.close)
        return A, runner, prefetch

    def test_prefetch_depth_one_skips_silent_done_and_existing_audio(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            A, runner, prefetch = self._prefetch_fixture(tmp, stack)
            base = {'course_id': '1', 'date': '2026-10-21', 'playurl': 'local', 'duration': 1}
            mute = dict(base, sub_id='m', title='马克思主义基本原理')
            done = dict(base, sub_id='d', title='已完成课')
            next_it = dict(base, sub_id='n', title='下一课')
            later = dict(base, sub_id='z', title='再下一课')
            from transcribe_one import commit_subtitles
            commit_subtitles(done, P.lecture_path(done)/'transcript',
                             [{'start':0, 'end':1, 'text':'已完成'}], {})
            child = mock.Mock(pid=123, returncode=0)
            child.poll.return_value = None
            stack.enter_context(mock.patch.object(A.subprocess, 'Popen', return_value=child))
            spawn = A.subprocess.Popen
            job = stack.enter_context(mock.patch.object(A, 'ProcessJob'))
            stack.enter_context(mock.patch.object(A.psutil, 'Process')).return_value.create_time.return_value = 1
            prefetch.start_next([mute, done, next_it, later])
            prefetch.start_next([later])
            self.assertEqual(spawn.call_count, 1)
            self.assertIn('--download-only', spawn.call_args.args[0])
            self.assertEqual(prefetch.sub_id, 'n')
            child.poll.return_value = 0
            prefetch.poll()
            self.assertIsNone(prefetch.child)
            job.return_value.close.assert_called()
            wav = P.lecture_path(next_it)/'audio'/'lecture.wav'
            wav.parent.mkdir(parents=True)
            wav.touch()
            prefetch.start_next([next_it, later])
            self.assertEqual(spawn.call_count, 1)

    def test_prefetch_failure_and_timeout_do_not_fail_foreground(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            A, runner, prefetch = self._prefetch_fixture(tmp, stack)
            it = {'sub_id':'n', 'course_id':'1', 'title':'下一课', 'date':'2026-10-21', 'playurl':'local'}
            spawn = stack.enter_context(mock.patch.object(A.subprocess, 'Popen', side_effect=OSError('test')))
            prefetch.start_next([it])
            self.assertIsNone(prefetch.child)
            self.assertIsNone(prefetch.log)
            child = mock.Mock(pid=123, returncode=1)
            child.poll.return_value = None
            spawn.side_effect = None
            spawn.return_value = child
            job = stack.enter_context(mock.patch.object(A, 'ProcessJob'))
            stack.enter_context(mock.patch.object(A.psutil, 'Process')).return_value.create_time.return_value = 1
            prefetch.start_next([it])
            child.poll.return_value = 1
            prefetch.poll()
            self.assertIsNone(prefetch.child)
            prefetch.start_next([it])
            child.poll.return_value = None
            prefetch.started = time.monotonic() - prefetch.timeout - 1
            prefetch.poll()
            job.return_value.close.assert_called()
            child.wait.assert_called()
            self.assertIsNone(prefetch.child)
            self.assertFalse(prefetch.state_path.exists())

    def test_prefetch_handoff_cancels_writer_before_foreground_download(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            A, runner, prefetch = self._prefetch_fixture(tmp, stack)
            it = {'sub_id':'n', 'title':'下一课', 'date':'2026-10-21', 'playurl':'local'}
            child = mock.Mock(pid=123, returncode=1)
            child.poll.return_value = None
            stack.enter_context(mock.patch.object(A.subprocess, 'Popen', return_value=child))
            job = stack.enter_context(mock.patch.object(A, 'ProcessJob'))
            stack.enter_context(mock.patch.object(A.psutil, 'Process')).return_value.create_time.return_value = 1
            prefetch.start_next([it])
            prefetch.claim('n')
            job.return_value.close.assert_called()
            self.assertIsNone(prefetch.child)

    def test_real_prefetch_cancellation_kills_its_entire_process_tree(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            A, runner, prefetch = self._prefetch_fixture(tmp, stack)
            project = Path(tmp)
            stack.enter_context(mock.patch.object(C, 'PROJ', str(project)))
            marker = project/'grandchild.txt'
            P.atomic_text(project/'transcribe_one.py',
                'import subprocess,sys,time,pathlib\n'
                'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"])\n'
                'pathlib.Path(%r).write_text(str(p.pid))\n'
                'time.sleep(60)\n' % str(marker))
            it = {'sub_id':'n', 'title':'下一课', 'date':'2026-10-21', 'playurl':'local'}
            prefetch.start_next([it])
            child = prefetch.child
            self.assertIsNotNone(child)
            try:
                deadline = time.monotonic()+5
                while not marker.exists() and time.monotonic()<deadline:
                    time.sleep(.02)
                self.assertTrue(marker.exists())
                descendant = psutil.Process(int(marker.read_text()))
                prefetch.claim('n')
                self.assertIsNotNone(child.poll())
                psutil.wait_procs([descendant], timeout=5)
                self.assertFalse(descendant.is_running())
                self.assertFalse(prefetch.state_path.exists())
            finally:
                prefetch.close()

    def test_parent_starts_next_download_before_current_asr(self):
        import audio_prefetch as A
        import course_inventory
        import ppt_download
        import runlock
        import workflow as W
        from transcribe_one import commit_subtitles
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            project = Path(tmp)
            state = project/'state';state.mkdir()
            data = [{'sub_id':str(n), 'course_id':str(n), 'date':'2026-10-21',
                     'title':'课%d' % n, 'playurl':'local'} for n in (1,2)]
            for obj, key, value in [(C,'PROJ',str(project)),(C,'DATA',str(project/'data')),
                (C,'CAT',str(project/'catalog.json')),(P,'STATE',state),(W,'STATE',state),
                (A,'STATE',state),(W,'STOP',state/'stop')]:
                stack.enter_context(mock.patch.object(obj,key,value))
            P.atomic_json(C.CAT,{'items':data})
            child=mock.Mock(pid=123, returncode=0);child.poll.return_value=None
            spawn=stack.enter_context(mock.patch.object(A.subprocess,'Popen',return_value=child))
            stack.enter_context(mock.patch.object(A,'ProcessJob'))
            stack.enter_context(mock.patch.object(A.psutil,'Process')).return_value.create_time.return_value=1
            stack.enter_context(mock.patch.object(W,'window',return_value=(True,None)))
            stack.enter_context(mock.patch.object(W.Runner,'allowed'))
            stack.enter_context(mock.patch.object(runlock,'peek',return_value=(False,{})))
            stack.enter_context(mock.patch.object(W,'generate',side_effect=lambda **kw:{'items':[],'ready':len(built),'total':2}))
            stack.enter_context(mock.patch.object(W,'run_report',return_value='test-report'))
            stack.enter_context(mock.patch.object(W,'manual_gold',return_value=False))
            stack.enter_context(mock.patch.object(ppt_download,'complete_download',return_value=True))
            built=set()
            stack.enter_context(mock.patch.object(W,'artifacts',side_effect=lambda it,**kw:
                {'ready':it['sub_id'] in built,'issues':[]}))
            calls=[]
            def command(runner,script,args=(),**kw):
                subid=str(args[list(args).index('--sub-id')+1]);calls.append((script,subid))
                if script=='transcribe_one.py':
                    self.assertEqual(spawn.call_count,1)
                    self.assertIn('2',spawn.call_args.args[0])
                    self.assertIn('--download-only',spawn.call_args.args[0])
                    it=next(x for x in data if x['sub_id']==subid)
                    commit_subtitles(it,P.lecture_path(it)/'transcript',
                        [{'start':0,'end':1,'text':'字幕'}],{})
                    child.poll.return_value=0
                else:
                    self.assertEqual(script,'ppt_build.py');built.add(subid)
                return 0,''
            stack.enter_context(mock.patch.object(W.Runner,'command',command))
            self.assertEqual(W.run(resume=True),0,P.read_json(state/'last_run.json'))
            self.assertEqual(calls,[('transcribe_one.py','1'),('ppt_build.py','1'),
                                    ('transcribe_one.py','2'),('ppt_build.py','2')])

    def test_real_worker_timeout_restart_reuses_chunks_and_commits_schema2(self):
        # Actual production transcribe() + Runner restart, fake model only. No CUDA imports.
        import workflow as W
        for fail_twice in (False, True):
            with self.subTest(fail_twice=fail_twice), tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp)
                data = project/'data'
                state = project/'state'
                state.mkdir()
                wav = project/'source.wav'
                subprocess.run([C.FF, '-y', '-v', 'error', '-f', 'lavfi', '-i',
                    'sine=frequency=440:duration=3:sample_rate=16000', str(wav)],
                    check=True, capture_output=True)
                it = {'sub_id':'n', 'course_id':'1', 'title':'测试课', 'date':'2026-10-21', 'playurl':'local'}
                P.atomic_json(project/'catalog.json', {'items':[it]})
                P.atomic_json(project/'settings.json', {'course_rules':{}, 'min_available_ram_gb':0,
                    'asr_beam_size':5, 'asr_chunk_timeout_factor':1,
                    'asr_chunk_timeout_min_sec':0.3, 'asr_chunk_timeout_max_sec':0.3})
                script = f'''
import sys,types,time,json
from pathlib import Path
sys.path.insert(0,{PROJECT!r})
import config as C
C.TERM_START='2026-09-14'
C.PROJ={str(project)!r};C.DATA={str(data)!r};C.CAT={str(project/'catalog.json')!r};C.LOCK={str(project/'gpu.lock')!r}
import pipeline_common as P
P.STATE=Path({str(state)!r});P.SETTINGS=Path({str(project/'settings.json')!r});P.window=lambda:(True,None)
import transcribe_one as T
T.window=P.window
import workflow
workflow.gpu_free_gb=lambda:8
root=P.lecture_path({it!r});audio=root/'audio';audio.mkdir(parents=True,exist_ok=True)
target=audio/'lecture.wav'
if not target.exists():
    import shutil
    shutil.copy2({str(wav)!r},target)
T.ensure_audio=lambda it:target
T.media_probe=lambda src:(3, [{{'codec_type':'audio'}}])
T.analyze_audio=lambda *args:([], -10)
T.speech_ranges=lambda *args:[[0,1],[1,2],[2,3]]
class Model:
    def half(self):return self
    def modules(self):return []
    def transcribe(self,chunk,**options):
        p=P.read_json(audio/'asr_progress.json')
        number=len(p.get('chunks',{{}}))
        with open({str(project/'calls.jsonl')!r},'a',encoding='utf-8') as f:
            f.write(json.dumps({{'chunk':number,'beam':options['beam_size']}})+'\\n')
        failed=[x for x in p.get('timeouts',[]) if x['chunk']==number]
        if number>0 and (not failed or {fail_twice!r}):time.sleep(20)
        return {{'segments':[{{'start':0,'end':0.8,'text':'早段' if number==0 else '后段'}}]}}
torch=types.ModuleType('torch');torch.set_num_threads=lambda n:None
torch.cuda=types.SimpleNamespace(reset_peak_memory_stats=lambda:None,max_memory_allocated=lambda:1000000000,empty_cache=lambda:None)
whisper=types.ModuleType('whisper');whisper.load_model=lambda *args,**kw:Model()
wm=types.ModuleType('whisper.model');wm.LayerNorm=type('LayerNorm',(),{{}})
sys.modules.update({{'torch':torch,'whisper':whisper,'whisper.model':wm}})
T.load_recognition_model=lambda *args,**kw:Model()
sys.argv=['transcribe_one.py','--sub-id','n']
raise SystemExit(T.main())
'''
                P.atomic_text(project/'transcribe_one.py', script)
                runner = W.Runner(enforce_window=False)
                with mock.patch.object(C, 'PROJ', str(project)), mock.patch.object(C, 'DATA', str(data)), \
                     mock.patch.object(C, 'CAT', str(project/'catalog.json')), mock.patch.object(W, 'STATE', state), \
                     mock.patch.object(runner, 'allowed'), mock.patch.object(W, 'temperature', return_value=40), \
                     mock.patch.object(W, 'gpu_free_gb', return_value=8):
                    rc, tail = runner.command('transcribe_one.py', ['--sub-id','n'], gpu=True, timeout=20)
                self.assertEqual(rc, 43 if fail_twice else 0, tail)
                self.assertEqual([x['exit_code'] for x in runner.metrics], [42,43] if fail_twice else [42,42,0])
                calls = [json.loads(x) for x in (project/'calls.jsonl').read_text().splitlines()]
                expected=[{'chunk':0,'beam':5}, {'chunk':1,'beam':5}, {'chunk':1,'beam':None}]
                if not fail_twice:expected += [{'chunk':2,'beam':5},{'chunk':2,'beam':None}]
                self.assertEqual(calls, expected)
                with mock.patch.object(C, 'DATA', str(data)):
                    root = P.lecture_path(it)
                    if fail_twice:
                        self.assertFalse((root/'transcript'/'done.json').exists())
                        progress = ChunkProgress(root/'audio'/'asr_progress.json',
                            P.read_json(root/'audio'/'asr_progress.json')['binding'])
                        with self.assertRaises(DecoderExhausted): progress.beam_for(1,5)
                    else:
                        done = P.read_json(root/'transcript'/'done.json')
                        self.assertEqual(done['schema'],2)
                        self.assertTrue(done['decoder_fallback'])
                        self.assertEqual(done['segments'],3)
                        self.assertEqual(len(done['decoder_timeouts']),2)
                        self.assertEqual(P.subtitle_status(it),'已提取')
                        self.assertIn('[00:00:01] 后段',(root/'transcript'/'transcript.txt').read_text(encoding='utf-8'))
                        self.assertFalse((root/'audio'/'lecture.wav').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
