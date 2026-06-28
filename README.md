# qwen-subtitle

用阿里云百炼(千问)系列模型,给视频做**字幕智能纠错**,并可进一步**翻译成多语言 + 用克隆原声配音**做视频出海。全程百炼 `bl` 命令行驱动。

## 它解决什么

录屏 / 教程 / 讲解类视频的字幕,无论哪家 ASR,**专有名词永远会被听错**(`Claude`→`cloud`、`Codex`→`class q`、`html2pptx`→`html to ppt`)。核心做法:**让视觉模型按时间戳去看那一帧,读屏幕上真实写着的字来纠正**——这是纯语音工具做不到的。

## 流程(5 步,全程 `bl`)

| 步骤 | 能力 | 模型 | bl 命令 |
|---|---|---|---|
| 1 听写 | 语音识别 | fun-asr | `bl speech recognize` |
| 2 看屏纠错 ★ | 标错 + 看帧 | qwen3.7-max + qwen3-vl-plus | `bl text chat` / `bl vision describe` |
| 3 顺滑 | 断句(算法) + 去水词 | qwen-plus | `bl text chat` |
| 4 翻译 | 字幕 / 配音稿 | qwen-mt-turbo / qwen-plus | `bl text chat` |
| 5 克隆配音 | 声音克隆 + 合成 | cosyvoice-v2 | `bl file upload` / `bl speech synthesize` |

> 例外:声音复刻 `create_voice` 百炼 CLI 暂无对应命令,走原始 DashScope API(同平台同认证)。

## 安装 / 依赖

- [`bl`](https://help.aliyun.com/zh/model-studio/)(百炼 CLI,已登录即可,脚本不需手动设密钥)
- `ffmpeg`、`python3`
- `flask`(仅预览页需要:`pip install flask`)

## 用法

```bash
# 1) 中文纠错(整片)
python3 scripts/subfix.py <video.mp4>

# 2) 多语言出海:克隆原声 + 翻译 + 配音(配音=中/英/日/韩,其余出字幕)
python3 scripts/dub_multi.py <video.mp4> --transcript <上一步>/transcript.json --langs en,ja,ko,es,th,vi

# 3) 多语言预览(tab 切字幕 + 切音轨;需 flask)
python3 scripts/preview_editor.py <out>/manifest.json
```

## 设计原则:零误改优先

错误的"纠正"比不纠正更糟。每一步都"宁可漏改,不可改错":只标明显听错的;VL 只认画面上能逐字读到的文字(禁图标幻觉);最小替换、发音一致;取不到画面证据就保留原文 + 标记待确认。细节见 [`references/design-gates.md`](references/design-gates.md)。

配音铁律:**有配音的语言,字幕 = 配音同一份文案,一字不差**;按整句念(不按字幕碎段);atempo 只压缩不拉慢。

---

`SKILL.md` 是给 AI Agent(Claude Code / Codex)读的完整说明。
