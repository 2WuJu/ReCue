export type Timed = { start_ms: number; end_ms: number };
export function activeSegment(segments: Timed[], time: number) {
  let low = 0, high = segments.length;
  while (low < high) { const mid = (low + high) >>> 1; if (segments[mid].start_ms <= time) low = mid + 1; else high = mid; }
  const index = low - 1;
  return index >= 0 && time < segments[index].end_ms ? index : -1;
}
export type Hit = { segment: number; start: number; end: number };
export function searchTranscript(segments: { text: string }[], query: string): Hit[] {
  if (!query) return [];
  // ASCII folding keeps UTF-16 offsets stable, including Chinese and emoji.
  const fold = (text: string) => text.replace(/[A-Z]/g, char => char.toLowerCase());
  const needle = fold(query), hits: Hit[] = [];
  segments.forEach(({ text }, segment) => {
    const source = fold(text);
    for (let start = source.indexOf(needle); start >= 0; start = source.indexOf(needle, start + 1))
      hits.push({ segment, start, end: start + query.length });
  });
  return hits;
}
export type Position = { position_ms: number; completed: boolean };
export function progressWriter(send: (value: Position) => Promise<unknown>, report: (error: unknown | null) => void) {
  let pending: Position | null = null, running = false;
  async function drain() {
    if (running) return;
    running = true;
    while (pending) {
      const value = pending; pending = null;
      try { await send(value); report(null); }
      catch (error) { report(error); if (!pending) { pending = value; break; } }
    }
    running = false;
  }
  return (value: Position) => { pending = value; void drain(); };
}

// Keep serialization across Reader unmount/remount within the same tab.
const channels = new Map<string, { write: (value: Position) => void; listeners: Set<(error: unknown | null) => void> }>();
export function connectProgressWriter(key: string, send: (value: Position) => Promise<unknown>, report: (error: unknown | null) => void) {
  let channel = channels.get(key);
  if (!channel) {
    const listeners = new Set<(error: unknown | null) => void>();
    channel = { write: progressWriter(send, error => listeners.forEach(listener => listener(error))), listeners };
    channels.set(key, channel);
  }
  channel.listeners.add(report);
  return { write: channel.write, disconnect: () => channel.listeners.delete(report) };
}
