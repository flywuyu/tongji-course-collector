"""Public package setup and publication boundaries; all fixtures are synthetic."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import config as C
import manage
import pipeline_common as P

ROOT=Path(__file__).resolve().parents[1]


class DistributionTests(unittest.TestCase):
    def test_init_is_portable_and_does_not_overwrite_existing_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            with mock.patch.object(C,'PROJ',str(root)),mock.patch.object(C,'LOCAL_CONFIG',root/'config.local.json'), \
                 mock.patch.object(C,'CAT',str(root/'catalog.json')):
                shutil.copy2(ROOT/'pipeline_settings.example.json',root/'pipeline_settings.example.json')
                args=argparse.Namespace(term_start='2026-09-14',output='courses',force=False)
                self.assertEqual(manage.initialize(args),0)
                config=json.loads((root/'config.local.json').read_text(encoding='utf-8'))
                self.assertEqual(Path(config['data_dir']),root/'courses')
                self.assertNotIn('password',config)
                with self.assertRaises(RuntimeError):manage.initialize(args)

    def test_help_does_not_load_model_or_browser_packages(self):
        code="""import builtins,sys
real=builtins.__import__
def check(name,*args,**kwargs):
    if name.split('.')[0] in ('torch','whisper','playwright','onnxruntime'):
        raise AssertionError('heavy dependency loaded for help')
    return real(name,*args,**kwargs)
builtins.__import__=check
import manage
manage.main(['--help'])
"""
        result=subprocess.run([sys.executable,'-X','utf8','-c',code],cwd=ROOT,capture_output=True,encoding='utf-8',timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)

    @unittest.skipUnless(shutil.which('git'),'Git is required for publication checks')
    def test_gitignore_blocks_credentials_course_content_and_local_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy2(ROOT/'.gitignore',Path(tmp)/'.gitignore')
            subprocess.run(['git','init','-q',tmp],check=True,capture_output=True)
            names=['.pipeline/account.dpapi','.browser_profile/Default/Cookies','config.local.json',
                   'pipeline_settings.json','catalog_playurls.json','weekly.json','data/courses/lecture.md',
                   '.venv/private.txt','random-temp.json']
            result=subprocess.run(['git','check-ignore','--stdin','-z'],cwd=tmp,input=('\0'.join(names)+'\0').encode(),
                                  capture_output=True,check=True)
            self.assertEqual(set(result.stdout.decode().strip('\0').split('\0')),set(names))

    def test_public_defaults_contain_no_course_rules_or_one_time_window(self):
        settings=P.settings()
        self.assertEqual(settings['course_rules'],{})
        for window in [settings,*settings['extra_windows']]:
            self.assertEqual(window['start'],'00:00')
            self.assertEqual(window['end'],'08:00')
            self.assertNotIn('date',window)

    def test_publication_guard_rejects_private_paths_and_detectable_credentials(self):
        spec=importlib.util.spec_from_file_location('publication_guard',ROOT/'scripts'/'check_public_tree.py')
        guard=importlib.util.module_from_spec(spec);spec.loader.exec_module(guard)
        self.assertTrue(guard.violations('.pipeline/account.dpapi',b'private'))
        self.assertTrue(guard.violations('catalog_playurls.json',b'{}'))
        token=('gh'+'p_'+'A'*36).encode()
        self.assertTrue(guard.violations('accidental.py',token))
        self.assertEqual(guard.violations('example.py',b'print("synthetic")'),[])

    @unittest.skipUnless(sys.platform=='win32','Windows scheduler script')
    def test_schedule_dry_run_does_not_register_tasks(self):
        result=subprocess.run(['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',
            str(ROOT/'install_schedule.ps1'),'-PythonPath',sys.executable,'-WhatIf'],
            capture_output=True,encoding='utf-8',errors='replace',timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('00:00-08:00',result.stdout)


if __name__=='__main__':unittest.main(verbosity=2)
