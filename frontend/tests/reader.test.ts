import { test } from 'node:test';
import assert from 'node:assert/strict';
import { activeSegment, searchTranscript, progressWriter, connectProgressWriter } from '../src/features/reader/model.ts';
test('binary lookup clears silence, exact end and handles seeks backwards', () => {
  const segments = [{ start_ms: 100, end_ms: 200 }, { start_ms: 300, end_ms: 400 }];
  assert.deepEqual([0, 100, 199, 200, 299, 300, 400, 150].map(t => activeSegment(segments, t)), [-1, 0, 0, -1, -1, 1, -1, 0]);
  assert.equal(activeSegment([], 0), -1);
});
test('literal occurrence search preserves offsets and includes overlaps', () => {
  const segments = [{ text: '中文 Docker/docker .* 😀' }, { text: 'aaa' }];
  assert.equal(searchTranscript(segments, 'DOCKER').length, 2);
  assert.equal(searchTranscript(segments, '中文').length, 1);
  assert.equal(searchTranscript(segments, '.*').length, 1);
  assert.deepEqual(searchTranscript(segments, 'aa').map(h => h.start), [0, 1]);
  assert.equal(searchTranscript(segments, '').length, 0);
  assert.equal(searchTranscript(segments, 'missing').length, 0);
});
test('slow saves serialize and coalesce without losing a backwards seek', async () => {
  const sent: number[] = [], releases: (() => void)[] = [];
  const write = progressWriter(value => { sent.push(value.position_ms); return new Promise<void>(resolve => releases.push(resolve)); }, () => {});
  write({ position_ms: 50, completed: false });
  write({ position_ms: 100, completed: false });
  write({ position_ms: 20, completed: false });
  assert.deepEqual(sent, [50]);
  releases.shift()!(); await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(sent, [50, 20]); releases.shift()!();
});
test('failed writes report error and retry with newest value', async () => {
  let fail = true;
  const sent: number[] = [], errors: unknown[] = [];
  const write = progressWriter(async value => { sent.push(value.position_ms); if (fail) throw Error('offline'); }, error => errors.push(error));
  write({ position_ms: 70, completed: false });
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(errors[0]); fail = false;
  write({ position_ms: 5, completed: false });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(sent, [70, 5]); assert.equal(errors.at(-1), null);
});


test('reader remount shares the same serial queue', async () => {
  const sent: number[] = [], releases: (() => void)[] = [];
  const send = (value: { position_ms: number }) => { sent.push(value.position_ms); return new Promise<void>(resolve => releases.push(resolve)); };
  const first = connectProgressWriter('remount-test', send, () => {});
  first.write({ position_ms: 80, completed: false }); first.disconnect();
  const next = connectProgressWriter('remount-test', send, () => {});
  next.write({ position_ms: 10, completed: false });
  assert.deepEqual(sent, [80]); releases.shift()!();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(sent, [80, 10]); releases.shift()!(); next.disconnect();
});
