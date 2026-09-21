#!/usr/bin/env python3
"""
subfix —— 百炼一步视觉字幕纠错。

  视频 ──► FunAudio ASR(句级+词级时间戳)
        ──► qwen3.8-max 读取整段视频并返回最小术语替换
        ──► 词级时间戳断句 + 去水词
        ──► corrected.srt + transcript.json + 证据报告

用法(bl 已登录即可,无需额外设置密钥):
  python3 subfix.py <video.mp4> [--out DIR] [--max-seconds N] [--lang zh]

依赖: bl (百炼 CLI,自带认证)、ffmpeg
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path


FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
CORRECTION_MODEL = os.environ.get("SUBFIX_CORRECTION_MODEL", "qwen3.8-max")
FILLER_MODEL = "qwen-plus"
MODEL_BATCH_CHARS = 12000
WORKERS = int(os.environ.get("SUBFIX_WORKERS", "8"))
_SEM = threading.Semaphore(WORKERS)
KNOWN_ARTIFACTS = {
    "run-meta.json", "asr.json", "correction.json",
    "corrected.srt", "transcript.json", "corrected.json", "report.json",
    "report.md",
}


def bl(args, retries=3):
    for attempt in range(retries):
        with _SEM:
            process = subprocess.run(["bl"] + args, capture_output=True, text=True)
        if process.returncode == 0:
            return process.stdout
        sys.stderr.write(
            f"[retry {attempt + 1}] bl {args[:2]}: {process.stderr[:160]}\n"
        )
        subprocess.run(["sleep", "3"])
    raise RuntimeError(f"bl failed: {args[:3]}")


def content(stdout):
    return json.loads(stdout)["choices"][0]["message"]["content"]


def extract_json(text):
    text = re.sub(r"^```[a-z]*\n?", "", text.strip())
    text = re.sub(r"\n?```$", "", text).strip()
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
            return value
        except json.JSONDecodeError:
            continue
    raise json.JSONDecodeError("未找到合法 JSON", text, 0)


def ms_to_srt(ms):
    hours, ms = divmod(int(ms), 3600000)
    minutes, ms = divmod(ms, 60000)
    seconds, ms = divmod(ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


def write_text(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path, payload):
    write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def source_signature(video, max_seconds, lang):
    """生成来源视频、语言和试跑范围的可复用指纹。"""
    path = Path(video).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"视频不存在: {path}")
    stat = path.stat()
    digest = hashlib.sha256()
    edge_size = 1024 * 1024
    with path.open("rb") as stream:
        digest.update(stream.read(edge_size))
        if stat.st_size > edge_size:
            stream.seek(max(0, stat.st_size - edge_size))
            digest.update(stream.read(edge_size))
    return {
        "video": str(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "edge_sha256": digest.hexdigest(),
        "max_seconds": int(max_seconds or 0),
        "language": lang,
    }


def prepare_run_meta(out_dir, signature, reuse):
    out_dir = Path(out_dir)
    meta_path = out_dir / "run-meta.json"
    if reuse:
        if not meta_path.exists():
            raise RuntimeError("--reuse 缺少 run-meta.json，无法确认中间产物属于当前视频")
        try:
            existing = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取 run-meta.json: {exc}") from exc
        if existing != signature:
            raise RuntimeError("--reuse 的视频、语言或试跑范围与原任务不一致，请使用新的 --out")
        return
    if out_dir.exists() and any((out_dir / name).exists() for name in KNOWN_ARTIFACTS):
        raise RuntimeError("输出目录已有字幕产物；请使用 --reuse 恢复，或指定新的 --out")
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(meta_path, signature)


def batches(items, render, max_chars=MODEL_BATCH_CHARS):
    """按提示词字符数切分低风险文本任务，同时保留原始条目。"""
    current, size = [], 0
    for item in items:
        rendered = render(item)
        if current and size + len(rendered) > max_chars:
            yield current
            current, size = [], 0
        current.append(item)
        size += len(rendered)
    if current:
        yield current


# ---------------- 1. ASR ----------------
def run_asr(video, out_dir, max_seconds, lang):
    wav = os.path.join(out_dir, "audio.wav")
    command = [FFMPEG, "-y", "-i", video]
    if max_seconds:
        command += ["-t", str(max_seconds)]
    command += ["-ar", "16000", "-ac", "1", wav]
    process = subprocess.run(command, capture_output=True, text=True)
    if process.returncode != 0 or not Path(wav).is_file():
        detail = (process.stderr or "").strip()[-500:]
        raise RuntimeError(f"ffmpeg 提取音频失败{(': ' + detail) if detail else ''}")
    asr_json = os.path.join(out_dir, "asr.json")
    bl(["speech", "recognize", "--url", wav, "--language", lang,
        "--out", asr_json, "--quiet"])
    data = json.loads(Path(asr_json).read_text(encoding="utf-8"))
    return data["transcripts"][0]["sentences"]


# ---------------- 2. 一步视觉纠错 ----------------
CORRECTION_USER = """你是技术视频字幕的事实校对器。请一次读取整段视频，并结合下面带时间范围的 ASR 句子找出真正的听错词。

只输出严格 JSON 对象，不要 Markdown、解释或思维过程：
{{"items":[{{"sid":0,"changes":[{{"wrong":"原文片段","correct":"最小替换","kind":"screen","change_type":"term","reason":"一句话理由","evidence":"画面中逐字可见的文字"}}]}}]}}

要求：
1. 每个 ASR 句子都要有一个 item；没有确认错误时 changes 为 []。只用对应句子的时间范围取证，不要用其他时间段的文字替代它。
2. 只输出真正的 ASR 错词、专有名词大小写/写法错误、命令、代码、文件名或界面文字错误。不要润色，不要改语气，不要补全，不要改写句子。
3. change_type 只能是 term 或 orthography。标点、空格、断句、语法、口语选择和“更顺口”的修改禁止输出。
4. screen：correct 必须能在对应时间范围的画面中逐字读到，evidence 必须引用画面原文；logo、图标、常识或猜测不能算证据。semantic：纯语义同音错，没有画面证据，默认只待人工确认。
5. wrong 必须是对应 ASR 句子的连续原文片段；correct 只能替换 wrong，不要带入相邻文字。原文是对的就不要输出。
6. 按 ASR 句子从前到后逐句检查，先检查画面里的产品名、插件名、命令、文件名、模型名，再决定 changes；不要因为只差大小写、连字符或英文单词之间的空格就跳过明确的 screen 术语。每个明确的 screen 写法差异都要返回一个最小替换。例如 ASR 的“oil codex title”与画面中的“oil-codex-title”必须返回为一个 orthography 替换。
7. 不要把“登录、表单、布局优化”改成“登录表单、布局优化”，不要把“一会”改成“一会儿”；这类属于表达或标点调整，不是 ASR 错词。

ASR 句子：
{lines}"""


def _is_format_only(old, new):
    format_chars = set(" \t\r\n，。！？、；：…,.!?;:()（）[]【】{}<>《》‘’“”\"'—-·|｜/")
    changed = (old or "") + (new or "")
    return bool(changed) and all(char in format_chars for char in changed)


def _without_format(text):
    format_chars = set(" \t\r\n，。！？、；：…,.!?;:()（）[]【】{}<>《》‘’“”\"'—-·|｜/")
    return "".join(char for char in text if char not in format_chars)


def _valid_change(change, sentence, allow_semantic_auto=False):
    if not isinstance(change, dict):
        return None
    wrong = change.get("wrong")
    correct_text = change.get("correct")
    kind = change.get("kind")
    change_type = change.get("change_type")
    evidence = str(change.get("evidence") or "").strip()
    if not isinstance(wrong, str) or not wrong.strip() or wrong not in sentence:
        return None
    if not isinstance(correct_text, str) or not correct_text.strip() or "\n" in correct_text:
        return None
    if (
        correct_text == wrong
        or _is_format_only(wrong, correct_text)
        or _without_format(wrong) == _without_format(correct_text)
    ):
        return None
    if kind not in {"screen", "semantic"} or change_type not in {"term", "orthography"}:
        return None
    if kind == "screen" and not evidence:
        return None
    if kind == "semantic" and not allow_semantic_auto:
        return {
            "wrong": wrong,
            "final": wrong,
            "kind": kind,
            "reason": str(change.get("reason") or ""),
            "via": "semantic-pending",
            "needs_review": True,
            "guess_only": correct_text,
        }
    return {
        "wrong": wrong,
        "final": correct_text,
        "kind": kind,
        "reason": str(change.get("reason") or ""),
        "via": f"{CORRECTION_MODEL}-video",
        "evidence": evidence,
    }


def validate_corrections(payload, sents, allow_semantic_auto=False):
    """只接受模型返回的连续最小替换，不把整句润色写回字幕。"""
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise RuntimeError("视觉纠错模型没有返回 items 数组")
    by_sid = {}
    for item in items:
        if not isinstance(item, dict) or type(item.get("sid")) is not int:
            continue
        by_sid.setdefault(item["sid"], item)
    results = []
    for sid, sentence in enumerate(sents):
        item = by_sid.get(sid) or {}
        changes = item.get("changes")
        if not isinstance(changes, list):
            continue
        seen = set()
        for raw_change in changes:
            change = _valid_change(
                raw_change,
                str(sentence.get("text") or ""),
                allow_semantic_auto=allow_semantic_auto,
            )
            if not change or change["wrong"] in seen:
                continue
            seen.add(change["wrong"])
            results.append({"sid": sid, **change})
    return results


def correct_video(video, sents, allow_semantic_auto=False):
    lines = "\n".join(
        json.dumps({
            "sid": sid,
            "start_ms": sentence.get("begin_time"),
            "end_ms": sentence.get("end_time"),
            "text": sentence.get("text", ""),
        }, ensure_ascii=False)
        for sid, sentence in enumerate(sents)
    )
    output = bl([
        "vision", "describe", "--model", CORRECTION_MODEL,
        "--video", video, "--prompt", CORRECTION_USER.format(lines=lines),
        "--output", "json", "--quiet",
    ])
    return validate_corrections(
        extract_json(content(output)),
        sents,
        allow_semantic_auto=allow_semantic_auto,
    )


# ---------------- 3. 断句:用词级时间戳按标点拆长句 ----------------
MAX_CHARS = 24
MIN_BREAK_CHARS = 12
MAX_DUR_MS = 6000
TAIL_MERGE = 6
BREAK_PUNCT = set("，。、；？！,.;?!")


def split_words(sent):
    words = sent.get("words") or []
    if not words:
        return [{"begin_time": sent["begin_time"], "end_time": sent["end_time"], "text": sent["text"]}]
    pieces, current = [], []

    def flush():
        if not current:
            return
        text = "".join(word["text"] + (word.get("punctuation") or "") for word in current).strip()
        pieces.append({"begin_time": current[0]["begin_time"], "end_time": current[-1]["end_time"], "text": text})

    for word in words:
        current.append(word)
        chars = sum(len(item["text"]) for item in current)
        duration = word["end_time"] - current[0]["begin_time"]
        punctuation = word.get("punctuation") or ""
        at_break = bool(punctuation) and punctuation[-1] in BREAK_PUNCT
        if (at_break and chars >= MIN_BREAK_CHARS) or chars >= MAX_CHARS or duration >= MAX_DUR_MS:
            flush()
            current = []
    flush()

    merged = []
    for piece in pieces:
        bare = piece["text"].strip("，。、；？！,.;?! ")
        if merged and len(bare) < TAIL_MERGE:
            merged[-1]["text"] += piece["text"]
            merged[-1]["end_time"] = piece["end_time"]
        else:
            merged.append(piece)
    return merged


# ---------------- 4. 去水词 ----------------
FILLER_SYS = "你是中文视频字幕的顺滑校对,只删水词,不改实义内容。"
FILLER_USER = """下面是按行编号的字幕。请去掉每行里的“水词”，让字幕更干净易读。

只删除:语气填充词(呃、啊、嗯、诶、唉、哦、噢)、明显的口吃重复(你你→你、就就→就、这个这个→这个、这里这里→这里)、纯语气的“那个/就是说”填充。

绝对不要:改动任何技术术语/产品名/数字/英文/代码;增删或改写实义内容;改变原意;合并或拆分行。某行本来就干净则原样返回。

严格按原编号、原条数返回 JSON 数组,每个元素是该行清理后的纯文本字符串(顺序与条数完全一致)。只输出 JSON 数组。

字幕:
{lines}"""
_FILLERS = ["呃", "啊", "嗯", "诶", "唉", "噢", "哦"]


def safe_filler_edit(before, after):
    """只接受删除水词、空白或相邻重复字的结果。"""
    from difflib import SequenceMatcher

    for tag, i1, i2, _j1, _j2 in SequenceMatcher(None, before, after).get_opcodes():
        if tag == "equal":
            continue
        if tag != "delete":
            return False
        for index in range(i1, i2):
            char = before[index]
            if char in _FILLERS or char.isspace():
                continue
            previous = before[index - 1] if index else ""
            following = before[index + 1] if index + 1 < len(before) else ""
            if char != previous and char != following:
                return False
    return True


def _rule_clean(text):
    for filler in _FILLERS:
        text = text.replace("，" + filler + "，", "，").replace(filler + "，", "").replace("，" + filler, "")
        text = text.replace(filler, "")
    text = re.sub(r"([一-龥])\1{1,}", r"\1", text)
    return re.sub(r"\s{2,}", " ", text).strip("， ").strip()


def clean_fillers(segs):
    cleaned = {}
    indexed = list(enumerate(segs))
    for chunk in batches(indexed, lambda item: f"{item[1]['text']}\n"):
        lines = "\n".join(f"{index}: {segment['text']}" for index, segment in chunk)
        try:
            output = bl([
                "text", "chat", "--model", FILLER_MODEL,
                "--system", FILLER_SYS,
                "--message", FILLER_USER.format(lines=lines),
                "--max-tokens", "5000", "--output", "json",
            ])
            data = extract_json(content(output))
            if isinstance(data, list) and len(data) == len(chunk):
                for (index, segment), candidate in zip(chunk, data):
                    candidate = candidate if isinstance(candidate, str) else (
                        candidate.get("text") if isinstance(candidate, dict) else None
                    )
                    if candidate is not None and safe_filler_edit(segment["text"], candidate):
                        cleaned[index] = candidate
        except Exception as exc:
            sys.stderr.write(f"  去水词(qwen)失败,回退规则法: {exc}\n")
    result = []
    for index, segment in enumerate(segs):
        text = (cleaned[index] if index in cleaned else _rule_clean(segment["text"])).strip()
        if text:
            segment = dict(segment)
            segment["text"] = text
            result.append(segment)
    return result


# ---------------- 5. 应用纠正 + 断句 + 产出 ----------------
def build_outputs(sents, results, out_dir):
    corrected = [dict(sentence) for sentence in sents]
    changes = []
    for result in results:
        sid, wrong, final = result["sid"], result["wrong"], result["final"]
        if final and final != wrong and wrong in corrected[sid]["text"]:
            corrected[sid]["text"] = corrected[sid]["text"].replace(wrong, final, 1)
            changes.append(result)

    segments = []
    for sid, sentence in enumerate(sents):
        replacements = [(item["wrong"], item["final"]) for item in changes if item["sid"] == sid]
        for piece in split_words(sentence):
            for wrong, final in replacements:
                if wrong in piece["text"]:
                    piece["text"] = piece["text"].replace(wrong, final, 1)
            segments.append(piece)

    segments = clean_fillers(segments)
    srt = []
    for index, segment in enumerate(segments, 1):
        srt.append(
            f"{index}\n{ms_to_srt(segment['begin_time'])} --> "
            f"{ms_to_srt(segment['end_time'])}\n{segment['text']}\n"
        )
    write_text(Path(out_dir, "corrected.srt"), "\n".join(srt))

    preview = [
        {"start": round(segment["begin_time"] / 1000, 3),
         "end": round(segment["end_time"] / 1000, 3),
         "text": segment["text"]}
        for segment in segments
    ]
    write_json(Path(out_dir, "transcript.json"), preview)
    write_json(Path(out_dir, "corrected.json"), corrected)
    write_json(Path(out_dir, "report.json"), results)

    review = [result for result in results if result.get("needs_review")]
    report = [
        "# 字幕纠错报告\n",
        f"共标记 {len(results)} 处可疑,有画面证据并修改 {len(changes)} 处,"
        f"取不到证据保留原文待人工确认 {len(review)} 处。\n",
        "## 已修改(均有画面铁证)",
    ]
    for result in changes:
        evidence = f"\n    画面证据: {result['evidence']}" if result.get("evidence") else ""
        report.append(
            f"- 句{result['sid']} [{result['kind']}/{result['via']}] "
            f"「{result['wrong']}」→「{result['final']}」{evidence}"
        )
    if review:
        report.append("\n## 待人工确认(没有自动采用的语义候选)")
        for result in review:
            guess = f",模型猜测可能是「{result['guess_only']}」" if result.get("guess_only") else ""
            report.append(
                f"- 句{result['sid']} 「{result['wrong']}」 可疑{guess}"
                f"(原因: {result.get('reason', '')})"
            )
    write_text(Path(out_dir, "report.md"), "\n".join(report))
    return segments, changes, review


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("video")
    parser.add_argument("--out", default=None)
    parser.add_argument("--max-seconds", type=int, default=0)
    parser.add_argument("--lang", default="zh")
    parser.add_argument(
        "--allow-semantic-auto", action="store_true",
        help="允许自动采用纯语义纠错猜测；默认保留待人工确认",
    )
    parser.add_argument(
        "--reuse", action="store_true",
        help="复用 out 目录已有的 asr.json，只重跑整段视频纠错",
    )
    args = parser.parse_args()

    if args.max_seconds < 0:
        parser.error("--max-seconds 不能为负数")
    try:
        signature = source_signature(args.video, args.max_seconds, args.lang)
        out_dir = Path(args.out or (os.path.splitext(args.video)[0] + ".subfix")).expanduser()
        prepare_run_meta(out_dir, signature, args.reuse)
    except (FileNotFoundError, RuntimeError) as exc:
        parser.error(str(exc))

    asr_path = out_dir / "asr.json"
    correction_path = out_dir / "correction.json"
    if args.reuse and asr_path.exists():
        print("[1/3] 复用 asr.json")
        sents = json.loads(asr_path.read_text(encoding="utf-8"))["transcripts"][0]["sentences"]
    else:
        print("[1/3] FunAudio ASR …")
        sents = run_asr(args.video, out_dir, args.max_seconds, args.lang)
    print(f"      {len(sents)} 句")

    print(f"[2/3] {CORRECTION_MODEL} 读取整段视频并纠正 …")
    results = correct_video(args.video, sents, args.allow_semantic_auto)
    write_json(correction_path, results)
    print(f"      返回 {len(results)} 处模型候选")

    print("[3/3] 断句顺滑(拆长句+去水词) + 产出 SRT/报告 …")
    segments, changes, review = build_outputs(sents, results, out_dir)

    print(f"\n========== 已修改 {len(changes)} 处(均有画面证据) ==========")
    for result in changes:
        evidence = f"  | {result.get('evidence', '')[:46]}" if result.get("evidence") else ""
        print(
            f"句{result['sid']:>2} [{result['via']:22s}] "
            f"「{result['wrong']}」→「{result['final']}」{evidence}"
        )
    if review:
        print(f"\n---------- 保留原文待确认 {len(review)} 处(没有自动采用) ----------")
        for result in review:
            guess = f" 猜「{result['guess_only']}」" if result.get("guess_only") else ""
            print(f"句{result['sid']:>2} 「{result['wrong']}」{guess}")
    print(f"\n{len(sents)} 句 → 断句顺滑后 {len(segments)} 条字幕")
    print(f"输出目录: {out_dir}")
    print("  corrected.srt / transcript.json / corrected.json / report.md / report.json")


if __name__ == "__main__":
    main()
