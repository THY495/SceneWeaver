import type { ReactNode, PointerEventHandler, DragEventHandler } from 'react'
import { defaultTransform, transformStyle, type StageTransform } from './stageTransform'
import { Sprite } from './CharacterStage'
import type { ReadingLine } from './stageReading'
import './stage-player.css'
import { objectZIndex, type SceneObject } from './sceneObjectModel'
import type { CharacterPresentation, PortraitVariant } from './outfitModel'

export type StagePerson = {
  id: string
  name: string
  presentation?: CharacterPresentation
  variants?: Pick<PortraitVariant, 'id' | 'sprite' | 'expression' | 'pose'>[]
  outfit?: string | null
  variant?: string | null
  base_sprite?: string | null
  position?: 'left' | 'center' | 'right'
  draw_order?: number
  transform?: StageTransform | null
}
export function StagePlayer({
  title,
  background,
  visualMode = 'stage',
  people,
  line,
  request,
  onSelect,
  selection,
  itemTarget,
  tray,
  children,
  editing,
  resourceDrop,
  objects = [],
  objectEditing,
  onObjectSelect,
  focusActorId,
  emptyText = '此刻还没有新的对白。你可以观察周围或与在场人物交谈。',
}: {
  title: string
  emptyText?: string
  focusActorId?: string | null
  background?: string | null
  visualMode?: 'stage' | 'cg'
  people: StagePerson[]
  objects?: SceneObject[]
  objectEditing?: {
    selected: string
    onSelect: (id: string) => void
    onPointerDown: (id: string) => PointerEventHandler<HTMLButtonElement>
  }
  onObjectSelect?: (object: SceneObject) => void
  line?: ReadingLine
  request: (path: string, init?: RequestInit) => Promise<unknown>
  onSelect?: (id: string) => void
  selection?: { id: string; label: (name: string) => string }
  itemTarget?: (id: string) => { label: string; enabled: boolean }
  tray?: ReactNode
  children?: ReactNode
  resourceDrop?: DragEventHandler<HTMLDivElement>
  editing?: {
    onPointerDown: (id: string) => PointerEventHandler<HTMLButtonElement>
    onPointerMove: PointerEventHandler<HTMLButtonElement>
    onPointerUp: PointerEventHandler<HTMLButtonElement>
    onPointerCancel: PointerEventHandler<HTMLButtonElement>
  }
}) {
  const free = visualMode === 'cg' || !!editing || objects.length > 0 || people.some((p) => p.transform)
  const trayOnFrame = free
  return (
    <section
      className={
        'vn-stage' + (free ? ' vn-stage-designed' : '') + (visualMode === 'cg' ? ' vn-stage-cg' : '')
      }
      aria-label="故事舞台"
    >
      <div className="vn-design-frame">
        <div className="vn-scenery">
          {background && <Sprite id={background} name={title} request={request} background />}
        </div>
        <div className="vn-scene-title">{title}</div>
        <div
          className={'vn-cast' + (free ? ' vn-cast-free' : '')}
          onDragOver={
            resourceDrop
              ? (e) => {
                  if (
                    e.dataTransfer.types.some(
                      (t) =>
                        t === 'application/x-sceneweaver-character' ||
                        t === 'application/x-sceneweaver-resource',
                    )
                  )
                    e.preventDefault()
                }
              : undefined
          }
          onDrop={resourceDrop}
        >
          {(visualMode === 'cg' ? [] : objects).map((o) => (
            <button
              key={'object:' + o.id}
              className={
                'vn-scene-object' +
                (objectEditing
                  ? ' vn-object-editable'
                  : onObjectSelect && o.kind === 'item'
                    ? ' vn-object-inspectable'
                    : '')
              }
              style={{ ...transformStyle(o.transform), zIndex: objectZIndex(o) }}
              aria-label={`${!objectEditing && onObjectSelect && o.kind === 'item' ? '查看物品' : '画面对象'}：${o.name}`}
              aria-pressed={objectEditing ? objectEditing.selected === o.id : undefined}
              disabled={!objectEditing && !(onObjectSelect && o.kind === 'item')}
              onClick={() => (objectEditing ? objectEditing.onSelect(o.id) : onObjectSelect?.(o))}
              onPointerDown={objectEditing?.onPointerDown(o.id)}
              onPointerMove={objectEditing ? editing?.onPointerMove : undefined}
              onPointerUp={objectEditing ? editing?.onPointerUp : undefined}
              onPointerCancel={objectEditing ? editing?.onPointerCancel : undefined}
            >
              <Sprite id={o.sprite} name={o.name} request={request} />
            </button>
          ))}
          {(visualMode === 'cg' ? [] : people).map((p, i) => (
            <button
              key={p.id}
              className={
                'vn-person ' +
                ((focusActorId === undefined ? line?.actorId : focusActorId) === p.id ? 'vn-speaking ' : '') +
                (itemTarget?.(p.id).enabled ? 'vn-item-target' : '')
              }
              style={
                free
                  ? {
                      ...transformStyle(
                        p.transform ||
                          defaultTransform(
                            p.position ||
                              (people.length === 1
                                ? 'center'
                                : (['left', 'center', 'right'] as const)[i % 3]),
                          ),
                      ),
                      zIndex: 4000 + (p.draw_order ?? i),
                    }
                  : {
                      gridColumn: p.position
                        ? { left: 1, center: 2, right: 3 }[p.position]
                        : people.length === 1
                          ? 2
                          : (i % 3) + 1,
                    }
              }
              onPointerDown={editing?.onPointerDown(p.id)}
              onPointerMove={editing?.onPointerMove}
              onPointerUp={editing?.onPointerUp}
              onPointerCancel={editing?.onPointerCancel}
              onClick={() => onSelect?.(p.id)}
              aria-label={
                selection
                  ? selection.label(p.name)
                  : itemTarget
                    ? itemTarget(p.id).label
                    : onSelect
                      ? `与${p.name}交谈`
                      : p.name
              }
              aria-pressed={selection ? selection.id === p.id : undefined}
              disabled={itemTarget ? !itemTarget(p.id).enabled : !onSelect}
              data-handoff-person={p.id}
            >
              {p.base_sprite ? (
                <Sprite id={p.base_sprite} name={p.name} request={request} />
              ) : (
                <span className="vn-placeholder">{p.name.slice(0, 1)}</span>
              )}
              <span className="vn-person-name">{p.name}</span>
              {itemTarget && <span className="vn-target-label">{itemTarget(p.id).label}</span>}
            </button>
          ))}
        </div>
        {visualMode === 'cg' && onSelect && people.length > 0 && (
          <nav className="vn-cg-people" aria-label="在场人物">
            {people.map((p) => (
              <button
                key={p.id}
                data-handoff-person={p.id}
                className={itemTarget?.(p.id).enabled ? 'vn-item-target' : undefined}
                onClick={() => onSelect(p.id)}
                disabled={itemTarget ? !itemTarget(p.id).enabled : false}
                aria-label={itemTarget ? `${p.name}：${itemTarget(p.id).label}` : `与${p.name}交谈`}
                title={itemTarget ? `${p.name}：${itemTarget(p.id).label}` : `与${p.name}交谈`}
              >
                {p.name}
              </button>
            ))}
          </nav>
        )}
        {trayOnFrame && tray && <div className="vn-stage-tray">{tray}</div>}
      </div>
      {!trayOnFrame && tray}
      <div className="vn-dialogue" data-reading-event={line?.id.replace(/:\d+$/, '')}>
        <strong>{line?.speaker || '旁白'}</strong>
        <p>{line?.text || emptyText}</p>
        {children}
      </div>
    </section>
  )
}
