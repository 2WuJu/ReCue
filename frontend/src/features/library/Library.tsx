import * as Select from '@radix-ui/react-select';
import { useEffect, useRef, useState } from 'react';
import { ApiError, formatTime, message, request, upload, uploadId, type Health, type Media, type MediaPage, type ModelAvailability, type Receipt } from '../../api/client';

type Transfer = { id: string; mediaId?: string; name: string; progress: number; state: 'waiting' | 'uploading' | 'received' | 'uncertain' | 'failed'; error?: string };
const stages: Record<string, string> = { preprocessing: '预处理中', transcribing: '转写中', organizing: '整理中', queued: '排队中', succeeded: '已完成', failed: '失败', interrupted: '中断', cancelled: '已取消' };
const modelLabels: Record<ModelAvailability['status'], string> = {
  available: '可用', below_recommended: '低于推荐配置', resource_unknown: '资源需求未知',
  memory_insufficient: '内存暂不足', pending: '待验证', unsupported: '不支持',
  check_failed: '检查失败', check_timeout: '检查超时', missing: '文件不可用',
  disabled: '已停用', offline: '转写进程未就绪',
};
const memorySources: Record<NonNullable<ModelAvailability['memory_source']>, string> = {
  reference: '参考估算', sampled_rss: '短测驻留内存估算', peak_commit: '短测提交内存估算',
};
function status(item: Media) {
  if (item.lifecycle !== 'active') return item.lifecycle === 'deleting' ? '删除中' : '删除失败';
  const job = item.latest_job;
  if (job?.status === 'queued' && job.wait_reason) return '等待模型';
  return job ? stages[job.status === 'running' ? job.stage ?? 'running' : job.status] ?? '处理中' : '等待任务';
}
function TaskMetrics({ item }: { item: Media }) {
  const job = item.latest_job;
  if (item.lifecycle === 'deleting') return <>正在停止任务并清理文件</>;
  const detail = item.delete_error ?? job?.wait_reason ?? job?.error_message;
  if (!job) return <>{detail ?? '—'}</>;
  const running = job.status === 'running';
  const end = running ? Date.now() : job.finished_at;
  return <span className="task-metrics">
    {detail && <span>{detail}<br /></span>}
    {end != null ? <span title="本次任务从入队到结束，包含排队时间">
      {running ? '已用' : '总耗时'} {formatTime(Math.max(0, end - job.created_at))}
    </span> : job.status === 'queued' && !detail ? '等待后台处理' : null}
    {running && job.stage === 'transcribing' && <>
      {job.model && ` · ${job.model}`}
      <progress aria-label="识别进度" value={job.progress_percent ?? undefined} max={100} />
      {job.progress_percent == null ? '等待识别进度' : `识别进度 ${job.progress_percent}%`}
      {job.cpu_percent != null && ` · CPU ${job.cpu_percent.toFixed(1)}%`}
      {job.peak_memory_mib != null && ` · 峰值提交内存 ${Math.round(job.peak_memory_mib)} MiB`}
    </>}
  </span>;
}
function ModelOption({ value, children, disabled }: { value: string; children: string; disabled?: boolean }) {
  return <Select.Item value={value} className="model-option" disabled={disabled}>
    <Select.ItemText>{children}</Select.ItemText>
    <Select.ItemIndicator className="model-check" aria-hidden="true">✓</Select.ItemIndicator>
  </Select.Item>;
}
export function Library({ health }: { health: Health | null }) {
  const [models, setModels] = useState<ModelAvailability[] | null>(null);
  const [model, setModel] = useState('');
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [modelError, setModelError] = useState(false);
  const workerReady = health?.transcription_ready;
  useEffect(() => {
    if (!settingsOpen) return;
    const controller = new AbortController();
    let timer: number;
    setModels(null);
    setModelError(false);
    async function refresh() {
      try {
        const available = await request<ModelAvailability[]>('/api/models/status', { signal: controller.signal });
        if (!controller.signal.aborted) {
          setModels(available);
          setModelError(false);
        }
      } catch {
        if (!controller.signal.aborted) {
          setModels(null);
          setModelError(true);
        }
      }
      if (!controller.signal.aborted) timer = window.setTimeout(refresh, 5000);
    }
    void refresh();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [settingsOpen, workerReady]);
  const availableModels = models?.filter(entry => entry.selectable) ?? [];
  const defaultModel = models?.find(entry => entry.is_default);
  const defaultModelLabel = defaultModel ? `服务端默认（${defaultModel.name}）` : '服务端默认';
  const chosenModel = model ? availableModels.find(entry => entry.name === model) : defaultModel;
  const modelUnavailable = models !== null && (model ? !chosenModel :
    !!defaultModel && !defaultModel.selectable && defaultModel.status !== 'offline');
  const modelStatus = modelUnavailable ? (model ? '原选模型已不可用，请重新选择。' : '服务端默认模型当前不可用，请选择其他模型。') :
    modelError ? '无法获取服务端模型，稍后自动重试。' :
    models === null ? '正在获取服务端模型…' :
    availableModels.length === 0 ? '服务端暂无可选模型。' : '';
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
        if (!controller.signal.aborted) {
          setPage({ items, next_cursor: cursor });
          const listed = new Set(items.map(item => item.id));
          setTransfers(rows => rows.some(row => row.mediaId && listed.has(row.mediaId))
            ? rows.filter(row => !row.mediaId || !listed.has(row.mediaId)) : rows);
          setLoadError('');
          failures = 0;
        }
      } catch (error) { if (!controller.signal.aborted) { setLoadError(message(error)); failures++; } }
      if (!controller.signal.aborted) timer = window.setTimeout(refresh, Math.min(30000, document.hidden ? 15000 : 2500 * 2 ** failures));
    }
    void refresh();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [limit, revision]);
  const update = (id: string, value: Partial<Transfer>) => { if (mounted.current) setTransfers(rows => rows.map(row => row.id === id ? { ...row, ...value } : row)); };
  async function queryReceipt(id: string) {
    try {
      const receipt = await request<Receipt>(`/api/uploads/${id}`);
      update(id, { state: 'received', mediaId: receipt.media_id, error: undefined });
      setRevision(n => n + 1);
    } catch (error) { update(id, { error: error instanceof ApiError && error.status === 404 ? '暂未查到接收记录，请稍后再次查询。不要重复上传。' : message(error) }); }
  }
  async function select(files: FileList | null) {
    if (!files?.length || uploadLock.current) return;
    if (modelUnavailable) {
      setError('当前模型不可用，请在转写设置中重新选择。');
      if (input.current) input.current.value = '';
      return;
    }
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
        const receipt = await upload(selected[i], id, progress => update(id, { progress }), model);
        update(id, { state: 'received', mediaId: receipt.media_id, progress: 100 });
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
  async function changeTaskModel(item: Media) {
    if (actionLock.current) return;
    if (!chosenModel?.selectable) {
      setSettingsOpen(true);
      setError('请先在转写设置中选择可用模型，再重试此操作。');
      return;
    }
    actionLock.current = true; setBusy(item.id);
    const queued = item.latest_job?.status === 'queued';
    try {
      await request(`/api/media/${item.id}/${queued ? 'model' : 'retry'}`, {
        method: queued ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: chosenModel.name }),
      });
      setRevision(n => n + 1);
      setError('');
    } catch (error) { setError(message(error)); }
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
    <p id="delete-description">只删除 ReCue 媒体库中的上传副本、播放副本、讲稿和阅读进度，删除后无法恢复。你电脑上用于上传的源文件会保留。运行中的任务会先停止。</p>
    <div className="modal-actions"><button autoFocus disabled={!!busy} onClick={() => setDeleting(null)}>取消</button><button className="danger" disabled={!!busy} onClick={() => void remove()}>{busy ? '正在请求…' : '确认删除'}</button></div>
  </dialog><div className="title"><h1>媒体</h1><div className="upload-tools"><button className="primary" disabled={uploading} onClick={() => input.current?.click()}>↑ 上传音视频</button></div>
    <input ref={input} type="file" multiple hidden accept="audio/*,video/*,.mkv,.mov" onChange={event => void select(event.target.files)} /></div>
    <details className="processing-settings" open={settingsOpen} onToggle={event => setSettingsOpen(event.currentTarget.open)}>
      <summary>转写设置</summary>
      <p className="muted">本次选择用于后续上传。排队受阻的任务可改用所选模型。</p>
      <div className="model-picker">
        <label className="model-picker-label" htmlFor="transcription-model">转写模型</label>
        <Select.Root value={model ? `model:${model}` : 'default'}
          onValueChange={value => setModel(value === 'default' ? '' : value.slice('model:'.length))}
          disabled={uploading || (!model && availableModels.length === 0)}>
          <Select.Trigger id="transcription-model" className="model-trigger" aria-describedby="model-status">
            <span className="model-trigger-value" title={model ? chosenModel?.name : defaultModel?.name}>
              <Select.Value>{model ? chosenModel?.name ?? '请选择可用模型' : defaultModelLabel}</Select.Value>
            </span>
            <Select.Icon className="model-chevron" />
          </Select.Trigger>
          <Select.Portal>
            <Select.Content className="model-menu" position="popper" align="start" sideOffset={6} collisionPadding={12}>
              <Select.ScrollUpButton className="model-scroll">⌃</Select.ScrollUpButton>
              <Select.Viewport>
                <ModelOption value="default" disabled={!!defaultModel && !defaultModel.selectable}>
                  {defaultModelLabel}
                </ModelOption>
                {model && !chosenModel &&
                  <ModelOption value={`model:${model}`} disabled>请选择可用模型</ModelOption>}
                {availableModels.map(entry =>
                  <ModelOption key={entry.name} value={`model:${entry.name}`}>{entry.name}</ModelOption>)}
              </Select.Viewport>
              <Select.ScrollDownButton className="model-scroll">⌄</Select.ScrollDownButton>
            </Select.Content>
          </Select.Portal>
        </Select.Root>
      </div>
      <p id="model-status" className="muted" role="status" hidden={!modelStatus}>{modelStatus}</p>
      {!!models?.length && <ul className="model-catalog" aria-label="模型可用性">
        {models.map(entry => <li key={entry.name}>
          <strong>{entry.name}</strong>
          <div>{modelLabels[entry.status]}{entry.is_default && ' · 服务端默认'}
            {!['available', 'disabled'].includes(entry.status) && ` · ${entry.message}`}</div>
          {entry.required_memory_mib != null && <div>
            {entry.memory_source && memorySources[entry.memory_source]} {entry.required_memory_mib} MiB
            {entry.recommended_memory_mib != null && ` · 推荐可用 ${entry.recommended_memory_mib} MiB`}
            {entry.available_memory_mib != null && ` · 处理电脑当前可用 ${entry.available_memory_mib} MiB`}
          </div>}
        </li>)}
      </ul>}
    </details>
    <p className="workflow">上传音视频 → 等待转写 → 点击「阅读」。完成后可播放媒体、搜索讲稿，点击句子定位播放。</p>
    {health && !health.transcription_ready && <section className="processing-notice" role="status" aria-label="转写服务状态">
      <h2>{health.worker.status === 'blocked_config' ? '转写配置缺失或无效' : health.worker.status === 'offline' ? '转写进程未运行或已失联' : '转写服务尚未就绪'}</h2>
      <p>现在上传只会保存文件并排队，不会生成讲稿。已接收的文件无需重新上传。服务恢复后，排队任务会自动开始；标为「中断」的任务请点击「重试」。</p>
      {health.worker.status === 'blocked_config' ? <p>请检查 processing.local.json 或启动终端中 <code>RECUE_PROCESSING_CONFIG</code>，按 README 的「首次启动」准备 whisper.cpp 和模型、核对配置及文件哈希，再停止并重新启动服务。具体校验错误见启动终端。</p>
        : <p>请检查启动终端输出。如果转写进程已退出，可在 Windows 或 macOS 上运行 <code>node scripts/recue.mjs launch</code> 启动完整服务。</p>}
    </section>}
    {loadError && <p role="alert" className="error">{loadError} · 正在重连，列表可能不是最新状态。 <button onClick={() => setRevision(n => n + 1)}>重新连接</button></p>}
    {error && <p role="alert" className="error">{error} <button onClick={() => setError('')}>关闭提示</button></p>}
    {transfers.length > 0 && <section className="upload-panel" aria-label="上传任务"><h2>上传</h2>{transfers.map(row => <div key={row.id} className="upload-row"><div className="transfer-name"><span title={row.name}>{row.name}</span>{row.state === 'uploading' && <progress value={row.progress} max="100" aria-label={`${row.name} 上传进度`} />}{row.error && <p className="error">{row.error}</p>}</div><span className={`status task-status ${row.state === 'received' ? 'success' : ''}`}>{({ waiting: '等待', uploading: `${row.progress}%`, received: '上传完成', uncertain: '结果待查询', failed: '上传失败' })[row.state]}</span>{row.state === 'uncertain' && <button onClick={() => void queryReceipt(row.id)}>查询结果</button>}</div>)}<p className="muted">上传完成前请保持页面打开。后台转写进度见下方媒体列表。</p></section>}
    <div className="tabs" role="group" aria-label="媒体筛选">{[['all', '全部'], ['working', '处理中'], ['errors', '异常']].map(([value, label]) => <button key={value} className={tab === value ? 'selected' : ''} aria-pressed={tab === value} onClick={() => setTab(value)}>{label}</button>)}</div>
    {!page ? <div className="empty">正在加载媒体…</div> : <><div className="table"><div className="media-row table-head"><span>文件</span><span>时长</span><span>导入时间</span><span>处理详情</span><span>状态</span><span>操作</span></div>
      {items.map(item => <div key={item.id} className="media-row"><div className="file"><span className="file-icon" aria-hidden="true">{item.media_type === 'audio' ? '♫' : '▷'}</span><span className="filename" title={item.display_name}>{item.display_name}</span></div><span className="num">{formatTime(item.duration_ms)}</span><time className="num">{new Date(item.created_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}</time><div className="detail"><TaskMetrics item={item} /></div><span className={`status ${item.lifecycle === 'delete_failed' ? 'failure' : item.lifecycle !== 'active' ? '' : item.latest_job?.status === 'succeeded' ? 'success' : ['failed', 'interrupted'].includes(item.latest_job?.status ?? '') ? 'failure' : ''}`}>{status(item)}</span><div className="media-actions">{item.lifecycle === 'active' && item.latest_job?.status === 'succeeded' ? <a className="open" href={`#${item.id}`} onClick={event => { if (uploading) event.preventDefault(); }} aria-disabled={uploading}>阅读</a> : item.lifecycle === 'active' && ['failed', 'interrupted'].includes(item.latest_job?.status ?? '') ? <button disabled={!!busy} onClick={() => void retry(item)}>重试</button> : null}{item.lifecycle === 'active' && (item.latest_job?.wait_reason ||
        (['failed', 'interrupted'].includes(item.latest_job?.status ?? '') && item.latest_job?.requested_model)) &&
        <button disabled={!!busy} onClick={() => void changeTaskModel(item)}>改用所选模型</button>}<button disabled={!!busy || item.lifecycle === 'deleting'} aria-label={`${item.lifecycle === 'delete_failed' ? '重试删除' : '删除'} ${item.display_name}`} title={item.lifecycle === 'delete_failed' ? '重试删除' : '删除'} onClick={() => setDeleting(item)}>{item.lifecycle === 'delete_failed' ? '重试删除' : '删除'}</button></div></div>)}
      {!items.length && <div className="empty">{page.items.length ? '没有符合筛选条件的媒体' : '还没有媒体，上传一份音视频开始复习。'}</div>}</div>
      {page.next_cursor && <button className="load-more" onClick={() => setLimit(n => n + 20)}>加载更多</button>}</>}
  </>;
}
