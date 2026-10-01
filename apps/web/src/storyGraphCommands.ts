import type { Narrative } from './NarrativeEditor'

export function graphPositions(graph: Narrative) {
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
    if (!progress) {
      for (const id of pending) levels.set(id, 0)
      break
    }
  }
  const rows = new Map<number, number>()
  return new Map(
    graph.nodes.map((n) => {
      const level = levels.get(n.id) || 0,
        row = rows.get(level) || 0
      rows.set(level, row + 1)
      return [n.id, n.editor_position || { x: level * 260, y: row * 160 }]
    }),
  )
}

export function connectionProblem(graph: Narrative, source: string, target: string): string {
  const ids = new Set(graph.nodes.map((n) => n.id))
  if (!ids.has(source) || !ids.has(target)) return '连线端点不存在。'
  if (source === target) return '不能连接场景自身。'
  if (graph.edges.some((e) => e.source === source && e.target === target))
    return '这两个场景已有连线，请编辑已有条件。'
  const seen = new Set<string>(),
    pending = [target]
  while (pending.length) {
    const id = pending.pop()!
    if (id === source) return '此连线会形成循环。场内可以持续互动，场景图需保持无环。'
    if (seen.has(id)) continue
    seen.add(id)
    pending.push(...graph.edges.filter((e) => e.source === id).map((e) => e.target))
  }
  return ''
}

export function copySubgraph(graph: Narrative, selected: string[]) {
  const ids = new Set(selected)
  return structuredClone({
    nodes: graph.nodes.filter((n) => ids.has(n.id)),
    edges: graph.edges.filter((e) => ids.has(e.source) && ids.has(e.target)),
  })
}

export function pasteSubgraph(graph: Narrative, copied: ReturnType<typeof copySubgraph>, id: () => string) {
  const mapping = new Map(copied.nodes.map((n) => [n.id, id()]))
  const nodes = copied.nodes.map((n, i) => ({
    ...structuredClone(n),
    id: mapping.get(n.id)!,
    title: n.title + '（副本）',
    editor_position: { x: (n.editor_position?.x ?? i * 240) + 60, y: (n.editor_position?.y ?? 0) + 80 },
  }))
  const edges = copied.edges.map((e) => ({
    ...structuredClone(e),
    id: id(),
    source: mapping.get(e.source)!,
    target: mapping.get(e.target)!,
  }))
  if (graph.nodes.length + nodes.length > 200 || graph.edges.length + edges.length > 600)
    throw Error('超过场景或连线数量上限。')
  return { ...graph, nodes: [...graph.nodes, ...nodes], edges: [...graph.edges, ...edges] }
}

export function removeSelection(graph: Narrative, nodes: string[], edges: string[]) {
  if (nodes.includes(graph.entry)) throw Error('请先将其他场景设为入口，再删除当前入口。')
  return {
    ...graph,
    nodes: graph.nodes.filter((n) => !nodes.includes(n.id)),
    edges: graph.edges.filter(
      (e) => !edges.includes(e.id) && !nodes.includes(e.source) && !nodes.includes(e.target),
    ),
  }
}
