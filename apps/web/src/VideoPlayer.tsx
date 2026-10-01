import { useEffect, useRef, useState } from 'react'

export function VideoPlayer({
  actor,
  node,
  title,
  epoch,
  paused,
  allowSkip,
  request,
  onToggle,
  onComplete,
}: {
  actor: string
  node: string
  title: string
  epoch: number
  paused: boolean
  allowSkip: boolean
  request: (path: string, init?: RequestInit) => Promise<unknown>
  onToggle: () => Promise<void>
  onComplete: (outcome: 'ended' | 'skipped') => Promise<void>
}) {
  const video = useRef<HTMLVideoElement>(null)
  const [source, setSource] = useState(''),
    [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0),
    [pending, setPending] = useState(false)
  const outcome = useRef<'ended' | 'skipped' | null>(null)
  const resumeAt = useRef(0)
  useEffect(() => {
    let live = true
    if (video.current) {
      resumeAt.current = video.current.currentTime || resumeAt.current
      video.current.pause()
    }
    setSource('')
    setError('')
    if (paused)
      return () => {
        live = false
      }
    request(
      `/role/presentation/player-videos/${encodeURIComponent(actor)}/${encodeURIComponent(node)}/ticket`,
      { method: 'POST' },
    )
      .then((value) => {
        if (!live) return
        const ticket = (value as { ticket: string }).ticket
        const base = /^\/runs\/[a-f0-9-]{36}(?=\/|$)/.exec(location.pathname)?.[0] || ''
        setSource(
          `${base}/api/role/presentation/player-video-stream/${encodeURIComponent(actor)}/${encodeURIComponent(node)}?ticket=${encodeURIComponent(ticket)}`,
        )
      })
      .catch(() => {
        if (live) setError('片段加载失败，请重试。')
      })
    return () => {
      live = false
    }
  }, [actor, node, attempt, epoch, paused])
  async function complete(value: 'ended' | 'skipped') {
    if (pending) return
    outcome.current ||= value
    if (paused) {
      setError('请先继续游玩，再确认片段进度。')
      return
    }
    setPending(true)
    setError('')
    video.current?.pause()
    try {
      await onComplete(outcome.current)
    } catch {
      setError('片段进度尚未确认，请重试。')
    } finally {
      setPending(false)
    }
  }
  return (
    <section className="vn-video" aria-label="故事片段">
      <h2>{title}</h2>
      {source ? (
        <video
          ref={video}
          src={source}
          controls
          playsInline
          preload="metadata"
          style={{ width: '100%', maxHeight: '70vh', background: '#000' }}
          onPlay={() => {
            if (paused || pending || outcome.current) video.current?.pause()
          }}
          onLoadedMetadata={() => {
            if (video.current && resumeAt.current > 0)
              video.current.currentTime = Math.min(
                resumeAt.current,
                video.current.duration || resumeAt.current,
              )
          }}
          onEnded={() => void complete('ended')}
          onError={() => setError('无法播放此片段。请重试，或在允许时跳过。')}
        />
      ) : (
        <div
          role="status"
          style={{
            aspectRatio: '16 / 9',
            background: '#000',
            color: '#fff',
            display: 'grid',
            placeItems: 'center',
          }}
        >
          {paused ? '继续游玩后加载片段。' : '正在加载片段…'}
        </div>
      )}
      <p>{paused ? '游戏已暂停，请继续游玩后播放。' : '点击播放观看片段；重新进入会从片段开头播放。'}</p>
      <button disabled={pending} onClick={() => void onToggle()}>
        {paused ? '继续游玩' : '暂停游玩'}
      </button>
      {allowSkip && (
        <button disabled={paused || pending} onClick={() => void complete('skipped')}>
          跳过片段
        </button>
      )}
      {error && (
        <div role="alert">
          <p>{error}</p>
          <button
            disabled={pending || (paused && !!outcome.current)}
            onClick={() => (outcome.current ? void complete(outcome.current) : setAttempt(attempt + 1))}
          >
            重试
          </button>
        </div>
      )}
      {!pending && !error && outcome.current && (
        <button disabled={paused} onClick={() => void complete(outcome.current!)}>
          确认片段进度
        </button>
      )}
      {pending && <p role="status">正在继续故事…</p>}
    </section>
  )
}
