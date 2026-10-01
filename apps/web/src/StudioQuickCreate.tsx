import { useState } from 'react'
export type QuickResource = {
  kind: 'person' | 'item' | 'background' | 'decoration'
  name: string
  core: string
  location: string
  sprite: string
  container: boolean
}
export function StudioQuickCreate({
  kind,
  images,
  location,
  onCreate,
  onClose,
  request,
}: {
  kind: QuickResource['kind']
  images: { id: string; label: string }[]
  location: string
  onCreate: (data: QuickResource) => Promise<void>
  onClose: () => void
  request: (path: string, init?: RequestInit) => Promise<unknown>
}) {
  const [data, setData] = useState<QuickResource>({
    kind,
    name: '',
    core: '',
    location,
    sprite: images[0]?.id || '',
    container: false,
  })
  const [busy, setBusy] = useState(false),
    [error, setError] = useState('')
  return (
    <div className="studio-modal-backdrop">
      <section role="dialog" aria-modal="true" aria-label="新建资源" className="studio-quick-dialog">
        <h2>新建{{ person: '人物', item: '物品', background: '背景', decoration: '装饰' }[kind]}</h2>
        <label>
          资源名称
          <input
            autoFocus
            value={data.name}
            maxLength={100}
            onChange={(e) => setData({ ...data, name: e.target.value })}
          />
        </label>
        {kind === 'person' && (
          <label>
            身份与性格
            <textarea value={data.core} onChange={(e) => setData({ ...data, core: e.target.value })} />
          </label>
        )}
        {(kind === 'person' || kind === 'item') && (
          <label>
            初始地点
            <input value={data.location} onChange={(e) => setData({ ...data, location: e.target.value })} />
          </label>
        )}
        {kind === 'item' && (
          <label>
            <input
              type="checkbox"
              checked={data.container}
              onChange={(e) => setData({ ...data, container: e.target.checked })}
            />
            可容纳其他物品
          </label>
        )}
        <label>
          资源图片
          <select value={data.sprite} onChange={(e) => setData({ ...data, sprite: e.target.value })}>
            <option value="">暂无图片</option>
            {images.map((i) => (
              <option key={i.id} value={i.id}>
                {i.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          导入 PNG
          <input
            type="file"
            accept="image/png"
            disabled={busy}
            onChange={async (e) => {
              const file = e.target.files?.[0]
              if (!file) return
              setBusy(true)
              setError('')
              try {
                const form = new FormData()
                form.append('file', file)
                const result = (await request('/role/presentation/sprites', {
                  method: 'POST',
                  body: form,
                })) as { id: string }
                setData((d) => ({ ...d, sprite: result.id }))
              } catch (err) {
                setError(String(err))
              } finally {
                setBusy(false)
              }
            }}
          />
        </label>
        {data.sprite && !images.some((i) => i.id === data.sprite) && <p>新图片已准备，创建时加入本工程。</p>}
        <p>创建后加入当前场景。取消不会新增工程资源。</p>
        {error && <p role="alert">{error}</p>}
        <button
          disabled={
            busy ||
            !data.name.trim() ||
            ((kind === 'background' || kind === 'decoration') && !data.sprite) ||
            (kind === 'person' && !data.core.trim())
          }
          onClick={async () => {
            setBusy(true)
            setError('')
            try {
              await onCreate({ ...data, name: data.name.trim() })
              onClose()
            } catch (e) {
              setError(String(e))
              setBusy(false)
            }
          }}
        >
          创建并加入场景
        </button>
        <button disabled={busy} onClick={onClose}>
          取消
        </button>
      </section>
    </div>
  )
}
