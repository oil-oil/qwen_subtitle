---
name: qwen-subtitle
description: "结合语音与真实画面纠正视频字幕，按需翻译字幕或克隆已获授权的声音配音。用户明确选择 qwen-subtitle、要求模型自动纠错或多语言配音时使用；普通中文成片字幕优先使用 oil-subtitle。不要用于修改 .screenstudio 工程时间线；仅翻译字幕时不克隆、不配音。"
---

# qwen-subtitle — 百炼字幕智能纠错 + 多语言出海配音

## API Key 配置入口

需要外部服务凭据时先读[API Key 配置与业务读取](references/api-key-setup.md)：复用已有安全入口；本机缺少 Key 时使用随附固定页面，保存后通过业务包装入口读取。内置能力与纯本地流程不要求配置 Key。

## 它解决什么问题

录屏 / 教程类视频的字幕,无论用哪家 ASR,**专有名词永远会被听错**:`Claude` 听成 `cloud`、`Codex` 听成 `class q`、`html2pptx` 听成 `html to ppt`、`OOXML` 听成 `OOCML`。靠一份手工维护的术语表去替换,既累又补不全。

本 skill 的核心洞察:**录屏视频里,正确的词往往就明明白白写在屏幕上**。所以——让视觉模型按时间戳去看那一帧,读屏幕上的真实文字来纠正。这是单通道(纯语音)工具做不到的事。

判断标准(避免按字面例子过拟合):凡是"屏幕上写着、却被听成同音词"的术语 / 产品名 / 代码 / 命令 / 文件名 / 路径,都属此类。

## 组合了千问的哪些能力

| 能力 | 模型 | 走的命令 | 角色 |
|---|---|---|---|
| 🎙️ 语音识别 | FunAudio-ASR | `bl speech recognize` | 出字幕,带**句级+词级毫秒时间戳**——后续抽帧、断句、对齐配音都靠它 |
| 🧠👁️ 视觉纠错 | qwen3.8-max(默认) / qwen3.8-flash(可选) | `bl vision describe` | 一次读取整段视频和带时间范围的 ASR,只返回最小术语替换与画面证据 |
| ✨ 去水词 | qwen-plus | `bl text chat` | 删呃啊嗯、口吃重复,顺滑 |

模型分工是**实测**定下来的:纠错改为一个视觉模型完成，不再串联“标错模型 + 看屏模型”；默认用 qwen3.8-max，因为在同一条 89 秒录屏上的 Flash 召回波动较大。需要成本优先试跑时，可用 `SUBFIX_CORRECTION_MODEL=qwen3.8-flash` 切换；去水词继续用 qwen-plus(低风险、提示词短、无需视觉)。脚本顶部的 `CORRECTION_MODEL / FILLER_MODEL` 是唯一模型配置入口。

## 流程(5 步,一条命令跑完)

```
视频
 ├─[1] FunAudio ASR ─────────► 字幕轨道(句+词级毫秒时间戳)
 ├─[2] qwen3.8-max 视觉纠错 ──► 整段视频 + ASR 时间范围 → 最小替换 + 证据
 │                              └ 取不到证据/纯语义 → 保留原文待确认〔零误改〕
 ├─[3] 词级时间戳断句 ─────────► 按标点拆长句(>24字 / >6秒)
 ├─[4] qwen-plus 去水词 ───────► 删语气词/口吃,顺滑
 └─► corrected.srt + transcript.json + report.md(画面证据)
```

## 准备

使用页面配置声音复刻 API 后，`dub_multi.py` 必须经上述 `run` 包装入口执行；其余 CLI 操作继续复用官方登录。

**认证**:`bl` 已登录就什么都不用设——脚本直接用 bl 自己的认证(`bl auth status` 验证)。克隆配音通过 Python HTTP 客户端调用原始 API，必须经配置页的 `run` 包装入口注入 `DASHSCOPE_API_KEY`；业务脚本不读取旧的明文凭据文件。缺少凭据时使用配置页或可信运行环境注入，不让用户发 Key 到聊天。

依赖:
- `bl`(百炼 CLI,v1.4+;`bl --version` 验证)
- `ffmpeg`(抽音频 + 抽帧;脚本自动从 PATH 找,或设 `FFMPEG` 环境变量)
- `python3`
- `flask`(**仅预览页需要**:`pip install flask`;纠错/翻译/配音不需要)

`bl speech recognize` 和 `bl vision describe` 都**直接吃本地文件路径**(CLI 内部已处理上传),所以不需要手动上传文件拿 URL。

## 第一步：确定输出

复用用户已给出的语言与范围；缺失时只问目标语言。所有语言默认仅字幕、保留原声。用户明确要求配音，且有权使用该声音后，才开启 `--dub --confirm-voice-rights`；已有 voice-id 也遵守此规则。不要把“英文字幕”解释为声音克隆。

语言代码：英语 en、日语 ja、韩语 ko、西语 es、葡语 pt、阿语 ar、印尼语 id、越语 vi、泰语 th。当前脚本仅 en/ja/ko 支持配音，其余只翻译字幕。

## A. 纠错中文字幕(基础,所有情况都先跑)

```bash
python3 scripts/subfix.py <video.mp4> [--out DIR]
```

参数:`--out DIR`(默认 `<视频名>.subfix/`)、`--max-seconds N`(ASR 试跑前 N 秒,视觉模型仍接收原视频但只按列出的时间范围取证)、`--lang zh`、`--reuse`(仅在来源视频、语言和试跑范围指纹一致时复用 ASR;每次都会重新读取视频做纠错)、`--allow-semantic-auto`(明确授权后才自动采用纯语义猜测)。已有产物时不要换视频复用目录。

产出:`corrected.srt`(纠错+断句+去水词的中文字幕)、`transcript.json`(`[{start,end,text}]`)、`report.md`(每处改动 + **画面证据** + "取不到证据保留原文待确认"清单);另有 `corrected.json`(句级结构化结果)、`correction.json`(视觉模型通过程序校验后的最小替换)与 `report.json`(机器可读证据)。中间产物:`asr.json`。

> 给用户看结果时:念 `report.md` 的改动+证据(最有说服力);诚实区分"已改(有画面铁证)"与"保留原文待确认",别把后者说成已修复。纯语义纠错默认也只进入待确认清单；只有用户明确允许时才传 `--allow-semantic-auto`。

## B. 多语言出海:克隆原声 + 翻译 + 配音

用户选了外语时,在 A 的基础上跑:

```bash
python3 scripts/dub_multi.py <video.mp4> \
  --transcript <A的transcript.json> --langs en,ja,es,pt --out <DIR> [--clip-seconds N]
```

> ⚠️ `--clip-seconds` **默认 0 = 整片**;它会**限制字幕/配音的实际范围**(不只是裁预览),只在试跑前 N 秒时才设。要整片就别带它(且上游 `subfix.py` 也别带 `--max-seconds`)。

`--langs` 传 code(en/ja/ko/es/pt/ar/id/vi/th…)。脚本按显式开关执行：未传 `--dub` 时所有语言仅翻译字幕；只有 `--dub --confirm-voice-rights` 才允许以下配音分支。
1. **已开启配音的语言(`DUB`={en,ja,ko})**:克隆原声(扒 18s → `bl file upload` → 复刻 API → voice_id,`--voice-id` 可跳过)→ **按整句、限定词数翻译**(配音=字幕同一份文案,要在镜头时长内说完)→ **克隆音色 TTS** → `atempo` 卡时长 → 出 `<code>.json` + `<code>.m4a`(配音轨)。
2. **仅字幕语言(其余)**:只翻译 → 出 `<code>.json`;视频保留原声。
3. 全选了仅字幕时**不克隆**(省一步)。产出 `manifest.json` + 各语言 transcript(+配音轨) + `clip.mp4`。

输出目录必须为空；已有结果请换新的 `--out`。使用页面保存的凭据时，先按[API Key 配置与业务读取](references/api-key-setup.md)的 `run` 入口执行，不能直接绕过包装器。

翻译分两路(脚本 `translate()` 按 `for_dub` 自动选):**配音语言走 `qwen-plus`**——同一份稿子既配音又当字幕,按词数(`≈时长×2.6`)压到能在时长内说完;**纯字幕语言走 `qwen-mt-turbo`**(`TRANSLATE_MODEL` 常量,百炼专用翻译模型,忠实、品牌/型号天然保留)。支持:**配音=中/英/日/韩**(CosyVoice v2 官方);**字幕=92 语种**(`LANGS` 表里加 code 即可)。英文配音已充分验证;日韩配音用同一克隆音色,质量需实跑确认。

配音的三条关键设计(踩坑换来的,改前先懂):
- **按整句念,不按字幕碎段**——碎段会念成半句、听着"没说完";所以先把字幕碎段合并回整句再配音。
- **字幕 = 配音同一份译文**,按句计时——否则屏幕显示的字和嘴里说的对不上。
- **只压缩不拉慢**(atempo 上限 1.5):译文短了就自然播放留空,不拉慢拖沓。

## 多语言预览(tab 切字幕 + 切音轨)

预览页**已自带**(`scripts/preview_editor.py`,基于 Flask 的本地交互页)。需要 `flask`(`pip install flask`;纠错/翻译/配音本身不需要)。把 manifest 交给它:

```bash
python3 scripts/preview_editor.py <out>/manifest.json        # 多语言(manifest)
python3 scripts/preview_editor.py <video> <transcript.json>  # 单语言
```

预览页右上角按语言出 **tab**,切 tab → 右侧换该语言字幕、左侧视频换该语言音轨(源语言用原声,外语用克隆配音轨,与视频帧同步)。人工过一遍后保存,再交烧录。

## 设计原则:零误改优先

字幕工具里,**错误的"纠正"比不纠正更糟**——把对的词改成错的会主动污染字幕。所以这套流程的每一步都在"宁可不改,不可改错":

- **模型别越界**:只返回明显听错的术语;本身通顺、像名字的中文(哪怕疑似音译)和本身正确只是"不够具体"的词,都不标。
- **视觉禁幻觉**:只认对应时间范围内能逐字读到的文字;靠图标/logo/常识推断的一律不算数。
- **最小替换 + 发音一致**:只替换听错的那几个字,不带进相邻路径段;改后的词必须和原文读音对得上。
- **证不了就留原文**:screen 类取不到画面证据,保留原文 + 标记待确认,绝不套用模型盲猜。
- **程序复核兜底**:只接受出现在原句中的连续 `wrong`,拒绝整句润色、标点/空格/语法调整;screen 还必须带画面证据,semantic 默认只进待确认。

这些闸门是踩了一轮坑(误标 ooxml、把"超级麦吉"改成"超级码力/Supermario")才加上的。每条闸门的来龙去脉、对应的提示词和程序复核规则,见 **[references/design-gates.md](references/design-gates.md)**——改提示词前务必先读,否则容易把精度调回原形。

## 调参(脚本顶部常量)

- `SUBFIX_CORRECTION_MODEL` / `CORRECTION_MODEL / FILLER_MODEL` —— 视觉纠错与去水词模型；默认纠错模型为 qwen3.8-max
- `MAX_CHARS=24` / `MAX_DUR_MS=6000` —— 断句阈值(单条字幕最大字数/时长)
- `MIN_BREAK_CHARS=12` —— 到标点且已够这么长就断
- 甜区:录屏 / 屏幕共享 / 教程类(术语都写在屏上)。纯口播、谈话头(术语不在画面)时,视觉模型取不到证据会更多保留原文——这是预期行为,不是 bug。
