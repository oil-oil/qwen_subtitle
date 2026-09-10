# qwen-subtitle

结合语音与真实画面纠正视频字幕，支持字幕翻译，以及用户明确授权后的声音克隆配音。

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

# 2) 多语言字幕，默认保留原声；有授权且需要配音时另加 --dub --confirm-voice-rights
python3 scripts/dub_multi.py <video.mp4> --transcript <上一步>/transcript.json --langs en,ja,ko,es,th,vi

# 3) 多语言预览(tab 切字幕 + 切音轨;需 flask)
python3 scripts/preview_editor.py <out>/manifest.json
```

## 设计原则:零误改优先

错误的"纠正"比不纠正更糟。每一步都"宁可漏改,不可改错":只标明显听错的;VL 只认画面上能逐字读到的文字(禁图标幻觉);最小替换、发音一致;取不到画面证据就保留原文 + 标记待确认。细节见 [`references/design-gates.md`](references/design-gates.md)。

配音铁律:**有配音的语言,字幕 = 配音同一份文案,一字不差**;按整句念(不按字幕碎段);atempo 只压缩不拉慢。

---

`SKILL.md` 是给 AI Agent读的完整说明。

## 翻译与声音授权

所有目标语言默认仅翻译字幕。需要配音时显式传 `--dub --confirm-voice-rights`，并先确认有权使用输入声音；已有 `--voice-id` 也需要这两个开关。输出目录非空会拒绝覆盖。媒体、文本和声音样本会按所选步骤发给百炼并可能计费；FFmpeg 或服务失败时不会写成功 manifest。

## 配置、依赖与使用边界

Python、ffmpeg 和已认证的百炼 CLI；预览另需 Flask。凭据使用官方登录或运行环境，不发到聊天。

默认只翻译字幕；声音克隆必须显式选择并有声音使用权。百炼处理所选媒体与文本，可能计费。

使用示例：

```text
给这个视频生成英文字幕，保留中文原声。
```

## GitHub 安装

把 [仓库地址](https://github.com/oil-oil/qwen_subtitle) 交给 Agent，要求按 README 安装；也可运行：

```bash
npx skills add oil-oil/qwen_subtitle
```

安装后由宿主重新加载 Skill。

## API Key 配置页面

首次使用外部服务时，可以在本机配置页亲自填写 Key；已有配置会复用，密钥存入系统凭据库。只为实际使用的外部服务配置；纯本地处理不需要 Key。页面需要 Node.js 22.18+ 与可用的系统凭据服务，业务运行仍使用原依赖。

安装、状态检查、打开页面和带凭据运行的完整入口见[配置说明](references/api-key-setup.md)。页面保存与业务读取已经接通；不把 Key 发进聊天，也不自动迁移旧文件。
