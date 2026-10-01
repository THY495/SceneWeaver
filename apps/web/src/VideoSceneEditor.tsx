import { useEffect, useRef, useState } from 'react'

export type VideoAsset = { id: string; name: string; kind: 'mp4'; content_hash: string; bytes: number }
export type VideoClip = { asset: string; allow_skip: boolean }
export function VideoSceneEditor({
  value,
  assets,
  request,
  onChange,
  onUpload,
}: {
  value?: VideoClip | null
  assets: VideoAsset[]
  request: (path: string, init?: RequestInit) => Promise<unknown>
  onChange: (value: VideoClip | null) => void
  onUpload: (asset: VideoAsset) => void
}) {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    [source, setSource] = useState('')
  const [retry, setRetry] = useState(0)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])
  const selected = assets.find((a) => a.id === value?.asset)
  useEffect(() => {
    let live = true
    setSource('')
    setError('')
    if (selected)
      request(`/role/presentation/videos/${selected.content_hash}/ticket`, { method: 'POST' })
        .then((v) => {
          if (live) {
            const base = /^\/runs\/[a-f0-9-]{36}(?=\/|$)/.exec(location.pathname)?.[0] || ''
            setSource(
              `${base}/api/role/presentation/video-preview/${selected.content_hash}?ticket=${encodeURIComponent((v as { ticket: string }).ticket)}`,
            )
          }
        })
        .catch(() => {
          if (live) setError('视频预览加载失败，请重试。')
        })
    return () => {
      live = false
    }
  }, [selected?.content_hash, retry])
  return (
    <section aria-label="视频场景编辑">
      <h3>固定视频片段</h3>
      <p>片段播放结束后，再执行下方固定对白与 AI 互动。幕后场景不向玩家播放视频。</p>
      <label>
        工程视频
        <select
          value={value?.asset || ''}
          disabled={busy}
          onChange={(e) =>
            onChange(e.target.value ? { asset: e.target.value, allow_skip: value?.allow_skip ?? true } : null)
          }
        >
          <option value="">请选择视频</option>
          {assets.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        上传 MP4（最多 256 MB，建议 H.264＋AAC）
        <input
          type="file"
          accept="video/mp4,.mp4"
          disabled={busy}
          onChange={async (e) => {
            const file = e.target.files?.[0]
            e.target.value = ''
            if (!file) return
            if (file.size > 256 * 1024 * 1024) {
              setError('视频不能超过 256 MB。')
              return
            }
            setBusy(true)
            setError('')
            try {
              const form = new FormData()
              form.append('file', file)
              const result = (await request('/role/presentation/videos', { method: 'POST', body: form })) as {
                id: string
                bytes: number
              }
              if (mounted.current)
                onUpload({
                  id: crypto.randomUUID(),
                  name: file.name,
                  kind: 'mp4',
                  content_hash: result.id,
                  bytes: result.bytes,
                })
            } catch (e) {
              if (mounted.current) setError(String(e))
            } finally {
              if (mounted.current) setBusy(false)
            }
          }}
        />
      </label>
      {value && (
        <label>
          <input
            type="checkbox"
            checked={value.allow_skip}
            onChange={(e) => onChange({ ...value, allow_skip: e.target.checked })}
          />
          允许玩家跳过片段
        </label>
      )}
      {source && (
        <video
          key={source}
          controls
          preload="metadata"
          src={source}
          style={{ width: '100%', maxHeight: 420, background: '#000' }}
          onError={() => setError('无法解码此视频，请改用 H.264＋AAC 的 MP4 文件。')}
        />
      )}
      {busy && <p role="status">正在上传视频…</p>}
      {error && (
        <div role="alert">
          {error}
          <button onClick={() => setRetry(retry + 1)}>重试预览</button>
        </div>
      )}
      <p>视频随完整工程和 Windows 游戏文件夹保存；单个 JSON 导出不携带视频。</p>
    </section>
  )
}
