import { traceScopeResolver } from './traceScope'

export type PerformanceSpan = {
  id: string
  parent_id: string | null
  name: string
  start_ms: number
  duration_ms: number | null
  status: string
  attributes: { actor_id?: string; step?: number; role?: string }
}

const stages: Record<string, string> = {
  'memory.context': '记忆工作集',
  'voice.candidate_prepare': '候选首句语音',
  'world.commit': '正式事件提交',
}
const roles: Record<string, string> = {
  director: '导演规划',
  actor_review: '台词审查',
  dialogue: '台词生成',
  intent: '意图规划',
  role_intent: '意图规划',
  director_assignment: '角色任务授权审查',
  director_progress: '段落完成评估',
}

export function performanceLanes(spans: PerformanceSpan[]) {
  const groups = new Map<string, { span: PerformanceSpan; label: string }[]>()
  const scopeFor = traceScopeResolver(spans)
  for (const span of spans) {
    if (span.name !== 'model.call' && !stages[span.name]) continue
    const scope = scopeFor(span.id)
    const owner = scope.actor || (span.attributes.role === 'director' ? 'director' : 'shared')
    const label = stages[span.name] || roles[span.attributes.role || ''] || span.attributes.role || '模型调用'
    if (!groups.has(owner)) groups.set(owner, [])
    groups.get(owner)!.push({ span, label })
  }
  return [...groups].map(([owner, entries]) => ({
    owner,
    entries: entries.sort((a, b) => a.span.start_ms - b.span.start_ms),
  }))
}
