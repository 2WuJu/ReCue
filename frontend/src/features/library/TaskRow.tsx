import { useEffect, useState, type ReactNode } from 'react';
import { formatTime, type Media } from '../../api/client';
import { memory } from './Controls';

export type TaskFilter = 'all' | 'working' | 'errors';

export const audioExtension = /\.(mp3|wav|m4a|aac|flac|ogg|opus|wma|aiff?)$/i;
export const videoExtension = /\.(mp4|mov|mkv|webm|avi|m4v|mpeg|mpg|ts|wmv)$/i;

export const retryable = (item: Media) =>
  item.lifecycle === 'active' && (item.latest_job?.status === 'failed' || item.latest_job?.status === 'interrupted');

export const waitingForModel = (item: Media) =>
  item.lifecycle === 'active' && item.latest_job?.status === 'queued' && !!item.latest_job.wait_reason;

export function mediaFilter(item: Media): TaskFilter {
  if (item.lifecycle === 'delete_failed' || waitingForModel(item) ||
      ['failed', 'interrupted', 'cancelled'].includes(item.latest_job?.status ?? '') && item.lifecycle === 'active') return 'errors';
  if (item.lifecycle === 'deleting' || ['queued', 'running'].includes(item.latest_job?.status ?? '')) return 'working';
  return 'all';
}

export const jobModel = (item: Media) => item.latest_job?.model ?? item.latest_job?.requested_model;

function mediaStatus(item: Media): [string, string] {
  if (item.lifecycle === 'deleting') return ['删除中', 'neutral'];
  if (item.lifecycle === 'delete_failed') return ['删除失败', 'failure'];
  if (waitingForModel(item)) return ['等待模型', 'warning'];
  switch (item.latest_job?.status) {
    case 'running': return [{
      preprocessing: '预处理中', transcribing: '转写中', organizing: '整理中',
    }[item.latest_job.stage ?? 'preprocessing'], ''];
    case 'queued': return ['排队中', 'neutral'];
    case 'succeeded': return ['已完成', 'success'];
    case 'failed': return ['失败', 'failure'];
    case 'interrupted': return ['中断', 'failure'];
    case 'cancelled': return ['已取消', 'failure'];
    default: return ['等待任务', 'neutral'];
  }
}

function ElapsedTime({ createdAt }: { createdAt: number }) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    let timer: number;
    function schedule(current: number) {
      timer = window.setTimeout(() => {
        const next = Date.now();
        setNow(next);
        schedule(next);
      }, 1000 - Math.max(0, current - createdAt) % 1000);
    }
    schedule(Date.now());
    return () => window.clearTimeout(timer);
  }, [createdAt]);
  return <>已用 {formatTime(Math.max(0, now - createdAt))}</>;
}

function Metrics({ item, busy, change }: { item: Media; busy: boolean; change?: () => void }) {
  if (item.lifecycle === 'deleting') return <>正在停止任务并清理文件</>;
  if (item.lifecycle === 'delete_failed') return <>{item.delete_error ?? '删除失败，请重试'}</>;
  const job = item.latest_job;
  if (!job) return null;
  const running = job.status === 'running';
  const transcribing = running && job.stage === 'transcribing';
  const progressLabel = running && (job.stage === 'preprocessing' ? '预处理进度' : transcribing ? '转写进度' : null);
  const resources: string[] = [];
  if (transcribing && job.cpu_percent != null) resources.push(`任务 CPU ${job.cpu_percent.toFixed(1)}%`);
  if (transcribing && job.current_memory_mib != null) resources.push(`当前驻留内存 ${memory(job.current_memory_mib)}`);
  if (job.peak_memory_mib != null && (transcribing || !running)) {
    const kind = job.peak_memory_kind === 'rss' ? '峰值驻留内存（采样）' : '峰值提交内存';
    resources.push(`${kind} ${memory(job.peak_memory_mib)}`);
  }
  const model = jobModel(item);
  const elapsed = job.finished_at == null ? null : formatTime(Math.max(0, job.finished_at - job.created_at));
  if (job.status === 'succeeded') {
    if (!elapsed && !resources.length) return model ?? null;
    return <details className="task-completed-details">
      <summary aria-label={model ? `${model}，转写详情` : undefined}>{model ?? '转写详情'}</summary>
      {elapsed && <div>总耗时 {elapsed}</div>}
      {!!resources.length && <div>{resources.join(' · ')}</div>}
    </details>;
  }
  const detail = job.wait_reason ?? job.error_message;
  return <>
    {(model || change) && <div className="task-model-detail">
      <span>{model ?? '主机默认'}</span>
      {change && <button className="task-action" disabled={busy} aria-label={`更换模型 ${item.display_name}`}
        onClick={change}>更换模型</button>}
    </div>}
    {detail && <div>{detail}</div>}
    {running ? <div title="从入队开始，包含排队时间"><ElapsedTime key={job.id} createdAt={job.created_at} /></div> :
      elapsed && <div>总耗时 {elapsed}</div>}
    {progressLabel && <>
      <div>{job.progress_percent == null ? `正在获取${progressLabel}` : `${progressLabel} ${job.progress_percent}%`}</div>
      <progress aria-label={progressLabel} value={job.progress_percent ?? undefined} max={100} />
    </>}
    {!!resources.length && <div className="task-resources">{resources.join(' · ')}</div>}
  </>;
}

export function TaskFile({ name, audio }: { name: string; audio: boolean }) {
  return <div className="task-file">
    <span className="task-file-icon" aria-hidden="true"><svg viewBox="0 0 24 24">
      <path d={audio ? 'M9 17V5l11-2v12M9 8l11-2M9 17c0 3-6 4-6 1s6-4 6-1M20 15c0 3-6 4-6 1s6-4 6-1' :
        'M3 5h13v14H3zM16 9l5-3v12l-5-3'} />
    </svg></span>
    <span className="task-file-name" title={name}>{name}</span>
  </div>;
}

export function TaskRow({ name, audio, duration, imported, details, state, action, deletion }: {
  name: string; audio: boolean; duration?: number | null; imported?: number;
  details?: ReactNode; state: [string, string]; action?: ReactNode; deletion?: ReactNode;
}) {
  const date = imported == null ? null : new Date(imported);
  const importedLabel = date?.toLocaleString('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  }) ?? '—';
  const durationLabel = formatTime(duration ?? null);
  const completed = state[1] === 'success';
  return <div className={completed ? 'task-row task-row-completed' : 'task-row'} role="listitem">
    <TaskFile name={name} audio={audio} />
    <span className="task-meta">{durationLabel}</span>
    {date ? <time className="task-meta" dateTime={date.toISOString()}>{importedLabel}</time> :
      <span className="task-meta">—</span>}
    {completed && <div className="task-completed-meta">
      <span>时长 {durationLabel}</span>
      {date ? <time dateTime={date.toISOString()}>导入 {importedLabel}</time> :
        <span>导入 {importedLabel}</span>}
    </div>}
    <div className="task-metrics">{details}</div>
    <span className="task-status-badge" data-tone={state[1]}>{state[0]}</span>
    <div className="task-row-actions"><div>{action}</div>{deletion}</div>
  </div>;
}

export function MediaRow({ item, busy, retry, change, remove }: {
  item: Media; busy: boolean; retry: () => void; change: () => void; remove: () => void;
}) {
  const active = item.lifecycle === 'active';
  const deletionLabel = item.lifecycle === 'delete_failed' ? '重试删除' : '删除';
  return <TaskRow name={item.display_name} audio={item.media_type === 'audio' ||
    (item.media_type === null && audioExtension.test(item.display_name))}
    duration={item.duration_ms} imported={item.created_at} state={mediaStatus(item)}
    details={<Metrics item={item} busy={busy} change={waitingForModel(item) || retryable(item) ? change : undefined} />}
    action={active && item.latest_job?.status === 'succeeded' ?
      <a className="task-action task-read" aria-label={`阅读 ${item.display_name}`} href={`#${item.id}`}>阅读</a> :
      retryable(item) ? <button className="task-action" disabled={busy}
        aria-label={`重试 ${item.display_name}`} onClick={retry}>重试</button> : null}
    deletion={<button className="task-action task-delete" disabled={busy || item.lifecycle === 'deleting'}
      aria-label={`${deletionLabel} ${item.display_name}`} onClick={remove}>删除</button>} />;
}
