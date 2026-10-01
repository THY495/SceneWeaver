export type RequestRecord = {
  span_id: string
  usage_id: string
  role: string
  message_count: number
  estimated_tokens: number
  max_output_tokens: number
  stream: boolean
  omitted_components: number
  components: {
    index: number
    role: string
    characters: number | null
    estimated_tokens: number
    workset_id: string | null
    section: string
    source_groups?: Record<string, { ids: string[]; total: number; omitted: number }>
  }[]
}
const names: Record<string, string> = {
  role_policy: '角色规则（可能追加阶段规则）',
  role_workset: '角色工作集',
  new_observations: '新观察',
  output_schema: '输出Schema',
  stage_message: '阶段消息（尚未归因）',
  review_grounding: '审核实际依据',
  retrieval_evidence: '检索材料',
}
const groups: Record<string, string> = {
  documents: '入选文档片段',
  capacity_omitted: '容量限制省略',
  excerpt_truncated: '正文节选截短',
  references: '声明引用',
  confirmed_events: '已确认事件',
  profile: '角色设定',
  subjective_goals: '主观目标',
  player_statements: '玩家原话记录',
}
export function RequestAudit({
  requests = [],
  dropped = 0,
  worksetPrefix,
  filtered = false,
}: {
  requests?: RequestRecord[]
  dropped?: number
  worksetPrefix?: string
  filtered?: boolean
}) {
  return (
    <details>
      <summary>模型最终请求组成</summary>
      <p>
        采集于HTTP发送尝试边界，不证明服务商已接收。估算tokens不等于账单tokens；未保存正文。工作集关联表示沿用组装消息容器，不保证其中未追加内容。
      </p>
      {!requests.length && (
        <p>
          {filtered
            ? '当前筛选没有可确认归属的发送记录。'
            : '无发送记录：旧任务、缓存命中、模拟模型或发送前失败均可能出现。'}
        </p>
      )}
      {dropped > 0 && <p>采集达到上限，省略 {dropped} 次请求。</p>}
      {requests.map((request, index) => (
        <section key={index}>
          <h4>
            {request.role} · {request.stream ? '流式' : '非流式'} · {request.message_count} 条消息
          </h4>
          <p>
            估算输入 {request.estimated_tokens} tokens · 输出上限 {request.max_output_tokens} tokens
          </p>
          <p>
            计费关联：{request.usage_id} · HTTP阶段：{request.span_id}
          </p>
          <ol>
            {request.components.map((c) => (
              <li key={c.index} value={c.index + 1}>
                {c.role} · {names[c.section] || c.section} · {c.estimated_tokens} tokens（估算）
                {c.workset_id && (
                  <span>
                    {' '}
                    ·{' '}
                    <a
                      href={`#${worksetPrefix}${c.workset_id}`}
                      onClick={(e) => {
                        e.preventDefault()
                        const target = e.currentTarget
                          .closest('.timing-inspector')
                          ?.querySelector(`#${CSS.escape(`${worksetPrefix}${c.workset_id}`)}`)
                        if (target) {
                          const section = target.closest('details')
                          if (section) section.open = true
                          target.scrollIntoView({ block: 'center' })
                        }
                      }}
                    >
                      定位组装记录 {c.workset_id}
                    </a>
                  </span>
                )}
                {Object.entries(c.source_groups || {}).map(([key, group]) => (
                  <p key={key}>
                    {groups[key] || key}（{group.total}）：{group.ids.join(', ') || '无'}
                    {group.omitted > 0 && `；清单截断 ${group.omitted} 个标识`}
                  </p>
                ))}
              </li>
            ))}
          </ol>
          {request.omitted_components > 0 && <p>省略 {request.omitted_components} 条消息的明细。</p>}
        </section>
      ))}
    </details>
  )
}
