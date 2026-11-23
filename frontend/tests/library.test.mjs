import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

const server = await createServer({
  root: fileURLToPath(new URL('..', import.meta.url)),
  server: { middlewareMode: true },
});
after(() => server.close());
const { Library } = await server.ssrLoadModule('/src/features/library/Library.tsx');
function render(status, ready = false, available = 32768, total = 65536) {
  return renderToStaticMarkup(createElement(Library, { health: status == null ? null : {
    status: 'ok', database: 'ok', worker: { status },
    ffmpeg_available: true, model_file_available: ready, transcription_ready: ready,
    available_memory_mib: available, total_memory_mib: total,
  } }));
}

test('blocked configuration keeps the memory reading and shows a short status', () => {
  const html = render('blocked_config');
  assert.match(html, /转写配置异常/);
  assert.match(html, /--available:50%/);
});

test('offline worker leaves host memory visible while the API responds', () => {
  const html = render('offline');
  assert.match(html, /转写服务未就绪/);
  assert.match(html, /处理电脑可用内存/);
});

test('disconnected host clears the reading and shows reconnecting', () => {
  const html = render(null);
  assert.match(html, /处理电脑内存未知/);
  assert.match(html, /主机重连中/);
  assert.doesNotMatch(html, /host-memory-total/);
});

test('unavailable memory stays unknown and a measured zero stays visible', () => {
  const unknown = render('processing', true, null);
  const zero = render('processing', true, 0);
  assert.match(unknown, /处理电脑内存未知/);
  assert.match(zero, /host-memory-number">0<\/strong>/);
  assert.match(zero, /处理电脑可用内存/);
});

test('ready service shows the transcription queue with staged upload controls', () => {
  const html = render('processing', true);
  assert.match(html, /<h1>转写任务<\/h1>/);
  assert.match(html, /选择文件/);
  assert.match(html, /本批转写模型/);
  assert.match(html, /提交转写/);
  assert.match(html, /aria-label="添加转写文件"/);
  assert.doesNotMatch(html, /设为此浏览器首选|恢复主机默认/);
  assert.match(html, /处理电脑可用内存/);
  assert.match(html, /host-memory-number">32<\/strong>/);
  assert.match(html, /host-memory-total">\/ 64 GiB/);
  assert.doesNotMatch(html, /<details|host-memory-message/);
});
