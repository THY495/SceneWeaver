import { useState } from 'react'
import type { SceneObject } from './sceneObjectModel'
import { defaultTransform } from './stageTransform'

export function SceneObjectsEditor({
  objects,
  images,
  items,
  onChange,
  selectedId,
  onSelect,
  inspectorOnly = false,
}: {
  selectedId?: string
  inspectorOnly?: boolean
  onSelect?: (id: string) => void
  objects: SceneObject[]
  images: { id: string; label: string }[]
  items: { id: string; name: string }[]
  onChange: (objects: SceneObject[]) => void
}) {
  const [image, setImage] = useState(''),
    [item, setItem] = useState(''),
    [localSelected, setLocalSelected] = useState('')
  const selected = selectedId ?? localSelected
  const select = onSelect || setLocalSelected
  const imageId = images.some((i) => i.id === image) ? image : images[0]?.id || ''
  const obj = objects.find((o) => o.id === selected)
  function patch(p: Partial<SceneObject>) {
    onChange(objects.map((o) => (o.id === selected ? { ...o, ...p } : o)))
  }
  return (
    <section aria-label="物品与装饰装配">
      {!inspectorOnly && (
        <>
          <h3>物品与装饰</h3>
          <p>真实物品绑定世界状态；装饰只显示图片。开场编排预览显示作者配置，实际游玩按感知过滤。</p>
          <label>
            实例图片
            <select value={imageId} onChange={(e) => setImage(e.target.value)}>
              {images.map((i) => (
                <option key={i.id} value={i.id}>
                  {i.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            绑定对象
            <select value={item} onChange={(e) => setItem(e.target.value)}>
              <option value="">纯画面装饰</option>
              {items.map((i) => (
                <option key={i.id} value={i.id}>
                  {i.name}
                </option>
              ))}
            </select>
          </label>
          <button
            disabled={!imageId || objects.length >= 100}
            onClick={() => {
              const id = crypto.randomUUID()
              onChange([
                ...objects,
                {
                  version: 1,
                  id,
                  kind: item ? 'item' : 'decoration',
                  item: item || null,
                  name: items.find((i) => i.id === item)?.name || images.find((i) => i.id === imageId)!.label,
                  sprite: imageId,
                  transform: defaultTransform(),
                  layer: 'entities',
                  order: 0,
                },
              ])
              select(id)
            }}
          >
            加入本场画面
          </button>
          {!images.length && <p>请先在图片素材中导入图片。</p>}
          <label>
            画面实例
            <select value={obj?.id || ''} onChange={(e) => select(e.target.value)}>
              <option value="">选择实例</option>
              {objects.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.name}
                </option>
              ))}
            </select>
          </label>
        </>
      )}
      {obj && (
        <fieldset>
          <legend>{obj.name}</legend>
          <label>
            实例名称
            <input value={obj.name} onChange={(e) => patch({ name: e.target.value })} />
          </label>
          <label>
            绘制层
            <select
              value={obj.layer}
              onChange={(e) => patch({ layer: e.target.value as SceneObject['layer'] })}
            >
              <option value="decoration">背景装饰</option>
              <option value="entities">场景实体</option>
              <option value="foreground">前景遮挡</option>
            </select>
          </label>
          <label>
            层内顺序
            <input
              type="number"
              value={obj.order}
              min={-1000}
              max={1000}
              onChange={(e) => {
                const n = e.target.valueAsNumber
                if (Number.isInteger(n)) patch({ order: Math.max(-1000, Math.min(1000, n)) })
              }}
            />
          </label>
          <small>实体层人物从 0 开始依次排列；负数在人物后，较大数值在前。</small>
          {(['x', 'y', 'scale_x', 'scale_y', 'rotation'] as const).map((key) => (
            <label key={key}>
              {
                {
                  x: '水平位置',
                  y: '垂直位置',
                  scale_x: '水平缩放',
                  scale_y: '垂直缩放',
                  rotation: '旋转角度',
                }[key]
              }
              <input
                type="number"
                value={obj.transform[key]}
                onChange={(e) => {
                  const n = e.target.valueAsNumber
                  const bounds = {
                    x: [-1600, 3200],
                    y: [-900, 1800],
                    scale_x: [0.1, 4],
                    scale_y: [0.1, 4],
                    rotation: [-180, 180],
                  }[key]
                  if (Number.isFinite(n))
                    patch({
                      transform: { ...obj.transform, [key]: Math.max(bounds[0], Math.min(bounds[1], n)) },
                    })
                }}
              />
            </label>
          ))}
          <label>
            <input
              type="checkbox"
              checked={!!obj.editor_hidden}
              onChange={(e) => patch({ editor_hidden: e.target.checked })}
            />
            编辑时隐藏实例
          </label>
          <label>
            <input
              type="checkbox"
              checked={!!obj.editor_locked}
              onChange={(e) => patch({ editor_locked: e.target.checked })}
            />
            锁定实例画布操作
          </label>
          <button onClick={() => onChange(objects.filter((o) => o.id !== selected))}>移除画面实例</button>
        </fieldset>
      )}
    </section>
  )
}
