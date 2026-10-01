import type { Narrative } from './NarrativeEditor'
export function graphReadiness(graph?: Narrative | null): string[] {
  return graphDiagnostics(graph).map((issue) => issue.message)
}
export function releaseReadiness(
  graph?: Narrative | null,
  people: { alias: string; location: string }[] = [],
  player?: { alias: string; location: string },
): string[] {
  if (!graph) return []
  const backstage = new Map(
    graph.nodes.filter((node) => node.mode === 'backstage').map((node) => [node.id, node]),
  )
  if (!backstage.size) return []
  const locations = new Map(
    [...people, ...(player ? [player] : [])].map((person) => [person.alias, person.location]),
  )
  const seen = new Set<string>()
  let current = graph.entry
  while (backstage.has(current) && !seen.has(current)) {
    seen.add(current)
    const node = backstage.get(current)!
    for (const entry of node.entry_locations || []) locations.set(entry.actor, entry.location)
    const exits = graph.edges.filter((edge) => edge.source === current)
    const places = new Set((node.participants || []).map((alias) => locations.get(alias)))
    const colocated = [...locations].filter(([, place]) => places.has(place)).map(([alias]) => alias)
    if (
      (node.visual_mode || 'stage') !== 'stage' ||
      exits.length !== 1 ||
      (exits[0].trigger || 'manual') !== 'manual' ||
      exits[0].requires_holding ||
      places.size !== 1 ||
      places.has(undefined) ||
      colocated.length !== (node.participants || []).length ||
      colocated.some((alias) => !node.participants?.includes(alias))
    )
      return [`场景「${node.title}」尚不能在独立游戏中自动运行；目前仅支持入口处同场 NPC 和唯一无条件出口。`]
    if (
      node.direction &&
      (node.direction.beats.length > 8 ||
        node.direction.beats.some((beat) =>
          beat.participants.some((alias) => !node.participants?.includes(alias)),
        ))
    )
      return [`场景「${node.title}」的幕后 AI 编排最多允许八个段落，参与者须在本场 NPC 中。`]
    current = exits[0].target
  }
  if (seen.size !== backstage.size)
    return ['独立游戏仅支持从入口连续经过的固定幕后场景；互动后的幕后场景还需要自动调度。']
  return []
}
export type AuthoringDiagnostic = {
  code: string
  scene: string
  panel: 'opening' | 'rules' | 'exits' | 'stage'
  message: string
}
export function itemReferences(graph?: Narrative | null): Record<string, string[]> {
  const result: Record<string, string[]> = {}
  const add = (id: string, label: string) => {
    ;(result[id] ||= []).push(label)
  }
  for (const node of graph?.nodes || [])
    for (const object of node.objects || [])
      if (object.kind === 'item' && object.item) add(object.item, `场景「${node.title}」中的物品画面`)
  for (const edge of graph?.edges || [])
    if (edge.requires_holding) add(edge.requires_holding.item, `出口「${edge.label}」的持有条件`)
  return result
}
export function graphDiagnostics(graph?: Narrative | null): AuthoringDiagnostic[] {
  if (!graph) return []
  const issues: AuthoringDiagnostic[] = []
  for (const node of graph.nodes)
    if (!node.observers.length)
      issues.push({
        code: 'observers:' + node.id,
        scene: node.id,
        panel: 'rules',
        message: `场景「${node.title}」至少需要一位角色获知开场内容；请在“参与与知情”中勾选。`,
      })
  for (const node of graph.nodes)
    if (node.entry_locations?.some((entry) => !entry.location.trim()))
      issues.push({
        code: 'entry-location:' + node.id,
        scene: node.id,
        panel: 'rules',
        message: `场景「${node.title}」的入场地点不能为空；请在“参与与知情”中填写。`,
      })
  for (const node of graph.nodes)
    if (node.visual_mode === 'video' && !node.video)
      issues.push({
        code: 'video-missing:' + node.id,
        scene: node.id,
        panel: 'stage',
        message: `场景「${node.title}」尚未选择视频素材（可保存草稿）`,
      })
  for (const node of graph.nodes)
    if (node.visual_mode === 'cg' && !node.background)
      issues.push({
        code: 'cg-image:' + node.id,
        scene: node.id,
        panel: 'stage',
        message: `场景「${node.title}」尚未选择 CG 主画面（可保存草稿）`,
      })
  for (const node of graph.nodes)
    if (
      node.direction &&
      (!node.direction.objective.trim() ||
        !node.direction.beats.length ||
        node.direction.beats.some(
          (beat) =>
            !beat.title.trim() ||
            !beat.objective.trim() ||
            !beat.completion.trim() ||
            !beat.participants.length,
        ))
    )
      issues.push({
        code: 'director-incomplete:' + node.id,
        scene: node.id,
        panel: 'opening',
        message: `场景「${node.title}」的 AI 互动编排尚未完成：请填写场景目标、段落名称、目标、完成条件并选择参与角色。`,
      })
  for (const node of graph.nodes)
    if (!node.opening?.length && !node.text?.trim())
      issues.push({
        code: 'opening:' + node.id,
        scene: node.id,
        panel: 'opening',
        message: `场景「${node.title}」尚未编写开场（可保存草稿）`,
      })
  const reached = new Set<string>([graph.entry])
  for (let i = 0; i < graph.nodes.length; i++)
    for (const edge of graph.edges) if (reached.has(edge.source)) reached.add(edge.target)
  const detached = graph.nodes.filter((n) => !reached.has(n.id))
  for (const node of detached)
    issues.push({
      code: 'detached:' + node.id,
      scene: node.id,
      panel: 'exits',
      message: `未连接到故事入口：${node.title}（可保存草稿，运行前需在故事图连线）`,
    })
  for (const edge of graph.edges)
    if (edge.trigger === 'ai' && !edge.condition?.trim())
      issues.push({
        code: 'condition:' + edge.id,
        scene: edge.source,
        panel: 'exits',
        message: `出口「${edge.label}」缺少 AI 触发条件（可保存草稿，请在来源场景的条件出口中填写）`,
      })
  return issues
}
export function removeActorFromGraph(graph: Narrative | null | undefined, actor: string) {
  if (!graph) return
  for (const node of graph.nodes) {
    node.stage = node.stage?.filter((s) => s.actor !== actor)
    node.observers = node.observers.filter((a) => a !== actor)
    node.participants = node.participants?.filter((a) => a !== actor)
    node.entry_locations = node.entry_locations?.filter((entry) => entry.actor !== actor)
    for (const beat of node.direction?.beats || [])
      beat.participants = beat.participants.filter((a) => a !== actor)
    if (node.opening) {
      node.opening = node.opening.filter((b) => b.speaker !== actor)
      if (!node.opening.length) node.opening = null
    }
  }
  for (const edge of graph.edges) if (edge.requires_holding?.actor === actor) edge.requires_holding = null
}
