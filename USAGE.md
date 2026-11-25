# ReCue 使用手册

[返回项目首页](README.md)

## 安装与模型下载

只需在运行 ReCue 的电脑上安装工具和模型。使用浏览器的其他设备无需安装。

1. 安装 [Node.js 与 npm](https://nodejs.org/)（Node 20.19+ 或 22.12+）、[uv](https://docs.astral.sh/uv/getting-started/installation/) 和 [FFmpeg](https://ffmpeg.org/download.html)。FFmpeg 需支持 libx264 和 AAC，并包含 FFprobe。macOS 可用 `brew install ffmpeg`。安装后在新终端运行 `node --version`、`uv --version`、`ffmpeg -version` 和 `ffprobe -version`，确认都能调用。
2. 准备一个转写模型。第一次使用可从 Whisper medium 开始。

   | 模型 | 准备内容 |
   | --- | --- |
   | Whisper | 安装 [whisper.cpp CLI](https://github.com/ggml-org/whisper.cpp/releases)（macOS 可用 `brew install whisper-cpp`），下载 [medium Q5_0（539 MB）](https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium-q5_0.bin) 或 [large-v3 Q5_0（1.08 GB）](https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-q5_0.bin)。中文课程请选择多语言权重。 |
   | Qwen3-ASR | 下载识别和对齐模型，并按[配置 Qwen3-ASR](#配置-qwen3-asr)准备独立环境。 |

3. 在项目根目录运行 `node recue.mjs`。第一次启动时，请按提示选择访问方式和模型文件。启动器会准备 Python 和网页依赖。使用 Whisper 时，如果找不到 `whisper-cli`，终端会要求输入它的路径。

### 配置 Qwen3-ASR

下载 [Qwen3-ASR 0.6B](https://huggingface.co/Qwen/Qwen3-ASR-0.6B/tree/main) 或 [1.7B](https://huggingface.co/Qwen/Qwen3-ASR-1.7B/tree/main)，再下载 [Qwen3-ForcedAligner-0.6B](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B/tree/main)。保留各自的完整目录，包括配置、分词器和全部模型权重。也可从 ModelScope 下载。[Qwen 官方下载说明](https://github.com/QwenLM/Qwen3-ASR#released-models-description-and-download)

把识别模型和对齐模型目录放在同一个父目录下，然后在项目根目录运行：

```text
uv venv --no-project --python 3.11 .local/qwen3-runtime
uv pip install --python .local/qwen3-runtime "qwen-asr==0.0.6"
```

在启动提示中输入识别模型目录。ReCue 会查找同级的对齐模型和上面的 Python 环境。若放在其他位置，按提示提供路径。Qwen3-ASR 在 CPU 上运行，需要较多内存，可在「本批转写模型」中查看可用内存和模型要求。

### 更换或添加模型

添加模型后可在「本批转写模型」中选择，无需更换默认模型。ReCue 会查找默认 Whisper 模型同目录中的 `ggml-*.bin`，以及项目 `models/`、`models/qwen3/` 和默认 Qwen3 模型所在位置中的 Qwen3 模型目录。添加后若暂时未出现在列表中，请等待当前任务完成，让转写进程验证模型。

需要重新选择默认模型时，先停止 ReCue，再把项目根目录的 `processing.local.json` 改名留作备份。重新启动后会提示选择。自定义工具或模型路径可参考 [Whisper 配置示例](examples/processing.example.json)和 [Qwen3-ASR 配置示例](examples/processing.qwen3.example.json)。

## 启动、连接与停止

在项目根目录运行 `node recue.mjs`。看到终端提示「ReCue 网页已就绪」后，在浏览器打开下方显示的地址。保持启动终端运行。

- **仅本机使用**适合只在处理电脑上上传和阅读。直接回车会采用这一选项。
- **允许局域网访问**会额外显示当前的局域网地址。同一可信网络里的设备可以打开这些地址。

要停止 ReCue，在启动终端按 `Ctrl+C` 并等进程退出。也可以从项目根目录另开终端执行 `node stop.mjs`，等启动终端退出后再重启或备份。如果当时有转写运行，重启后可在任务列表点击「重试」。已排队的任务会继续。

### 局域网访问

首次启动时输入 `2`，让需要访问的设备接入同一个可信网络。在另一台设备的浏览器里打开启动终端标为「局域网打开」的地址。如有多条，请使用连到同一网络的网卡对应的地址。IP 变了就重启 ReCue，使用新显示的地址。

要更改访问方式，运行下面的命令，选择后重启 ReCue。以后仍然使用 `node recue.mjs` 启动。

```text
node recue.mjs configure
```

如果之前手动设置过 `RECUE_HOST`，请先清除它，再启动以应用保存的设置。macOS 终端或 Git Bash 用 `unset RECUE_HOST`，PowerShell 用 `Remove-Item Env:RECUE_HOST`。ReCue 无需登录，能连上服务的设备可以查看、上传和删除媒体。请只在可信网络开启，并避免把端口转发到公网。

## 上传和转写

1. 打开「转写任务」，拖入或选择音视频。每批最多 5 个文件，单个文件最多 4 GiB、120 分钟。可处理的格式取决于音视频编码。
2. 核对文件和「本批转写模型」，点击「提交转写」。上传显示「已接收」后可以关闭浏览器，后台会继续处理。若显示「待查回执」，先点击「查询回执」，确认是否收到了文件再决定是否重新上传。
3. 等状态变为「已完成」，点击「阅读」。一次只转写一个文件，后续任务会排队。任务详情会显示当前阶段、耗时和可获取的进度。

如果任务显示「等待模型」，可以查看等待原因，等内存释放，或点「更换模型」。失败或中断时，先查看错误详情，再点「重试」。

## 阅读和复习

- 在「阅读」页搜索讲稿，用上下箭头或 Enter、Shift+Enter 切换结果。点一句话即可跳到相应音视频位置。
- 播放时页面会跟随当前句。手动滚动后可点击「回到当前播放句」。再次打开媒体时会恢复最近保存的位置，并保持暂停。
- 桌面上可拖动播放器与讲稿之间的分隔条，双击恢复默认比例。点击「时间轴」里句子旁的「编辑」可修正文字，原有播放定位会保留。

### 文章式文字稿

「文章」视图会给讲稿补充标点并按段落显示。第一次使用前，在处理电脑联网执行以下命令，下载本地标点模型（约 62 MiB）：

```text
uv sync --locked
uv run python scripts/setup-punctuation.py
```

重启 ReCue 后，在阅读页切到「文章」。已有媒体可以直接使用。首次打开会整理并保存结果；之后无需重新上传或重新转写。重要内容可点击文字回听原声。

### 删除媒体

在任务列表点「删除」并确认后，原文件、播放副本、讲稿和进度都会清除。删除不可撤销。若显示清理失败，请查看提示，关闭占用文件的程序后再点击「重试删除」。

## 常见问题

**另一台电脑打不开。** 确认启动终端显示了「局域网打开」，先在处理电脑上打开同一个地址。如果处理电脑可以打开，检查另一台设备是否连在同一个网络上。访客 Wi-Fi 可能限制设备互访，也请确认处理电脑的防火墙允许可信网络访问该 TCP 端口。

Windows 可以在 PowerShell 用下面的命令检查端口，把示例 IP 和端口换成启动终端显示的值。看到 `TcpTestSucceeded: True` 表示端口连通，再在浏览器确认网页可打开。

```powershell
Test-NetConnection 192.168.88.8 -Port 4318
```

**提示没有找到网页或 uv。** 检查是否已经安装 uv，并在项目目录运行 `node recue.mjs setup`。该命令用于安装依赖和构建网页，需要联网。

**提示端口已被占用。** 停止此前启动的 ReCue，等旧启动终端退出后再启动。如果端口由其他程序占用，可关闭该程序，或为 ReCue 设置其他端口。

**页面能打开，任务一直不动。** 查看页面右上角的状态和启动终端的提示。模型或工具路径错误时，请按[安装与模型下载](#安装与模型下载)重新配置并重启。已排队的文件无需重传。

**需要断网使用。** 先在有网时完成安装和模型下载。保留项目中的 `.venv`、`frontend/dist` 和本地模型。断网时可以继续使用本机地址。若由另一台设备访问，需要保留设备之间的局域网连接。

## 备份与恢复

1. 在处理电脑停止 ReCue，等启动终端退出。若仍有转写程序占用文件或 `data/runtime/processing.guard` 仍在，请先处理清理问题，再复制数据。
2. 将项目下整个 `data/` 文件夹复制到一个新的目录。自定义过 `RECUE_DATA_DIR` 的话，请备份对应目录。数据库和 `media/` 必须一起复制。另存 `processing.local.json`、模型和相关工具，便于在另一台电脑恢复环境。
3. 恢复时使用相同版本的 ReCue。先停止服务，把备份复制到新目录，保留当前的数据目录以便回退。可将原 `data/` 改名保留，再把备份副本放进项目并命名为 `data/`。使用自定义数据目录时，通过 `RECUE_DATA_DIR` 指向恢复出的目录。
4. 启动后打开一条已有媒体，检查讲稿、播放和进度，再接收新上传。恢复旧备份会把数据回退到备份时的状态。
