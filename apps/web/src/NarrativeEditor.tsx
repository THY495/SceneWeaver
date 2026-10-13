import { resolveAppearance, type CharacterPresentation } from './outfitModel'
import type { QuickResource } from './StudioQuickCreate'
import { useEffect, useState } from 'react'
import { SceneCanvas } from './SceneCanvas'
import { CGSceneEditor } from './CGSceneEditor'
import { VideoSceneEditor, type VideoAsset, type VideoClip } from './VideoSceneEditor'
import type { StageSlot } from './stageTransform'
import type { SceneObject } from './sceneObjectModel'

import { OpeningEditor, type OpeningBlock } from './OpeningEditor'
import { DirectionEditor, type Direction } from './DirectionEditor'
import './scene-editor.css'
import { backstagePlacement } from './backstagePlacement'
import { scenePreviewLocations, unstagedColocatedActors } from './scenePreviewLocations'
export type Narrative = {
  entry: string
  nodes: {
    id: string
    title: string
    editor_position?: { x: number; y: number } | null
    text: string
    observers: string[]
    background?: string | null
    visual_mode?: 'stage' | 'cg' | 'video'
    video?: VideoClip | null
    stage?: StageSlot[] | null
    objects?: SceneObject[]
    mode?: 'player' | 'backstage'
    participants?: string[]
    entry_locations?: { actor: string; location: string }[]
    opening?: OpeningBlock[] | null
    direction?: Direction | null
    ending?: boolean
  }[]
  edges: {
    id: string
    source: string
    target: string
    label: string
    trigger?: 'manual' | 'ai'
    condition?: string
    requires_holding?: { item: string; actor: string } | null
    requires_agreement?: string[]
  }[]
}

function NarrativeMap({
  graph,
  selected,
  onSelect,
}: {
  graph: Narrative
  selected: string
  onSelect: (id: string) => void
}) {
  const levels = new Map<string, number>()
  const pending = new Set(graph.nodes.map((n) => n.id))
  while (pending.size) {
    let progress = false
    for (const id of pending) {
      const incoming = graph.edges.filter((e) => e.target === id)
      if (incoming.every((e) => levels.has(e.source))) {
        levels.set(id, Math.max(0, ...incoming.map((e) => levels.get(e.source)! + 1)))
        pending.delete(id)
        progress = true
      }
    }
    if (!progress) return <p role="status">图中存在循环或失效连线，请修改后查看预览。</p>
  }
  const rows = new Map<number, number>()
  const positions = new Map(
    graph.nodes.map((n) => {
      const level = levels.get(n.id)!
      const row = rows.get(level) || 0
      rows.set(level, row + 1)
      return [n.id, { x: level * 210 + 10, y: row * 76 + 10 }]
    }),
  )
  const width = (Math.max(0, ...levels.values()) + 1) * 210
  const height = Math.max(1, ...rows.values()) * 76
  return (
    <div style={{ overflowX: 'auto' }}>
      <svg aria-label="剧情走向预览，选择场景进行编辑" width={width} height={height}>
        <title>剧情走向：从左到右，分支可汇合</title>
        {graph.edges.map((e) => {
          const a = positions.get(e.source),
            b = positions.get(e.target)
          return a && b ? (
            <line key={e.id} x1={a.x + 170} y1={a.y + 24} x2={b.x} y2={b.y + 24} stroke="#8aa298">
              <title>{e.label}</title>
            </line>
          ) : null
        })}
        {graph.nodes.map((n) => {
          const p = positions.get(n.id)!
          return (
            <g
              key={n.id}
              role="button"
              tabIndex={0}
              aria-label={`编辑场景：${n.title}`}
              aria-pressed={selected === n.id}
              onClick={() => onSelect(n.id)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  onSelect(n.id)
                }
              }}
            >
              <rect
                x={p.x}
                y={p.y}
                width={170}
                height={48}
                rx={6}
                fill={n.id === graph.entry ? '#dcece4' : '#f0f3f2'}
                stroke={selected === n.id ? '#23765b' : '#8aa298'}
                strokeWidth={selected === n.id ? 3 : 1}
              />
              <text x={p.x + 8} y={p.y + 29} fontSize={13}>
                {(n.title || '未命名节点').slice(0, 11)}
              </text>
              <title>{n.title}</title>
            </g>
          )
        })}
      </svg>
    </div>
  )
}

export function NarrativeEditor({
  value,
  actors,
  items = [],
  player,
  onChange,
  request,
  initialScene = '',
  selectedScene,
  externalSceneList = false,
  onSelectScene,
  onBackToGraph,
  onSave,
  history,
  images = [],
  onCreateResource,
  diagnosticFocus,
  videos = [],
  onUploadVideo,
}: {
  videos?: VideoAsset[]
  onUploadVideo?: (scene: string, asset: VideoAsset) => void
  diagnosticFocus?: { scene: string; panel: 'opening' | 'rules' | 'exits' | 'stage'; revision: number } | null
  onCreateResource?: (scene: string, data: QuickResource) => Promise<void>
  images?: { id: string; label: string }[]
  history?: { canUndo: boolean; canRedo: boolean; undo: () => void; redo: () => void }
  initialScene?: string
  selectedScene?: string
  externalSceneList?: boolean
  onSelectScene?: (id: string) => void
  onBackToGraph?: () => void
  onSave?: () => void
  value?: Narrative | null
  actors: {
    alias: string
    name: string
    location?: string
    base_sprite?: string | null
    presentation?: CharacterPresentation
  }[]
  player: string
  items?: { id: string; name: string }[]
  request: (path: string, init?: RequestInit) => Promise<unknown>
  onChange: (value: Narrative | null) => void
}) {
  const [selection, setLocalSelection] = useState(initialScene)
  const [panel, setPanel] = useState('stage')
  useEffect(() => {
    if (diagnosticFocus) setPanel(diagnosticFocus.panel)
  }, [diagnosticFocus])
  function setSelection(id: string) {
    setLocalSelection(id)
    onSelectScene?.(id)
  }
  const [past, setPast] = useState<(Narrative | null)[]>([])
  const [future, setFuture] = useState<(Narrative | null)[]>([])
  const cursor = selectedScene ?? selection
  const selected = value?.nodes.some((n) => n.id === cursor) ? cursor : value?.entry || ''
  const preview = value ? scenePreviewLocations(value, actors, selected) : null
  const beforeEntry = value
    ? scenePreviewLocations(
        {
          ...value,
          nodes: value.nodes.map((node) => (node.id === selected ? { ...node, entry_locations: [] } : node)),
        },
        actors,
        selected,
      )
    : null
  function commit(next: Narrative | null) {
    if (!history) {
      setPast((p) => [...p.slice(-39), structuredClone(value || null)])
      setFuture([])
    }
    onChange(next)
  }
  function restore(redo: boolean) {
    const source = redo ? future : past
    if (!source.length) return
    const next = source[source.length - 1]
    if (redo) {
      setFuture(source.slice(0, -1))
      setPast((p) => [...p, structuredClone(value || null)])
    } else {
      setPast(source.slice(0, -1))
      setFuture((p) => [...p, structuredClone(value || null)])
    }
    onChange(structuredClone(next))
  }
  function addScene(copy: boolean) {
    if (!value) return
    const source = value.nodes.find((n) => n.id === selected)!
    const id = crypto.randomUUID()
    update((g) => {
      g.nodes.push(
        copy
          ? { ...structuredClone(source), id, title: source.title + '（副本）' }
          : { id, title: '新场景', text: '', observers: [player] },
      )
      g.edges.push({
        id: crypto.randomUUID(),
        source: source.id,
        target: id,
        label: '继续',
        trigger: 'ai',
        condition: '',
      })
    })
    setSelection(id)
  }
  function update(fn: (g: Narrative) => void) {
    if (!value) return
    const next = structuredClone(value)
    fn(next)
    commit(next)
  }
  return (
    <section aria-label="剧情图编辑">
      <div className="scene-editor-toolbar" hidden={Boolean(history)}>
        <button
          disabled={history ? !history.canUndo : !past.length}
          onClick={() => (history ? history.undo() : restore(false))}
        >
          撤销场景修改
        </button>
        <button
          disabled={history ? !history.canRedo : !future.length}
          onClick={() => (history ? history.redo() : restore(true))}
        >
          重做
        </button>
        <small>修改后请使用工作室的保存按钮；撤销记录仅在本次编辑中保留。</small>
      </div>
      {value && !externalSceneList && (
        <details className="scene-map-disclosure">
          <summary>故事全图 · 查看分支与汇合</summary>
          <NarrativeMap graph={value} selected={selected} onSelect={setSelection} />
        </details>
      )}
      {!value ? (
        <button
          onClick={() =>
            commit({
              entry: 'opening',
              nodes: [{ id: 'opening', title: '开场', text: '', observers: [player] }],
              edges: [],
            })
          }
        >
          添加剧情图
        </button>
      ) : (
        <>
          <details className="scene-entry-settings">
            <summary>故事入口：{value.nodes.find((n) => n.id === value.entry)?.title || '未指定'}</summary>
            <label>
              入口节点
              <select
                value={value.entry}
                onChange={(e) =>
                  update((g) => {
                    g.entry = e.target.value
                  })
                }
              >
                {value.nodes.map((n) => (
                  <option key={n.id} value={n.id}>
                    {n.title || '未命名节点'}
                  </option>
                ))}
              </select>
            </label>
          </details>
          <div className={`scene-editor-layout${externalSceneList ? ' scene-editor-external-list' : ''}`}>
            <nav className="scene-editor-list" aria-label="场景列表">
              {!externalSceneList &&
                value.nodes.map((n) => (
                  <button key={n.id} aria-pressed={selected === n.id} onClick={() => setSelection(n.id)}>
                    {n.id === value.entry ? '起点 · ' : ''}
                    {n.title || '未命名场景'}
                    <small>
                      {n.background ? '已配背景' : '未配背景'} · {n.observers.length} 位知情者
                    </small>
                  </button>
                ))}
              <button onClick={() => addScene(false)}>接续新场景</button>
              <button onClick={() => addScene(true)}>复制当前场景并接续</button>
            </nav>
            <div className="scene-editor-detail">
              {value.nodes.map(
                (node, i) =>
                  node.id === selected && (
                    <article className="studio-person" key={node.id}>
                      <div className="scene-local-navigation" role="group" aria-label="当前场景编辑内容">
                        {onBackToGraph && <button onClick={onBackToGraph}>返回故事图</button>}
                        {onSave && <button onClick={onSave}>保存项目</button>}
                        {[
                          ['opening', '演出与预览'],
                          ['stage', '背景与站位'],
                          ['rules', '参与与知情'],
                          ['exits', '条件出口'],
                        ].map(([id, label]) => (
                          <button key={id} aria-pressed={panel === id} onClick={() => setPanel(id)}>
                            {label}
                          </button>
                        ))}
                      </div>
                      <label>
                        场景 {i + 1} 名称
                        <input
                          value={node.title}
                          onChange={(e) =>
                            update((g) => {
                              g.nodes[i].title = e.target.value
                            })
                          }
                        />
                      </label>
                      {!node.ending && !value.edges.some((edge) => edge.source === node.id) && (
                        <div role="status" className="scene-leaf-status">
                          <p>
                            本场没有后续出口，但不会自动结束故事。可以保留为开放场景，让人物继续互动；
                            如果这里是故事结局，请在「条件出口」中勾选「结束节点」。
                          </p>
                          <button onClick={() => setPanel('exits')}>查看结局设置</button>
                        </div>
                      )}
                      <div hidden={panel !== 'exits'}>
                        <label>
                          <input
                            type="checkbox"
                            checked={Boolean(node.ending)}
                            disabled={
                              !node.ending &&
                              (node.mode === 'backstage' ||
                                Boolean(node.direction) ||
                                value.edges.some((e) => e.source === node.id))
                            }
                            onChange={(e) =>
                              update((g) => {
                                g.nodes[i].ending = e.target.checked
                              })
                            }
                          />
                          结束节点
                        </label>
                        <p>
                          固定收尾播放完后结束故事；未设置固定演出或动画时，进入本场即标记结束。结束后不再触发
                          AI 互动。启用前请移除本场 AI 互动及后续连线，并使用玩家场景。
                        </p>
                      </div>
                      <div hidden={panel !== 'rules'}>
                        <label>
                          场景运行方式
                          <select
                            value={node.mode || 'player'}
                            onChange={(e) =>
                              update((g) => {
                                const backstage = e.target.value === 'backstage'
                                g.nodes[i].mode = backstage ? 'backstage' : 'player'
                                const participants = backstage
                                  ? actors.filter((a) => a.alias !== player).map((a) => a.alias)
                                  : []
                                g.nodes[i].participants = participants
                                g.nodes[i].observers = backstage
                                  ? participants
                                  : [...new Set([...node.observers, player])]
                              })
                            }
                          >
                            <option value="player">玩家参与</option>
                            <option value="backstage">幕后 · Studio 单步调试／独立游戏自动推进</option>
                          </select>
                        </label>
                        {node.mode === 'backstage' && (
                          <fieldset>
                            <legend>实际参与 NPC</legend>
                            <p role="status">
                              按开局位置与本场入场设置检查：
                              {backstagePlacement(
                                node.participants || [],
                                actors.map((a) => ({
                                  ...a,
                                  location: preview?.locations.get(a.alias) || '',
                                })),
                              )}{' '}
                              进入本场时仍会按当时位置重新核验；后续场景的提示不代表届时一定冲突。
                            </p>
                            <p>
                              这些 NPC 必须已同处一地，玩家及其他角色必须在别处。Studio
                              预览由作者单步调试；独立游戏仅在符合发行限制时按导演段落自动推进。舞台和内容不向玩家展示。
                            </p>
                            {actors
                              .filter((a) => a.alias !== player)
                              .map((a) => (
                                <label key={a.alias}>
                                  <input
                                    type="checkbox"
                                    checked={node.participants?.includes(a.alias) || false}
                                    onChange={(e) =>
                                      update((g) => {
                                        g.nodes[i].participants = e.target.checked
                                          ? [...(node.participants || []), a.alias]
                                          : (node.participants || []).filter((id) => id !== a.alias)
                                        if (!e.target.checked)
                                          g.nodes[i].observers = node.observers.filter((id) => id !== a.alias)
                                      })
                                    }
                                  />
                                  {a.name}
                                </label>
                              ))}
                          </fieldset>
                        )}
                        <fieldset>
                          <legend>进入本场时的世界地点</legend>
                          <p>
                            仅勾选的角色会在进入本场时移动；这会写入世界事件并影响谁能见面、听到对话。画面站位不会改变世界地点。
                            场景名称只决定剧情节点与画面，不会自动改变世界地点。入场移动属于已确定的剧情安排；若
                            NPC 可以拒绝同行，请在进入本场的 AI 出口设置“本人同意”条件，并给拒绝另设分支。
                          </p>
                          {!!node.entry_locations?.length && !!node.title.trim() && (
                            <button
                              type="button"
                              onClick={() =>
                                update((g) => {
                                  for (const entry of g.nodes[i].entry_locations || [])
                                    entry.location = node.title.trim()
                                })
                              }
                            >
                              将勾选角色移至「{node.title}」
                            </button>
                          )}
                          {actors.map((a) => {
                            const arrival = node.entry_locations?.find((entry) => entry.actor === a.alias)
                            return (
                              <div key={a.alias}>
                                <label>
                                  <input
                                    type="checkbox"
                                    checked={!!arrival}
                                    onChange={(e) =>
                                      update((g) => {
                                        const entries = g.nodes[i].entry_locations || []
                                        g.nodes[i].entry_locations = e.target.checked
                                          ? [
                                              ...entries,
                                              {
                                                actor: a.alias,
                                                location:
                                                  a.alias === player
                                                    ? preview?.locations.get(a.alias) || a.location || '大厅'
                                                    : preview?.locations.get(player) ||
                                                      actors.find((p) => p.alias === player)?.location ||
                                                      a.location ||
                                                      '大厅',
                                              },
                                            ]
                                          : entries.filter((entry) => entry.actor !== a.alias)
                                      })
                                    }
                                  />
                                  {a.name}（开局：{a.location || '未设置'}）
                                </label>
                                {arrival && (
                                  <>
                                    <label>
                                      {a.name}进入本场时到达
                                      <input
                                        value={arrival.location}
                                        maxLength={100}
                                        onChange={(e) =>
                                          update((g) => {
                                            g.nodes[i].entry_locations!.find(
                                              (entry) => entry.actor === a.alias,
                                            )!.location = e.target.value
                                          })
                                        }
                                      />
                                    </label>
                                    {beforeEntry?.locations.get(a.alias) === arrival.location && (
                                      <small role="status">与入场前地点相同，此项不会移动{a.name}。</small>
                                    )}
                                  </>
                                )}
                              </div>
                            )
                          })}
                        </fieldset>
                      </div>
                      <div hidden={panel !== 'stage'}>
                        {preview && preview.ambiguous.length > 0 && (
                          <p role="status">
                            多条路线到达本场时，
                            {preview.ambiguous
                              .map((alias) => actors.find((a) => a.alias === alias)?.name || alias)
                              .join('、')}
                            的世界地点可能不同；请为本场明确设置入场地点，或在实际运行时核对画面。
                          </p>
                        )}
                        {node.mode !== 'backstage' &&
                          preview &&
                          unstagedColocatedActors(actors, player, preview.locations, node.stage).length >
                            0 && (
                            <p role="status">
                              世界地点预览中，
                              {unstagedColocatedActors(actors, player, preview.locations, node.stage)
                                .map((actor) => actor.name)
                                .join('、')}
                              与玩家同处一地，但没有摆入本场舞台；试玩画面不会自动显示他们。若希望人物出镜，请从人物列表拖入舞台。
                            </p>
                          )}
                        <label>
                          画面形式
                          <select
                            value={node.visual_mode || 'stage'}
                            onChange={(e) =>
                              update((g) => {
                                g.nodes[i].visual_mode = e.target.value as 'stage' | 'cg' | 'video'
                              })
                            }
                          >
                            <option value="stage">背景＋人物舞台</option>
                            <option value="cg">整张 CG＋对白</option>
                            <option value="video">固定视频片段</option>
                          </select>
                        </label>
                        {node.visual_mode === 'video' ? (
                          <VideoSceneEditor
                            key={node.id}
                            value={node.video}
                            assets={videos}
                            request={request}
                            onChange={(video) =>
                              update((g) => {
                                g.nodes[i].video = video
                              })
                            }
                            onUpload={(asset) => onUploadVideo?.(node.id, asset)}
                          />
                        ) : node.visual_mode === 'cg' ? (
                          <CGSceneEditor
                            title={node.title}
                            background={node.background}
                            images={images || []}
                            request={request}
                            onChange={(id) =>
                              update((g) => {
                                g.nodes[i].background = id
                              })
                            }
                          />
                        ) : (
                          <SceneCanvas
                            location={
                              preview?.locations.get(player) ||
                              actors.find((a) => a.alias === player)?.location
                            }
                            onCreateResource={
                              onCreateResource ? (data) => onCreateResource(node.id, data) : undefined
                            }
                            onCompositionChange={(stage, objects) =>
                              update((g) => {
                                g.nodes[i].stage = stage
                                g.nodes[i].objects = objects
                              })
                            }
                            images={images}
                            items={items}
                            onObjectsChange={(objects) =>
                              update((g) => {
                                g.nodes[i].objects = objects
                              })
                            }
                            objects={node.objects}
                            key={node.id}
                            title={node.title}
                            background={node.background}
                            backgrounds={Array.from(
                              new Set(value.nodes.flatMap((n) => (n.background ? [n.background] : []))),
                            ).map((id) => ({
                              id,
                              label: value.nodes
                                .filter((n) => n.background === id)
                                .map((n) => n.title || '未命名场景')
                                .join('、'),
                            }))}
                            slots={node.stage}
                            availablePeople={actors
                              .filter((a) => a.alias !== player)
                              .map((a) => ({ ...a, id: a.alias }))}
                            people={
                              node.stage == null
                                ? actors
                                    .filter(
                                      (a) =>
                                        a.alias !== player &&
                                        (node.mode === 'backstage'
                                          ? node.participants?.includes(a.alias)
                                          : Boolean(preview?.locations.get(player)) &&
                                            preview?.locations.get(a.alias) ===
                                              preview?.locations.get(player)),
                                    )
                                    .map((a) => ({ ...a, id: a.alias }))
                                : node.stage.flatMap((slot, draw_order) => {
                                    const actor = actors.find((a) => a.alias === slot.actor)
                                    return actor
                                      ? [
                                          {
                                            ...actor,
                                            id: actor.alias,
                                            base_sprite: resolveAppearance(actor.presentation, slot).sprite,
                                            outfit: slot.outfit,
                                            variant: slot.variant,
                                            position: slot.position,
                                            transform: slot.transform,
                                            draw_order: slot.draw_order ?? draw_order,
                                          },
                                        ]
                                      : []
                                  })
                            }
                            request={request}
                            onBackground={(id) =>
                              update((g) => {
                                g.nodes[i].background = id
                              })
                            }
                            onChange={(stage) =>
                              update((g) => {
                                g.nodes[i].stage = stage
                              })
                            }
                          />
                        )}
                      </div>

                      <details
                        className="scene-bottom-performance"
                        hidden={panel !== 'opening' && panel !== 'stage'}
                        open={panel === 'opening'}
                      >
                        <summary>场内演出与预览</summary>
                        <DirectionEditor
                          value={node.direction}
                          actors={actors.filter((a) => a.alias !== player)}
                          backstage={node.mode === 'backstage'}
                          onChange={(direction) =>
                            update((g) => {
                              g.nodes[i].direction = direction
                            })
                          }
                        />
                        <OpeningEditor
                          directed={!!node.direction}
                          objects={node.objects}
                          key={node.id}
                          blocks={node.opening}
                          legacy={node.text}
                          actors={actors.filter((a) => node.observers.includes(a.alias))}
                          onConfigureObservers={() => setPanel('rules')}
                          title={node.title}
                          background={node.background}
                          visualMode={node.visual_mode === 'cg' ? 'cg' : 'stage'}
                          request={request}
                          people={
                            node.stage == null
                              ? actors
                                  .filter(
                                    (a) =>
                                      a.alias !== player &&
                                      (node.mode === 'backstage'
                                        ? node.participants?.includes(a.alias)
                                        : Boolean(preview?.locations.get(player)) &&
                                          preview?.locations.get(a.alias) === preview?.locations.get(player)),
                                  )
                                  .map((a) => ({ ...a, id: a.alias }))
                              : node.stage.flatMap((slot, draw_order) => {
                                  const a = actors.find((a) => a.alias === slot.actor)
                                  return a
                                    ? [
                                        {
                                          ...a,
                                          id: a.alias,
                                          base_sprite: resolveAppearance(a.presentation, slot).sprite,
                                          outfit: slot.outfit,
                                          variant: slot.variant,
                                          position: slot.position,
                                          transform: slot.transform,
                                          draw_order: slot.draw_order ?? draw_order,
                                        },
                                      ]
                                    : []
                                })
                          }
                          onChange={(opening) =>
                            update((g) => {
                              g.nodes[i].opening = opening
                            })
                          }
                        />
                      </details>
                      <div hidden={panel !== 'rules'}>
                        <details>
                          <summary>旧版开场兼容内容</summary>
                          <label>
                            设置开场积木后，此内容不再执行
                            <textarea
                              value={node.text}
                              onChange={(e) =>
                                update((g) => {
                                  g.nodes[i].text = e.target.value
                                })
                              }
                            />
                          </label>
                        </details>
                        <fieldset>
                          <legend>哪些角色获知开场旁白并听到对白</legend>
                          {actors
                            .filter((a) => node.mode !== 'backstage' || node.participants?.includes(a.alias))
                            .map((a) => (
                              <label key={a.alias}>
                                <input
                                  type="checkbox"
                                  disabled={a.alias === player}
                                  checked={node.observers.includes(a.alias)}
                                  onChange={(e) =>
                                    update((g) => {
                                      g.nodes[i].observers = e.target.checked
                                        ? [...node.observers, a.alias]
                                        : node.observers.filter((id) => id !== a.alias)
                                    })
                                  }
                                />
                                {a.name}
                              </label>
                            ))}
                        </fieldset>
                      </div>
                      <div hidden={panel !== 'exits'}>
                        <p>AI 根据互动证据检查条件；以下配置不会提前展示给玩家。</p>
                        {value.edges
                          .filter((e) => e.source === node.id)
                          .map((edge) => (
                            <div key={edge.id} className="studio-person">
                              <label>
                                出口名称
                                <input
                                  value={edge.label}
                                  onChange={(e) =>
                                    update((g) => {
                                      g.edges.find((x) => x.id === edge.id)!.label = e.target.value
                                    })
                                  }
                                />
                              </label>
                              <label>
                                推进方式
                                <select
                                  value={edge.trigger || 'manual'}
                                  onChange={(e) =>
                                    update((g) => {
                                      const selected = g.edges.find((x) => x.id === edge.id)!
                                      selected.trigger = e.target.value as 'manual' | 'ai'
                                      if (selected.trigger === 'manual') selected.requires_agreement = []
                                    })
                                  }
                                >
                                  <option value="manual">
                                    {node.mode === 'backstage'
                                      ? '幕后段落完成后自动转场'
                                      : '玩家点击（兼容旧故事）'}
                                  </option>
                                  <option value="ai">AI 根据互动自动判断</option>
                                </select>
                              </label>
                              {edge.trigger === 'ai' && (
                                <label>
                                  触发条件
                                  <textarea
                                    value={edge.condition || ''}
                                    placeholder="例如：玩家明确表示愿意跟随沈舟前往码头，沈舟也明确同意。"
                                    onChange={(e) =>
                                      update((g) => {
                                        g.edges.find((x) => x.id === edge.id)!.condition = e.target.value
                                      })
                                    }
                                  />
                                  <small>
                                    当前支持意愿、同意、拒绝等语义条件；不要用言论判断物品已经交接等客观动作。条件及目标场景不会作为选项提前展示给玩家。
                                  </small>
                                </label>
                              )}
                              {edge.trigger === 'ai' &&
                                !!value.nodes
                                  .find((target) => target.id === edge.target)
                                  ?.entry_locations?.some((entry) => entry.actor !== player) && (
                                  <fieldset>
                                    <legend>同行前必须由 NPC 自己同意</legend>
                                    <p>
                                      目标场景会移动以下 NPC。若角色可以拒绝同行，请勾选；AI
                                      出口须引用他们本场亲口同意的发言，玩家代替承诺不会触发转场。拒绝后可另设不带该角色的分支。
                                    </p>
                                    {actors
                                      .filter(
                                        (actor) =>
                                          actor.alias !== player &&
                                          value.nodes
                                            .find((target) => target.id === edge.target)
                                            ?.entry_locations?.some((entry) => entry.actor === actor.alias),
                                      )
                                      .map((actor) => (
                                        <label key={actor.alias}>
                                          <input
                                            type="checkbox"
                                            checked={edge.requires_agreement?.includes(actor.alias) || false}
                                            onChange={(event) =>
                                              update((g) => {
                                                const selected = g.edges.find((item) => item.id === edge.id)!
                                                selected.requires_agreement = event.target.checked
                                                  ? [...(selected.requires_agreement || []), actor.alias]
                                                  : (selected.requires_agreement || []).filter(
                                                      (alias) => alias !== actor.alias,
                                                    )
                                              })
                                            }
                                          />
                                          {actor.name}
                                        </label>
                                      ))}
                                  </fieldset>
                                )}
                              {edge.trigger === 'ai' && (
                                <fieldset>
                                  <legend>必须同时满足的物品状态</legend>
                                  <select
                                    aria-label="转场必需物品"
                                    value={edge.requires_holding?.item || ''}
                                    onChange={(e) =>
                                      update((g) => {
                                        g.edges.find((x) => x.id === edge.id)!.requires_holding = e.target
                                          .value
                                          ? { item: e.target.value, actor: player }
                                          : null
                                      })
                                    }
                                  >
                                    <option value="">无物品要求</option>
                                    {items.map((item) => (
                                      <option key={item.id} value={item.id}>
                                        {item.name}
                                      </option>
                                    ))}
                                  </select>
                                  {edge.requires_holding && (
                                    <label>
                                      实际持有者
                                      <select
                                        value={edge.requires_holding.actor}
                                        onChange={(e) =>
                                          update((g) => {
                                            g.edges.find((x) => x.id === edge.id)!.requires_holding!.actor =
                                              e.target.value
                                          })
                                        }
                                      >
                                        {actors.map((a) => (
                                          <option key={a.alias} value={a.alias}>
                                            {a.name}
                                          </option>
                                        ))}
                                      </select>
                                    </label>
                                  )}
                                </fieldset>
                              )}
                              <label>
                                前往节点
                                <select
                                  value={edge.target}
                                  onChange={(e) =>
                                    update((g) => {
                                      g.edges.find((x) => x.id === edge.id)!.target = e.target.value
                                    })
                                  }
                                >
                                  {value.nodes
                                    .filter((n) => n.id !== node.id)
                                    .map((n) => (
                                      <option key={n.id} value={n.id}>
                                        {n.title || '未命名节点'}
                                      </option>
                                    ))}
                                </select>
                              </label>
                              <button
                                onClick={() =>
                                  update((g) => {
                                    g.edges = g.edges.filter((e) => e.id !== edge.id)
                                  })
                                }
                              >
                                移除出口
                              </button>
                            </div>
                          ))}
                        <button
                          disabled={value.nodes.length < 2}
                          onClick={() =>
                            update((g) => {
                              g.edges.push({
                                id: crypto.randomUUID(),
                                source: node.id,
                                target: g.nodes.find((n) => n.id !== node.id)!.id,
                                label: '',
                                trigger: 'ai',
                                condition: '',
                              })
                            })
                          }
                        >
                          添加条件出口
                        </button>
                      </div>
                      <details className="scene-danger-actions">
                        <summary>删除场景</summary>
                        <button
                          disabled={value.nodes.length === 1 || node.id === value.entry}
                          onClick={() =>
                            update((g) => {
                              g.nodes = g.nodes.filter((n) => n.id !== node.id)
                              g.edges = g.edges.filter((e) => e.source !== node.id && e.target !== node.id)
                            })
                          }
                        >
                          删除当前场景及关联出口
                        </button>
                      </details>
                    </article>
                  ),
              )}
            </div>
          </div>
          <button
            onClick={() =>
              update((g) => {
                g.nodes.push({ id: crypto.randomUUID(), title: '', text: '', observers: [player] })
              })
            }
          >
            新增节点
          </button>
          <button
            onClick={() => {
              if (window.confirm('移除整个剧情图？普通人物对话仍可使用。')) commit(null)
            }}
          >
            移除剧情图
          </button>
          <p>
            未连入入口的场景、未填写的 AI
            条件可以保存草稿；运行前需补齐。循环与无效引用会阻止保存。无出口场景仍可继续人物对话。
          </p>
        </>
      )}
    </section>
  )
}
