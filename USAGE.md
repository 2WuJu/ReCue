# 使用与 API 参考

返回 [README](README.md)。本文件描述当前已实现接口，完整 Schema 由运行服务的 `/openapi.json` 提供。

## 上传与接收

- `POST /api/media?display_name=URL编码文件名`：请求头 `X-Upload-ID` 为客户端生成的 UUID，`Content-Type: application/octet-stream`，请求体为原始字节，不使用 multipart。
- 首次完整接收返回 201 及 `request_id/media_id/job_id/status`。`status=received` 只表示保存并入队，音轨、时长、可解码性和转写仍须后台验证。
- `GET /api/uploads/{request_id}` 查询已完成接收结果。响应不明时先查询；404 表示尚无完整接收记录，上传可能仍在进行。
- 相同 ID 重放已完成请求返回 200 和原结果，即使内容不同也不会替换原文件；上传新文件必须使用新 ID。不是断点续传。
- 同时只接收一个新文件，其余返回 409 `upload_busy`。上限为 4 GiB（4,294,967,296 字节），按实际接收字节检查；空文件、超限或中断不入队。磁盘空间不足返回 507，连续 60 秒没有新数据超时。
- 前端每批最多选择 5 个文件并串行上传。处理阶段只显示真实状态与耗时，不提供估算百分比。

API 启动及运行中每小时扫描遗留上传文件，与上传共用系统锁；活跃上传时跳过扫描。清理 UUID 临时文件及超过 24 小时且无数据库引用的原文件，保留已登记媒体及未知文件，失败留待后续重试。不要手工向服务管理的 UUID 目录放入文件。

## 媒体与任务

| 接口 | 行为 |
| --- | --- |
| `GET /api/media?limit=20&cursor=...` | 按 `created_at/id` 倒序，返回 `items/next_cursor`；limit 为 1–100；`next_cursor=null` 表示末页 |
| `GET /api/media/{media_id}` | 媒体元数据和 `latest_job`，含真实状态、阶段、attempt、错误和处理配置，不暴露服务端路径 |
| `POST /api/media/{media_id}/retry` | 仅 active 且失败/中断的媒体可重试，新建 attempt，返回 201；并发重复或冲突 409，不存在 404 |
| `GET /api/media/{media_id}/transcript` | 完整发布的 version 和稳定片段 `id/sequence/start_ms/end_ms/text`；未就绪 409，不存在 404 |
| `GET /api/media/{media_id}/playback` | 只提供 active 且最新任务成功的已发布 MP4/M4A；支持 HEAD、Range 200/206/416；未就绪 409，不存在 404，副本缺失 503 |
| `GET /api/health` | API、数据库、worker 和转写配置就绪状态 |

API 日期和 `started_at` 为 Unix 毫秒；讲稿时间为相对播放起点的整数毫秒。未验证的媒体类型或时长为 null。

## 处理与时间轴

单 worker 使用实例锁与短事务 FIFO 领取任务，保存处理配置快照。每次 attempt 使用独立目录，依次执行真实预处理、CPU 转写、时间校验和讲稿整理，讲稿、播放路径、元数据和成功状态在同一事务发布；失败不公开半成品。

FFmpeg 生成 H.264/AAC MP4（音频为 AAC M4A）及 16 kHz 单声道 PCM。映射为 `source_pts_seconds - source_origin_seconds`，音视频共用原点，保留初始偏移和内部间隙。不删除静音，不平均分配时间。异常转写时间戳拒绝发布。点击讲稿从 `max(0, start_ms - 300)` 开始播放，原始讲稿时间戳不变。

`RECUE_PROCESSING_CONFIG` 指向显式部署 JSON，字段见 [示例](examples/processing.example.json)。worker 校验版本、CPU 后端、工具/模型的 SHA-256、FFmpeg 与 FFprobe；固定中文、8 线程、单处理器、不启用 VAD/分块。缺失或无效时为 `blocked_config`，不领取排队任务。修改后重启 worker，不自动选模型或降级。`health.transcription_ready` 只代表配置就绪，不代表质量达标。

## 停止与恢复

单独重启 API 不影响运行任务。重启 worker 前先确认旧工具进程树清理，再把遗留运行任务标记为 interrupted，排队任务保留；中断任务可手动重试，不承诺从转写中途续算。

Windows 工具创建时即纳入 Job Object，超时、取消或宿主退出后清理后代。独立 guardian 保留外层句柄，确认整树退出后才移除 `runtime/processing.guard`。清理失败、guardian 异常退出或断电留下标记时保守阻止启动，错误见 `runtime/guardian.log`；未核实整树退出前不要删除标记。底层生命周期检查已具备，面向用户的删除/取消及完整清理体验尚待实现。

## 常见问题

- **队列不动**：检查健康状态和启动终端输出；`blocked_config` 时检查配置绝对路径、文件哈希（小写）、工具路径，修正后重启 worker。
- **缺少前端**：运行 `node scripts/recue.mjs setup` 或 `npm --prefix frontend run build`；不要在服务进行 HTTP 验证时同时重写构建目录。
- **端口占用**：停止已有实例或设置 `RECUE_PORT`，分开运行的 API 与 worker 使用同一数据目录。
- **uv 找不到**：按官方说明安装并刷新 PATH；通用入口兼容用户目录 `.local/bin` 的默认安装位置。
- **停止无效**：核对 `--service` 和绝对路径 `RECUE_DATA_DIR` 是否与启动一致；stop 写入请求后，须等待启动终端退出。
- **macOS/Linux 无法转写**：当前工具进程保护仍依赖 Windows Job Object；仅可用 `start --service api` 进行 API/界面开发。
