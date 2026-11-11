import type { components } from './schema';
export type Media = components['schemas']['MediaView'];
export type MediaPage = components['schemas']['MediaPage'];
export type Transcript = components['schemas']['Transcript'];
export type Health = components['schemas']['Health'];
export type Receipt = components['schemas']['UploadReceipt'];
export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}
export async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, options);
  const body = await response.json();
  if (!response.ok) throw new ApiError(body.message ?? `服务返回 ${response.status}`, response.status);
  return body as T;
}
export function message(error: unknown) { return error instanceof Error ? error.message : '连接失败'; }
export function formatTime(ms: number | null) {
  if (ms === null) return '—';
  const s = Math.floor(ms / 1000);
  return [Math.floor(s / 3600), Math.floor(s / 60) % 60, s % 60].slice(s >= 3600 ? 0 : 1).map(n => String(n).padStart(2, '0')).join(':');
}
// getRandomValues also works on trusted LAN HTTP, where randomUUID is unavailable.
export function uploadId() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
export function upload(file: File, id: string, progress: (value: number) => void): Promise<Receipt> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `/api/media?display_name=${encodeURIComponent(file.name)}`);
    xhr.setRequestHeader('X-Upload-ID', id);
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = e => { if (e.lengthComputable) progress(Math.floor(e.loaded / e.total * 100)); };
    xhr.onload = () => {
      try {
        const body = JSON.parse(xhr.responseText);
        if (xhr.status >= 200 && xhr.status < 300) resolve(body);
        else reject(new ApiError(body.message ?? '上传失败', xhr.status));
      } catch { reject(new Error('响应无法解析，请查询接收结果')); }
    };
    xhr.onerror = () => reject(new Error('连接中断，请查询接收结果'));
    xhr.send(file);
  });
}
