"""Regressions for the actual 2026-10-30 incident: loading, retry and honest outcomes."""
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

import psutil
import config as C
import pipeline_common as P
import transcribe_one as T
import weekly_supervisor as S
import workflow as W

PROJECT=str(Path(__file__).resolve().parents[1])


class NightRecoveryTests(unittest.TestCase):
    def setUp(self):
        # Synthetic fixture rules; public defaults never assume any course is silent.
        self.rules=mock.patch.object(C,'SKIP_TITLES',{'马克思主义基本原理'})
        self.term=mock.patch.object(C,'TERM_START','2026-09-14')
        self.rules.start();self.term.start()
        self.addCleanup(self.rules.stop);self.addCleanup(self.term.stop)

    def test_remote_probe_timeout_recovers_with_bounded_socket_reads(self):
        result=types.SimpleNamespace(stderr='Duration: 01:10:00.00\nStream #0:0: Audio: aac',returncode=1)
        timeout=subprocess.TimeoutExpired(['ffmpeg'],25)
        with mock.patch.object(T.subprocess,'run',side_effect=[timeout,result]) as run, \
             mock.patch.object(T.time,'sleep'):
            duration,streams=T.media_probe('https://example.test/media.mp4?secret=hidden')
        self.assertEqual(duration,4200)
        self.assertEqual(streams,[{'codec_type':'audio'}])
        self.assertEqual(run.call_count,2)
        self.assertIn('-rw_timeout',run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs['timeout'],25)

    def test_remote_probe_exhaustion_is_not_silence_and_does_not_leak_url(self):
        with mock.patch.object(T.subprocess,'run',side_effect=subprocess.TimeoutExpired('sensitive-url',25)) as run, \
             mock.patch.object(T.time,'sleep'):
            with self.assertRaises(T.MediaUnavailable) as raised:
                T.media_probe('https://example.test/media.mp4?secret=hidden')
        self.assertEqual(run.call_count,3)
        self.assertNotIn('sensitive',str(raised.exception))
        self.assertNotIn('secret',str(raised.exception))


    def test_checkpoint_load_maps_cpu_and_keeps_same_weights_in_half_precision(self):
        import torch
        from whisper.model import Whisper,ModelDimensions,LayerNorm
        import model_loader as M
        dims=ModelDimensions(n_mels=80,n_audio_ctx=16,n_audio_state=32,n_audio_head=4,n_audio_layer=1,
                            n_vocab=64,n_text_ctx=16,n_text_state=32,n_text_head=4,n_text_layer=1)
        original=Whisper(dims).half()
        expected={k:v.detach().clone() for k,v in original.state_dict().items()}
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'tiny-test.pt'
            torch.save({'dims':dims.__dict__,'model_state_dict':expected},path)
            before=torch.get_default_dtype()
            with mock.patch.object(torch,'load',wraps=torch.load) as load:
                model=M.load_recognition_model(name='local-test',device='cpu',path=path)
            self.assertEqual(load.call_args.kwargs['map_location'],'cpu')
            self.assertTrue(load.call_args.kwargs['mmap'])
            self.assertTrue(load.call_args.kwargs['weights_only'])
            self.assertEqual(torch.get_default_dtype(),before)
            for key,value in model.state_dict().items():
                self.assertTrue(torch.equal(value.half(),expected[key]))
            self.assertTrue(all(m.weight.dtype==torch.float32 for m in model.modules() if isinstance(m,LayerNorm)))
            self.assertEqual(model.decoder.token_embedding.weight.dtype,torch.float16)

    def test_checkpoint_integrity_cache_invalidates_after_file_change(self):
        import model_loader as M
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cache=root/'whisper';cache.mkdir()
            checkpoint=cache/'test.pt';checkpoint.write_bytes(b'valid')
            digest=hashlib.sha256(b'valid').hexdigest()
            fake=types.ModuleType('whisper');fake._MODELS={'test':'https://example.test/'+digest+'/test.pt'}
            with mock.patch.dict(sys.modules,{'whisper':fake}), mock.patch.dict(os.environ,{'XDG_CACHE_HOME':tmp}), \
                 mock.patch.object(M,'STATE',root):
                self.assertEqual(M.checkpoint_file('test'),checkpoint)
                checkpoint.write_bytes(b'corrupted-more-data')
                with self.assertRaises(RuntimeError):M.checkpoint_file('test')

    def _supervisor_context(self, tmp, stack):
        root=Path(tmp);state=root/'state';state.mkdir()
        for obj,key,value in [(C,'PROJ',str(root)),(S,'STATE',state),(P,'STATE',state),
                              (S,'STOP',state/'stop')]:
            stack.enter_context(mock.patch.object(obj,key,value))
        identity={'key':'test-window','start':P.now()-dt.timedelta(seconds=10),
                  'end':P.now()+dt.timedelta(seconds=20)}
        stack.enter_context(mock.patch.object(S,'window_identity',return_value=identity))
        stack.enter_context(mock.patch.object(S,'settings',return_value={'window_retry_sec':.01}))
        return root,state,identity

    def test_real_supervisor_recovers_resource_exit_and_crash_then_records_completion(self):
        for first_exit in (40,1):
            with self.subTest(first_exit=first_exit), tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
                root,state,identity=self._supervisor_context(tmp,stack)
                script=f'''
import sys,os,json,psutil
from pathlib import Path
sys.path.insert(0,{PROJECT!r})
from pipeline_common import atomic_json
p=Path({str(root/'counter')!r});number=int(p.read_text())+1 if p.exists() else 1;p.write_text(str(number))
code={first_exit!r} if number==1 else 0
atomic_json(Path({str(state/'last_run.json')!r}),{{'pid':os.getpid() if code!=1 else -1,
    'created':psutil.Process().create_time(),'exit_code':code,'requires_login':code==40,
    'reason':'模拟短暂内存保护' if code else '', 'inventory':{{'ready':number,'total':2}}}})
raise SystemExit(code)
'''
                P.atomic_text(root/'workflow.py',script)
                with mock.patch.object(W,'notify_message') as notify:
                    self.assertEqual(S.supervise(identity),0)
                    self.assertEqual(notify.call_count,1 if first_exit==40 else 0)
                status=P.read_json(state/'window_supervisor.json')
                self.assertEqual(status['attempts'],2)
                self.assertEqual(status['status'],'complete')
                self.assertEqual((root/'counter').read_text(),'2')

    def test_manual_stop_inside_window_prevents_auto_restart(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root,state,identity=self._supervisor_context(tmp,stack)
            (state/'stop').touch()
            with mock.patch.object(S.subprocess,'Popen') as spawn:
                self.assertEqual(S.main(recover=True),0)
                spawn.assert_not_called()
            import runlock
            with mock.patch.object(W,'STOP',state/'stop'), \
                 mock.patch.object(W,'window',return_value=(True,identity['end'])), \
                 mock.patch.object(runlock,'peek',return_value=(False,{})):
                self.assertEqual(W.run(),30)
                self.assertTrue((state/'stop').exists())

    def test_stop_from_prior_window_is_cleared_for_next_authorized_window(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root,state,identity=self._supervisor_context(tmp,stack)
            flag=state/'stop';flag.touch()
            prior=identity['start'].timestamp()-3600
            os.utime(flag,(prior,prior))
            with mock.patch.object(S,'supervise',return_value=0) as run:
                self.assertEqual(S.main(),0)
                run.assert_called_once()
                self.assertFalse(flag.exists())

    def test_recovery_does_not_repeat_a_completed_window(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root,state,identity=self._supervisor_context(tmp,stack)
            P.atomic_json(state/'window_supervisor.json',{'window':identity['key'],'status':'complete'})
            with mock.patch.object(S,'supervise') as run:
                self.assertEqual(S.main(recover=True),0)
                run.assert_not_called()

    def test_duplicate_supervisor_cannot_launch_second_batch(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root,state,identity=self._supervisor_context(tmp,stack)
            with P.lock('weekly-supervisor'), mock.patch.object(S,'supervise') as run:
                self.assertEqual(S.main(),0)
                run.assert_not_called()

    def test_supervisor_ends_worker_tree_at_window_boundary(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root,state,identity=self._supervisor_context(tmp,stack)
            identity['end']=P.now()+dt.timedelta(seconds=2)
            marker=root/'descendant.txt'
            P.atomic_text(root/'workflow.py',
                'import subprocess,sys,time,pathlib\n'
                'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"])\n'
                'pathlib.Path(%r).write_text(str(p.pid))\ntime.sleep(60)\n'%str(marker))
            self.assertEqual(S.supervise(identity),40)
            self.assertTrue(marker.exists())
            try:
                descendant=psutil.Process(int(marker.read_text()))
                psutil.wait_procs([descendant],timeout=5)
                self.assertFalse(descendant.is_running())
            except psutil.NoSuchProcess:
                pass
            self.assertEqual(P.read_json(state/'window_supervisor.json')['status'],'window_ended')

    def test_scheduler_returns_supervisor_failure_instead_of_false_success(self):
        import scheduled_entry as E
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(E,'STATE',Path(tmp)), \
             mock.patch.object(sys,'argv',['scheduled_entry.py','--run']), \
             mock.patch.object(S,'main',return_value=40) as supervise, \
             mock.patch.object(W,'main') as old_entry:
            self.assertEqual(E.main(),40)
            supervise.assert_called_once_with(recover=False)
            old_entry.assert_not_called()

    def test_real_stage_logs_retain_both_failures_and_following_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);state=root/'state';state.mkdir()
            P.atomic_text(root/'one.py','print("first-step-failed")\nraise SystemExit(1)\n')
            P.atomic_text(root/'two.py','print("second-step-ok")\n')
            runner=W.Runner(enforce_window=False)
            with mock.patch.object(C,'PROJ',str(root)),mock.patch.object(W,'STATE',state), \
                 mock.patch.object(runner,'allowed'):
                self.assertEqual(runner.command('one.py')[0],1)
                self.assertEqual(runner.command('two.py')[0],0)
            first,second=[Path(row['log']) for row in runner.metrics]
            self.assertNotEqual(first,second)
            self.assertIn('first-step-failed',first.read_text())
            self.assertIn('second-step-ok',second.read_text())

    def test_insufficient_dedicated_vram_waits_and_respects_stop_before_spawn(self):
        runner=W.Runner(enforce_window=False)
        with mock.patch.object(runner,'allowed',side_effect=[None,W.PauseRun('停止','stop',False)]), \
             mock.patch.object(W,'temperature',return_value=40), \
             mock.patch.object(W,'gpu_free_gb',return_value=2), \
             mock.patch.object(W.subprocess,'Popen') as spawn:
            with self.assertRaises(W.PauseRun):runner.command('transcribe_one.py',gpu=True)
            spawn.assert_not_called()

    def test_resource_protection_is_retained_and_marked_retryable(self):
        runner=W.Runner(enforce_window=False,honour_stop=False)
        with mock.patch.object(W.psutil,'virtual_memory',return_value=types.SimpleNamespace(available=1e9)):
            with self.assertRaises(W.PauseRun) as raised:runner.allowed()
        self.assertTrue(raised.exception.retryable)

    def test_weekly_entry_outside_window_is_not_reported_as_success(self):
        with mock.patch.object(S,'window_identity',return_value=None),mock.patch.object(S,'supervise') as run:
            self.assertEqual(S.main(),30)
            self.assertEqual(S.main(recover=True),0)
            run.assert_not_called()

    def test_incomplete_batch_writes_nonzero_result_even_when_exception_was_handled(self):
        import runlock
        with tempfile.TemporaryDirectory() as tmp,contextlib.ExitStack() as stack:
            root=Path(tmp);state=root/'state';state.mkdir()
            it={'sub_id':'n','course_id':'1','title':'测试课','date':'2026-10-28','playurl':'local'}
            for obj,key,value in [(C,'PROJ',str(root)),(C,'DATA',str(root/'data')),
                (C,'CAT',str(root/'catalog.json')),(P,'STATE',state),(W,'STATE',state),(W,'STOP',state/'stop')]:
                stack.enter_context(mock.patch.object(obj,key,value))
            P.atomic_json(C.CAT,{'items':[it]})
            stack.enter_context(mock.patch.object(W,'window',return_value=(True,P.now()+dt.timedelta(hours=1))))
            stack.enter_context(mock.patch.object(W.Runner,'allowed'))
            stack.enter_context(mock.patch.object(W.Runner,'command',return_value=(1,'模拟错误')))
            stack.enter_context(mock.patch.object(runlock,'peek',return_value=(False,{})))
            stack.enter_context(mock.patch.object(W,'artifacts',return_value={'ready':False}))
            stack.enter_context(mock.patch.object(W,'generate',return_value={'items':[],'ready':0,'total':1}))
            stack.enter_context(mock.patch.object(W,'run_report',return_value='report'))
            self.assertEqual(W.run(resume=True),40)
            report=P.read_json(state/'last_run.json')
            self.assertEqual(report['exit_code'],40)
            self.assertEqual(report['inventory'],{'ready':0,'total':1})

    def test_preflight_checks_health_without_claiming_login_is_the_only_requirement(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(P,'STATE',Path(tmp)), \
             mock.patch.object(W,'STATE',Path(tmp)), \
             mock.patch.object(W,'generate',return_value={'items':[],'ready':0,'total':1}), \
             mock.patch.object(W,'run_report',return_value='report'), \
             mock.patch.object(W,'notify_message'), \
             mock.patch.object(W.Runner,'command',side_effect=[(0,''),(40,'')]) as command:
            self.assertEqual(W.preflight(),40)
            self.assertEqual([call.args[0] for call in command.call_args_list],['platform_session.py','healthcheck.py'])
            self.assertIn('待恢复',P.read_json(Path(tmp)/'preflight.json')['result'])

    def test_cpu_preparation_deadline_stops_before_night_window(self):
        runner=W.Runner(enforce_window=False,honour_stop=False)
        runner.deadline=P.now()-dt.timedelta(seconds=1)
        with self.assertRaises(W.PauseRun) as raised:runner.allowed()
        self.assertEqual(raised.exception.code,'window')
        self.assertFalse(raised.exception.retryable)

    def test_daytime_ocr_prepares_cache_without_publishing_incomplete_lecture(self):
        from PIL import Image
        import ppt_build as B
        with tempfile.TemporaryDirectory() as tmp,mock.patch.object(C,'DATA',tmp), \
             mock.patch.object(C,'CAT',str(Path(tmp)/'catalog.json')):
            it={'sub_id':'n','course_id':'1','title':'测试课','date':'2026-10-28','duration':30}
            P.atomic_json(C.CAT,{'items':[it]})
            root=P.lecture_path(it);slides=root/'slides';slides.mkdir(parents=True)
            name='001_00-00-00.jpg';Image.new('RGB',(320,180),'white').save(slides/name)
            def ocr(src,names,path,workers):
                rows=[{'file':n,'sec':0,'text':'白天准备图形和文字'} for n in names]
                P.atomic_json(path,rows)
                return rows
            args=['ppt_build.py','--date',it['date'],'--title',it['title'],'--sub-id','n']
            with mock.patch.object(sys,'argv',args+['--prepare-ocr']),mock.patch.object(B,'run_ocr',side_effect=ocr):
                self.assertEqual(B.main(),0)
            self.assertTrue((root/'ocr.json').exists())
            self.assertFalse((root/'lecture.md').exists())
            self.assertFalse((root/'ppt_build.json').exists())
            T.commit_subtitles(it,root/'transcript',[{'start':0,'end':1,'text':'夜间字幕'}],{})
            with mock.patch.object(sys,'argv',args),mock.patch.object(B,'run_ocr') as rerun:
                self.assertEqual(B.main(),0)
                rerun.assert_not_called()
            self.assertIn('夜间字幕',(root/'lecture.md').read_text(encoding='utf-8'))


if __name__=='__main__':
    unittest.main(verbosity=2)
