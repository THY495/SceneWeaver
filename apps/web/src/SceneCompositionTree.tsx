import type { StageSlot } from './stageTransform'
import type { StagePerson } from './StagePlayer'
import type { SceneObject } from './sceneObjectModel'
import { compositionEntries, reorderComposition } from './sceneComposition'
export function SceneCompositionTree({
  slots,
  objects,
  people,
  selected,
  onSelect,
  onChange,
}: {
  slots: StageSlot[]
  objects: SceneObject[]
  people: StagePerson[]
  selected: string
  onSelect: (kind: 'person' | 'object', id: string) => void
  onChange: (slots: StageSlot[], objects: SceneObject[]) => void
}) {
  const entries = compositionEntries(slots, objects)
  return (
    <section aria-label="场景层级">
      <h3>层级 · 从前到后</h3>
      <small>同层可拖动排序；人物与物品共用实体层。</small>
      {(['foreground', 'entities', 'decoration'] as const).map((layer) => (
        <div key={layer}>
          <h4>{{ foreground: '前景遮挡', entities: '场景实体', decoration: '背景装饰' }[layer]}</h4>
          {entries
            .filter((e) => e.layer === layer)
            .map((e, i, group) => {
              const entity =
                e.kind === 'person' ? slots.find((s) => s.actor === e.id) : objects.find((o) => o.id === e.id)
              const name =
                e.kind === 'person'
                  ? people.find((p) => p.id === e.id)?.name
                  : objects.find((o) => o.id === e.id)?.name
              function move(to: string) {
                const result = reorderComposition(slots, objects, e.key, to)
                onChange(result.slots, result.objects)
              }
              return (
                <div
                  key={e.key}
                  className="scene-tree-row"
                  draggable
                  onDragStart={(event) =>
                    event.dataTransfer.setData('application/x-sceneweaver-layer', e.key)
                  }
                  onDragOver={(event) => {
                    if (event.dataTransfer.types.includes('application/x-sceneweaver-layer'))
                      event.preventDefault()
                  }}
                  onDrop={(event) => {
                    event.preventDefault()
                    const result = reorderComposition(
                      slots,
                      objects,
                      event.dataTransfer.getData('application/x-sceneweaver-layer'),
                      e.key,
                    )
                    onChange(result.slots, result.objects)
                  }}
                >
                  <button aria-pressed={selected === e.key} onClick={() => onSelect(e.kind, e.id)}>
                    {name}
                    {entity?.editor_hidden ? ' · 隐藏' : ''}
                    {entity?.editor_locked ? ' · 锁定' : ''}
                  </button>
                  <button
                    aria-label={`${name} 前移`}
                    disabled={i === 0}
                    onClick={() => move(group[i - 1].key)}
                  >
                    ↑
                  </button>
                  <button
                    aria-label={`${name} 后移`}
                    disabled={i === group.length - 1}
                    onClick={() => move(group[i + 1].key)}
                  >
                    ↓
                  </button>
                </div>
              )
            })}
        </div>
      ))}
    </section>
  )
}
