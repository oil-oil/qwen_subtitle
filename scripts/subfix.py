#!/usr/bin/env python3
"""
subfix —— 百炼三段式字幕纠错(纯工作流,一步到位)

  视频 ──► FunAudio ASR(词级时间戳)
        ──► qwen3.7-max 高精度标错(只标明显听错)
        ──► qwen-vl 按时间戳抽帧、读屏纠正
        ──► 修正后 SRT + transcript.json + 证据报告

用法(bl 已登录即可,**无需设任何密钥/环境变量**):
  python3 subfix.py <video.mp4> [--out DIR] [--max-seconds N] [--lang zh]

依赖: bl (百炼 CLI,自带认证)、ffmpeg
"""
import argparse, hashlib, json, subprocess, sys, re, os, shutil
from pathlib import Path

FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"

# 模型分工(实测:max 标错最准但慢108s;plus 快5×但精度崩。故精度命门用max,低风险用plus)
FLAG_MODEL = "qwen3.7-max"   # 标错:精度命门
VL_MODEL = "qwen3-vl-plus"   # 看帧纠正
FILLER_MODEL = "qwen-plus"   # 去水词:低风险,plus 够用且快


from concurrent.futures import ThreadPoolExecutor
import threading
WORKERS = int(os.environ.get("SUBFIX_WORKERS", "8"))   # 全局并发上限
_SEM = threading.Semaphore(WORKERS)                     # 限同时在飞的 bl 调用
MODEL_BATCH_CHARS = 12000
KNOWN_ARTIFACTS = {
    "run-meta.json", "asr.json", "suspects.json", "corrected.srt",
    "transcript.json", "corrected.json", "report.json", "report.md",
}

def pmap(fn, items):
    items = list(items)
    if len(items) <= 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        return list(ex.map(fn, items))                 # 保序

def bl(args, retries=3):
    for a in range(retries):
        with _SEM:
            p = subprocess.run(["bl"] + args, capture_output=True, text=True)
        if p.returncode == 0:
            return p.stdout
        sys.stderr.write(f"[retry {a+1}] bl {args[:2]}: {p.stderr[:160]}\n")
        subprocess.run(["sleep", "3"])
    raise RuntimeError(f"bl failed: {args[:3]}")


def content(stdout):
    return json.loads(stdout)["choices"][0]["message"]["content"]


def extract_json(text):
    text = re.sub(r"^```[a-z]*\n?", "", text.strip())
    text = re.sub(r"\n?```$", "", text).strip()
    m = re.search(r"(\[.*\]|\{.*\})", text, re.S)
    return json.loads(m.group(1)) if m else json.loads(text)


def ms_to_srt(ms):
    h, ms = divmod(int(ms), 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_json(path, payload):
    """原子写入 JSON 产物，避免中断时留下半个文件。"""
    write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def write_text(path, content):
    """原子写入文本产物，避免中断时留下半个文件。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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
    """按提示词字符数切分模型输入，同时保留原始条目。"""
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
    cmd = [FFMPEG, "-y", "-i", video]
    if max_seconds:
        cmd += ["-t", str(max_seconds)]
    cmd += ["-ar", "16000", "-ac", "1", wav]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0 or not Path(wav).is_file():
        detail = (result.stderr or "").strip()[-500:]
        raise RuntimeError(f"ffmpeg 提取音频失败{(': ' + detail) if detail else ''}")
    asr_json = os.path.join(out_dir, "asr.json")
    bl(["speech", "recognize", "--url", wav, "--language", lang,
        "--out", asr_json, "--quiet"])
    data = json.loads(Path(asr_json).read_text(encoding="utf-8"))
    return data["transcripts"][0]["sentences"]


# ---------------- 2. 高精度标错 ----------------
FLAG_SYS = "你是中文技术视频字幕的资深校对。字幕由语音识别(ASR)生成。你只做一件事:找出明显被听错的地方。不润色、不补全、不规整。"

FLAG_USER = """下面是按句编号的字幕。请标记所有"明显被听错"的位置。

【该标记 —— 真实术语/名称被念岔、听错】
- 专有名词/产品名被听成发音相近的错形:cloud→Claude、class q→Claude、play right→Playwright。
- 文件名/命令被口语化或听错:把 "SKILL.md" 念成 "skill 点 md"(“点”就是口述的小数点)、"html2pptx" 听成 "html to ppt"、"OOCML" 实为 "OOXML"。
- 中文同音字错误、词义不通:原数据→元数据、飞树→飞书。

【绝不标记 —— 否则就是过度纠正】
- 本身正确、只是"可能更具体"的词:说话人说 "ooxml" 就是 ooxml,不要因为屏幕上有 "ooxml.md" 就标它。不补后缀、不补全、不扩写。判断标准:说话人嘴上发出的音对应的词本身有没有错;有错才标,只是"不够具体"不标。
- 读起来通顺、像一个名字/词的中文(哪怕你怀疑它是某英文产品的音译)。例:"超级麦吉" 读着就是个产品名,不要因为你猜它可能是 SuperAGI/Supermaven 就标它。只有当中文本身明显不通、是同音错字时才标(如 飞树→飞书、原数据→元数据)。
- 语气词、口语重复、不影响理解的口误。

【kind 分类规则】
- 只要正确写法是英文/产品名/文件名/命令/代码/界面文字这类"屏幕上能查到"的,一律填 "screen"(交给画面取证,即使你已经很确定)。
- 仅当是纯中文同音错、画面上不会出现对应文字时,才填 "semantic"。

对每个可疑点输出对象:
- sid: 句子编号(整数)
- wrong: 听错的原文片段(尽量短)
- reason: 一句话理由
- kind: "screen" 或 "semantic"(按上面规则)
- guess: 修正猜测(screen 类最终以画面为准)

只输出 JSON 数组,无多余文字。若没有任何可疑点,输出 []。

字幕:
{lines}"""


def validate_suspects(items, sents):
    if not isinstance(items, list):
        raise RuntimeError("标错模型没有返回 JSON 数组")
    valid, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            continue
        sid = item.get("sid")
        wrong = item.get("wrong")
        kind = item.get("kind")
        if type(sid) is not int or not 0 <= sid < len(sents):
            continue
        if not isinstance(wrong, str) or not wrong.strip() or wrong not in sents[sid].get("text", ""):
            continue
        if kind not in {"screen", "semantic"}:
            continue
        key = (sid, wrong)
        if key in seen:
            continue
        seen.add(key)
        valid.append({
            "sid": sid,
            "wrong": wrong,
            "reason": str(item.get("reason") or ""),
            "kind": kind,
            "guess": str(item.get("guess") or ""),
        })
    return valid


def flag_suspects(sents):
    suspects = []
    indexed = list(enumerate(sents))
    for chunk in batches(indexed, lambda item: f"{item[1]['text']}\n"):
        lines = "\n".join(f"{index}: {sent['text']}" for index, sent in chunk)
        out = bl(["text", "chat", "--model", FLAG_MODEL, "--system", FLAG_SYS,
                  "--message", FLAG_USER.format(lines=lines),
                  "--max-tokens", "3000", "--output", "json"])
        result = extract_json(content(out))
        if not isinstance(result, list):
            raise RuntimeError("标错模型返回的不是 JSON 数组")
        suspects.extend(result)
    return validate_suspects(suspects, sents)


# ---------------- 3. VL 看帧纠正 ----------------
VL_PROMPT = """这是一段录屏视频的一帧画面。该处字幕(语音识别)写的是:
"{sent}"
其中 "{wrong}" 疑似被听错。请看画面上显示的文字(代码/界面/文件名/标题/按钮等),判断 "{wrong}" 真实应该写成什么。

铁律(违反任何一条都要把 found 设为 false):
1. 只能依据画面上能**逐字读到的文字**。如果你是靠图标、logo、配色、氛围或常识"推断"出来的(画面上并没有把这个词写出来),必须 found=false,绝不许猜。
2. correct 必须是 "{wrong}" 的**最小替换**:只替换听错的那几个字,长度/范围对齐,不要把相邻的路径段、文件夹名、按钮名也带进来。例:画面路径是 'claude skill',但 "class q" 对应的只是产品名,correct 应为 "Claude" 而不是 "claude skill"。
3. 大小写:知名产品/专有名词用其规范写法(Claude、Codex、GitHub、Node.js),即使画面某处是小写的文件夹名也用规范大小写。
4. 不补全、不扩写:不要因为画面上有带后缀的变体(如 ooxml.md)就给原文 ooxml 加后缀。
5. 若画面证据表明原文其实没错,让 correct 完全等于原文。
6. 发音一致: correct 必须是 "{wrong}" 的"读音还原"——即原文是这个词被听错的结果,两者读音必须对得上(cloud↔Claude、at↔@、class q↔Claude 都是同音/近音)。如果你在画面找到的词与 "{wrong}" 的读音明显对不上(例如 "超级麦吉" 配 "Supermario"——mài-jí 和 ma-rio 读音不符),那它就不是同一个词,必须 found=false。

只返回 JSON: {{"found": true 或 false, "correct": "最终写法", "evidence": "在画面哪里逐字读到的"}}
任何不确定一律 found=false(保留原文好过改错)。"""


def word_time_ms(sent, wrong):
    for w in sent.get("words", []):
        if w.get("text") and (w["text"] in wrong or wrong in w["text"]):
            return (w["begin_time"] + w["end_time"]) // 2
    return (sent["begin_time"] + sent["end_time"]) // 2


def grab_frame(video, ms, path):
    subprocess.run([FFMPEG, "-y", "-ss", f"{ms/1000:.2f}", "-i", video,
                    "-frames:v", "1", "-q:v", "2", path], capture_output=True)
    return path


def correct(video, sents, suspects, frames_dir, allow_semantic_auto=False):
    # 各 suspect 相互独立 → 并发看帧纠正(每个内部 3 帧仍按命中即停顺序试)
    def one(idx_s):
        idx, s = idx_s
        sent = sents[s["sid"]]
        if s["kind"] != "screen":
            if not allow_semantic_auto:
                return {**s, "final": s["wrong"], "via": "semantic-pending",
                        "needs_review": True, "guess_only": s.get("guess", "")}
            return {**s, "final": s.get("guess", s["wrong"]), "via": "semantic"}
        decided = None
        for j, ms in enumerate([word_time_ms(sent, s["wrong"]),
                                sent["begin_time"], sent["end_time"]]):
            img = grab_frame(video, ms, os.path.join(frames_dir, f"s{idx}_{j}.jpg"))
            try:
                vj = extract_json(content(bl(["vision", "describe", "--model", VL_MODEL, "--image", img,
                    "--prompt", VL_PROMPT.format(sent=sent["text"], wrong=s["wrong"]),
                    "--output", "json", "--quiet"])))
            except Exception as e:
                sys.stderr.write(f"  VL parse fail s{idx}: {e}\n"); continue
            correct_text = vj.get("correct") if isinstance(vj, dict) else None
            evidence = vj.get("evidence") if isinstance(vj, dict) else None
            if (vj.get("found") is True and isinstance(correct_text, str)
                    and correct_text.strip() and "\n" not in correct_text
                    and isinstance(evidence, str) and evidence.strip()):
                decided = {**s, "final": vj["correct"], "via": f"vl@{ms/1000:.1f}s",
                           "evidence": evidence,
                           "frame": os.path.basename(img)}
                break
        if not decided:
            # 零误改原则: screen 类取不到画面证据,保留原文 + 标记待确认,
            # 绝不套用 qwen-max 的盲猜(那正是 超级麦吉→超级码力 这类误改的来源)。
            decided = {**s, "final": s["wrong"], "via": "kept-no-evidence",
                       "needs_review": True, "guess_only": s.get("guess", "")}
        return decided
    return pmap(one, list(enumerate(suspects)))


# ---------------- 4a. 断句:用词级时间戳按标点拆长句 ----------------
MAX_CHARS = 24       # 单条字幕最大字数
MIN_BREAK_CHARS = 12 # 到标点且已够这么长就断
MAX_DUR_MS = 6000    # 单条最大时长
TAIL_MERGE = 6       # 过短尾巴并入上一条
BREAK_PUNCT = set("，。、；？！,.;?!")

def split_words(sent):
    words = sent.get("words") or []
    if not words:
        return [{"begin_time": sent["begin_time"], "end_time": sent["end_time"], "text": sent["text"]}]
    pieces, cur = [], []
    def flush():
        if not cur:
            return
        text = "".join(w["text"] + (w.get("punctuation") or "") for w in cur).strip()
        pieces.append({"begin_time": cur[0]["begin_time"], "end_time": cur[-1]["end_time"], "text": text})
    for w in words:
        cur.append(w)
        chars = sum(len(x["text"]) for x in cur)
        dur = w["end_time"] - cur[0]["begin_time"]
        punc = (w.get("punctuation") or "")
        brk = bool(punc) and punc[-1] in BREAK_PUNCT
        if (brk and chars >= MIN_BREAK_CHARS) or chars >= MAX_CHARS or dur >= MAX_DUR_MS:
            flush(); cur = []
    flush()
    merged = []
    for p in pieces:
        bare = p["text"].strip("，。、；？！,.;?! ")
        if merged and len(bare) < TAIL_MERGE:
            merged[-1]["text"] += p["text"]
            merged[-1]["end_time"] = p["end_time"]
        else:
            merged.append(p)
    return merged


# ---------------- 4b. 去水词(qwen 顺滑,失败回退规则法) ----------------
FILLER_SYS = "你是中文视频字幕的顺滑校对,只删水词,不改实义内容。"
FILLER_USER = """下面是按行编号的字幕。请去掉每行里的"水词",让字幕更干净易读。

只删除:语气填充词(呃、啊、嗯、诶、唉、哦、噢)、明显的口吃重复(你你→你、就就→就、这个这个→这个、这里这里→这里)、纯语气的"那个/就是说"填充。

绝对不要:改动任何技术术语/产品名/数字/英文/代码;增删或改写实义内容;改变原意;合并或拆分行。某行本来就干净则原样返回。

严格按原编号、原条数返回 JSON 数组,每个元素是该行清理后的纯文本字符串(顺序与条数必须和输入完全一致)。只输出 JSON 数组。

字幕:
{lines}"""

import re as _re2
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


def _rule_clean(t):
    for f in _FILLERS:
        t = t.replace("，" + f + "，", "，").replace(f + "，", "").replace("，" + f, "")
        t = t.replace(f, "")
    t = _re2.sub(r"([一-龥])\1{1,}", r"\1", t)  # 叠字口吃: 你你→你
    return _re2.sub(r"\s{2,}", " ", t).strip("， ").strip()

def clean_fillers(segs):
    cleaned = {}
    indexed = list(enumerate(segs))
    for chunk in batches(indexed, lambda item: f"{item[1]['text']}\n"):
        lines = "\n".join(f"{index}: {segment['text']}" for index, segment in chunk)
        try:
            out = bl(["text", "chat", "--model", FILLER_MODEL, "--system", FILLER_SYS,
                      "--message", FILLER_USER.format(lines=lines),
                      "--max-tokens", "5000", "--output", "json"])
            data = extract_json(content(out))
            if isinstance(data, list) and len(data) == len(chunk):
                for (index, segment), candidate in zip(chunk, data):
                    candidate = candidate if isinstance(candidate, str) else (
                        candidate.get("text") if isinstance(candidate, dict) else None
                    )
                    if candidate is not None and safe_filler_edit(segment["text"], candidate):
                        cleaned[index] = candidate
        except Exception as e:
            sys.stderr.write(f"  去水词(qwen)失败,回退规则法: {e}\n")
    res = []
    for i, s in enumerate(segs):
        t = (cleaned[i] if i in cleaned else _rule_clean(s["text"])).strip()
        if t:
            s = dict(s); s["text"] = t; res.append(s)
    return res


# ---------------- 4c. 应用纠正 + 断句 + 顺滑 + 产出 ----------------
def build_outputs(sents, results, out_dir):
    corrected = [dict(s) for s in sents]
    changes = []
    for r in results:
        sid, wrong, final = r["sid"], r["wrong"], r["final"]
        if final and final != wrong and wrong in corrected[sid]["text"]:
            corrected[sid]["text"] = corrected[sid]["text"].replace(wrong, final, 1)
            changes.append(r)

    # 断句:从原始 words 拆,再把该句的纠正套回每个片段
    segments = []
    for sid, sent in enumerate(sents):
        repls = [(c["wrong"], c["final"]) for c in changes if c["sid"] == sid]
        for p in split_words(sent):
            for wrong, final in repls:
                if wrong in p["text"]:
                    p["text"] = p["text"].replace(wrong, final, 1)
            segments.append(p)

    # 去水词
    segments = clean_fillers(segments)

    # SRT(最终交付:已纠错 + 已断句 + 已去水词)
    srt = []
    for i, s in enumerate(segments, 1):
        srt.append(f"{i}\n{ms_to_srt(s['begin_time'])} --> {ms_to_srt(s['end_time'])}\n{s['text']}\n")
    write_text(Path(out_dir, "corrected.srt"), "\n".join(srt))

    # 预览页格式(start/end 秒 + text)
    preview = [{"start": round(s["begin_time"] / 1000, 3),
                "end": round(s["end_time"] / 1000, 3),
                "text": s["text"]} for s in segments]
    write_json(Path(out_dir, "transcript.json"), preview)
    write_json(Path(out_dir, "corrected.json"), corrected)
    write_json(Path(out_dir, "report.json"), results)

    review = [r for r in results if r.get("needs_review")]

    # 可读报告
    rep = ["# 字幕纠错报告\n",
           f"共标记 {len(results)} 处可疑,有画面证据并修改 {len(changes)} 处,"
           f"取不到证据保留原文待人工确认 {len(review)} 处。\n",
           "## 已修改(均有画面铁证)"]
    for r in changes:
        ev = f"\n    画面证据: {r['evidence']}" if r.get("evidence") else ""
        rep.append(f"- 句{r['sid']} [{r['kind']}/{r['via']}] 「{r['wrong']}」→「{r['final']}」{ev}")
    if review:
        rep.append("\n## 待人工确认(画面未能逐字取证,已保留原文)")
        for r in review:
            g = f",模型猜测可能是「{r['guess_only']}」" if r.get("guess_only") else ""
            rep.append(f"- 句{r['sid']} 「{r['wrong']}」 可疑{g}(原因: {r.get('reason','')})")
    write_text(Path(out_dir, "report.md"), "\n".join(rep))
    return segments, changes, review


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--out", default=None)
    ap.add_argument("--max-seconds", type=int, default=0)
    ap.add_argument("--lang", default="zh")
    ap.add_argument("--allow-semantic-auto", action="store_true",
                    help="允许自动采用纯语义纠错猜测；默认保留待人工确认")
    ap.add_argument("--reuse", action="store_true",
                    help="复用 out 目录已有的 asr.json / suspects.json,只重跑 VL")
    args = ap.parse_args()

    # 认证由 bl CLI 自管；缺少官方登录时，下面第一个 bl 调用会自然报错。

    if args.max_seconds < 0:
        ap.error("--max-seconds 不能为负数")
    try:
        signature = source_signature(args.video, args.max_seconds, args.lang)
        out_dir = Path(args.out or (os.path.splitext(args.video)[0] + ".subfix")).expanduser()
        prepare_run_meta(out_dir, signature, args.reuse)
    except (FileNotFoundError, RuntimeError) as exc:
        ap.error(str(exc))
    frames_dir = out_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    asr_path = out_dir / "asr.json"
    sus_path = out_dir / "suspects.json"

    if args.reuse and os.path.exists(asr_path):
        print(f"[1/4] 复用 asr.json")
        sents = json.loads(asr_path.read_text(encoding="utf-8"))["transcripts"][0]["sentences"]
    else:
        print(f"[1/4] FunAudio ASR …")
        sents = run_asr(args.video, out_dir, args.max_seconds, args.lang)
    print(f"      {len(sents)} 句")

    if args.reuse and os.path.exists(sus_path):
        print(f"[2/4] 复用 suspects.json")
        suspects = validate_suspects(
            json.loads(sus_path.read_text(encoding="utf-8")), sents
        )
    else:
        print(f"[2/4] qwen3.7-max 标错(高精度)…")
        suspects = flag_suspects(sents)
        write_json(sus_path, suspects)
    print(f"      标出 {len(suspects)} 处可疑")
    for s in suspects:
        print(f"        [{s['kind']:8s}] 句{s['sid']}: 「{s['wrong']}」→ 猜「{s.get('guess','')}」")

    print(f"[3/4] qwen-vl 看帧纠正 …")
    results = correct(args.video, sents, suspects, frames_dir, args.allow_semantic_auto)

    print(f"[4/4] 断句顺滑(拆长句+去水词) + 产出 SRT/报告 …")
    segments, changes, review = build_outputs(sents, results, out_dir)

    print(f"\n========== 已修改 {len(changes)} 处(均有画面证据) ==========")
    for r in changes:
        ev = f"  | {r.get('evidence','')[:46]}" if r.get("evidence") else ""
        print(f"句{r['sid']:>2} [{r['via']:16s}] 「{r['wrong']}」→「{r['final']}」{ev}")
    if review:
        print(f"\n---------- 保留原文待确认 {len(review)} 处(画面取不到证据) ----------")
        for r in review:
            g = f" 猜「{r['guess_only']}」" if r.get("guess_only") else ""
            print(f"句{r['sid']:>2} 「{r['wrong']}」{g}")
    print(f"\n{len(sents)} 句 → 断句顺滑后 {len(segments)} 条字幕")
    print(f"输出目录: {out_dir}")
    print(f"  corrected.srt / transcript.json / corrected.json / report.md / report.json")


if __name__ == "__main__":
    main()
