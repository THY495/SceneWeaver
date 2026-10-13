import type { Narrative } from './NarrativeEditor'
export function graphReadiness(graph?: Narrative | null): string[] {
  return graphDiagnostics(graph).map((issue) => issue.message)
}
export function releaseReadiness(
  graph?: Narrative | null,
  people: { alias: string; location: string }[] = [],
  player?: { alias: string; location: string },
): string[] {
  return releaseDiagnostics(graph, people, player).map((issue) => issue.message)
}
export function releaseDiagnostics(
  graph?: Narrative | null,
  people: { alias: string; location: string }[] = [],
  player?: { alias: string; location: string },
): AuthoringDiagnostic[] {
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
      return [
        {
          code: 'backstage-release:' + node.id,
          scene: node.id,
          panel:
            (node.visual_mode || 'stage') !== 'stage'
              ? 'stage'
              : exits.length !== 1 || (exits[0].trigger || 'manual') !== 'manual' || exits[0].requires_holding
                ? 'exits'
                : 'rules',
          message: `场景「${node.title}」尚不能在独立游戏中自动运行；目前仅支持入口处同场 NPC 和唯一无条件出口。`,
        },
      ]
    if (
      node.direction &&
      (node.direction.beats.length > 8 ||
        node.direction.beats.some((beat) =>
          beat.participants.some((alias) => !node.participants?.includes(alias)),
        ))
    )
      return [
        {
          code: 'backstage-direction:' + node.id,
          scene: node.id,
          panel: 'opening',
          message: `场景「${node.title}」的幕后 AI 编排最多允许八个段落，参与者须在本场 NPC 中。`,
        },
      ]
    current = exits[0].target
  }
  if (seen.size !== backstage.size) {
    const unvisited = [...backstage.keys()].find((id) => !seen.has(id))!
    return [
      {
        code: 'backstage-chain:' + unvisited,
        scene: unvisited,
        panel: 'exits',
        message: '独立游戏仅支持从入口连续经过的固定幕后场景；互动后的幕后场景还需要自动调度。',
      },
    ]
  }
  return []
}
export type AuthoringDiagnostic = {
  code: string
  scene: string
  panel: 'opening' | 'rules' | 'exits' | 'stage'
  message: string
}
export function diagnosticWorkspace(issue: AuthoringDiagnostic): 'graph' | 'scene' {
  return issue.code.startsWith('detached:') || issue.code.startsWith('backstage-chain:') ? 'graph' : 'scene'
}
export function openingRecipientWarnings(graph?: Narrative | null): AuthoringDiagnostic[] {
  if (!graph) return []
  return graph.nodes.flatMap((node) =>
    (node.opening || [])
      .filter(
        (block) =>
          block.kind === 'dialogue' &&
          !block.target &&
          node.observers.length >= 3 &&
          /你(?!们)/.test(block.text) &&
          /[?？]/.test(block.text),
      )
      .map((block) => ({
        code: `opening-recipient:${node.id}:${block.id}`,
        scene: node.id,
        panel: 'opening' as const,
        message: `场景「${node.title}」有面向“你”的固定提问，且有多位知情者，但尚未指定“说给谁听”；旁听角色可能误认自己是接收者。若本来就是对所有人在场者发问，可保留未指定。`,
      })),
  )
}
export function transitionWarnings(graph?: Narrative | null, playerAlias = 'player'): AuthoringDiagnostic[] {
  if (!graph) return []
  const nodes = new Map(graph.nodes.map((node) => [node.id, node]))
  const warnings: AuthoringDiagnostic[] = []
  for (const source of graph.nodes) {
    const exits = graph.edges.filter((edge) => edge.source === source.id)
    const aiConditions = new Map<string, typeof exits>()
    for (const edge of exits) {
      if (edge.trigger !== 'ai' || !edge.condition?.trim()) continue
      const key = edge.condition.trim().replace(/\s+/g, ' ')
      aiConditions.set(key, [...(aiConditions.get(key) || []), edge])
    }
    for (const sameCondition of aiConditions.values())
      if (sameCondition.length > 1)
        warnings.push({
          code: `ai-condition-duplicate:${source.id}:${sameCondition[0].id}`,
          scene: source.id,
          panel: 'exits',
          message: `场景「${source.title}」的出口「${sameCondition.map((edge) => edge.label).join('」「')}」使用相同的 AI 触发条件；若附加条件也同时满足，系统会因多个出口成立而留在当前场景。请写出可区分的条件或确认附加条件互斥。`,
        })
    if (exits.length && exits.every((edge) => edge.trigger === 'ai' && edge.requires_agreement?.length))
      warnings.push({
        code: `agreement-no-alternative:${source.id}`,
        scene: source.id,
        panel: 'exits',
        message: `场景「${source.title}」的所有出口都要求 NPC 本人同意；相关角色拒绝、含糊或没有发言时，玩家会留在当前场景。若希望玩家仍可继续，请增加不依赖同意的分支。`,
      })
  }
  for (const edge of graph.edges) {
    const source = nodes.get(edge.source)
    const target = nodes.get(edge.target)
    if (!source || !target) continue
    for (const actor of edge.requires_agreement || [])
      if (
        source.mode === 'backstage' ||
        !source.direction?.beats.some((beat) => beat.participants.includes(actor)) ||
        !source.observers.includes(actor)
      )
        warnings.push({
          code: `agreement-unreachable:${edge.id}:${actor}`,
          scene: source.id,
          panel: 'exits',
          message: `出口「${edge.label}」要求 NPC ${actor} 亲口同意，但该角色没有同时参与本场 AI 演出和开场知情；请检查他是否有机会在本场向玩家表态。`,
        })
    if (!source.direction) continue
    const performers = new Set(source.direction.beats.flatMap((beat) => beat.participants))
    for (const entry of target.entry_locations || []) {
      if (entry.actor === playerAlias || !performers.has(entry.actor)) continue
      if (edge.requires_agreement?.includes(entry.actor)) continue
      warnings.push({
        code: `transition-commitment:${edge.id}:${entry.actor}`,
        scene: source.id,
        panel: 'exits',
        message: `「${source.title}」的 NPC ${entry.actor} 参与 AI 演出，出口「${edge.label}」却会让其固定进入「${target.title}」。若该角色可以拒绝同行，请改用 AI 出口并要求本人同意；拒绝路线应通往不移动该角色的场景。固定剧情移动则可保留。`,
      })
    }
  }
  return warnings
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
    for (const beat of node.direction?.beats || [])
      if (beat.npc_exchange_required && beat.participants.length < 2)
        issues.push({
          code: 'director-npc-exchange:' + beat.id,
          scene: node.id,
          panel: 'opening',
          message: `场景「${node.title}」的段落「${beat.title}」要求 NPC 交流，请选择至少两位参与角色。`,
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
    for (const beat of node.direction?.beats || []) {
      beat.participants = beat.participants.filter((a) => a !== actor)
      if (beat.role_focus) delete beat.role_focus[actor]
      if (beat.direct_response_actor === actor) beat.direct_response_actor = null
    }
    if (node.opening) {
      node.opening = node.opening.filter((b) => b.speaker !== actor)
      for (const block of node.opening) if (block.target === actor) block.target = null
      if (!node.opening.length) node.opening = null
    }
  }
  for (const edge of graph.edges) {
    if (edge.requires_holding?.actor === actor) edge.requires_holding = null
    edge.requires_agreement = edge.requires_agreement?.filter((alias) => alias !== actor)
  }
}
