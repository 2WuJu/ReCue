import { StrictMode, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { request, type Health } from './api/client';
import { Library } from './features/library/Library';
import { Reader } from './features/reader/Reader';
import './styles.css';
function App() {
  const [id, setId] = useState(() => location.hash.slice(1));
  const [health, setHealth] = useState<Health | null>(null);
  useEffect(() => {
    const change = () => setId(location.hash.slice(1));
    window.addEventListener('hashchange', change);
    let disposed = false;
    let timer: number;
    async function refresh() {
      try { const value = await request<Health>('/api/health'); if (!disposed) setHealth(value); }
      catch { if (!disposed) setHealth(null); }
      if (!disposed) timer = window.setTimeout(refresh, 5000);
    }
    void refresh();
    return () => { disposed = true; clearTimeout(timer); window.removeEventListener('hashchange', change); };
  }, []);
  return <><header className="nav"><a href="#" className="brand">ReCue</a><span className={health?.transcription_ready ? 'connection' : 'muted'} role="status">{health ? health.transcription_ready ? '● 服务就绪' : '● 服务在线 · 后台处理未就绪' : '○ 连接中断 · 正在重连'}</span></header><main><div hidden={!!id}><Library /></div>{id && <Reader key={id} mediaId={id} />}</main></>;
}
createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);
