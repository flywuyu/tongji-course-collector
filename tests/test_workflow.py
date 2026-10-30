"""Acceptance regressions for the rules that can lose data or occupy the computer."""
import datetime as dt
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import psutil
from PIL import Image, ImageDraw
import config as C
import pipeline_common as P
from transcribe_one import commit_subtitles, fmt, speech_ranges


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        # Synthetic fixture rules; public defaults never assume any course is silent.
        self.rules=mock.patch.object(C,'SKIP_TITLES',{'马克思主义基本原理'})
        self.term=mock.patch.object(C,'TERM_START','2026-09-14')
        self.rules.start();self.term.start()
        self.addCleanup(self.rules.stop);self.addCleanup(self.term.stop)

    def test_friday_window_exact_boundaries(self):
        from weekly_supervisor import window_identity
        for stamp, expected in [('2026-11-05T23:59:59', False), ('2026-11-06T00:00:00', True),
                ('2026-11-06T07:00:00', True), ('2026-11-06T07:59:59', True),
                ('2026-11-06T08:00:00', False), ('2026-11-06T18:59:59', False),
                ('2026-11-06T19:00:00', False), ('2026-11-06T23:55:00', False),
                ('2026-11-07T00:00:00', True), ('2026-11-07T07:59:59', True),
                ('2026-11-07T08:00:00', False), ('2026-11-07T19:00:00', False),
                ('2026-11-08T00:00:00', False), ('2026-10-30T19:00:00', False)]:
            at=dt.datetime.fromisoformat(stamp).replace(tzinfo=P.LOCAL)
            self.assertEqual(P.window(at)[0], expected)
            self.assertEqual(window_identity(at) is not None, expected)

    def test_nutrition_continuous_70_minutes(self):
        ranges = speech_ranges(4200, [])
        self.assertEqual(sum(b-a for a,b in ranges), 4200)
        self.assertEqual(ranges[-1][1], 4200)
        self.assertTrue(any(a <= 45*60 < b for a,b in ranges))

    def test_measured_tail_and_break_keep_original_time(self):
        ranges = speech_ranges(5700, [[2700, 3000], [4200, 5700]])
        self.assertTrue(any(a == 2998 for a,b in ranges))
        self.assertEqual(ranges[-1][1], 4202)
        self.assertFalse(any(a < 2800 < b for a,b in ranges))

    def test_full_silence_produces_no_fake_boundary_clips(self):
        self.assertEqual(speech_ranges(6000, [[0, 6000]]), [])

    def test_timestamps_round_without_millisecond_overflow(self):
        self.assertEqual(fmt(59.9997, True), '00:01:00,000')
        self.assertEqual(fmt(4200), '01:10:00')

    def test_unreadable_model_bytes_are_flagged_without_dropping_other_words(self):
        from transcribe_one import normalized_asr_text
        text,damaged=normalized_asr_text('有效内容\ufffd 后续内容')
        self.assertTrue(damaged)
        self.assertEqual(text,'有效内容[识别不清] 后续内容')
        self.assertNotIn('\ufffd',text)

    def test_subtitle_commit_hashes_detect_partial_overwrite(self):
        it = {'sub_id':'test', 'course_id':'test','title':'测试课程','date':'2026-10-21'}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(C, 'DATA', tmp):
            target = P.lecture_path(it) / 'transcript'
            commit_subtitles(it, target, [{'start':3000,'end':3002,'text':'实际授课内容'}], {})
            self.assertEqual(P.subtitle_status(it), '已提取')
            (target/'commit.pending').write_text('{}',encoding='utf-8')
            (target/'transcript.txt').write_text('[00:50:00] 篡改后的内容\n', encoding='utf-8')
            self.assertIn('需修复', P.subtitle_status(it))
            (target/'commit.pending').unlink()
            self.assertIn('待核对', P.subtitle_status(it))

    def test_pending_first_commit_not_mistaken_for_legacy(self):
        it={'sub_id':'x','course_id':'x','title':'测试','date':'2026-10-21'}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(C,'DATA',tmp):
            target=P.lecture_path(it)/'transcript'
            target.mkdir(parents=True)
            (target/'transcript.txt').write_text('[00:00:01] 内容\n',encoding='utf-8')
            (target/'transcript.srt').write_text('1\n00:00:01,000 --> 00:00:02,000\n内容\n',encoding='utf-8')
            (target/'commit.pending').write_text('{}',encoding='utf-8')
            self.assertIn('需修复',P.subtitle_status(it))

    def test_approved_cleanup_updates_commit_hashes_without_asr_rerun(self):
        from clean_halluc import process
        it={'sub_id':'z','course_id':'z','title':'清理验收','date':'2026-10-21'}
        with tempfile.TemporaryDirectory() as tmp,mock.patch.object(C,'DATA',tmp):
            target=P.lecture_path(it)/'transcript'
            commit_subtitles(it,target,[{'start':0,'end':1,'text':'真实课堂内容'},
                                      {'start':2,'end':3,'text':'由Amara.org社群提供的字幕'}],{})
            self.assertEqual(P.subtitle_status(it),'已提取')
            result=process(str(target/'transcript.txt'),apply=True)
            self.assertEqual(result['kept'],1)
            self.assertEqual(P.subtitle_status(it),'已提取')
            self.assertTrue((target/'transcript.txt.bak').exists())

    def test_marxism_skips_old_hallucinated_subtitles(self):
        self.assertTrue(P.audio_done({'title':'马克思主义基本原理'}))
        self.assertIn('免字幕',P.subtitle_status({'title':'马克思主义基本原理'}))

    def test_figures_with_same_ocr_are_not_merged(self):
        from ppt_build import dedupe
        with tempfile.TemporaryDirectory() as tmp:
            names=['001_00-00-00.jpg','002_00-00-20.jpg','003_00-00-40.jpg']
            first=Image.new('RGB',(1280,720),'white')
            first.save(Path(tmp)/names[0])
            changed=first.copy()
            ImageDraw.Draw(changed).rectangle((800,200,1120,500),fill='black')
            changed.save(Path(tmp)/names[1]);changed.save(Path(tmp)/names[2])
            ocr=[{'file':n,'text':'相同的标题和相同的说明文字123456789'} for n in names]
            result=dedupe(tmp,names,ocr)
            self.assertEqual(len(result),2)
            self.assertEqual([r['sec'] for r in result],[0,20])

    def test_video_dedup_ignores_people_but_keeps_screen_diagram(self):
        from ppt_build import dedupe
        with tempfile.TemporaryDirectory() as tmp:
            src=Path(tmp)/'slides';src.mkdir()
            P.atomic_json(Path(tmp)/'ppt清单.json',{'source':'video'})
            names=['001_00-00-00.jpg','002_00-00-20.jpg','003_00-00-40.jpg']
            im=Image.new('RGB',(1280,720),(30,30,30))
            ImageDraw.Draw(im).rectangle((80,80,800,480),fill='white')
            im.save(src/names[0])
            ImageDraw.Draw(im).rectangle((600,550,900,719),fill='red')
            im.save(src/names[1])
            ImageDraw.Draw(im).rectangle((400,180,600,380),fill='black')
            im.save(src/names[2])
            ocr=[{'file':n,'text':'相同标题相同文字，图形内容需要保留12345'} for n in names]
            self.assertEqual(len(dedupe(str(src),names,ocr)),2)

    def test_more_than_999_frames_are_built_in_video_time_order(self):
        import ppt_build
        it={'sub_id':'z','course_id':'z','title':'排序验收','date':'2026-10-21','duration':'4200'}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(C,'DATA',tmp), \
             mock.patch.object(C,'CAT',str(Path(tmp)/'catalog.json')):
            P.atomic_json(C.CAT,{'items':[it]})
            root=P.lecture_path(it);src=root/'slides';src.mkdir(parents=True)
            names=['999_00-59-59.jpg','1000_01-00-00.jpg','1001_01-00-20.jpg']
            for n,color in zip(names,['red','blue','green']):Image.new('RGB',(320,180),color).save(src/n)
            P.atomic_json(root/'ocr.json',[{'file':n,'text':n,'sec':ppt_build.sec_of(n)} for n in names])
            commit_subtitles(it,root/'transcript',[{'start':0,'end':1,'text':'排序检查'}],{})
            with mock.patch.object(sys,'argv',['ppt_build.py','--date',it['date'],'--title',it['title'],'--skip-ocr']):
                self.assertEqual(ppt_build.main(),0)
            text=(root/'lecture.md').read_text(encoding='utf-8')
            self.assertLess(text.index('视频 00:59:59'),text.index('视频 01:00:00'))
            self.assertLess(text.index('视频 01:00:00'),text.index('视频 01:00:20'))
            pdf=str(root/('PPT讲义_%s.pdf'%it['date']))
            self.assertTrue(ppt_build.cached_outputs_complete(str(root),pdf,3))
            (root/'slides_unique'/'002_01-00-00.jpg').unlink()
            self.assertFalse(ppt_build.cached_outputs_complete(str(root),pdf,3))

    def test_path_escape_is_rejected(self):
        with self.assertRaises(ValueError):
            P.lecture_path({'title':'..','date':'2026-10-21'})

    def test_token_and_url_redaction(self):
        text=P.redact('error https://example.test/path?token=eyJaaa.bbbbb.ccccc')
        self.assertNotIn('token=',text)

    def test_atomic_file_has_no_bom_and_crlf(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'file.md'
            P.atomic_text(p,'一\n二\n')
            self.assertEqual(p.read_bytes(),'一\r\n二\r\n'.encode('utf-8'))
            self.assertEqual(list(Path(tmp).glob('*.tmp')),[])

    def test_worker_tree_dies_when_job_closed(self):
        from process_job import ProcessJob
        with tempfile.TemporaryDirectory() as tmp:
            marker=Path(tmp)/'child.txt'
            code='import subprocess,sys,time,pathlib;p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]);pathlib.Path(sys.argv[1]).write_text(str(p.pid));time.sleep(60)'
            proc=subprocess.Popen([sys.executable,'-c',code,str(marker)],creationflags=subprocess.CREATE_NO_WINDOW)
            job=ProcessJob(proc)
            try:
                deadline=time.monotonic()+10
                while not marker.exists() and time.monotonic()<deadline:
                    time.sleep(.05)
                self.assertTrue(marker.exists())
                child=psutil.Process(int(marker.read_text()))
                job.close()
                proc.wait(timeout=5)
                psutil.wait_procs([child],timeout=5)
                self.assertFalse(child.is_running())
            finally:
                job.close()
                if proc.poll() is None: proc.kill()

    def test_temperature_pause_before_gpu_spawn(self):
        import workflow
        runner=workflow.Runner(enforce_window=False)
        with mock.patch.object(runner,'allowed'), mock.patch.object(workflow,'temperature',return_value=None), \
             mock.patch.object(workflow.subprocess,'Popen') as spawn:
            with self.assertRaises(workflow.PauseRun):
                runner.command('transcribe_one.py',gpu=True)
            spawn.assert_not_called()

    def test_window_stop_before_process_spawn(self):
        import workflow
        runner=workflow.Runner()
        with mock.patch.object(workflow,'window',return_value=(False,P.now())), \
             mock.patch.object(workflow.subprocess,'Popen') as spawn:
            with self.assertRaises(workflow.PauseRun):
                runner.command('transcribe_one.py',gpu=True)
            spawn.assert_not_called()

    def test_preflight_not_blocked_by_last_friday_stop_flag(self):
        import workflow
        with tempfile.TemporaryDirectory() as tmp:
            flag=Path(tmp)/'stop';flag.touch()
            with mock.patch.object(workflow,'STOP',flag):
                with self.assertRaises(workflow.PauseRun):
                    workflow.Runner(enforce_window=False).allowed()
                workflow.Runner(enforce_window=False,honour_stop=False).allowed()



    def test_stale_collector_pid_does_not_claim_reused_process(self):
        import runlock
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(runlock,'LOCK',str(Path(tmp)/'lock.json')):
            P.atomic_json(runlock.LOCK,{'pid':os.getpid(),'created':1,'start':'2000-01-01 00:00:00'})
            self.assertFalse(runlock.peek()[0])
            P.atomic_json(runlock.LOCK,{'pid':os.getpid(),'created':psutil.Process().create_time()})
            self.assertTrue(runlock.peek()[0])

    def test_legacy_gpu_entry_cannot_bypass_window_lock(self):
        import runlock
        with mock.patch.object(P,'window',return_value=(False,P.now())):
            ok,info=runlock.acquire()
            self.assertFalse(ok)
            self.assertEqual(info['reason'],'outside_window')



    def test_real_audio_duration_and_early_silence(self):
        from transcribe_one import media_probe, measured_silences
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp)/'measured.wav'
            subprocess.run([C.FF,'-y','-v','error','-f','lavfi','-i',
                            'sine=frequency=440:duration=70:sample_rate=16000',
                            '-af','apad=pad_dur=35','-ac','1',str(wav)],check=True,capture_output=True)
            duration, streams = media_probe(str(wav))
            self.assertAlmostEqual(duration,105,places=1)
            self.assertTrue(any(x['codec_type']=='audio' for x in streams))
            silence = measured_silences(wav,duration)
            self.assertAlmostEqual(silence[0][0],70,places=0)
            self.assertAlmostEqual(silence[-1][1],105,places=0)
            self.assertAlmostEqual(speech_ranges(duration,silence)[-1][1],72,places=0)

    def test_download_rejects_html_disguised_as_jpeg(self):
        import ppt_download as D
        response = mock.MagicMock()
        response.__enter__.return_value=response
        response.status=200
        response.read.return_value=b'<html>'+b'x'*6000
        with mock.patch.dict(D.ABORT,{'flag':False,'reason':''},clear=True), \
             mock.patch.object(D.urllib.request,'urlopen',return_value=response), \
             mock.patch.object(D.time,'sleep'):
            _,data,_=D.fetch_one('https://example.test/',0)
            self.assertIsNone(data)
            self.assertTrue(D.ABORT['flag'])

    def test_interrupted_audio_tmp_is_not_promoted_by_duration_alone(self):
        import audio_download as V
        with tempfile.TemporaryDirectory() as tmp:
            dst=Path(tmp)/'lecture.wav'
            Path(str(dst)+'.tmp').write_bytes(b'x'*200000)
            failed=subprocess.CompletedProcess([],1,'','network failed')
            with mock.patch.object(V,'valid_audio',return_value=True), \
                 mock.patch.object(V.subprocess,'run',return_value=failed) as ffmpeg:
                ok,_=V.download_audio('https://example.test/video.mp4',str(dst),6000)
                self.assertFalse(ok)
                self.assertFalse(dst.exists())
                self.assertEqual(ffmpeg.call_count,1)

    def test_404_does_not_retry_or_mark_network_failure(self):
        import ppt_download as D
        import urllib.error
        error=urllib.error.HTTPError('https://example.test/',404,'missing',{},None)
        with mock.patch.dict(D.ABORT,{'flag':False,'reason':''},clear=True), \
             mock.patch.object(D.urllib.request,'urlopen',side_effect=error) as fetch, \
             mock.patch.object(D.time,'sleep'):
            self.assertIsNone(D.fetch_one('https://example.test/',0)[1])
            self.assertFalse(D.ABORT['flag'])
            self.assertEqual(fetch.call_count,1)

    def test_gold_refresh_preserves_original(self):
        from manual_merge import merge
        it={'sub_id':'1917570','course_id':'96191','title':'高级语言程序设计A1','date':'2026-10-21'}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(C,'DATA',tmp):
            root=P.lecture_path(it)
            root.mkdir(parents=True)
            P.atomic_text(root/'lecture.md','原有手工讲义\n')
            original=P.sha(root/'lecture.md')
            P.atomic_json(root/'ocr.json',[{'file':'001_00-00-00.jpg','text':'完整画面OCR'}])
            P.atomic_text(root/'slides_去重索引.csv','新文件名,视频时间,来源原文件,合并帧数\n001_00-00-00.jpg,00:00:00,001_00-00-00.jpg,1\n',bom=True)
            (root/'transcript').mkdir()
            P.atomic_text(root/'transcript'/'transcript.txt','[00:00:01] 新字幕内容\n')
            merge(it)
            self.assertEqual(P.sha(root/'lecture.md'),original)
            self.assertIn('新字幕内容',(root/'lecture_自动更新.md').read_text(encoding='utf-8'))

    def test_quiet_audio_is_not_discarded_as_silence(self):
        from transcribe_one import analyze_audio
        with tempfile.TemporaryDirectory() as tmp:
            wav=Path(tmp)/'quiet.wav'
            subprocess.run([C.FF,'-y','-v','error','-f','lavfi','-i',
                'sine=frequency=440:duration=35:sample_rate=16000','-af','volume=0.005',
                str(wav)],check=True,capture_output=True)
            silent,peak=analyze_audio(wav,35)
            self.assertTrue(silent)
            self.assertGreater(peak,-75)
            adjusted,_=analyze_audio(wav,35,max(-90,peak-25))
            self.assertEqual(adjusted,[])

    def test_integrated_two_routes_silent_course_and_reports(self):
        import contextlib
        import course_inventory
        import ppt_build
        import runlock
        import workflow
        data=[{'sub_id':'a','course_id':'1','title':'马克思主义基本原理','date':'2026-09-15',
               'sub_title':'第1-3节','duration':'6000','playurl':'https://example.test/a'},
              {'sub_id':'b','course_id':'2','title':'科学营养减重','date':'2026-09-16',
               'sub_title':'第9-10节','duration':'4200','playurl':'https://example.test/b'},
              {'sub_id':'c','course_id':'1','title':'马克思主义基本原理','date':'2026-09-17',
               'sub_title':'第1-3节','duration':'6000','playurl':'https://example.test/c'}]
        calls=[]
        def fake_command(runner,script,args=(),gpu=False,timeout=7200):
            args=list(map(str,args))
            calls.append((script, tuple(args), gpu))
            if script=='platform_session.py': return 0,''
            subid=str(args[list(args).index('--sub-id')+1])
            it=next(x for x in data if x['sub_id']==subid)
            root=P.lecture_path(it)
            if script=='ppt_download.py' and subid=='c':
                return 4,'UUID存在，但图片没有命中'
            if script=='transcribe_one.py':
                self.assertEqual(subid,'b')
                self.assertTrue(gpu)
                commit_subtitles(it,root/'transcript',[{'start':3000,'end':3002,'text':'第五十分钟仍在连续讲课'}],{})
                return 0,''
            if script in ('ppt_download.py','ppt_extract.py'):
                slides=root/'slides';slides.mkdir(parents=True,exist_ok=True)
                name='001_00-00-00.jpg'
                Image.new('RGB',(1280,720),'white').save(slides/name)
                P.atomic_json(root/'ppt清单.json',{'sub_id':subid,'count':1,
                     'source':'platform' if script=='ppt_download.py' else 'video',
                     'items':[{'file':name,'ms':0,'bytes':(slides/name).stat().st_size}]})
                P.atomic_text(root/'slides_索引.csv','序号,视频时间,文件\n1,00:00:00,'+name+'\n',bom=True)
                P.atomic_json(root/'ocr.json',[{'file':name,'sec':0,'text':'图文原始内容','lines':['图文原始内容']}])
                return 0,''
            self.assertEqual(script,'ppt_build.py')
            with mock.patch.object(sys,'argv',['ppt_build.py',*args,'--skip-ocr']):
                return ppt_build.main(),''
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            project=Path(tmp)/'project';project.mkdir()
            output=Path(tmp)/'data';output.mkdir()
            state=project/'.pipeline';state.mkdir()
            for obj,name,value in [(C,'PROJ',str(project)),(C,'DATA',str(output)),
                    (C,'CAT',str(project/'catalog.json')),(C,'REPORT',str(project/'对账报告.md')),
                    (P,'STATE',state),(workflow,'STATE',state),
                    (P,'STOP',state/'stop'),(workflow,'STOP',state/'stop')]:
                stack.enter_context(mock.patch.object(obj,name,value))
            P.atomic_json(C.CAT,{'items':data})
            P.atomic_json(project/'ppt_清单.json',{'items':[{'sub_id':'a','ppt_base':'https://example.test/ppt/'},
                                                          {'sub_id':'b','ppt_base':'','status':'no_uuid'},
                                                          {'sub_id':'c','ppt_base':'https://example.test/missing/'}]})
            stack.enter_context(mock.patch.object(workflow,'window',return_value=(True,P.now()+dt.timedelta(hours=1))))
            stack.enter_context(mock.patch.object(workflow.Runner,'allowed'))
            stack.enter_context(mock.patch.object(workflow.Runner,'command',fake_command))
            stack.enter_context(mock.patch.object(runlock,'peek',return_value=(False,{})))
            self.assertEqual(workflow.run(),0)
            self.assertEqual([x[0] for x in calls],['platform_session.py','ppt_download.py','ppt_build.py',
                         'transcribe_one.py','ppt_extract.py','ppt_build.py',
                         'ppt_download.py','ppt_extract.py','ppt_build.py'])
            result=P.read_json(output/'index.json')
            self.assertEqual(result['ready'],3)
            self.assertEqual(result['audio_skipped'],2)
            self.assertEqual(next(x for x in result['items'] if x['sub_id']=='c')['ppt_route'],'二级·视频画面')
            self.assertIn('[00:50:00]',(P.lecture_path(data[1])/'lecture.md').read_text(encoding='utf-8'))
            self.assertTrue((output/'运行报告'/'最新报告.md').exists())


if __name__=='__main__':
    unittest.main(verbosity=2)
