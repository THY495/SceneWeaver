import { useEffect, useRef, useState } from 'react'
import { Sprite } from './CharacterStage'
import {
  imageAssetUsages,
  type SceneImageTarget,
  type CharacterImageTarget,
  type ImageUsage,
} from './imageAssetUsage'

type Usage = ImageUsage
export type ImageAsset = { id: string; name: string; kind: 'png'; content_hash: string }

export function ProjectAssets({
  scenes,
  characters,
  request,
  onLocate,
  onApply,
  catalog,
  onRegister,
  onUploadState,
}: {
  catalog: ImageAsset[]
  onUploadState: (busy: boolean) => void
  onRegister: (hash: string, name: string) => void
  scenes: SceneImageTarget[]
  characters: CharacterImageTarget[]
  request: (path: string, init?: RequestInit) => Promise<unknown>
  onLocate: (usage: Usage) => void
  onApply: (usage: Pick<Usage, 'kind' | 'id' | 'name'>, image: string) => void
}) {
  const [query, setQuery] = useState('')
  const [kind, setKind] = useState('all')
  const [selection, setSelection] = useState('')
  const [targetKind, setTargetKind] = useState<'scene' | 'character'>('scene')
  const [targetId, setTargetId] = useState('')
  const [notice, setNotice] = useState('')
  const [limit, setLimit] = useState(12)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])
  const assets = imageAssetUsages(catalog, scenes, characters)
  const assetName = (id: string, uses: Usage[]) =>
    catalog.find((a) => a.content_hash === id)?.name || uses.map((u) => u.name).join('、') || '未命名图片'
  const matches = [...assets.entries()].filter(
    ([id, uses]) =>
      (kind === 'all' ||
        (kind === 'unused'
          ? !uses.length
          : uses.some((u) =>
              kind === 'scene'
                ? u.purpose === 'background'
                : kind === 'object'
                  ? u.purpose === 'object'
                  : u.kind === kind,
            ))) &&
      `${id} ${assetName(id, uses)} ${uses.map((u) => u.name).join(' ')}`
        .toLocaleLowerCase()
        .includes(query.trim().toLocaleLowerCase()),
  )
  const selected = matches.some(([id]) => id === selection) ? selection : matches[0]?.[0]
  const uses = selected ? assets.get(selected) || [] : []
  const targets = targetKind === 'scene' ? scenes : characters
  const target = targets.find((t) => t.id === targetId)
  return (
    <section aria-label="工程图片素材">
      <p>导入的图片即使尚未使用，也会随工程保存和导出。同一图片只保留一个素材条目。</p>
      <fieldset disabled={uploading} className="asset-library-controls">
        <label>
          导入工程图片（PNG，最多 4 MB）
          <input
            type="file"
            accept="image/png"
            onChange={async (e) => {
              const file = e.target.files?.[0]
              e.target.value = ''
              if (!file) return
              if (file.size > 4 * 1024 * 1024) {
                setError('图片不能超过 4 MB')
                return
              }
              setUploading(true)
              onUploadState(true)
              setError('')
              try {
                const form = new FormData()
                form.append('file', file)
                const result = (await request('/role/presentation/sprites', {
                  method: 'POST',
                  body: form,
                })) as { id: string }
                if (!mounted.current) return
                const prior = catalog.find((a) => a.content_hash === result.id)
                onRegister(result.id, prior?.name || file.name.slice(0, 160))
                setSelection(result.id)
                setQuery('')
                setKind('all')
                setNotice(prior ? '该图片已在工程中，已选中现有素材。' : '图片已加入工程草稿，请保存工程。')
              } catch (e) {
                if (mounted.current) setError(String(e))
              } finally {
                onUploadState(false)
                if (mounted.current) setUploading(false)
              }
            }}
          />
        </label>
        {uploading && <p role="status">正在导入；离开此页将取消加入工程。</p>}
        {error && <p role="alert">{error}</p>}
        <div className="asset-browser-filters">
          <label>
            查找图片
            <input
              type="search"
              value={query}
              placeholder="引用对象名称或图片标识"
              onChange={(e) => {
                setQuery(e.target.value)
                setLimit(12)
                setNotice('')
              }}
            />
          </label>
          <label>
            图片用途
            <select
              value={kind}
              onChange={(e) => {
                setKind(e.target.value)
                setLimit(12)
                setNotice('')
              }}
            >
              <option value="all">全部图片</option>
              <option value="unused">未使用图片</option>
              <option value="scene">场景背景</option>
              <option value="character">角色立绘</option>
              <option value="object">物品与装饰</option>
            </select>
          </label>
        </div>
        <div className="asset-browser-workspace">
          <div>
            <div className="asset-browser-grid" aria-label="图片列表">
              {matches.slice(0, limit).map(([id, references]) => (
                <button
                  key={id}
                  className="asset-browser-card"
                  aria-label={`查看图片：${assetName(id, references)}`}
                  aria-pressed={id === selected}
                  onClick={() => {
                    setSelection(id)
                    setNotice('')
                  }}
                >
                  <Sprite
                    id={id}
                    name={assetName(id, references)}
                    request={request}
                    background={references.some((u) => u.purpose === 'background')}
                  />
                  <span>{assetName(id, references)}</span>
                  <small>{references.length} 处引用</small>
                </button>
              ))}
            </div>
            {!matches.length && (
              <p role="status">
                {assets.size
                  ? '没有匹配图片。请调整搜索或用途筛选。'
                  : '本工程尚无图片。可从上方导入，也可在场景或角色属性中添加。'}
              </p>
            )}
            {matches.length > limit && <button onClick={() => setLimit(limit + 12)}>再显示 12 张</button>}
          </div>
          {selected && (
            <aside className="asset-browser-inspector" aria-label="图片属性与引用">
              <h3>图片引用</h3>
              <label>
                素材名称
                <input
                  key={selected}
                  maxLength={160}
                  defaultValue={assetName(selected, uses)}
                  onBlur={(e) => {
                    const name = e.target.value.trim()
                    if (name) onRegister(selected, name)
                    else e.target.value = assetName(selected, uses)
                  }}
                />
              </label>
              {!uses.length && <p>尚未使用。保存后仍会保留在工程中。</p>}
              <code>{selected}</code>
              <ul>
                {uses.map((usage) => (
                  <li key={usage.referenceId}>
                    <button onClick={() => onLocate(usage)}>
                      {usage.kind === 'scene' ? '定位场景' : '定位角色'}：{usage.name}
                    </button>
                  </li>
                ))}
              </ul>
              <h3>复用到其他对象</h3>
              <label>
                应用用途
                <select
                  value={targetKind}
                  onChange={(e) => {
                    setTargetKind(e.target.value as 'scene' | 'character')
                    setTargetId('')
                    setNotice('')
                  }}
                >
                  <option value="scene">场景背景</option>
                  <option value="character">角色常态立绘</option>
                </select>
              </label>
              <label>
                目标对象
                <select
                  value={target?.id || ''}
                  onChange={(e) => {
                    setTargetId(e.target.value)
                    setNotice('')
                  }}
                >
                  <option value="">请选择目标对象</option>
                  {targets.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name || '未命名对象'}
                    </option>
                  ))}
                </select>
              </label>
              {target?.image && target.image !== selected && (
                <p>将替换“{target.name}”当前图片引用，其他对象不受影响。</p>
              )}
              <button
                disabled={!target || target.image === selected}
                onClick={() => {
                  if (!target) return
                  onApply({ kind: targetKind, id: target.id, name: target.name }, selected)
                  setNotice(`已应用到“${target.name}”，请保存工程。`)
                }}
              >
                {target?.image === selected ? '目标已使用此图片' : '应用到目标对象'}
              </button>
              {notice && <p role="status">{notice}</p>}
            </aside>
          )}
        </div>
      </fieldset>
    </section>
  )
}
