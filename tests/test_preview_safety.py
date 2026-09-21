"""本地预览编辑器的绑定、校验和原子保存测试。"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("preview_editor", ROOT / "scripts/preview_editor.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PreviewSafetyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.video = root / "demo.mp4"
        self.video.write_bytes(b"video")
        self.transcript = root / "transcript.json"
        self.transcript.write_text(json.dumps({"segments": [{"start": 0, "end": 1, "text": "原文"}]}))
        MODULE.VIDEO_PATH = str(self.video)
        MODULE.TRANSCRIPT_PATH = str(self.transcript)
        MODULE.MANIFEST = None
        MODULE.WORKSPACE = str(root)

    def tearDown(self):
        self.directory.cleanup()

    def test_invalid_payload_does_not_overwrite_file(self):
        client = MODULE.app.test_client()
        response = client.post(
            "/api/transcript",
            json={"segments": [{"start": 1, "end": 0, "text": "坏时间"}]},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(self.transcript.read_text())["segments"][0]["text"], "原文")

    def test_overlapping_segments_are_rejected(self):
        client = MODULE.app.test_client()
        response = client.post(
            "/api/transcript",
            json={
                "segments": [
                    {"start": 0, "end": 2, "text": "第一句"},
                    {"start": 1, "end": 3, "text": "第二句"},
                ]
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(self.transcript.read_text())["segments"][0]["text"], "原文")

    def test_valid_payload_is_saved_and_unknown_language_is_rejected(self):
        client = MODULE.app.test_client()
        response = client.post(
            "/api/transcript",
            json={"segments": [{"start": 0, "end": 1, "text": "修改后"}]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(self.transcript.read_text())["segments"][0]["text"], "修改后")

        MODULE.MANIFEST = {"languages": [{"code": "zh", "transcript": "transcript.json", "source": True}]}
        response = client.post(
            "/api/transcript",
            json={"lang": "unknown", "segments": [{"start": 0, "end": 1, "text": "越权"}]},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(json.loads(self.transcript.read_text())["segments"][0]["text"], "修改后")


if __name__ == "__main__":
    unittest.main()
