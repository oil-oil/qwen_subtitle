#!/usr/bin/env python3
"""
qwen-subtitle 多语言出海管线: 克隆原声 → 多语言字幕 + 多语言配音 → manifest(喂多语言预览页)。

输入: 已纠错中文 transcript.json + 视频
产出: <out>/ 下 clip.mp4 + zh.json + <lang>.json + <lang>.m4a(配音轨) + manifest.json
用法(默认整片;--clip-seconds 只用于试跑前 N 秒):
  python3 dub_multi.py <video> --transcript <zh.json> --langs en,ja [--out DIR] [--clip-seconds N] [--voice-id ID]
  注意: --clip-seconds 会限制字幕/配音的范围(不只是裁预览),省略=整片。
"""
import json, subprocess, sys, re, os, argparse, shutil

FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = os.environ.get("FFPROBE") or shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"
ENROLL = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization"

# 支持的目标语言: code -> (中文显示名, CosyVoice --language)
# 菜单和 tab 都用中文名(用户看得懂);译文本身是该语言。
LANGS = {
    "en": ("英语", "en"), "ja": ("日语", "ja"), "ko": ("韩语", "ko"),
    "es": ("西班牙语", "es"), "pt": ("葡萄牙语", "pt"), "ar": ("阿拉伯语", "ar"),
    "id": ("印尼语", "id"), "th": ("泰语", "th"), "vi": ("越南语", "vi"),
    "fr": ("法语", "fr"), "de": ("德语", "de"), "ru": ("俄语", "ru"),
}
# 能"克隆配音"的语言(CosyVoice v2 官方支持 + 我们承诺的):其余只出字幕。
DUB = {"en", "ja", "ko"}

from concurrent.futures import ThreadPoolExecutor
import threading
WORKERS = int(os.environ.get("SUBFIX_WORKERS", "8"))   # 全局并发上限
_SEM = threading.Semaphore(WORKERS)                     # 限同时在飞的 bl 调用,避免压垮 API

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
        if p.returncode == 0: return p.stdout
        sys.stderr.write(f"[retry {a+1}] {p.stderr[:120]}\n"); subprocess.run(["sleep", "2"])
    raise RuntimeError(f"bl failed: {args[:3]}")

def dashscope_key():
    # 克隆需要 curl 直调原始 API,要 key:优先环境变量,否则读 bl 自己的配置(免每次手动注入)
    k = os.environ.get("DASHSCOPE_API_KEY")
    if k:
        return k
    try:
        return json.load(open(os.path.expanduser("~/.bailian/config.json"))).get("api_key")
    except Exception:
        return None
def content(s): return json.loads(s)["choices"][0]["message"]["content"]
def ejson(t):
    t = re.sub(r"^```[a-z]*\n?", "", t.strip()); t = re.sub(r"\n?```$", "", t).strip()
    m = re.search(r"(\[.*\]|\{.*\})", t, re.S); return json.loads(m.group(1) if m else t)
def dur(f):
    return float(subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                  "-of", "csv=p=0", f], capture_output=True, text=True).stdout.strip() or 0)

def clone_voice(video, out_dir, sample_start):
    key = dashscope_key()
    if not key:
        sys.exit("ERROR: 克隆配音需要百炼密钥(环境变量 DASHSCOPE_API_KEY 或 ~/.bailian/config.json)")
    print("[克隆] 扒人声 + 上传 + 复刻…")
    sample = os.path.join(out_dir, "voice_sample.wav")
    subprocess.run([FFMPEG, "-y", "-ss", str(sample_start), "-t", "18", "-i", video,
                    "-ar", "16000", "-ac", "1", sample], capture_output=True)
    oss = json.loads(bl(["file", "upload", "--file", sample, "--model", "cosyvoice-v2", "--output", "json"]))["url"]
    body = json.dumps({"model": "voice-enrollment", "input": {"action": "create_voice",
                       "target_model": "cosyvoice-v2", "prefix": "oil", "url": oss}})
    r = subprocess.run(["curl", "-s", "-X", "POST", ENROLL,
        "-H", f"Authorization: Bearer {key}",
        "-H", "Content-Type: application/json", "-H", "X-DashScope-OssResourceResolve: enable",
        "-d", body], capture_output=True, text=True)
    vid = json.loads(r.stdout)["output"]["voice_id"]; print(f"[克隆] {vid}"); return vid

def merge_sentences(segs, max_dur=9.0):
    units, cur = [], None
    for s in segs:
        if cur is None: cur = {"start": s["start"], "end": s["end"], "text": s["text"]}
        else: cur["end"] = s["end"]; cur["text"] += s["text"]
        if re.search(r"[。！？!?.]$", s["text"].strip()) or (cur["end"] - cur["start"]) >= max_dur:
            units.append(cur); cur = None
    if cur: units.append(cur)
    return units

TRANSLATE_MODEL = "qwen-mt-turbo"   # 纯字幕语言:专用翻译模型(忠实、92 语言)

def translate(units, lang_name, for_dub=False):
    # 铁律:配音语言里,这一份稿子既当配音又当字幕,必须一字不差。
    # 所以配音语言用 qwen-plus 按词数压到"能在时长内说完"(否则配音被 atempo 压得很赶);
    # 纯字幕语言没有配音,用 qwen-mt 忠实翻译即可。
    def one(u):
        if for_dub:
            words = max(2, int((u["end"] - u["start"]) * 2.6))
            return content(bl(["text", "chat", "--model", "qwen-plus",
                "--system", f"你是影视配音译者,译入{lang_name},必须在限定词数内自然说完。",
                "--message", f"翻成{lang_name},≤{words}词,简洁口语,品牌/型号/数字/英文保持原样,只输出译文:{u['text']}",
                "--max-tokens", "200", "--output", "json"])).strip()
        return content(bl(["text", "chat", "--model", TRANSLATE_MODEL,
            "--message", f"翻成{lang_name},品牌/型号/数字/英文保持原样:{u['text']}",
            "--output", "json"])).strip()
    return pmap(one, units)   # 逐句并发翻译(保序)

def build_lang(code, units, voice_id, out_dir, clip_seconds):
    name, tts_lang = LANGS[code]
    is_dub = code in DUB and voice_id
    print(f"[{code}] 翻译{'+配音' if is_dub else '(仅字幕)'} ({name})…")
    # 配音语言: 同一份稿子既配音又当字幕(一字不差) → for_dub 走 qwen-plus 压时长
    txt = translate(units, name, for_dub=is_dub)
    entry = {"code": code, "name": name, "transcript": f"{code}.json"}

    if is_dub:
        # 克隆音色配音 + atempo 卡时长(逐句并发,保序)
        def synth(itu):
            i, u, t = itu
            raw = os.path.join(out_dir, f"{code}_{i}_raw.mp3"); fit = os.path.join(out_dir, f"{code}_{i}.mp3")
            bl(["speech", "synthesize", "--model", "cosyvoice-v2", "--voice", voice_id,
                "--language", tts_lang, "--text", t, "--out", raw])
            nxt = units[i + 1]["start"] if i + 1 < len(units) else clip_seconds
            slot = max(0.8, nxt - u["start"]); ratio = min(1.5, max(1.0, dur(raw) / slot))
            subprocess.run([FFMPEG, "-y", "-i", raw, "-filter:a", f"atempo={ratio:.3f}", fit], capture_output=True)
            return (u["start"], fit, dur(fit))
        clips = pmap(synth, [(i, u, t) for i, (u, t) in enumerate(zip(units, txt))])
        track = os.path.join(out_dir, f"{code}.m4a")
        inp, filt, lab = [], [], []
        for idx, (st, fit, _) in enumerate(clips):
            inp += ["-i", fit]; ms = int(st * 1000); filt.append(f"[{idx}]adelay={ms}|{ms}[a{idx}]"); lab.append(f"[a{idx}]")
        subprocess.run([FFMPEG, "-y"] + inp + ["-filter_complex",
            ";".join(filt) + ";" + "".join(lab) + f"amix=inputs={len(clips)}:normalize=0:dropout_transition=0[o]",
            "-map", "[o]", "-t", str(clip_seconds), track], capture_output=True)
        entry["audio"] = f"{code}.m4a"
        # 字幕 = 配音文案,按配音时长计时
        tj = [{"start": u["start"],
               "end": min(units[i + 1]["start"] if i + 1 < len(units) else clip_seconds, u["start"] + clips[i][2] + 0.5),
               "text": t} for i, (u, t) in enumerate(zip(units, txt))]
    else:
        # 仅字幕:不配音,视频保留原声;字幕按句计时
        tj = [{"start": u["start"],
               "end": units[i + 1]["start"] if i + 1 < len(units) else clip_seconds,
               "text": t} for i, (u, t) in enumerate(zip(units, txt))]

    json.dump(tj, open(os.path.join(out_dir, f"{code}.json"), "w"), ensure_ascii=False, indent=2)
    return entry

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video"); ap.add_argument("--transcript", required=True)
    ap.add_argument("--out", default=None)
    # 0 = 整片(默认);>0 仅用于试跑前 N 秒。影响字幕/配音范围 + 预览裁剪 + 末段边界。
    ap.add_argument("--clip-seconds", type=int, default=0)
    ap.add_argument("--langs", default="en"); ap.add_argument("--voice-id", default=None)
    args = ap.parse_args()
    # 翻译/TTS 经 bl 调用,认证由 bl 自管;只有"克隆配音"那步要 key,在 clone_voice 里懒检查。
    out_dir = args.out or os.path.join(os.path.dirname(os.path.abspath(args.transcript)), "ml_out")
    os.makedirs(out_dir, exist_ok=True)

    limit = args.clip_seconds                       # 0 = 整片
    end_bound = limit if limit else dur(args.video)  # 末段/音轨的时间边界
    all_segs = json.load(open(args.transcript))
    segs = [s for s in all_segs if not limit or s["end"] <= limit]
    units = merge_sentences(segs)
    print(f"[源] {'整片' if not limit else f'前{limit}s'}: {len(all_segs)} 碎段取 {len(segs)} → {len(units)} 整句")

    # 预览视频(带原声):整片直接 remux 复制(秒级,不重编码);试跑才裁剪重编码
    clipv = os.path.join(out_dir, "clip.mp4")
    if limit:
        subprocess.run([FFMPEG, "-y", "-t", str(limit), "-i", args.video,
                        "-c:v", "libx264", "-crf", "24", "-preset", "veryfast", "-c:a", "aac", clipv], capture_output=True)
    else:
        subprocess.run([FFMPEG, "-y", "-i", args.video, "-c", "copy", clipv], capture_output=True)
    json.dump(segs, open(os.path.join(out_dir, "zh.json"), "w"), ensure_ascii=False, indent=2)

    codes = [c.strip() for c in args.langs.split(",") if c.strip() in LANGS]
    need_clone = any(c in DUB for c in codes)
    voice_id = args.voice_id or (clone_voice(args.video, out_dir, max(0, segs[0]["start"])) if need_clone and segs else None)
    langs = [{"code": "zh", "name": "中文(原声)", "transcript": "zh.json", "source": True}]
    for code in codes:
        langs.append(build_lang(code, units, voice_id, out_dir, end_bound))

    json.dump({"video": "clip.mp4", "languages": langs},
              open(os.path.join(out_dir, "manifest.json"), "w"), ensure_ascii=False, indent=2)
    print(f"\n✅ manifest: {out_dir}/manifest.json  语言: {', '.join(l['code'] for l in langs)}")

if __name__ == "__main__":
    main()
