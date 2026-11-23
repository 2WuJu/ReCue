import * as Select from '@radix-ui/react-select';
import { useId, type CSSProperties } from 'react';
import type { Health, ModelAvailability } from '../../api/client';

const labels: Record<ModelAvailability['status'], string> = {
  available: '可用', below_recommended: '低于推荐', resource_unknown: '内存数据不足',
  memory_insufficient: '内存不足', pending: '待验证', unsupported: '不支持',
  check_failed: '检查失败', check_timeout: '检查超时', missing: '文件不可用',
  disabled: '已停用', offline: '服务未就绪',
};

export function memory(mib: number) {
  return mib < 1024 ? `${Math.round(mib)} MiB` :
    `${(mib / 1024).toLocaleString('zh-CN', { maximumFractionDigits: 1 })} GiB`;
}

export function HostMemory({ health }: { health: Health | null }) {
  const available = health?.available_memory_mib ?? null;
  const reportedTotal = health?.total_memory_mib ?? 0;
  const total = reportedTotal > 0 ? reportedTotal : null;
  const useGiB = (total ?? available ?? 0) >= 1024;
  const unit = useGiB ? 'GiB' : 'MiB';
  const number = (mib: number) => (mib / (useGiB ? 1024 : 1))
    .toLocaleString('zh-CN', { maximumFractionDigits: 1 });
  const amount = available == null ? '—' : useGiB && available > 0 && available / 1024 < 0.05 ? '<0.1' : number(available);
  const percent = available != null && total != null ? Math.min(100, Math.max(0, 100 * available / total)) : 0;
  const status = !health ? '主机重连中' : health.transcription_ready ? '' :
    health.worker.status === 'blocked_config' ? '转写配置异常' : '转写服务未就绪';

  return <div className="host-memory" role="group" aria-label="处理电脑内存">
    <span className="host-memory-ring" data-known={available != null} aria-hidden="true"
      style={{ '--available': `${percent}%` } as CSSProperties}>
      <span className="host-memory-center"><span className="host-memory-chip" /></span>
    </span>
    <div>
      <div className="host-memory-reading">
        <strong className="host-memory-number">{amount}</strong>
        {total != null ? <span className="host-memory-total">/ {number(total)} {unit}</span> :
          available != null && <span className="host-memory-total">{unit}</span>}
      </div>
      <span className="host-memory-label">{available == null ? '处理电脑内存未知' : '处理电脑可用内存'}</span>
      {status && <span className="host-memory-message" role="status">{status}</span>}
    </div>
  </div>;
}

function ModelMemoryScale({ entry, available, scale, id }: {
  entry: ModelAvailability; available: number | null; scale: number; id: string;
}) {
  const minimum = entry.required_memory_mib;
  const recommended = entry.recommended_memory_mib;
  if (minimum == null && recommended == null) return null;

  const useGiB = scale >= 1024;
  const unit = useGiB ? 'GiB' : 'MiB';
  const number = (mib: number) => (mib / (useGiB ? 1024 : 1))
    .toLocaleString('zh-CN', { maximumFractionDigits: 2 });
  const markers = [
    { kind: 'minimum', label: '最低', amount: minimum },
    { kind: 'recommended', label: '推荐', amount: recommended },
    { kind: 'current', label: '可用', amount: available },
  ].flatMap(({ kind, label, amount }) => amount == null ? [] : [{
    kind, label, value: number(amount), position: `${100 * amount / scale}%`,
  }]);

  return <>
    <span className="task-option-resource" aria-hidden="true">
      <span className="task-resource-track" style={{ '--available': `${100 * (available ?? 0) / scale}%` } as CSSProperties}>
        {markers.map(({ kind, label, value, position }) => <span className="task-resource-marker" key={kind} data-kind={kind}
          style={{ '--position': position } as CSSProperties}>
          <span className="task-resource-label">{label} {value}{(kind === 'current' || available == null) && ` ${unit}`}</span>
        </span>)}
      </span>
    </span>
    <span id={id} className="task-picker-sr">
      {markers.map(({ label, value }) => `${label} ${value} ${unit}`).join('，')}
    </span>
  </>;
}

export function ModelPicker({ models, value, onChange, label, forSubmission, offlineDefault, disabled, container, id }: {
  models: ModelAvailability[] | null;
  value: string;
  onChange: (value: string) => void;
  label: string;
  forSubmission?: boolean;
  offlineDefault?: boolean;
  disabled?: boolean;
  container?: HTMLElement | null;
  id?: string;
}) {
  const labelId = useId();
  const options = models ?? [];
  const selected = options.find(entry => entry.name === value);
  const available = options.find(entry => entry.available_memory_mib != null)?.available_memory_mib ?? null;
  const scale = Math.max(1, available ?? 0, ...options.map(entry =>
    Math.max(entry.required_memory_mib ?? 0, entry.recommended_memory_mib ?? 0)));
  const visibleName = selected?.name ?? '请选择可用模型';
  const triggerBadge = selected?.status === 'below_recommended' ?
    (forSubmission ? '低于推荐 · 可提交' : '低于推荐 · 可用') : selected?.is_default ? '主机默认' : '';

  return <div className="task-picker-field">
    <span id={labelId} className="task-picker-label">{label}</span>
    <Select.Root value={selected?.name ?? ''} onValueChange={onChange} disabled={disabled || !options.length}>
      <Select.Trigger id={id} className="task-picker-trigger" aria-labelledby={`${labelId} ${labelId}-value`}
        aria-describedby={triggerBadge ? `${labelId}-badge` : undefined}>
        <span id={`${labelId}-value`} className="task-picker-value" title={visibleName}>
          <Select.Value>{visibleName}</Select.Value>
        </span>
        {triggerBadge && <span id={`${labelId}-badge`} className="task-option-badge" data-tone={selected?.status}>{triggerBadge}</span>}
        <Select.Icon asChild><span className="task-chevron" /></Select.Icon>
      </Select.Trigger>
      <Select.Portal container={container ?? undefined}>
        <Select.Content className="task-picker-menu" position="popper" align="start" sideOffset={6} collisionPadding={12}>
          {available == null && <p className="task-resource-unavailable">处理电脑内存数据不足</p>}
          <Select.ScrollUpButton className="task-picker-scroll">⌃</Select.ScrollUpButton>
          <Select.Viewport>
            {options.map((entry, index) => {
              const enabled = entry.selectable || (entry.is_default && !!offlineDefault);
              const status = entry.status === 'available' ? (entry.is_default ? '主机默认' : '') : labels[entry.status];
              const chartId = `${labelId}-memory-${index}`;
              const hasThresholds = entry.required_memory_mib != null || entry.recommended_memory_mib != null;
              return <Select.Item key={entry.name} value={entry.name} disabled={!enabled} textValue={entry.name}
                aria-describedby={hasThresholds ? chartId : undefined} className="task-picker-option">
                <span className="task-option-heading">
                  <Select.ItemText>{entry.name}</Select.ItemText>
                  <span className="task-option-badge" data-tone={entry.status} hidden={!status}>{status}</span>
                  <Select.ItemIndicator className="task-option-check" aria-hidden="true">✓</Select.ItemIndicator>
                </span>
                <ModelMemoryScale entry={entry} available={available} scale={scale} id={chartId} />
                {!enabled && (entry.status !== 'memory_insufficient' || !hasThresholds) &&
                  <span className="task-option-note">{entry.message}</span>}
              </Select.Item>;
            })}
          </Select.Viewport>
          <Select.ScrollDownButton className="task-picker-scroll">⌄</Select.ScrollDownButton>
        </Select.Content>
      </Select.Portal>
    </Select.Root>
  </div>;
}
