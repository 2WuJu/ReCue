import { useEffect, useRef, useState } from 'react';
import { ApiError, message, request, upload, uploadId, type Health, type Media, type MediaPage, type ModelAvailability, type Receipt } from '../../api/client';
import { HostMemory, ModelPicker } from './Controls';
import { audioExtension, jobModel, mediaFilter, MediaRow, TaskFile, TaskRow, videoExtension, type TaskFilter } from './TaskRow';
import './transcription.css';

type Draft = { id: string; file: File; audio: boolean };
type Transfer = {
  id: string; mediaId?: string; name: string; audio: boolean; model: string; progress: number;
  state: 'waiting' | 'uploading' | 'received' | 'uncertain' | 'failed'; error?: string;
};
type TaskDialog = { kind: 'delete' | 'model'; item: Media };

const lastModelKey = 'recue.transcription.lastModel';
const maxBatch = 5;
const maxBytes = 4 * 1024 ** 3;
const accept = 'audio/*,video/*,.mp3,.wav,.m4a,.aac,.flac,.ogg,.opus,.mp4,.mov,.mkv,.webm,.avi,.m4v,.mpeg,.mpg,.ts,.wma,.wmv,.aif,.aiff';
const tabs: [TaskFilter, string][] = [['all', '全部'], ['working', '处理中'], ['errors', '异常']];
const emptyMessages: Record<TaskFilter, string> = {
  all: '暂无转写任务', working: '没有正在处理的任务', errors: '没有异常任务',
};
const transferStatus: Record<Transfer['state'], [string, string]> = {
  waiting: ['等待上传', 'neutral'], uploading: ['上传中', ''], received: ['上传完成', 'success'],
  uncertain: ['待查回执', 'warning'], failed: ['上传失败', 'failure'],
};

function readLastModel() {
  if (typeof window === 'undefined') return null;
  try { return window.localStorage.getItem(lastModelKey); } catch { return null; }
}

function useModels(ready?: boolean) {
  const [models, setModels] = useState<ModelAvailability[] | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let timer: number;
    async function refresh() {
      try {
        const entries = await request<ModelAvailability[]>('/api/models/status', { signal: controller.signal });
        if (!controller.signal.aborted) { setModels(entries); setError(false); }
      } catch {
        if (!controller.signal.aborted) setError(true);
      }
      if (!controller.signal.aborted) timer = window.setTimeout(refresh, 5000);
    }
    void refresh();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [ready]);
  return { models, error };
}

export function Library({ health }: { health: Health | null }) {
  const { models, error: modelError } = useModels(health?.transcription_ready);
  const [lastModel, setLastModel] = useState(readLastModel);
  const [selection, setSelection] = useState<string | null>(null);
  const defaultModel = models?.find(entry => entry.is_default);
  const lastAvailable = models?.find(entry => entry.name === lastModel && entry.selectable);
  const selected = selection ?? lastAvailable?.name ?? defaultModel?.name ?? '';
  const chosenModel = models?.find(entry => entry.name === selected);
  const offlineDefault = !!health && !health.transcription_ready && defaultModel?.status === 'offline';
  const waitingForWorker = chosenModel?.is_default && offlineDefault;
  const canSubmit = !modelError && (chosenModel?.selectable || waitingForWorker);
  const fellBack = selection == null && lastModel != null && lastModel !== selected;
  const modelNote = modelError ? '无法获取模型状态，正在重试。' : models === null ? '正在获取主机模型…' :
    fellBack && !canSubmit ? '上次使用的模型暂不可用，请选择可用模型。' :
    waitingForWorker ? (fellBack ? '上次使用的模型暂不可用，已选主机默认模型。文件将在主机恢复后开始处理。' :
      '转写服务尚未就绪，文件将在主机恢复后开始处理。') :
    !canSubmit ? chosenModel?.message ?? '请选择可用模型。' :
    fellBack ? '上次使用的模型暂不可用，已选主机默认模型。' : '';

  const [page, setPage] = useState<MediaPage | null>(null);
  const [error, setError] = useState('');
  const [loadError, setLoadError] = useState('');
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [transfers, setTransfers] = useState<Transfer[]>([]);
  const [uploading, setUploading] = useState(false);
  const [checking, setChecking] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [taskDialog, setTaskDialog] = useState<TaskDialog | null>(null);
  const [taskModel, setTaskModel] = useState('');
  const [dialogError, setDialogError] = useState('');
  const [tab, setTab] = useState<TaskFilter>('all');
  const [limit, setLimit] = useState(20);
  const [revision, setRevision] = useState(0);
  const [dragging, setDragging] = useState(false);
  const dragDepth = useRef(0);
  const input = useRef<HTMLInputElement>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const actionLock = useRef(false);
  const uploadLock = useRef(false);
  const mounted = useRef(true);

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    if (taskDialog) dialog.current?.showModal();
    else dialog.current?.close();
  }, [taskDialog]);
  useEffect(() => {
    function beforeUnload(event: BeforeUnloadEvent) {
      if (uploadLock.current) event.preventDefault();
    }
    function preventFileNavigation(event: DragEvent) {
      if (!event.dataTransfer?.types.includes('Files')) return;
      event.preventDefault();
      if (event.type === 'drop') {
        dragDepth.current = 0;
        setDragging(false);
      }
    }
    window.addEventListener('beforeunload', beforeUnload);
    window.addEventListener('dragover', preventFileNavigation);
    window.addEventListener('drop', preventFileNavigation);
    return () => {
      window.removeEventListener('beforeunload', beforeUnload);
      window.removeEventListener('dragover', preventFileNavigation);
      window.removeEventListener('drop', preventFileNavigation);
    };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    let timer: number;
    let failures = 0;
    async function refresh() {
      try {
        const items: Media[] = [];
        let cursor: string | null = null;
        do {
          const query = new URLSearchParams({ limit: String(Math.min(100, limit - items.length)) });
          if (cursor) query.set('cursor', cursor);
          const result: MediaPage = await request<MediaPage>(`/api/media?${query}`, { signal: controller.signal });
          items.push(...result.items);
          cursor = result.next_cursor;
        } while (cursor && items.length < limit);
        if (!controller.signal.aborted) {
          setPage({ items, next_cursor: cursor });
          const mediaIds = new Set(items.map(item => item.id));
          const requestIds = new Set(items.map(item => item.request_id));
          setTransfers(rows => rows.filter(row => !requestIds.has(row.id) && !(row.mediaId && mediaIds.has(row.mediaId))));
          setLoadError('');
          failures = 0;
        }
      } catch (error) {
        if (!controller.signal.aborted) { setLoadError(message(error)); failures++; }
      }
      if (!controller.signal.aborted) {
        timer = window.setTimeout(refresh, Math.min(30000, document.hidden ? 15000 : 2500 * 2 ** failures));
      }
    }
    void refresh();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [limit, revision]);

  function addFiles(files: FileList) {
    const added: Draft[] = [];
    const errors: string[] = [];
    for (const file of files) {
      if (drafts.length + added.length >= maxBatch) {
        errors.push('每批最多 5 个文件。');
        break;
      }
      const audio = file.type.startsWith('audio/') || audioExtension.test(file.name);
      if (!audio && !file.type.startsWith('video/') && !videoExtension.test(file.name)) {
        errors.push(`${file.name}：请选择音视频文件。`);
      } else if (!file.size || file.size > maxBytes) {
        errors.push(`${file.name}：文件须非空且不超过 4 GB。`);
      } else {
        added.push({ id: uploadId(), file, audio });
      }
    }
    setError(errors.join(' '));
    if (added.length) setDrafts(rows => [...added, ...rows]);
  }

  function updateTransfer(id: string, fields: Partial<Transfer>) {
    if (mounted.current) setTransfers(rows => rows.map(row => row.id === id ? { ...row, ...fields } : row));
  }

  async function queryReceipt(id: string) {
    setChecking(id);
    try {
      const receipt = await request<Receipt>(`/api/uploads/${id}`);
      updateTransfer(id, { state: 'received', mediaId: receipt.media_id, error: undefined });
      setRevision(n => n + 1);
    } catch (error) {
      if (error instanceof ApiError && error.status === 410) updateTransfer(id, { state: 'failed', error: error.message });
      else updateTransfer(id, { error: error instanceof ApiError && error.status === 404 ?
        '暂未查到接收记录，请稍后再次查询。不要重复上传。' : message(error) });
    } finally {
      setChecking(null);
    }
  }

  async function submit() {
    if (uploadLock.current || !drafts.length || !canSubmit || !chosenModel) return;
    const batch = drafts;
    const model = chosenModel.name;
    uploadLock.current = true;
    setUploading(true);
    setError('');
    setDrafts([]);
    setTransfers(rows => [...batch.map<Transfer>(({ id, file, audio }) => ({
      id, name: file.name, audio, model, progress: 0, state: 'waiting',
    })), ...rows]);
    setLastModel(model);
    try { window.localStorage.setItem(lastModelKey, model); } catch {}
    setSelection(null);
    setTab('working');
    try {
      for (const { id, file } of batch) {
        updateTransfer(id, { state: 'uploading' });
        try {
          const receipt = await upload(file, id, progress => updateTransfer(id, { progress }), model);
          updateTransfer(id, { state: 'received', mediaId: receipt.media_id, progress: 100 });
        } catch (error) {
          const rejected = error instanceof ApiError && error.status < 500 && error.status !== 408 && error.status !== 409;
          updateTransfer(id, { state: rejected ? 'failed' : 'uncertain', error: message(error) });
        }
        if (mounted.current) setRevision(n => n + 1);
      }
    } finally {
      uploadLock.current = false;
      if (mounted.current) setUploading(false);
    }
  }

  function openTaskDialog(kind: TaskDialog['kind'], item: Media) {
    const original = jobModel(item) ?? defaultModel?.name;
    setTaskModel(original ?? '');
    setDialogError('');
    setTaskDialog({ kind, item });
  }

  async function runTaskAction(item: Media, action: 'retry' | 'delete' | 'model', model?: string) {
    if (actionLock.current) return;
    actionLock.current = true;
    setBusy(true);
    const url = `/api/media/${item.id}`;
    const changeQueued = action === 'model' && item.latest_job?.status === 'queued';
    try {
      if (action === 'delete') await request(url, { method: 'DELETE' });
      else await request(`${url}/${changeQueued ? 'model' : 'retry'}`, {
        method: changeQueued ? 'PUT' : 'POST',
        ...(model ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model }) } : {}),
      });
      setTaskDialog(null);
      setError('');
    } catch (error) {
      if (action === 'retry') setError(message(error));
      else setDialogError(action === 'delete' ? `删除请求未确认：${message(error)}。可安全重试。` : message(error));
    } finally {
      actionLock.current = false;
      setBusy(false);
      setRevision(n => n + 1);
    }
  }

  const taskChoice = !modelError ? models?.find(entry => entry.selectable && entry.name === taskModel) : undefined;
  const taskOriginal = taskDialog ? jobModel(taskDialog.item) ?? defaultModel?.name : undefined;
  const modelChanged = taskChoice && taskChoice.name !== taskOriginal;
  const items = page?.items.filter(item => tab === 'all' || tab === mediaFilter(item)) ?? [];
  const pending = transfers.filter(row => tab === 'all' ||
    tab === (['failed', 'uncertain'].includes(row.state) ? 'errors' : 'working'));
  const empty = !!page && !page.items.length && !drafts.length && !transfers.length;
  const dialogIsDelete = taskDialog?.kind === 'delete';

  return <div className="transcription">
    <div className="task-title">
      <h1>转写任务</h1>
      <HostMemory health={health} />
    </div>
    <section className="task-upload-card" aria-label="添加转写文件">
      <div className="task-drop-wrap" data-empty={empty}>
        <button type="button" className="task-drop" data-dragging={dragging} onClick={() => input.current?.click()}
          onDragEnter={event => {
            if (event.dataTransfer.types.includes('Files')) { dragDepth.current++; setDragging(true); }
          }}
          onDragLeave={() => {
            dragDepth.current = Math.max(0, dragDepth.current - 1);
            if (!dragDepth.current) setDragging(false);
          }}
          onDragOver={event => {
            if (event.dataTransfer.types.includes('Files')) { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; }
          }}
          onDrop={event => {
            event.preventDefault();
            addFiles(event.dataTransfer.files);
          }}>
          <span className="task-upload-icon" aria-hidden="true">↑</span>
          <span><strong>拖入音视频，或<span className="task-pick">选择文件</span></strong>
            <span className="task-help">每批最多 5 个文件，单个不超过 4 GB</span></span>
        </button>
        <input ref={input} type="file" multiple hidden accept={accept} onChange={event => {
          if (event.currentTarget.files) addFiles(event.currentTarget.files);
          event.currentTarget.value = '';
        }} />
      </div>
      {drafts.length > 0 && <section className="task-drafts" aria-labelledby="task-draft-title">
        <h2 id="task-draft-title">待上传文件</h2>
        <ul>
          {drafts.map(({ id, file, audio }) => <li key={id} className="task-draft-row">
            <TaskFile name={file.name} audio={audio} />
            <span className="task-draft-size">{file.size >= 1024 ** 2 ?
              `${Math.round(file.size / 1024 ** 2)} MB` : `${Math.ceil(file.size / 1024)} KB`}</span>
            <button className="task-action" aria-label={`移除 ${file.name}`}
              onClick={() => setDrafts(rows => rows.filter(row => row.id !== id))}>移除</button>
          </li>)}
        </ul>
      </section>}
      {error && <div className="task-error" role="alert">{error} <button className="task-action" onClick={() => setError('')}>关闭提示</button></div>}
      <div className="task-batch">
        <div>
          <ModelPicker id="batch-model" models={models} value={selected} onChange={setSelection}
            label="本批转写模型" forSubmission offlineDefault={offlineDefault} disabled={uploading} />
          {modelNote && <p className="task-warning" role="status">{modelNote}</p>}
        </div>
        <button className="task-primary" disabled={!drafts.length || uploading || !canSubmit}
          onClick={() => void submit()}>
          {drafts.length ? `提交转写（${drafts.length} 个文件）` : '提交转写'}
        </button>
      </div>
    </section>
    <section className="task-card" aria-label="转写任务列表">
      {loadError && <div className="task-error" role="alert">{loadError} · 正在重新连接
        <button className="task-action" onClick={() => setRevision(n => n + 1)}>重新连接</button></div>}
      <div className="task-tabs" role="group" aria-label="任务筛选">
        {tabs.map(([value, label]) => <button key={value} aria-pressed={tab === value} onClick={() => setTab(value)}>{label}</button>)}
      </div>
      <div className="task-row task-head" aria-hidden="true">
        <span>文件</span><span>时长</span><span>导入时间</span><span>处理详情</span><span>状态</span><span>操作</span>
      </div>
      <div role="list">
        {pending.map(row => <TaskRow key={row.id} name={row.name} audio={row.audio} state={transferStatus[row.state]}
          details={<>
            <div>{row.model}</div>
            {row.state === 'uploading' && <>
              <div>上传进度 {row.progress}%</div>
              <progress max={100} value={row.progress} aria-label="上传进度" />
            </>}
            {row.error && <div>{row.error}</div>}
          </>}
          action={row.state === 'uncertain' ? <button className="task-action" disabled={checking !== null}
            aria-label={`查询回执 ${row.name}`} onClick={() => void queryReceipt(row.id)}>查询回执</button> :
            row.state === 'failed' ? <button className="task-action" aria-label={`关闭 ${row.name} 的上传提示`}
              onClick={() => setTransfers(rows => rows.filter(item => item.id !== row.id))}>关闭</button> : null} />)}
        {items.map(item => <MediaRow key={item.request_id ?? item.id} item={item} busy={busy}
          retry={() => void runTaskAction(item, 'retry')} change={() => openTaskDialog('model', item)}
          remove={() => openTaskDialog('delete', item)} />)}
      </div>
      {!page && <div className="task-empty">正在加载转写任务…</div>}
      {page && !items.length && !pending.length &&
        <div className="task-empty">{emptyMessages[tab]}</div>}
      {page?.next_cursor && <button className="task-more" onClick={() => setLimit(n => n + 20)}>加载更多</button>}
    </section>
    {uploading && <p className="task-help" role="status">上传完成前请保持页面打开。</p>}
    <dialog className="task-dialog" ref={dialog} aria-labelledby="task-dialog-title"
      aria-describedby={dialogIsDelete ? 'task-dialog-delete-copy' : 'task-dialog-name'}
      onCancel={event => { event.preventDefault(); if (!actionLock.current) setTaskDialog(null); }}>
      {taskDialog && <>
        <h2 id="task-dialog-title">{dialogIsDelete ? '删除此任务？' : '更换转写模型'}</h2>
        <p id="task-dialog-name" className="task-dialog-name">{taskDialog.item.display_name}</p>
        {dialogIsDelete ? <p id="task-dialog-delete-copy">将删除 ReCue 中的媒体副本、讲稿和阅读进度。原文件会保留。运行中的任务会先停止。</p> :
          <ModelPicker models={models} value={taskModel} onChange={setTaskModel} label="转写模型"
            disabled={busy} container={dialog.current} />}
        {dialogError && <p className="task-error" role="alert">{dialogError}</p>}
        <div className="task-dialog-actions">
          <button className="task-action" disabled={busy} onClick={() => setTaskDialog(null)}>{dialogIsDelete ? '取消' : '关闭'}</button>
          {dialogIsDelete ? <button className="task-primary task-destructive" disabled={busy}
            onClick={() => void runTaskAction(taskDialog.item, 'delete')}>确认删除</button> :
            <button className="task-primary" disabled={busy || !modelChanged}
              onClick={() => { if (taskChoice) void runTaskAction(taskDialog.item, 'model', taskChoice.name); }}>更换模型</button>}
        </div>
      </>}
    </dialog>
  </div>;
}
