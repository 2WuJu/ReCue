import { StrictMode, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';

type Health = {
  status: string; database: string; worker: { status: string };
  ffmpeg_available: boolean; model_file_available: boolean; transcription_ready: boolean;
};

function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    async function refresh() {
      try {
        const response = await fetch('/api/health', { signal: controller.signal });
        if (!response.ok) throw new Error(`服务返回 ${response.status}`);
        setHealth(await response.json());
        setError('');
      } catch (error) {
        if (!controller.signal.aborted) {
          setHealth(null);
          setError(error instanceof Error ? error.message : '连接失败');
        }
      }
    }
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, []);
  return <><header><a href="/" className="brand">ReCue</a><span>本地音视频复习</span></header>
    <main><div className="title"><h1>服务状态</h1><span className="badge">工程基础</span></div>
      <p>工程与启动骨架已接入。上传、识别和讲稿阅读将在后续任务中开放。</p>
      <section aria-label="运行状态" aria-live="polite">
        {error ? <p role="alert" className="error">无法连接服务：{error}。正在自动重连。</p> : !health ? <p>正在连接服务…</p> : <dl>
          <div><dt>API</dt><dd>{health.status === 'ok' ? '可用' : '异常'}</dd></div>
          <div><dt>数据库</dt><dd>{health.database === 'ok' ? '可用' : '异常'}</dd></div>
          <div><dt>后台工作进程</dt><dd>{health.worker.status === 'idle' ? '在线 · 等待接入队列' : '离线'}</dd></div>
          <div><dt>FFmpeg</dt><dd>{health.ffmpeg_available ? '已找到' : '未找到'}</dd></div>
          <div><dt>模型文件</dt><dd>{health.model_file_available ? '已配置 · 待验证' : '未配置'}</dd></div>
          <div><dt>转写能力</dt><dd>{health.transcription_ready ? '就绪' : '等待真实样本验证与接入'}</dd></div>
        </dl>}
      </section>
    </main></>;
}

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);
