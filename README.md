# ReCue

个人局域网音视频转写与定位复习工具。

采用 React + TypeScript、FastAPI 和 SQLite，Windows 原生运行。当前提供服务健康页、文件接收/列表/详情/重试API及队列事务基础；上传界面、识别和讲稿阅读尚未实现。

已建立媒体接收登记与队列的数据库事务基础：重复请求ID返回原接收记录、失败/中断任务以新attempt重试、FIFO领取且全局仅一个运行任务。worker取得单实例锁后将上次运行中的任务标记为中断，排队任务保留；单独重启API不影响运行任务。worker暂不领取任务，等待真实媒体处理器接入。

需要 uv、Node.js（20.19+或22.12+，推荐受支持的LTS）、npm，以及可运行的FFmpeg。Python由uv按 `.python-version` 下载，依赖由 `pyproject.toml` 和 `uv.lock` 管理。

在项目根目录的PowerShell执行：

```powershell
.\scripts\setup.ps1
.\scripts\start.ps1
# 在另一终端停止
.\scripts\stop.ps1
```

访问 http://127.0.0.1:4318/ 。首次安装需要网络，依赖准备完成后启动使用uv离线模式。可用 `-Service api` 或 `-Service worker` 分别启停服务；停止时使用与启动相同的参数。

配置来自环境变量：`RECUE_HOST`（默认127.0.0.1）、`RECUE_PORT`（默认4318）、`RECUE_DATA_DIR`（默认项目data目录）、`RECUE_FFMPEG`（默认ffmpeg）、`RECUE_MODEL_PATH`（尚未接入模型）。自定义目录使用绝对路径。当前默认仅监听本机。

单文件接收契约（也在 `/openapi.json` 中提供）：

- `POST /api/media?display_name=URL编码文件名`：请求头 `X-Upload-ID` 为客户端生成的UUID，`Content-Type: application/octet-stream`，请求体为原始文件字节，不使用multipart。前端后续可直接发送浏览器File对象。
- 首次完整接收返回201及 `request_id/media_id/job_id/status`，`status=received`仅表示文件保存并入队，不表示音轨、时长、可解码性或识别通过。文件名仅展示，存储路径由服务端生成。
- `GET /api/uploads/{request_id}` 查询已完成接收结果。响应丢失时先查询；404表示尚无已完成记录，上传可能仍在进行。使用相同ID重放已完成请求返回200及原结果，即使新请求内容不同也不会替换原文件；上传新文件须使用新ID。不支持断点续传。
- 每次接收一个文件；其他新上传返回409 `upload_busy`，可以稍后查询或重试。大小上限为4GiB（4,294,967,296字节），按实际接收字节检查；空文件、超限、接收中断不入队，磁盘空间不足返回507。连续60秒没有新数据会超时。
- API启动及运行中每小时扫描，与上传共用系统锁；活跃上传时跳过扫描。清理遗留UUID临时文件及超过24小时且无数据库引用的原文件，保留已登记媒体及未知文件；清理失败保留文件供下一轮扫描重试。请勿手工将文件放入服务管理的UUID目录。当前尚无上传页面。

媒体查询与重试：

- `GET /api/media?limit=20&cursor=...`：按 `created_at/id` 倒序返回 `items/next_cursor`，limit范围1–100；游标为空表示首屏，`next_cursor=null` 表示没有下一页。刷新首屏可获取新导入的文件。
- `GET /api/media/{media_id}`：媒体元数据和 `latest_job`，包含真实任务状态、阶段、attempt、错误及处理配置；不返回服务端文件路径。时间均为Unix毫秒，未解析的时长/媒体类型为null，表示尚未验证。
- `POST /api/media/{media_id}/retry`：仅失败或中断且生命周期active的媒体可重试，创建新attempt后返回201及media_id/job_id；并发重复或状态冲突返回409，媒体不存在404。旧attempt及错误保留。
- `GET /api/media/{media_id}/transcript`：读取已完整发布的讲稿，包含version及稳定ID、sequence、start_ms、end_ms、text；未发布返回409，媒体不存在返回404。后台自动识别尚未接入，因此正常接收的文件仍会排队，不会自行产生讲稿。

后端现有Whisper CPU适配与讲稿事务发布基础：校验真实原始片段的时间范围、顺序和文本，完整发布与任务成功在同一事务提交；异常时间戳会拒绝发布。该适配尚未连接运行worker，上传、自动处理及阅读播放界面仍待后续接入。

Whisper外部进程通过Windows Job Object管理整个进程树：创建时即纳入管理，超时、取消或宿主强制退出时终止后代；正常结束也清理遗留后代。输出直接写入本地文件，避免管道阻塞或日志占满内存。需要Windows 10及以上，管理初始化失败会终止本次处理，不降级为无保护执行。此为适配器生命周期基础，尚未接入任务删除和worker自动处理。

媒体预处理模块现可独立探测和完整解码输入，生成H.264/AAC MP4（纯音频为AAC M4A）及16kHz单声道PCM。音频与视频减去同一个源时间原点，保留音画偏移与时间戳间隙，记录映射及实际解码后的时长；不删除静音、不均分时间。输入限本地媒体格式，拒绝播放列表；超时、取消、损坏或校验失败不返回可发布成果。这仍是后台处理基础，尚未连接自动队列、Range播放或上传/阅读界面；不代表已通过浏览器播放及定位验收。

前端API类型由FastAPI OpenAPI生成，修改API Schema后运行 `npm --prefix frontend run api:generate`，用 `npm --prefix frontend run api:check` 检查是否过期。导出无需启动服务或创建数据库。生成器使用独立npm工作区锁定openapi-typescript及其TypeScript 5依赖，应用继续使用TypeScript 7；`npm ci`会同时安装两个工作区。

M0样本清点（需要FFmpeg附带的FFprobe，不调用识别模型）：在 `backend` 目录执行：

```powershell
uv run --offline --locked python -m app.media.inspect 'D:\Courses\实际课程.mp4' --output ..\.local\m0\sample.json
```

输入必须是本地绝对路径；可用 `--ffprobe` 指定探测程序。工具只读源文件，流式计算SHA-256，记录FFprobe版本、音视频流、时长及原始起始时间，不写入业务数据库或队列。省略 `--output` 时输出JSON；报告文件仅允许保存到项目 `.local/`，已有报告不会覆盖。退出码0表示未发现清点问题，2表示报告含无音轨、时长未知或超限等问题，1表示探测或保存失败。清点不执行完整解码、时间轴映射或识别，不能证明媒体完整可处理、质量或性能达标。报告包含本地路径，仅保留本地。

验证（需先完成安装和前端构建）：

```powershell
uv lock --check --offline
uv pip check
uv run --offline --locked python -m unittest discover -s tests -v
npm --prefix frontend run api:check
npm --prefix frontend run build
```
