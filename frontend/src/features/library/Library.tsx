import { useEffect, useRef, useState } from 'react';
import { ApiError, formatTime, message, request, upload, uploadId, type Health, type Media, type MediaPage, type Receipt } from '../../api/client';

type Transfer = { id: string; name: string; progress: number; state: 'waiting' | 'uploading' | 'received' | 'uncertain' | 'failed'; error?: string };
const stages: Record<string, string> = { preprocessing: '预处理中', transcribing: '转写中', organizing: '整理中', queued: '排队中', succeeded: '已完成', failed: '失败', interrupted: '中断', cancelled: '已取消' };
function status(item: Media) {
  if (item.lifecycle !== 'active') return item.lifecycle === 'deleting' ? '删除中' : '删除失败';
  const job = item.latest_job;
  return job ? stages[job.status === 'running' ? job.stage ?? 'running' : job.status] ?? '处理中' : '等待任务';
}
export function Library({ health }: { health: Health | null }) {
  const [page, setPage] = useState<MediaPage | null>(null);
  const [error, setError] = useState('');
  const [loadError, setLoadError] = useState('');
  const [transfers, setTransfers] = useState<Transfer[]>([]);
  const [uploading, setUploading] = useState(false);
  const [busy, setBusy] = useState('');
  const [deleting, setDeleting] = useState<Media | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const actionLock = useRef(false);
  useEffect(() => { if (deleting) dialog.current?.showModal(); else dialog.current?.close(); }, [deleting]);
  const [tab, setTab] = useState('all');
  const [limit, setLimit] = useState(20);
  const [revision, setRevision] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const mounted = useRef(true);
  const uploadLock = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => { if (uploadLock.current) event.preventDefault(); };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    let timer: number;
    let failures = 0;
    async function refresh() {
      try {
        const items: Media[] = [];
        let cursor: string | null = null;
        let result: MediaPage;
        do {
          result = await request<MediaPage>(`/api/media?limit=${Math.min(100, limit - items.length)}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`, { signal: controller.signal });
          items.push(...result.items); cursor = result.next_cursor;
        } while (cursor && items.length < limit);
        if (!controller.signal.aborted) { setPage({ items, next_cursor: cursor }); setLoadError(''); failures = 0; }
      } catch (error) { if (!controller.signal.aborted) { setLoadError(message(error)); failures++; } }
      if (!controller.signal.aborted) timer = window.setTimeout(refresh, Math.min(30000, document.hidden ? 15000 : 2500 * 2 ** failures));
    }
    void refresh();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [limit, revision]);
  const update = (id: string, value: Partial<Transfer>) => { if (mounted.current) setTransfers(rows => rows.map(row => row.id === id ? { ...row, ...value } : row)); };
  async function queryReceipt(id: string) {
    try {
      await request<Receipt>(`/api/uploads/${id}`);
      update(id, { state: 'received', error: undefined });
      setRevision(n => n + 1);
    } catch (error) { update(id, { error: error instanceof ApiError && error.status === 404 ? '暂未查到接收记录，请稍后再次查询。不要重复上传。' : message(error) }); }
  }
  async function select(files: FileList | null) {
    if (!files?.length || uploadLock.current) return;
    if (files.length > 5) { setError('每批最多选择 5 个文件'); return; }
    const selected = Array.from(files);
    if (selected.some(file => file.size === 0 || file.size > 4 * 1024 ** 3)) { setError('请选择非空且不超过 4GB 的文件'); return; }
    uploadLock.current = true; setUploading(true); setError('');
    const batch = selected.map(file => ({ id: uploadId(), name: file.name, progress: 0, state: 'waiting' as const }));
    setTransfers(rows => [...rows, ...batch]);
    for (let i = 0; i < selected.length; i++) {
      const id = batch[i].id;
      update(id, { state: 'uploading' });
      try {
        await upload(selected[i], id, progress => update(id, { progress }));
        update(id, { state: 'received', progress: 100 });
      } catch (error) {
        update(id, { state: error instanceof ApiError && error.status < 500 && error.status !== 408 && error.status !== 409 ? 'failed' : 'uncertain', error: message(error) });
      }
      if (mounted.current) setRevision(n => n + 1);
    }
    uploadLock.current = false;
    if (mounted.current) { setUploading(false); if (input.current) input.current.value = ''; }
  }
  async function retry(item: Media) {
    if (actionLock.current) return;
    actionLock.current = true; setBusy(item.id);
    try { await request(`/api/media/${item.id}/retry`, { method: 'POST' }); setRevision(n => n + 1); }
    catch (error) { setError(message(error)); }
    finally { actionLock.current = false; setBusy(''); }
  }
  async function remove() {
    if (!deleting || actionLock.current) return;
    actionLock.current = true; setBusy(deleting.id);
    try {
      await request(`/api/media/${deleting.id}`, { method: 'DELETE' });
      setDeleting(null); setError('');
    } catch (error) {
      setError(`删除请求未确认：${message(error)}。列表会重新查询状态，可安全重试删除。`);
      setDeleting(null);
    } finally { actionLock.current = false; setBusy(''); setRevision(n => n + 1); }
  }
  const items = page?.items.filter(item => tab === 'all' || (tab === 'working' ? item.lifecycle === 'deleting' || (item.lifecycle === 'active' && ['queued', 'running'].includes(item.latest_job?.status ?? '')) : item.lifecycle === 'delete_failed' || ['failed', 'interrupted', 'cancelled'].includes(item.latest_job?.status ?? ''))) ?? [];
  return <><dialog ref={dialog} aria-labelledby="delete-title" aria-describedby="delete-description" onCancel={event => { if (actionLock.current) event.preventDefault(); else setDeleting(null); }} onClose={() => setDeleting(null)}>
    <h2 id="delete-title">删除这段媒体？</h2><p className="delete-name">{deleting?.display_name}</p>
    <p id="delete-description">原文件、播放副本、讲稿和进度将一并删除，无法恢复。运行中的任务会先停止。</p>
    <div className="modal-actions"><button autoFocus disabled={!!busy} onClick={() => setDeleting(null)}>取消</button><button className="danger" disabled={!!busy} onClick={() => void remove()}>{busy ? '正在请求…' : '确认删除'}</button></div>
  </dialog><div className="title"><h1>媒体</h1><button className="primary" disabled={uploading} onClick={() => input.current?.click()}>↑ 上传</button>
    <input ref={input} type="file" multiple hidden accept="audio/*,video/*,.mkv,.mov" onChange={event => void select(event.target.files)} /></div>
    <p className="workflow">上传音视频 → 等待转写 → 点击「阅读」。完成后可播放媒体、搜索讲稿，点击句子定位播放。</p>
    {health && !health.transcription_ready && <section className="processing-notice" role="status" aria-label="转写服务状态">
      <h2>{health.worker.status === 'blocked_config' ? '转写配置缺失或无效' : health.worker.status === 'offline' ? '转写进程未运行或已失联' : '转写服务尚未就绪'}</h2>
      <p>现在上传只会保存文件并排队，不会生成讲稿。已接收的文件无需重新上传。服务恢复后，排队任务会自动开始；标为「中断」的任务请点击「重试」。</p>
      {health.worker.status === 'blocked_config' ? <p>请检查 processing.local.json 或启动终端中 <code>RECUE_PROCESSING_CONFIG</code>，按 README 的「首次启动」准备 whisper.cpp 和模型、核对配置及文件哈希，再停止并重新启动服务。具体校验错误见启动终端。</p>
        : <p>请检查启动终端输出；若进程已退出，运行 <code>node scripts/recue.mjs launch</code> 同时启动网页与转写进程。仅启动 API 无法转写；macOS/Linux 当前仅支持 API 与界面开发。</p>}
    </section>}
    {loadError && <p role="alert" className="error">{loadError} · 正在重连，列表可能不是最新状态。 <button onClick={() => setRevision(n => n + 1)}>重新连接</button></p>}
    {error && <p role="alert" className="error">{error} <button onClick={() => setError('')}>关闭提示</button></p>}
    {transfers.length > 0 && <section className="upload-panel" aria-label="上传任务"><h2>上传</h2>{transfers.map(row => <div key={row.id} className="upload-row"><div className="transfer-name"><span title={row.name}>{row.name}</span>{row.state === 'uploading' && <progress value={row.progress} max="100" aria-label={`${row.name} 上传进度`} />}{row.error && <p className="error">{row.error}</p>}</div><span className={`status task-status ${row.state === 'received' ? 'success' : ''}`}>{({ waiting: '等待', uploading: `${row.progress}%`, received: '已接收待处理', uncertain: '结果待查询', failed: '上传失败' })[row.state]}</span>{row.state === 'uncertain' && <button onClick={() => void queryReceipt(row.id)}>查询结果</button>}</div>)}<p className="muted">上传完成前请保持页面打开。已接收不代表转写完成；请在下方列表查看处理状态，完成后点击「阅读」。</p></section>}
    <div className="tabs" role="group" aria-label="媒体筛选">{[['all', '全部'], ['working', '处理中'], ['errors', '异常']].map(([value, label]) => <button key={value} className={tab === value ? 'selected' : ''} aria-pressed={tab === value} onClick={() => setTab(value)}>{label}</button>)}</div>
    {!page ? <div className="empty">正在加载媒体…</div> : <><div className="table"><div className="media-row table-head"><span>文件</span><span>时长</span><span>导入时间</span><span>处理详情</span><span>状态</span><span>操作</span></div>
      {items.map(item => <div key={item.id} className="media-row"><div className="file"><span className="file-icon" aria-hidden="true">{item.media_type === 'audio' ? '♫' : '▷'}</span><span className="filename" title={item.display_name}>{item.display_name}{item.delete_error && <small className="deletion-note">{item.delete_error}</small>}</span></div><span className="num">{formatTime(item.duration_ms)}</span><time className="num">{new Date(item.created_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}</time><span className="detail" title={item.delete_error ?? item.latest_job?.error_message ?? ''}>{item.delete_error ?? (item.lifecycle === 'deleting' ? '正在停止任务并清理文件' : null) ?? item.latest_job?.error_message ?? (item.latest_job?.started_at && item.latest_job.status === 'running' ? `已用时 ${formatTime(Math.max(0, Date.now() - item.latest_job.started_at))}` : item.latest_job?.status === 'queued' ? '等待后台处理' : '—')}</span><span className={`status ${item.lifecycle === 'delete_failed' ? 'failure' : item.lifecycle !== 'active' ? '' : item.latest_job?.status === 'succeeded' ? 'success' : ['failed', 'interrupted'].includes(item.latest_job?.status ?? '') ? 'failure' : ''}`}>{status(item)}</span><div className="media-actions">{item.lifecycle === 'active' && item.latest_job?.status === 'succeeded' ? <a className="open" href={`#${item.id}`} onClick={event => { if (uploading) event.preventDefault(); }} aria-disabled={uploading}>阅读</a> : item.lifecycle === 'active' && ['failed', 'interrupted'].includes(item.latest_job?.status ?? '') ? <button disabled={!!busy} onClick={() => void retry(item)}>重试</button> : null}<button disabled={!!busy || item.lifecycle === 'deleting'} aria-label={`${item.lifecycle === 'delete_failed' ? '重试删除' : '删除'} ${item.display_name}`} title={item.lifecycle === 'delete_failed' ? '重试删除' : '删除'} onClick={() => setDeleting(item)}>{item.lifecycle === 'delete_failed' ? '重试删除' : '删除'}</button></div></div>)}
      {!items.length && <div className="empty">{page.items.length ? '没有符合筛选条件的媒体' : '还没有媒体，上传一份音视频开始复习。'}</div>}</div>
      {page.next_cursor && <button className="load-more" onClick={() => setLimit(n => n + 20)}>加载更多</button>}</>}
  </>;
}
