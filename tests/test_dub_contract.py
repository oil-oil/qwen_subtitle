"""从 CLI 入口验证翻译与声音克隆的授权边界；不访问真实服务。"""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

SPEC = importlib.util.spec_from_file_location('dub_multi', Path(__file__).parents[1] / 'scripts/dub_multi.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

class DubContractTests(unittest.TestCase):
    def run_case(self, extra):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcript = root / 'source.json'
            transcript.write_text(json.dumps([{'start': 0, 'end': 2, 'text': '你好。'}]))
            argv = ['dub_multi.py', str(root / 'source.mp4'), '--transcript', str(transcript), '--out', str(root/'out'), *extra]
            with patch.object(sys, 'argv', argv), patch.object(MODULE, 'dur', return_value=2), patch.object(MODULE.subprocess, 'run'), patch.object(MODULE, 'clone_voice', return_value='test-voice') as clone, patch.object(MODULE, 'build_lang', return_value={'code':'en'}) as build:
                MODULE.main()
                return clone.call_count, build.call_args.args[2]

    def test_translation_never_clones_by_language_alone(self):
        self.assertEqual(self.run_case(['--langs', 'en']), (0, None))

    def test_explicit_dub_requires_voice_authorization(self):
        with patch.object(MODULE, 'clone_voice') as clone:
            with self.assertRaises(SystemExit):
                self.run_case(['--langs', 'en', '--dub'])
            clone.assert_not_called()

    def test_authorized_dub_runs_clone(self):
        self.assertEqual(self.run_case(['--langs','en','--dub','--confirm-voice-rights']), (1, 'test-voice'))

    def test_unknown_language_fails_before_work(self):
        with self.assertRaises(SystemExit):
            self.run_case(['--langs', 'not-a-language'])

    def test_empty_output_languages_fail(self):
        with self.assertRaises(SystemExit):
            self.run_case(['--langs', ''])

    def test_ffmpeg_failure_never_writes_success_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source.json'
            source.write_text(json.dumps([{'start':0,'end':2,'text':'你好。'}]))
            args=['dub_multi.py',str(root/'video.mp4'),'--transcript',str(source),'--out',str(root/'out')]
            with patch.object(sys,'argv',args),patch.object(MODULE,'dur',return_value=2),patch.object(MODULE.subprocess,'run',side_effect=MODULE.subprocess.CalledProcessError(1,['ffmpeg'])),patch.object(MODULE,'build_lang') as build,self.assertRaises(MODULE.subprocess.CalledProcessError):
                MODULE.main()
            build.assert_not_called()
            self.assertFalse((root/'out/manifest.json').exists())

    def test_enrollment_secret_does_not_enter_process_arguments(self):
        secret = 'dummy-test-credential'
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"output":{"voice_id":"test"}}'
        with tempfile.TemporaryDirectory() as directory, patch.object(MODULE, 'dashscope_key', return_value=secret), patch.object(MODULE, 'bl', return_value='{"url":"https://example.test/audio"}'), patch.object(MODULE.subprocess, 'run') as run, patch.object(MODULE, 'urlopen', return_value=response):
            self.assertEqual(MODULE.clone_voice('test.mp4', directory, 0), 'test')
            self.assertNotIn(secret, repr(run.call_args_list))

if __name__ == '__main__':
    unittest.main()
