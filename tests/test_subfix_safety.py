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
    def test_extract_json_ignores_trailing_model_text(self):
        self.assertEqual(
            MODULE.extract_json('{"items": []}\n补充说明：没有发现需要修改的内容。'),
            {"items": []},
        )

    def test_screen_term_is_accepted(self):
        sents = [{"text": "打开 codex", "begin_time": 0, "end_time": 1000}]
        result = MODULE.validate_corrections(
            {
                "items": [{
                    "sid": 0,
                    "changes": [{
                        "wrong": "codex",
                        "correct": "Codex",
                        "kind": "screen",
                        "change_type": "orthography",
                        "evidence": "画面标题：Codex",
                    }],
                }],
            },
            sents,
        )
        self.assertEqual(result[0]["final"], "Codex")
        self.assertFalse(result[0].get("needs_review", False))

    def test_semantic_guess_waits_for_review_by_default(self):
        sents = [{"text": "这是飞树项目", "begin_time": 0, "end_time": 1000}]
        result = MODULE.validate_corrections(
            {
                "items": [{
                    "sid": 0,
                    "changes": [{
                        "wrong": "飞树",
                        "correct": "飞书",
                        "kind": "semantic",
                        "change_type": "term",
                        "reason": "同音猜测",
                        "evidence": "",
                    }],
                }],
            },
            sents,
        )
        self.assertEqual(result[0]["final"], "飞树")
        self.assertTrue(result[0]["needs_review"])
        self.assertEqual(result[0]["guess_only"], "飞书")

    def test_punctuation_and_grammar_candidates_are_rejected(self):
        sents = [{"text": "登录、表单、布局优化，一会完成", "begin_time": 0, "end_time": 1000}]
        result = MODULE.validate_corrections(
            {
                "items": [{
                    "sid": 0,
                    "changes": [
                        {
                            "wrong": "登录、表单、布局优化",
                            "correct": "登录表单、布局优化",
                            "kind": "screen",
                            "change_type": "orthography",
                            "evidence": "画面文字",
                        },
                        {
                            "wrong": "一会",
                            "correct": "一会儿",
                            "kind": "semantic",
                            "change_type": "grammar",
                            "evidence": "",
                        },
                    ],
                }],
            },
            sents,
        )
        self.assertEqual(result, [])

    def test_invalid_changes_are_discarded(self):
        sents = [{"text": "打开 Claude", "begin_time": 0, "end_time": 1000}]
        result = MODULE.validate_corrections(
            {
                "items": [{
                    "sid": 0,
                    "changes": [
                        {
                            "wrong": "Claude",
                            "correct": "Claude",
                            "kind": "screen",
                            "change_type": "term",
                            "evidence": "画面文字",
                        },
                        {
                            "wrong": "不存在",
                            "correct": "Codex",
                            "kind": "screen",
                            "change_type": "term",
                            "evidence": "画面文字",
                        },
                        {
                            "wrong": "Claude",
                            "correct": "Codex",
                            "kind": "unknown",
                            "change_type": "term",
                            "evidence": "画面文字",
                        },
                        {
                            "wrong": "Claude",
                            "correct": "Codex",
                            "kind": "screen",
                            "change_type": "term",
                            "evidence": "",
                        },
                    ],
                }, {"sid": 99, "changes": []}],
            },
            sents,
        )
        self.assertEqual(result, [])

    def test_filler_model_cannot_change_meaning(self):
        self.assertTrue(MODULE.safe_filler_edit("呃你好", "你好"))
        self.assertTrue(MODULE.safe_filler_edit("你你看", "你看"))
        self.assertFalse(MODULE.safe_filler_edit("打开 Claude", "打开 Codex"))
        self.assertFalse(MODULE.safe_filler_edit("评测得分 9.8", "评测得分 98"))

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


if __name__ == "__main__":
    unittest.main()
