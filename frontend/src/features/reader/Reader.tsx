import { useEffect, useMemo, useRef, useState } from 'react';
import { formatTime, message, request, type Media, type Transcript, type Progress } from '../../api/client';
import { activeSegment, searchTranscript, connectProgressWriter } from './model';

export function Reader({ mediaId }: { mediaId: string }) {
  const [wide, setWide] = useState(false);
  const [data, setData] = useState<{ media: Media; transcript: Transcript; progress: Progress } | null>(null);
  const [error, setError] = useState(''), [playError, setPlayError] = useState(''), [saveError, setSaveError] = useState('');
  const [revision, setRevision] = useState(0), [ready, setReady] = useState(false);
  const [active, setActive] = useState(-1), [following, setFollowing] = useState(true);
  const [query, setQuery] = useState(''), [selected, setSelected] = useState(0), [completed, setCompleted] = useState(false);
  const player = useRef<HTMLMediaElement | null>(null), sentences = useRef<HTMLDivElement | null>(null);
  const restored = useRef(false), save = useRef<(() => void) | null>(null);
  const programScroll = useRef<number | null>(null);
  const hits = useMemo(() => searchTranscript(data?.transcript.segments ?? [], query), [data, query]);
  const currentHit = hits[selected];
  useEffect(() => {
    const controller = new AbortController();
    setError(''); setData(null); setReady(false); restored.current = false;
    Promise.all([
      request<Media>(`/api/media/${mediaId}`, { signal: controller.signal }),
      request<Transcript>(`/api/media/${mediaId}/transcript`, { signal: controller.signal }),
      request<Progress>(`/api/media/${mediaId}/progress`, { signal: controller.signal }),
    ]).then(([media, transcript, progress]) => { if (!controller.signal.aborted) { setData({ media, transcript, progress }); setCompleted(progress.completed); } })
      .catch(error => { if (!controller.signal.aborted) setError(message(error)); });
    return () => controller.abort();
  }, [mediaId, revision]);
  useEffect(() => {
    if (!data) return;
    const media = player.current!;
    let disposed = false, frame = 0;
    const channel = connectProgressWriter(mediaId, value => request(`/api/media/${mediaId}/progress`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value), keepalive: true,
    }), error => { if (!disposed) setSaveError(error ? `进度保存失败：${message(error)}` : ''); });
    const flush = () => {
      if (!restored.current || !Number.isFinite(media.currentTime)) return;
      const duration = data.media.duration_ms!;
      const ended = media.ended || media.currentTime * 1000 >= duration;
      channel.write({ position_ms: ended ? duration : Math.min(duration, Math.max(0, Math.round(media.currentTime * 1000))), completed: ended });
      setCompleted(ended);
    };
    save.current = flush;
    const sync = () => setActive(activeSegment(data.transcript.segments, media.currentTime * 1000));
    const seeked = () => { sync(); flush(); };
    const tick = () => { sync(); frame = requestAnimationFrame(tick); };
    const play = () => { cancelAnimationFrame(frame); tick(); };
    const pause = () => { cancelAnimationFrame(frame); sync(); flush(); };
    const hidden = () => { if (document.visibilityState === 'hidden') flush(); };
    media.addEventListener('play', play); media.addEventListener('pause', pause);
    media.addEventListener('timeupdate', sync); media.addEventListener('seeking', sync); media.addEventListener('seeked', seeked);
    media.addEventListener('ended', pause); window.addEventListener('pagehide', flush);
    document.addEventListener('visibilitychange', hidden);
    const timer = window.setInterval(() => { if (!media.paused) flush(); }, 5000);
    return () => {
      flush(); disposed = true; channel.disconnect(); save.current = null; cancelAnimationFrame(frame); clearInterval(timer);
      media.removeEventListener('play', play); media.removeEventListener('pause', pause);
      media.removeEventListener('timeupdate', sync); media.removeEventListener('seeking', sync); media.removeEventListener('seeked', seeked);
      media.removeEventListener('ended', pause); window.removeEventListener('pagehide', flush);
      document.removeEventListener('visibilitychange', hidden);
    };
  }, [data, mediaId]);
  function scrollTo(index: number, hit?: number) {
    const pane = sentences.current, row = pane?.querySelector<HTMLElement>(`[data-segment="${index}"]`);
    const target = hit === undefined ? row : pane?.querySelector<HTMLElement>(`[data-hit="${hit}"]`);
    if (pane && target) {
      const before = pane.scrollTop;
      pane.scrollTop += target.getBoundingClientRect().top - pane.getBoundingClientRect().top - pane.clientHeight / 3;
      if (pane.scrollTop !== before) programScroll.current = pane.scrollTop;
    }
  }
  useEffect(() => { if (following && active >= 0) scrollTo(active); }, [active, following]);
  useEffect(() => { if (currentHit) scrollTo(currentHit.segment, selected); }, [currentHit, selected]);
  async function seek(start: number) {
    const media = player.current;
    if (!media || !ready) return;
    media.currentTime = Math.max(0, start - 300) / 1000;
    setCompleted(false);
    try { await media.play(); setPlayError(''); }
    catch { setPlayError('未能自动播放，请点击播放器的播放按钮。'); }
  }
  const props = {
    controls: true, preload: 'metadata', src: `/api/media/${mediaId}/playback`,
    onLoadedMetadata: () => {
      const media = player.current;
      if (!media || !data) return;
      if (!restored.current) {
        media.currentTime = Math.min(data.progress.position_ms / 1000, media.duration);
        media.pause(); restored.current = true;
      }
      setReady(true); setPlayError('');
      setActive(activeSegment(data.transcript.segments, media.currentTime * 1000));
    },
    onError: () => { setReady(false); setPlayError('媒体无法播放，请检查服务连接或浏览器支持。'); },
  };
  function text(index: number, value: string) {
    const matches = hits.map((hit, id) => ({ ...hit, id })).filter(hit => hit.segment === index);
    if (!matches.length) return value;
    // Split at every boundary so overlapping literal matches are counted and marked.
    const boundaries = [...new Set([0, value.length, ...matches.flatMap(hit => [hit.start, hit.end])])].sort((a, b) => a - b);
    return boundaries.slice(0, -1).map((start, part) => {
      const end = boundaries[part + 1], covering = matches.filter(hit => hit.start <= start && hit.end >= end);
      return covering.length ? <mark key={start} data-hit={matches.find(hit => hit.start === start)?.id} className={covering.some(hit => hit.id === selected) ? 'current-hit' : ''}>{value.slice(start, end)}</mark> : value.slice(start, end);
    });
  }
  return <><div className="reader-head"><a className="back" href="#" aria-label="返回媒体列表">←</a><h1 title={data?.media.display_name}>{data?.media.display_name ?? '讲稿阅读'}</h1></div>
    {error ? <div className="error" role="alert">媒体、讲稿或进度加载失败：{error} <button onClick={() => setRevision(n => n + 1)}>重新加载</button></div> : !data ? <div className="empty">正在加载媒体、讲稿和进度…</div> : <div className={wide ? "split wide-video" : "split"}><section className="player-panel" aria-label="媒体播放">
      {data.media.media_type === 'audio' ? <audio {...props} ref={el => { player.current = el; }} /> : <video {...props} ref={el => { player.current = el; }} />}
      <div className="player-options">{data.media.media_type === 'video' && <button className="video-size" aria-pressed={wide} onClick={() => setWide(!wide)}>{wide ? '标准视图' : '宽屏视图'}</button>}<label>播放速度 <select aria-label="播放速度" defaultValue="1" onChange={event => { if (player.current) player.current.playbackRate = Number(event.target.value); }}>{[0.5, 0.75, 1, 1.25, 1.5, 2].map(rate => <option key={rate} value={rate}>{rate}×</option>)}</select></label><span className="muted">{formatTime(data.media.duration_ms)}</span></div>
      {completed && <div className="completion">已播放完成 <button disabled={!ready} onClick={() => { if (player.current) { player.current.currentTime = 0; void player.current.play().catch(() => setPlayError('未能自动播放，请点击播放器的播放按钮。')); setCompleted(false); } }}>从头播放</button></div>}
      {saveError && <p className="error" role="alert">{saveError} <button onClick={() => save.current?.()}>重试保存</button></p>}
      {playError && <p className="error" role="alert">{playError} <button onClick={() => { setPlayError(''); player.current?.load(); }}>重新加载播放器</button></p>}
    </section><section className="transcript" aria-label="讲稿"><div className="transcript-top"><h2>讲稿</h2><span className="muted">{data.transcript.segments.length} 段</span></div>
      <div className="search-tools"><input type="search" aria-label="搜索讲稿" placeholder="搜索当前讲稿" value={query} onChange={event => { setQuery(event.target.value); setSelected(0); if (event.target.value) setFollowing(false); }} onKeyDown={event => { if (event.key === 'Enter' && hits.length) { event.preventDefault(); setFollowing(false); setSelected(n => (n + (event.shiftKey ? hits.length - 1 : 1)) % hits.length); } }} />
        <span role="status">{hits.length ? `${selected + 1} / ${hits.length}` : query ? '无匹配' : '0 / 0'}</span>
        <button aria-label="上一个匹配" disabled={!hits.length} onClick={() => { setFollowing(false); setSelected(n => (n + hits.length - 1) % hits.length); }}>↑</button>
        <button aria-label="下一个匹配" disabled={!hits.length} onClick={() => { setFollowing(false); setSelected(n => (n + 1) % hits.length); }}>↓</button>
      </div>
      <div className="follow-tools"><span className="muted">{following ? '正在跟随播放' : '自由阅读 · 跟随已暂停'}</span><button onClick={() => { setFollowing(true); scrollTo(active); }}>回到当前播放句</button></div>
      <div className="sentences" ref={sentences} tabIndex={0} aria-label="讲稿内容" onScroll={event => {
        const expected = programScroll.current; programScroll.current = null;
        if (expected === null || Math.abs(event.currentTarget.scrollTop - expected) > 1) setFollowing(false);
      }} onWheel={() => setFollowing(false)} onTouchMove={() => setFollowing(false)} onPointerDown={event => { if (event.target === event.currentTarget) setFollowing(false); }} onKeyDown={event => { if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key)) setFollowing(false); }}>
        {data.transcript.segments.length ? data.transcript.segments.map((segment, index) => <button key={segment.id} data-segment={index} aria-current={active === index ? 'true' : undefined} className={`sentence${active === index ? ' playing' : ''}`} disabled={!ready} onClick={() => void seek(segment.start_ms)}><time>{formatTime(segment.start_ms)}</time><span>{text(index, segment.text)}</span></button>) : <div className="empty">未识别到语音，可播放原内容核对音轨。</div>}
      </div></section></div>}
  </>;
}
