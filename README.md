# ReCue

个人局域网音视频转写与定位复习工具。

采用 React + TypeScript、FastAPI 和 SQLite，Windows 原生运行。当前提供工程骨架及服务健康页，上传、识别和讲稿阅读尚未实现。

已建立媒体接收登记与队列的数据库事务基础：重复请求ID返回原接收记录、失败/中断任务以新attempt重试、FIFO领取且全局仅一个运行任务。worker取得单实例锁后将上次运行中的任务标记为中断，排队任务保留；单独重启API不影响运行任务。当前尚无上传接口，worker暂不领取任务，等待真实媒体处理器接入。

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

验证（需先完成安装和前端构建）：

```powershell
uv lock --check --offline
uv pip check
uv run --offline --locked python -m unittest discover -s tests -v
```
