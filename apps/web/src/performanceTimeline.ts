import { traceScopeResolver } from './traceScope'

export type PerformanceSpan = {
  id: string
  parent_id: string | null
  name: string
  start_ms: number
  duration_ms: number | null
  status: string
  attributes: {
    actor_id?: string
    step?: number
    role?: string
    candidate_revision?: number
    review_provider_accepted?: boolean
    review_accepted?: boolean
    review_issue_count?: number
    review_unsupported_count?: number
    response_review_binding?: boolean
    response_binding_accepted?: boolean
    response_task_status?: string
    grounded_response_pruning_disabled?: boolean
  }
}

const stages: Record<string, string> = {
  'memory.context': '记忆工作集',
  'voice.candidate_prepare': '候选首句语音',
  'world.commit': '正式事件提交',
  'npc.semantic_review_result': '发布前审核校验结果',
  'npc.review_pruning': '候选局部剪裁',
}

export function performanceEvidence(span: PerformanceSpan): string[] {
  const a = span.attributes
  const result: string[] = []
  if (typeof a.candidate_revision === 'number') result.push(`稿次 ${a.candidate_revision}`)
  if (typeof a.review_provider_accepted === 'boolean')
    result.push(`模型通过标记：${a.review_provider_accepted ? '通过' : '拒绝'}`)
  if (typeof a.review_accepted === 'boolean') {
    // Older traces stored the provider flag here. Counts preserve their real
    // rejection; missing counts cannot prove a full successful review.
    if (!a.review_accepted || (a.review_issue_count || 0) > 0 || (a.review_unsupported_count || 0) > 0)
      result.push('本轮审核校验：拒绝')
    else if (a.review_issue_count === 0 && a.review_unsupported_count === 0) result.push('本轮审核校验：通过')
    else result.push('本轮审核校验：通过标记（完整结论未记录）')
  }
  if (a.response_review_binding) {
    result.push('回应绑定实验')
    const status: Record<string, string> = {
      responded: '已回应',
      refused: '拒绝',
      deferred: '暂缓',
      unknown: '未知',
      not_addressed: '未回应',
    }
    if (a.response_task_status && Object.hasOwn(status, a.response_task_status))
      result.push(`回应评估（模型）：${status[a.response_task_status]}`)
    else result.push('回应评估（模型）：未记录')
    if (typeof a.response_binding_accepted === 'boolean')
      result.push(`绑定结构门槛：${a.response_binding_accepted ? '通过' : '拒绝'}`)
  } else if (span.name === 'npc.semantic_review_result') result.push('回应绑定评估：未记录')
  if (a.grounded_response_pruning_disabled) result.push('保留回应完整性，禁用整段剪裁')
  return result
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
