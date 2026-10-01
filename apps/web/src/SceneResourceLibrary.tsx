import { useState } from 'react'
import type { StagePerson } from './StagePlayer'
import type { QuickResource } from './StudioQuickCreate'
export type ResourceDrop = { kind: QuickResource['kind']; id: string; sprite?: string }
export function SceneResourceLibrary({
  people,
  items,
  images,
  onAdd,
  onCreate,
}: {
  people: StagePerson[]
  items: { id: string; name: string }[]
  images: { id: string; label: string }[]
  onAdd: (resource: ResourceDrop) => void
  onCreate: (kind: QuickResource['kind']) => void
}) {
  const [kind, setKind] = useState<QuickResource['kind']>('person'),
    [query, setQuery] = useState(''),
    [sprite, setSprite] = useState('')
  const selectedSprite = images.some((i) => i.id === sprite) ? sprite : images[0]?.id || ''
  const entries =
    kind === 'person'
      ? people.map((p) => ({ id: p.id, name: p.name }))
      : kind === 'item'
        ? items
        : images.map((i) => ({ id: i.id, name: i.label }))
  return (
    <section aria-label="场景资源库">
      <h3>资源库</h3>
      <label>
        资源类型
        <select value={kind} onChange={(e) => setKind(e.target.value as QuickResource['kind'])}>
          <option value="person">人物</option>
          <option value="item">物品</option>
          <option value="background">背景</option>
          <option value="decoration">装饰</option>
        </select>
      </label>
      <input
        type="search"
        aria-label="搜索场景资源"
        placeholder="搜索资源"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      <button onClick={() => onCreate(kind)}>
        新建{{ person: '人物', item: '物品', background: '背景', decoration: '装饰' }[kind]}
      </button>
      {kind === 'item' && (
        <label>
          摆放图片
          <select value={selectedSprite} onChange={(e) => setSprite(e.target.value)}>
            {images.map((i) => (
              <option key={i.id} value={i.id}>
                {i.label}
              </option>
            ))}
          </select>
        </label>
      )}
      <small>拖入画布或点击加入。背景替换本场背景；物品仍受世界可见性规则约束。</small>
      {kind === 'item' && !selectedSprite && (
        <p>物品已在世界中创建。要摆入画布，请先导入一张图片并选择“摆放图片”。</p>
      )}
      {entries
        .filter((e) => e.name.includes(query))
        .map((e) => {
          const data = { kind, id: e.id, sprite: kind === 'item' ? selectedSprite : e.id }
          return (
            <button
              key={e.id}
              draggable
              disabled={kind === 'item' && !selectedSprite}
              onDragStart={(event) => {
                event.dataTransfer.setData('application/x-sceneweaver-resource', JSON.stringify(data))
                event.dataTransfer.effectAllowed = 'copy'
              }}
              onClick={() => onAdd(data)}
            >
              {e.name}
            </button>
          )
        })}
      {!entries.length && <p>还没有资源，可以点击上方新建。</p>}
    </section>
  )
}
