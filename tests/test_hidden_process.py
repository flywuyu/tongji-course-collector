"""Windows background helpers must not allocate a flashing console window."""
import json
import os
import subprocess
import sys
import unittest
from unittest import mock

import hidden_process as H


@unittest.skipUnless(os.name == 'nt', 'Windows console behavior')
class HiddenProcessTests(unittest.TestCase):
    def test_real_child_has_no_console_and_keeps_output(self):
        code = ('import ctypes,json; '
                'ctypes.windll.kernel32.GetConsoleWindow.restype=ctypes.c_void_p; '
                'print(json.dumps({"window":ctypes.windll.kernel32.GetConsoleWindow()}))')
        result = H.run([sys.executable, '-X', 'utf8', '-c', code],
                       capture_output=True, encoding='utf-8', check=True, timeout=15)
        self.assertFalse(json.loads(result.stdout)['window'])
        self.assertEqual(result.stderr, '')

    def test_hides_even_if_caller_requests_new_console_and_preserves_priority(self):
        original = {'creationflags': subprocess.BELOW_NORMAL_PRIORITY_CLASS |
                    subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS}
        result = H.options(original)
        self.assertTrue(result['creationflags'] & subprocess.CREATE_NO_WINDOW)
        self.assertTrue(result['creationflags'] & subprocess.BELOW_NORMAL_PRIORITY_CLASS)
        self.assertFalse(result['creationflags'] & subprocess.CREATE_NEW_CONSOLE)
        self.assertFalse(result['creationflags'] & subprocess.DETACHED_PROCESS)
        self.assertTrue(result['startupinfo'].dwFlags & subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(result['startupinfo'].wShowWindow, subprocess.SW_HIDE)
        self.assertTrue(original['creationflags'] & subprocess.CREATE_NEW_CONSOLE)

    def test_media_probe_uses_hidden_process_options(self):
        import transcribe_one as T
        result = subprocess.CompletedProcess([], 0,
            stdout=json.dumps({'format': {'duration': '60'}, 'streams': [{'codec_type': 'audio'}]}),
            stderr='Duration: 00:01:00.00\nStream #0:0: Audio: pcm_s16le')
        with mock.patch.object(H.subprocess, 'run', return_value=result) as run:
            duration, streams = T.media_probe('test.wav')
        self.assertEqual(duration, 60)
        self.assertEqual(streams, [{'codec_type': 'audio'}])
        self.assertTrue(run.call_args.kwargs['creationflags'] & subprocess.CREATE_NO_WINDOW)

    def test_background_media_modules_use_hidden_runner(self):
        import transcribe_one
        import ppt_extract
        import runlock
        import workflow
        for module in (transcribe_one, ppt_extract, runlock, workflow):
            self.assertIs(module.run_hidden, H.run)


if __name__ == '__main__':
    unittest.main()
