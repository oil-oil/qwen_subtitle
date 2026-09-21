"""字幕纠错的安全边界测试；不访问真实模型或视频服务。"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("subfix", ROOT / "scripts/subfix.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SubfixSafetyTests(unittest.TestCase):
    def test_semantic_guess_waits_for_review_by_default(self):
        sents = [{"text": "这是飞树项目", "begin_time": 0, "end_time": 1000}]
        result = MODULE.correct(
            "unused.mp4",
            sents,
            [{"sid": 0, "wrong": "飞树", "kind": "semantic", "guess": "飞书"}],
            Path(tempfile.mkdtemp()),
        )
        self.assertEqual(result[0]["final"], "飞树")
        self.assertTrue(result[0]["needs_review"])

    def test_filler_model_cannot_change_meaning(self):
        self.assertTrue(MODULE.safe_filler_edit("呃你好", "你好"))
        self.assertTrue(MODULE.safe_filler_edit("你你看", "你看"))
        self.assertFalse(MODULE.safe_filler_edit("打开 Claude", "打开 Codex"))
        self.assertFalse(MODULE.safe_filler_edit("评测得分 9.8", "评测得分 98"))

    def test_invalid_suspects_are_discarded(self):
        sents = [{"text": "打开 Claude", "begin_time": 0, "end_time": 1000}]
        valid = MODULE.validate_suspects(
            [
                {"sid": 0, "wrong": "Claude", "kind": "screen", "guess": "Claude"},
                {"sid": 99, "wrong": "Claude", "kind": "screen"},
                {"sid": 0, "wrong": "不存在", "kind": "screen"},
                {"sid": 0, "wrong": "Claude", "kind": "unknown"},
            ],
            sents,
        )
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]["wrong"], "Claude")

    def test_reuse_requires_matching_source_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "demo.mp4"
            video.write_bytes(b"video-a")
            signature = MODULE.source_signature(video, 0, "zh")
            output = root / "work"
            MODULE.prepare_run_meta(output, signature, reuse=False)
            self.assertEqual(
                json.loads((output / "run-meta.json").read_text()), signature
            )
            MODULE.prepare_run_meta(output, signature, reuse=True)
            video.write_bytes(b"video-b")
            with self.assertRaises(RuntimeError):
                MODULE.prepare_run_meta(
                    output, MODULE.source_signature(video, 0, "zh"), reuse=True
                )

    def test_reused_suspects_are_revalidated(self):
        sents = [{"text": "打开 Claude", "begin_time": 0, "end_time": 1000}]
        valid = MODULE.validate_suspects(
            [
                {"sid": 0, "wrong": "Claude", "kind": "screen"},
                {"sid": 9, "wrong": "Claude", "kind": "screen"},
            ],
            sents,
        )
        self.assertEqual([item["sid"] for item in valid], [0])


if __name__ == "__main__":
    unittest.main()
