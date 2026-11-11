import { StrictMode, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';
import type { components } from './api/schema';

type Health = components['schemas']['Health'];
const workerLabels: Record<string, string> = {
  processing: '在线 · 自动处理队列', blocked_config: '在线 · 处理配置未就绪',
  error: '异常 · 已停止领取', idle: '在线', stopped: '已停止', offline: '离线',
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
      <p>后台自动处理已接入。上传和讲稿阅读页面正在开发。</p>
      <section aria-label="运行状态" aria-live="polite">
        {error ? <p role="alert" className="error">无法连接服务：{error}。正在自动重连。</p> : !health ? <p>正在连接服务…</p> : <dl>
          <div><dt>API</dt><dd>{health.status === 'ok' ? '可用' : '异常'}</dd></div>
          <div><dt>数据库</dt><dd>{health.database === 'ok' ? '可用' : '异常'}</dd></div>
          <div><dt>后台工作进程</dt><dd>{workerLabels[health.worker.status] ?? '未知状态'}</dd></div>
          <div><dt>FFmpeg</dt><dd>{health.ffmpeg_available ? '已找到' : '未找到'}</dd></div>
          <div><dt>模型文件</dt><dd>{health.model_file_available ? '已配置 · 待验证' : '未配置'}</dd></div>
          <div><dt>转写能力</dt><dd>{health.transcription_ready ? '就绪' : '未就绪 · 请检查后台配置和日志'}</dd></div>
        </dl>}
      </section>
    </main></>;
}

createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);
