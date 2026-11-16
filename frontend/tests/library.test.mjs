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
function render(status, ready = false) {
  return renderToStaticMarkup(createElement(Library, { health: {
    status: 'ok', database: 'ok', worker: { status },
    ffmpeg_available: true, model_file_available: ready, transcription_ready: ready,
  } }));
}

test('blocked configuration explains why uploads wait and how to resume', () => {
  const html = render('blocked_config');
  assert.match(html, /转写配置缺失或无效/);
  assert.match(html, /RECUE_PROCESSING_CONFIG/);
  assert.match(html, /无需重新上传/);
});

test('offline worker gives a different recovery action', () => {
  const html = render('offline');
  assert.match(html, /转写进程未运行或已失联/);
  assert.match(html, /node scripts\/recue.mjs launch/);
  assert.doesNotMatch(html, /转写配置缺失或无效/);
});

test('ready service explains the upload to reading workflow without a warning', () => {
  const html = render('processing', true);
  assert.match(html, /上传音视频 → 等待转写 → 点击「阅读」/);
  assert.doesNotMatch(html, /转写配置缺失或无效|转写进程未运行或已失联/);
});
