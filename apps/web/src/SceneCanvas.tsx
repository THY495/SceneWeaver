import { useRef, useState, type PointerEvent } from 'react'
import { StagePlayer, type StagePerson } from './StagePlayer'
import { SpriteEditor } from './CharacterStage'
import { SceneResourceLibrary, type ResourceDrop } from './SceneResourceLibrary'
import { StudioQuickCreate, type QuickResource } from './StudioQuickCreate'
import { SceneCompositionTree } from './SceneCompositionTree'
import { OutfitSelector } from './OutfitEditor'
import type { SceneObject } from './sceneObjectModel'
import { SceneObjectsEditor } from './SceneObjectsEditor'

import {
  defaultTransform,
  dragTransform,
  type TransformTool,
  type StageSlot as Slot,
  type StageTransform,
} from './stageTransform'

export function SceneCanvas({
  title,
  background,
  people,
  slots,
  onChange,
  request,
  availablePeople,
  onBackground,
  backgrounds,
  objects = [],
  onObjectsChange,
  images = [],
  items = [],
  onCreateResource,
  onCompositionChange,
  location = '大厅',
}: {
  onCreateResource?: (data: QuickResource) => Promise<void>
  location?: string
  onCompositionChange: (slots: Slot[], objects: SceneObject[]) => void
  onObjectsChange: (objects: SceneObject[]) => void
  images?: { id: string; label: string }[]
  items?: { id: string; name: string }[]
  objects?: SceneObject[]
  title: string
  background?: string | null
  backgrounds?: { id: string; label: string }[]
  people: StagePerson[]
  slots?: Slot[] | null
  onChange: (slots: Slot[] | null) => void
  availablePeople: StagePerson[]
  onBackground: (id: string | null) => void
  request: (path: string, init?: RequestInit) => Promise<unknown>
}) {
  const [selected, setSelected] = useState('')
  const [selectedObject, setSelectedObject] = useState('')
  function select(id: string) {
    setSelected(id)
    setSelectedObject('')
  }
  function selectObject(id: string) {
    setSelectedObject(id)
    setSelected('')
  }
  const [tool, setTool] = useState<TransformTool>('move')
  const person = people.find((p) => p.id === selected)
  const slot = slots?.find((p) => p.actor === selected)
  const [creating, setCreating] = useState<QuickResource['kind'] | null>(null)
  const [libraryTab, setLibraryTab] = useState('resources')
  const [draft, setDraft] = useState<{
    id: string
    kind: 'person' | 'object'
    transform: StageTransform
  } | null>(null)
  const drag = useRef<{
    id: string
    kind: 'person' | 'object'
    pointer: number
    x: number
    y: number
    width: number
    height: number
    start: StageTransform
    tool: TransformTool
  } | null>(null)
  const free = objects.length > 0 || slots?.some((s) => s.transform) || false
  function setTransform(id: string, transform: StageTransform) {
    onChange((slots || []).map((s) => (s.actor === id ? { ...s, transform } : s)))
  }
  function addCharacter(id: string, transform = defaultTransform()) {
    if (!availablePeople.some((p) => p.id === id)) return
    const base: Slot[] =
      slots ||
      people.map((p, i) => ({
        actor: p.id,
        position:
          p.position || (people.length === 1 ? 'center' : (['left', 'center', 'right'] as const)[i % 3]),
      }))
    if (base.some((s) => s.actor === id)) {
      select(id)
      return
    }
    onChange([
      ...base.map((s) => ({ ...s, transform: s.transform || defaultTransform(s.position) })),
      { actor: id, position: 'center', transform },
    ])
    select(id)
  }
  function effectiveSlots(): Slot[] {
    return (
      slots ||
      people.map((p) => ({
        actor: p.id,
        position: p.position || 'center',
        transform: p.transform || defaultTransform(p.position),
      }))
    )
  }
  function addResource(resource: ResourceDrop, transform = defaultTransform()) {
    if (resource.kind === 'person') {
      addCharacter(resource.id, transform)
      return
    }
    if (resource.kind === 'background') {
      onBackground(resource.id)
      select('')
      return
    }
    if (objects.length >= 100) return
    const sprite = resource.sprite || (resource.kind === 'decoration' ? resource.id : '')
    if (!sprite) return
    const id = crypto.randomUUID()
    onObjectsChange([
      ...objects,
      {
        version: 1,
        id,
        kind: resource.kind === 'item' ? 'item' : 'decoration',
        item: resource.kind === 'item' ? resource.id : null,
        name:
          (resource.kind === 'item'
            ? items.find((i) => i.id === resource.id)?.name
            : images.find((i) => i.id === sprite)?.label) || '新对象',
        sprite,
        transform,
        layer: 'entities',
        order: objects.length + people.length,
      },
    ])
    selectObject(id)
  }
  function cancelDrag() {
    drag.current = null
    setDraft(null)
  }
  function moved(e: PointerEvent<HTMLButtonElement>) {
    const d = drag.current
    return d && d.pointer === e.pointerId
      ? {
          id: d.id,
          kind: d.kind,
          transform: dragTransform(
            d.start,
            d.tool,
            ((e.clientX - d.x) * 1600) / d.width,
            ((e.clientY - d.y) * 900) / d.height,
          ),
        }
      : null
  }
  return (
    <div className="scene-canvas-workspace">
      <aside className="scene-workspace-library" aria-label="场景资源与层级">
        <nav>
          <button aria-pressed={libraryTab === 'resources'} onClick={() => setLibraryTab('resources')}>
            资源
          </button>
          <button aria-pressed={libraryTab === 'hierarchy'} onClick={() => setLibraryTab('hierarchy')}>
            层级
          </button>
        </nav>
        {libraryTab === 'resources' ? (
          <SceneResourceLibrary
            people={availablePeople}
            items={items}
            images={images}
            onAdd={addResource}
            onCreate={setCreating}
          />
        ) : (
          <SceneCompositionTree
            slots={effectiveSlots()}
            objects={objects}
            people={availablePeople}
            selected={selected ? 'person:' + selected : 'object:' + selectedObject}
            onSelect={(kind, id) => (kind === 'person' ? select(id) : selectObject(id))}
            onChange={onCompositionChange}
          />
        )}
        <button onClick={() => select('')}>场景背景属性</button>
      </aside>
      <div className="scene-workspace-center">
        {free && (
          <div className="scene-transform-tools" aria-label="画布变换工具">
            {(
              [
                ['move', '移动'],
                ['scale', '等比缩放'],
                ['rotate', '旋转'],
              ] as const
            ).map(([value, label]) => (
              <button key={value} aria-pressed={tool === value} onClick={() => setTool(value)}>
                {label}
              </button>
            ))}
            <small>
              {tool === 'move'
                ? '拖动人物移动位置'
                : tool === 'scale'
                  ? '向上拖动人物放大，向下缩小'
                  : '左右拖动人物旋转'}
              ；松开提交一次修改。
            </small>
          </div>
        )}
        <StagePlayer
          objects={objects
            .filter((o) => !o.editor_hidden)
            .map((o) =>
              draft?.kind === 'object' && draft.id === o.id ? { ...o, transform: draft.transform } : o,
            )}
          objectEditing={{
            selected: selectedObject,
            onSelect: selectObject,
            onPointerDown: (id) => (e) => {
              if (e.button !== 0) return
              selectObject(id)
              const o = objects.find((o) => o.id === id)
              if (!o || o.editor_locked) return
              const bounds = e.currentTarget.parentElement!.getBoundingClientRect()
              e.currentTarget.setPointerCapture(e.pointerId)
              drag.current = {
                id,
                kind: 'object',
                pointer: e.pointerId,
                x: e.clientX,
                y: e.clientY,
                width: bounds.width,
                height: bounds.height,
                start: o.transform,
                tool,
              }
            },
          }}
          title={title}
          background={background}
          people={people
            .filter((p) => !slots?.find((s) => s.actor === p.id)?.editor_hidden)
            .map((p) =>
              draft?.kind === 'person' && draft.id === p.id ? { ...p, transform: draft.transform } : p,
            )}
          editing={
            free
              ? {
                  onPointerDown: (id) => (e) => {
                    if (e.button !== 0) return
                    const s = slots?.find((s) => s.actor === id)
                    if (!s || s.editor_locked) return
                    const bounds = e.currentTarget.parentElement!.getBoundingClientRect()
                    select(id)
                    e.currentTarget.setPointerCapture(e.pointerId)
                    drag.current = {
                      id,
                      kind: 'person',
                      pointer: e.pointerId,
                      x: e.clientX,
                      y: e.clientY,
                      width: bounds.width,
                      height: bounds.height,
                      start: s.transform || defaultTransform(s.position),
                      tool,
                    }
                  },
                  onPointerMove: (e) => {
                    const next = moved(e)
                    if (next) setDraft(next)
                  },
                  onPointerUp: (e) => {
                    const next = moved(e),
                      start = drag.current?.start
                    cancelDrag()
                    if (next && start && JSON.stringify(next.transform) !== JSON.stringify(start)) {
                      if (next.kind === 'object')
                        onObjectsChange(
                          objects.map((o) => (o.id === next.id ? { ...o, transform: next.transform } : o)),
                        )
                      else setTransform(next.id, next.transform)
                    }
                  },
                  onPointerCancel: cancelDrag,
                }
              : undefined
          }
          request={request}
          onSelect={select}
          selection={{ id: selected, label: (name) => `编辑画面人物：${name}` }}
          resourceDrop={(e) => {
            const raw = e.dataTransfer.getData('application/x-sceneweaver-resource')
            if (!raw) return
            try {
              const resource = JSON.parse(raw) as ResourceDrop
              e.preventDefault()
              const bounds = e.currentTarget.getBoundingClientRect()
              addResource(resource, {
                ...defaultTransform(),
                x: ((e.clientX - bounds.left) / bounds.width) * 1600,
                y: ((e.clientY - bounds.top) / bounds.height) * 900,
              })
            } catch {
              /* Ignore foreign drag payloads. */
            }
          }}
          line={{
            id: 'author-canvas',
            speaker: '作者画布',
            text:
              slots == null
                ? '自动人物预览。启用本场安排后可编辑站位。'
                : people.length
                  ? free
                    ? '拖动人物调整构图；一次拖动可一次撤销。'
                    : '选择画面人物，调整本场站位。'
                  : '当前为纯背景场景。从属性区添加人物。',
          }}
        />
      </div>
      <aside className="scene-canvas-inspector" aria-label="选中对象属性">
        <h3>{selectedObject ? '物品 / 装饰属性' : person ? person.name + ' · 本场属性' : '场景属性'}</h3>
        {selectedObject && (
          <SceneObjectsEditor
            inspectorOnly
            objects={objects}
            images={images}
            items={items}
            onChange={onObjectsChange}
            selectedId={selectedObject}
            onSelect={selectObject}
          />
        )}
        {!selectedObject && (
          <>
            {!free && (
              <button
                onClick={() =>
                  onChange(
                    (
                      slots ||
                      people.map((p, i) => ({
                        actor: p.id,
                        position:
                          p.position ||
                          (people.length === 1 ? 'center' : (['left', 'center', 'right'] as const)[i % 3]),
                      }))
                    ).map((s) => ({ ...s, transform: defaultTransform(s.position) })),
                  )
                }
              >
                启用自由构图
              </button>
            )}
            {free && <small>设计画幅 1600 × 900；锚点为人物框底部中央。构图只作用于本场画面。</small>}
            <details open={!person}>
              <summary>场景背景</summary>
              <SpriteEditor
                value={background}
                background
                choices={backgrounds}
                request={request}
                onChange={onBackground}
              />
            </details>
            <label>
              <input
                type="checkbox"
                checked={slots != null}
                onChange={(e) => {
                  onChange(
                    e.target.checked
                      ? people.map((p, i) => ({
                          actor: p.id,
                          position:
                            p.position ||
                            (people.length === 1 ? 'center' : (['left', 'center', 'right'] as const)[i % 3]),
                        }))
                      : null,
                  )
                }}
              />
              为本场单独安排画面人物
            </label>
            <fieldset>
              <legend>{person ? `${person.name} · 本场属性` : '画面人物属性'}</legend>
              {person && slot ? (
                <>
                  <OutfitSelector
                    presentation={person.presentation}
                    outfit={slot.outfit}
                    variant={slot.variant}
                    onChange={(selection) =>
                      onChange((slots || []).map((s) => (s.actor === selected ? { ...s, ...selection } : s)))
                    }
                  />
                  <label>
                    <input
                      type="checkbox"
                      checked={!!slot.editor_locked}
                      onChange={(e) =>
                        onChange(
                          (slots || []).map((s) =>
                            s.actor === selected ? { ...s, editor_locked: e.target.checked } : s,
                          ),
                        )
                      }
                    />
                    锁定画布操作
                  </label>
                  <label>
                    <input
                      type="checkbox"
                      checked={!!slot.editor_hidden}
                      onChange={(e) =>
                        onChange(
                          (slots || []).map((s) =>
                            s.actor === selected ? { ...s, editor_hidden: e.target.checked } : s,
                          ),
                        )
                      }
                    />
                    编辑时隐藏人物
                  </label>
                  {slot.transform && (
                    <fieldset>
                      <legend>本场变换</legend>
                      {(
                        [
                          { key: 'x', name: '水平位置', min: -1600, max: 3200, step: 1 },
                          { key: 'y', name: '垂直位置', min: -900, max: 1800, step: 1 },
                          { key: 'scale_x', name: '水平缩放', min: 0.1, max: 4, step: 0.05 },
                          { key: 'scale_y', name: '垂直缩放', min: 0.1, max: 4, step: 0.05 },
                          { key: 'rotation', name: '旋转角度', min: -180, max: 180, step: 1 },
                        ] as const
                      ).map((f) => (
                        <label key={f.key}>
                          {f.name}
                          <input
                            type="number"
                            min={f.min}
                            max={f.max}
                            step={f.step}
                            value={slot.transform![f.key]}
                            onChange={(e) => {
                              const v = e.target.valueAsNumber
                              if (Number.isFinite(v))
                                setTransform(selected, {
                                  ...slot.transform!,
                                  [f.key]: Math.max(f.min, Math.min(f.max, v)),
                                })
                            }}
                          />
                        </label>
                      ))}
                      <button onClick={() => setTransform(selected, defaultTransform(slot.position))}>
                        重置本场变换
                      </button>
                    </fieldset>
                  )}
                  <label>
                    画面位置
                    <select
                      value={slot.position}
                      onChange={(e) =>
                        onChange(
                          (slots || []).map((s) =>
                            s.actor === selected
                              ? {
                                  ...s,
                                  position: e.target.value as Slot['position'],
                                  transform: s.transform
                                    ? {
                                        ...s.transform,
                                        x: defaultTransform(e.target.value as Slot['position']).x,
                                      }
                                    : s.transform,
                                }
                              : s,
                          ),
                        )
                      }
                    >
                      <option value="left">左侧</option>
                      <option value="center">中央</option>
                      <option value="right">右侧</option>
                    </select>
                  </label>
                  <button onClick={() => onChange((slots || []).filter((s) => s.actor !== selected))}>
                    从本场画面移除
                  </button>
                  <p>只改变画面安排，不移动角色，也不改变角色所知。</p>
                </>
              ) : (
                <p>
                  {slots == null
                    ? '当前沿用实际可见人物；本图沿故事路线预览入场地点，分支汇合时可能不确定，运行时以实际位置为准。'
                    : '选择画面人物，或从上方添加人物。'}
                </p>
              )}
            </fieldset>
            <small>画面安排不移动人物、不授予知识；运行时仍按在场与感知规则过滤。</small>
          </>
        )}
      </aside>
      {creating && onCreateResource && (
        <StudioQuickCreate
          kind={creating}
          location={location}
          images={images}
          request={request}
          onClose={() => setCreating(null)}
          onCreate={onCreateResource}
        />
      )}
    </div>
  )
}
