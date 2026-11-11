import { useEffect, useRef, useState } from 'react';
import { formatTime, message, request, type Media, type Transcript } from '../../api/client';
export function Reader({ mediaId }: { mediaId: string }) {
  const [data, setData] = useState<{ media: Media; transcript: Transcript } | null>(null);
  const [error, setError] = useState('');
  const [playError, setPlayError] = useState('');
  const [revision, setRevision] = useState(0);
  const [ready, setReady] = useState(false);
  const player = useRef<HTMLMediaElement | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setError(''); setData(null); setReady(false);
    Promise.all([request<Media>(`/api/media/${encodeURIComponent(mediaId)}`, { signal: controller.signal }), request<Transcript>(`/api/media/${encodeURIComponent(mediaId)}/transcript`, { signal: controller.signal })])
      .then(([media, transcript]) => { if (!controller.signal.aborted) setData({ media, transcript }); })
      .catch(error => { if (!controller.signal.aborted) setError(message(error)); });
    return () => controller.abort();
  }, [mediaId, revision]);
  async function seek(start: number) {
    const media = player.current;
    if (!media || !ready) return;
    media.currentTime = Math.max(0, start - 300) / 1000;
    try { await media.play(); setPlayError(''); }
    catch { setPlayError('未能自动播放，请点击播放器的播放按钮。'); }
  }
  const props = {
    controls: true, preload: 'metadata', src: `/api/media/${encodeURIComponent(mediaId)}/playback`,
    onLoadedMetadata: () => { setReady(true); setPlayError(''); },
    onError: () => { setReady(false); setPlayError('媒体无法播放，请检查服务连接或浏览器支持。'); },
  };
  return <><div className="reader-head"><a className="back" href="#" aria-label="返回媒体列表">←</a><h1 title={data?.media.display_name}>{data?.media.display_name ?? '讲稿阅读'}</h1></div>
    {error ? <div className="error" role="alert">{error} <button onClick={() => setRevision(n => n + 1)}>重新加载</button></div> : !data ? <div className="empty">正在加载媒体和讲稿…</div> : <div className="split"><section className="player-panel" aria-label="媒体播放">
      {data.media.media_type === 'audio' ? <audio {...props} ref={el => { player.current = el; }} /> : <video {...props} ref={el => { player.current = el; }} />}
      <div className="player-options"><label>播放速度 <select defaultValue="1" onChange={event => { if (player.current) player.current.playbackRate = Number(event.target.value); }}>{[0.5, 0.75, 1, 1.25, 1.5, 2].map(rate => <option key={rate} value={rate}>{rate}×</option>)}</select></label><span className="muted">{formatTime(data.media.duration_ms)}</span></div>
      {playError && <p className="error" role="alert">{playError} <button onClick={() => { setPlayError(''); player.current?.load(); }}>重新加载播放器</button></p>}
    </section><section className="transcript" aria-label="讲稿"><div className="transcript-top"><h2>讲稿</h2><span className="muted">{data.transcript.segments.length} 段</span></div><div className="sentences">
      {data.transcript.segments.length ? data.transcript.segments.map(segment => <button key={segment.id} className="sentence" disabled={!ready} onClick={() => void seek(segment.start_ms)}><time>{formatTime(segment.start_ms)}</time><span>{segment.text}</span></button>) : <div className="empty">未识别到语音，可播放原内容核对音轨。</div>}
    </div></section></div>}
  </>;
}
