import { useEffect, useState } from 'react'
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  Position,
  applyNodeChanges,
  applyEdgeChanges,
  type Node,
  type Edge,
  type Connection,
  type ReactFlowInstance,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import type { Narrative } from './NarrativeEditor'
import { Sprite } from './CharacterStage'
import {
  connectionProblem,
  copySubgraph,
  pasteSubgraph,
  removeSelection,
  graphPositions,
} from './storyGraphCommands'

export default function StoryGraphWorkspace({
  graph,
  player,
  onChange,
  onOpen,
  request,
  focus,
}: {
  graph: Narrative | null | undefined
  player: string
  onChange: (graph: Narrative) => void
  onOpen: (id: string) => void
  request: (path: string, init?: RequestInit) => Promise<unknown>
  focus?: { scene: string; revision: number } | null
}) {
  const [nodes, setNodes] = useState<Node[]>([])
  const [edges, setEdges] = useState<Edge[]>([])
  const [flow, setFlow] = useState<ReactFlowInstance<Node, Edge> | null>(null)
  const [copied, setCopied] = useState<ReturnType<typeof copySubgraph> | null>(null)
  const [error, setError] = useState('')
  const [edgeId, setEdgeId] = useState('')
  const [from, setFrom] = useState(''),
    [to, setTo] = useState('')
  useEffect(() => {
    const positions = graph ? graphPositions(graph) : new Map()
    setNodes((old) =>
      (graph?.nodes || []).map((n) => ({
        id: n.id,
        selected: old.find((x) => x.id === n.id)?.selected,
        position: positions.get(n.id),
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        data: {
          label: (
            <div className="story-graph-node">
              {n.background && <Sprite id={n.background} name={n.title} request={request} background />}
              <strong>
                {n.id === graph?.entry ? '起点 · ' : ''}
                {n.title}
              </strong>
              <small>{n.ending ? '结束节点' : n.mode === 'backstage' ? '幕后场景' : '玩家场景'}</small>
            </div>
          ),
        },
      })),
    )
    setEdges((old) =>
      (graph?.edges || []).map((e) => ({
        id: e.id,
        source: e.source,
        target: e.target,
        label: e.label,
        selected: old.find((x) => x.id === e.id)?.selected,
      })),
    )
  }, [graph, request])
  useEffect(() => {
    if (!focus) return
    setNodes((old) =>
      old.some((node) => node.id === focus.scene)
        ? old.map((node) => ({ ...node, selected: node.id === focus.scene }))
        : old,
    )
    setEdges((old) => old.map((edge) => ({ ...edge, selected: false })))
    const frame = requestAnimationFrame(() => {
      if (flow) void flow.fitView({ nodes: [{ id: focus.scene }], maxZoom: 1, padding: 0.5 })
    })
    return () => cancelAnimationFrame(frame)
  }, [focus, flow])
  const selected = nodes.filter((n) => n.selected).map((n) => n.id)
  const edge = graph?.edges.find((e) => e.id === edgeId)
  function run(action: () => void) {
    try {
      action()
      setError('')
    } catch (e) {
      setError(String(e))
    }
  }
  function connect(c: Connection | { source: string; target: string }) {
    if (!graph) return
    const problem = connectionProblem(graph, c.source, c.target)
    if (problem) {
      setError(problem)
      return
    }
    if (graph.edges.length >= 600) {
      setError('最多支持 600 条连线。')
      return
    }
    const id = crypto.randomUUID()
    onChange({
      ...graph,
      edges: [
        ...graph.edges,
        { id, source: c.source, target: c.target, label: '新出口', trigger: 'ai', condition: '' },
      ],
    })
    setEdgeId(id)
    setError('')
  }
  function remove() {
    if (graph)
      run(() =>
        onChange(
          removeSelection(
            graph,
            selected,
            edges.filter((e) => e.selected).map((e) => e.id),
          ),
        ),
      )
  }
  return (
    <section
      className="story-graph-workspace"
      aria-label="故事节点图"
      onKeyDown={(event) => {
        if ((event.target as HTMLElement).closest('input,textarea,select,[contenteditable="true"]')) return
        if (event.key === 'Delete') {
          event.preventDefault()
          remove()
        }
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'c' && graph && selected.length) {
          event.preventDefault()
          setCopied(copySubgraph(graph, selected))
        }
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'v' && graph && copied) {
          event.preventDefault()
          run(() => onChange(pasteSubgraph(graph, copied, () => crypto.randomUUID())))
        }
      }}
    >
      <div className="scene-editor-toolbar">
        <button
          onClick={() => {
            if ((graph?.nodes.length || 0) >= 200) {
              setError('最多支持 200 个场景。')
              return
            }
            const id = crypto.randomUUID(),
              node = {
                id,
                title: '新场景',
                text: '请编排本场开场内容。',
                observers: [player],
                editor_position: {
                  x: ((graph?.nodes.length || 0) % 4) * 250,
                  y: Math.floor((graph?.nodes.length || 0) / 4) * 180 + 80,
                },
              }
            onChange(
              graph ? { ...graph, nodes: [...graph.nodes, node] } : { entry: id, nodes: [node], edges: [] },
            )
          }}
        >
          新建场景
        </button>
        <button disabled={!selected.length} onClick={() => graph && setCopied(copySubgraph(graph, selected))}>
          复制选中场景
        </button>
        <button
          disabled={!copied || !graph}
          onClick={() =>
            graph && copied && run(() => onChange(pasteSubgraph(graph, copied, () => crypto.randomUUID())))
          }
        >
          粘贴场景
        </button>
        <button disabled={!selected.length && !edges.some((e) => e.selected)} onClick={remove}>
          删除选中内容
        </button>
        <button disabled={selected.length !== 1} onClick={() => onOpen(selected[0])}>
          编辑选中场景
        </button>
        <button
          disabled={selected.length !== 1 || selected[0] === graph?.entry}
          onClick={() => graph && onChange({ ...graph, entry: selected[0] })}
        >
          设为故事入口
        </button>
      </div>
      <small>
        双击节点进入场景；Shift 多选／框选，拖动空白平移。复制只携带选中场景之间的连线。未连线、未填写 AI
        条件也可保存草稿；运行前需补齐。
      </small>
      {error && <p role="status">{error}</p>}
      <div className="story-graph-canvas">
        <ReactFlow
          onInit={setFlow}
          nodes={nodes}
          edges={edges}
          fitView
          deleteKeyCode={null}
          onNodesChange={(changes) => {
            setNodes((prior) => applyNodeChanges(changes, prior))
            const keyboardMoves = changes.filter(
              (c) => c.type === 'position' && c.dragging === undefined && c.position,
            )
            if (graph && keyboardMoves.length) {
              const positions = new Map(
                keyboardMoves.flatMap((c) => (c.type === 'position' ? [[c.id, c.position] as const] : [])),
              )
              onChange({
                ...graph,
                nodes: graph.nodes.map((n) =>
                  positions.get(n.id) ? { ...n, editor_position: positions.get(n.id)! } : n,
                ),
              })
            }
          }}
          onEdgesChange={(changes) => setEdges((prior) => applyEdgeChanges(changes, prior))}
          onNodeDoubleClick={(_, node) => onOpen(node.id)}
          onEdgeClick={(_, edge) => setEdgeId(edge.id)}
          onConnect={connect}
          onNodeDragStop={(_, node, moving) => {
            if (!graph) return
            const positions = new Map((moving.length ? moving : [node]).map((n) => [n.id, n.position]))
            onChange({
              ...graph,
              nodes: graph.nodes.map((n) =>
                positions.has(n.id) ? { ...n, editor_position: positions.get(n.id)! } : n,
              ),
            })
          }}
        >
          <Background />
          <Controls />
          <MiniMap />
        </ReactFlow>
      </div>
      {graph && (
        <details>
          <summary>通过列表连线（键盘操作）</summary>
          <label>
            起始场景
            <select value={from} onChange={(e) => setFrom(e.target.value)}>
              <option value="">请选择</option>
              {graph.nodes.map((n) => (
                <option key={n.id} value={n.id}>
                  {n.title}
                </option>
              ))}
            </select>
          </label>
          <label>
            目标场景
            <select value={to} onChange={(e) => setTo(e.target.value)}>
              <option value="">请选择</option>
              {graph.nodes.map((n) => (
                <option key={n.id} value={n.id}>
                  {n.title}
                </option>
              ))}
            </select>
          </label>
          <button disabled={!from || !to} onClick={() => connect({ source: from, target: to })}>
            连接场景
          </button>
        </details>
      )}
      {edge && graph && (
        <fieldset aria-label="选中连线属性">
          <legend>出口条件</legend>
          {edge.trigger === 'ai' && !edge.condition?.trim() && (
            <p role="status">请填写 AI 触发条件后保存。</p>
          )}
          <label>
            出口名称
            <input
              value={edge.label}
              onChange={(e) =>
                onChange({
                  ...graph,
                  edges: graph.edges.map((x) => (x.id === edge.id ? { ...x, label: e.target.value } : x)),
                })
              }
            />
          </label>
          <label>
            AI 触发条件
            <textarea
              value={edge.condition || ''}
              onChange={(e) =>
                onChange({
                  ...graph,
                  edges: graph.edges.map((x) =>
                    x.id === edge.id ? { ...x, trigger: 'ai', condition: e.target.value } : x,
                  ),
                })
              }
            />
          </label>
          <small>修改条件将使用 AI 自动判断。物品状态等详细约束在源场景的“条件出口”中编辑。</small>
          <button onClick={() => onOpen(edge.source)}>打开源场景</button>
        </fieldset>
      )}
    </section>
  )
}
