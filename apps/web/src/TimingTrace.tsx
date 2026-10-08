import { useEffect, useState } from 'react'
import { RequestAudit, type RequestRecord } from './RequestAudit'
import { inTraceScope } from './traceScope'
import { PerformanceLanes } from './PerformanceLanes'
import { StoryTiming } from './StoryTiming'
import { VoiceFailureDetails } from './VoiceFailureDetails'
import { performanceEvidence, type PerformanceSpan } from './performanceTimeline'

type Span = {
  id: string
  parent_id: string | null
  name: string
  start_ms: number
  duration_ms: number | null
  status: string
  attributes: PerformanceSpan['attributes'] & {
    role?: string
    cache_hit?: boolean
    actor_id?: string
    step?: number
    candidate_status?: string
    audio_status?: string
    candidate_revision?: number
    assignment_authorization?: 'semantic' | 'grounded_response'
    assignment_source_event_id?: string
    assignment_grounded_rejected?: boolean
    query_cache_hit?: boolean
    query_has_indexed_memory?: boolean
    query_inflight_joined?: boolean
    tts_connection_ms?: number
    tts_task_ready_ms?: number
    tts_first_pcm_ms?: number
    tts_complete_ms?: number
    audio_error_type?: string
    audio_failure_phase?: string
    audio_received_close_code?: number
    audio_sent_close_code?: number
    audio_received_close_first?: boolean
  }
  marks: Record<string, number>
}
type Run = {
  requests?: RequestRecord[]
  dropped_requests?: number
  dropped_worksets?: number
  dropped_stage_origins?: number
  worksets?: {
    span_id?: string
    id?: string
    selection?: {
      before_tokens: number
      recall_before: number
      recall_after: number
      tail_before: number
      tail_after: number
      archive_capacity_trim: boolean
      tail_capacity_selection: boolean
    }
    actor_id: string
    revision: number
    snapshot_id: string | null
    estimated_tokens: number
    observation_ids: string[]
    memory_ids: string[]
    omitted_id_count: number
  }[]
  id: string
  attempt: number
  task_status: string
  duration_ms: number
  initial_queue_ms: number | null
  enqueue_to_worker_ms: number
  spans: Span[]
  dropped_spans: number
}
type Playback = { playback_id: string; status: string; timings: Record<string, unknown> }
type RoutingAudit = {
  accepted: boolean
  source_seq: number
  decision: {
    reason: string
    edge_id: string | null
    evidence?: { event_id: string; quote: string }[]
    evidence_scope_repair_count?: number
    assessments?: {
      edge_id: string
      status: 'met' | 'not_met' | 'uncertain'
      player_action?: 'independent' | 'dependent' | 'uncertain'
      current_agreements?: { actor: string; status: string; reason: string }[]
      reason: string
      evidence: { event_id: string; quote: string }[]
    }[]
  }
}
type Trace = {
  scene_routing?: RoutingAudit | null
  player_action_routing?: RoutingAudit | null
  timings: Run[]
  cost_usd: number
  unavailable: string[]
  events: { event_id: string; playback: Playback[] }[]
}
type Task = { id: string; kind: string; status: string }
type Props = {
  story: string
  initialTask?: string
  request: (path: string, init?: RequestInit) => Promise<unknown>
}
const labels: Record<string, string> = {
  'worker.execute': '执行及收尾',
  'npc.turn': 'NPC互动链',
  'scene.routing': '剧情出口评估（独立行动或整场互动）',
  'npc.plan': '意图规划',
  'npc.perform': '表达与审核',
  'npc.claim_validation': '声明与来源校验',
  'npc.intent_validation': '意图引用校验',
  'npc.semantic_review_result': '台词审查与调度校验结论',
  'context.assemble': '上下文组装',
  'memory.context': '个人记忆工作集',
  'memory.query': '记忆查询准备',
  'memory.reflect': '记忆整理',
  'knowledge.search': '知识搜索',
  'knowledge.retrieve': '深入检索',
  'embedding.encode': '向量请求',
  'model.call': '模型调用（含重试）',
  'model.attempt': '单次调用尝试',
  'model.http': '模型HTTP往返与读取',
  'model.slot_wait': '模型并发等待',
  'model.background_wait': '后台名额等待',
  'model.retry_wait': '重试退避',
  'world.commit': '校验与业务提交',
  'task.finish': '任务完成提交',
  'budget.reserve': '费用预留',
  'budget.settle': '费用结算',
  'director.plan': '导演规划',
  'npc.assignment': '角色任务授权',
  'npc.review_audio': '审查与首句语音并行',
  'voice.candidate_prepare': '候选首句语音准备',
}
const marks: Record<string, string> = {
  semantic_rejected: '台词未通过审查或调度校验',
  semantic_accepted: '台词通过本轮校验（不代表人工验收）',
  review_flag_rejected: '审核返回拒绝',
  reply_capacity_exhausted: 'NPC 待答问题没有后续发言位置',
  director_addressee_mismatch: '台词接收者与导演任务冲突',
  response_contract_reply_mismatch: '仅回应片段额外请求答复',
  response_binding_source_invalid: '回应任务来源无效',
  response_binding_missing_or_invalid: '回应绑定字段缺失或格式无效',
  response_binding_source_mismatch: '回应来源与当前任务不一致',
  response_binding_quote_mismatch: '回应片段不在当前候选原文中',
  response_binding_quote_blank: '回应片段只有空白',
  response_binding_quote_missing: '没有实际回应原文片段',
  response_binding_delivery_mismatch: '回应状态与发言类型不一致',
  response_task_not_addressed: '模型判为未回应实际任务',
  duplicate_npc_request: '同一 NPC 本轮已有待答问题',
  review_has_issues: '审核列出问题',
  unsupported_check: '存在不受支持的断言',
  reason_unsupported_fact: '无依据事实',
  reason_invented_history: '新增过去经历或行动',
  reason_acquisition_mismatch: '获知方式不匹配',
  reason_statement_promotion: '把言论内容升格为事实',
  reason_agreement_promotion: '扩大约定或承诺范围',
  reason_player_agency: '替玩家行动或决定',
  reason_mixed_recipient: '混合多个主要接收者',
  reason_other: '其他原因',
  reason_unclassified: '未提供有效分类',
  source_unavailable: '来源不存在或不可用',
  reply_unavailable: '待回应事项不属于角色或已结束',
  quote_empty: '来源引文为空',
  quote_mismatch: '引文未逐字匹配来源',
  claim_text_mismatch: '声明片段未逐字匹配正文',
  statement_promoted: '将言论当作客观事实或完成行动',
  recalled_quote_mismatch: '历史原话未逐字匹配',
  rejected: '本次校验拒绝',
  accepted: '本次校验通过（仍需语义审核）',
  response_headers: '响应头',
  first_content: '首内容',
  body_complete: '内容读完',
}
const voiceMarks: Record<string, string> = {
  first_byte_ms: 'TTS准备开始至首包',
  synthesis_ms: 'TTS准备总耗时',
  send_to_release_ms: '用户提交至请求播放（浏览器）',
  first_audio_ms: '用户提交至首采样排程（浏览器）',
  output_latency_ms: '浏览器估算音频输出延迟',
}
const seconds = (ms: number) => `${(ms / 1000).toFixed(3)}秒`

export function TimingTrace({ story, request, initialTask = '' }: Props) {
  const [actorNames, setActorNames] = useState<Record<string, string>>({})
  useEffect(() => {
    let active = true
    setActorNames({})
    request(`/role/stories/${story}/roster`)
      .then((data) => {
        if (active)
          setActorNames(
            Object.fromEntries(
              (data as { id: string; name: string }[]).map((person) => [person.id, person.name]),
            ),
          )
      })
      .catch(() => {
        /* Optional current-name labels; identifiers remain usable. */
      })
    return () => {
      active = false
    }
  }, [story])
  const [tasks, setTasks] = useState<Task[]>([]),
    [selected, setSelected] = useState(initialTask)
  const [trace, setTrace] = useState<Trace | null>(null),
    [error, setError] = useState(''),
    [refresh, setRefresh] = useState(0)
  const [actorFilter, setActorFilter] = useState(''),
    [stepFilter, setStepFilter] = useState('')
  useEffect(() => {
    let active = true
    request(`/role/stories/${story}/traces`)
      .then((data) => {
        if (active) setTasks(data as Task[])
      })
      .catch((e) => {
        if (active) setError(String(e))
      })
    return () => {
      active = false
    }
  }, [story, refresh])
  useEffect(() => {
    let active = true
    setTrace(null)
    setActorFilter('')
    setStepFilter('')
    setError('')
    if (selected)
      request(`/role/stories/${story}/tasks/${selected}/trace`)
        .then((data) => {
          if (active) setTrace(data as Trace)
        })
        .catch((e) => {
          if (active) setError(String(e))
        })
    return () => {
      active = false
    }
  }, [story, selected, refresh])
  return (
    <details className="timing-inspector" open={initialTask ? true : undefined}>
      <summary>流程耗时（创作者）</summary>
      <StoryTiming key={story} story={story} request={request} names={actorNames} />
      <p className="muted">
        执行结束后保存。横条表示并行或嵌套阶段，不能把所有时长相加；模型HTTP阶段也包含读取、解析和回调，不是纯网络或推理时间。
      </p>
      <label className="field">
        选择任务
        <select value={selected} onChange={(e) => setSelected(e.target.value)}>
          <option value="">选择一次互动或场外活动</option>
          {initialTask && !tasks.some((t) => t.id === initialTask) && (
            <option value={initialTask}>关联任务 · {initialTask.slice(0, 8)}</option>
          )}
          {tasks.map((t) => (
            <option key={t.id} value={t.id}>
              {t.kind === 'npc_step' ? '实验单步' : t.kind === 'scene_run' ? '场外活动' : '对话'} · {t.status}{' '}
              · {t.id.slice(0, 8)}
            </option>
          ))}
        </select>
      </label>
      <button onClick={() => setRefresh((n) => n + 1)}>刷新耗时</button>
      {error && <p role="status">{error}</p>}
      {trace && (
        <>
          <p>此任务已结算费用：${trace.cost_usd.toFixed(5)}（不含尚未结算预留及其他后台任务）</p>
          <details>
            <summary>AI 转场判断</summary>
            {trace.scene_routing || trace.player_action_routing ? (
              <>
                {[
                  { title: '整场互动出口', routing: trace.scene_routing },
                  { title: '玩家独立行动出口（不代表 NPC 表演完成）', routing: trace.player_action_routing },
                ]
                  .filter((row) => row.routing)
                  .map(
                    ({ title, routing }) =>
                      routing && (
                        <section key={title}>
                          <h4>{title}</h4>
                          <p>
                            {routing.accepted ? '已提出转场候选，仍需阅读及执行校验' : '未提出转场候选'}
                            {' · '}世界序号 {routing.source_seq}
                          </p>
                          <p>{routing.decision.reason}</p>
                          {!!routing.decision.evidence_scope_repair_count && (
                            <p>
                              引用字段已按来源归位；下面保留模型原始引用分配，最终候选仅使用已校验的行动证据。
                            </p>
                          )}
                          {routing.accepted && routing.decision.evidence && (
                            <details>
                              <summary>最终候选的行动证据</summary>
                              {routing.decision.evidence.map((evidence, i) => (
                                <blockquote key={`${evidence.event_id}:${i}`}>
                                  {evidence.quote}
                                  <br />
                                  <small>来源事件：{evidence.event_id}</small>
                                </blockquote>
                              ))}
                            </details>
                          )}
                          {routing.decision.assessments?.map((assessment) => (
                            <div key={assessment.edge_id}>
                              <strong>
                                出口 {assessment.edge_id}：
                                {{ met: '满足', not_met: '不满足', uncertain: '不确定' }[assessment.status]}
                              </strong>
                              <p>{assessment.reason}</p>
                              {assessment.player_action && (
                                <p>
                                  行动依赖：
                                  {
                                    { independent: '独立', dependent: '依赖其他回应', uncertain: '尚不明确' }[
                                      assessment.player_action
                                    ]
                                  }
                                </p>
                              )}
                              {assessment.current_agreements?.map((agreement) => (
                                <p key={agreement.actor}>
                                  当前同行立场 · {agreement.actor}：
                                  {{ agreed: '同意', declined: '拒绝', uncertain: '不确定' }[
                                    agreement.status
                                  ] ?? agreement.status}{' '}
                                  · {agreement.reason}
                                </p>
                              ))}
                              {assessment.evidence.map((evidence, i) => (
                                <blockquote key={`${evidence.event_id}:${i}`}>
                                  {evidence.quote}
                                  <br />
                                  <small>来源事件：{evidence.event_id}</small>
                                </blockquote>
                              ))}
                            </div>
                          ))}
                        </section>
                      ),
                  )}
                <p>
                  这里只显示通过物品硬条件筛选后送入模型的出口。候选不代表已经转场，模型判断也不等于世界事实。
                </p>
              </>
            ) : (
              <p>
                本任务没有已保存的转场判断。可能未触发、没有可评估出口、调用失败或属于旧任务；不能据此判定所有条件不满足。
              </p>
            )}
          </details>
          <div className="trace-filters">
            <label>
              角色{' '}
              <select
                aria-label="诊断角色筛选"
                value={actorFilter}
                onChange={(e) => setActorFilter(e.target.value)}
              >
                <option value="">全部（含共享阶段）</option>
                {[
                  ...new Set(
                    trace.timings.flatMap((run) =>
                      run.spans.map((s) => s.attributes.actor_id).filter((id): id is string => !!id),
                    ),
                  ),
                ].map((id) => (
                  <option key={id} value={id}>
                    {actorNames[id] ? `${actorNames[id]}（当前名称） · ${id.slice(0, 8)}` : id}
                  </option>
                ))}
              </select>
            </label>
            <label>
              步骤{' '}
              <select
                aria-label="诊断步骤筛选"
                value={stepFilter}
                onChange={(e) => setStepFilter(e.target.value)}
              >
                <option value="">全部</option>
                {[
                  ...new Set(
                    trace.timings.flatMap((run) =>
                      run.spans
                        .map((s) => s.attributes.step)
                        .filter((step): step is number => step !== undefined),
                    ),
                  ),
                ]
                  .sort((a, b) => a - b)
                  .map((step) => (
                    <option key={step} value={String(step)}>
                      {step}
                    </option>
                  ))}
              </select>
            </label>
          </div>
          {(actorFilter || stepFilter) && (
            <p>
              只显示可沿父阶段确认归属的记录；共享或缺少归属的阶段被隐藏。任务总时长、费用和语音区仍为整次任务，不能当作筛选后的合计。
            </p>
          )}
          {!trace.timings.length && <p>暂无已保存的阶段耗时：旧版本任务、仍在执行或采集未成功时可能为空。</p>}
          {trace.timings.map((run) => (
            <section key={run.id}>
              <h4>
                尝试 {run.attempt} · {run.task_status} · 执行及收尾 {seconds(run.duration_ms)}
              </h4>
              <p className="muted">
                首次入队到领取：
                {run.initial_queue_ms === null ? '未知（恢复尝试）' : seconds(run.initial_queue_ms)}
                ；从任务创建至本次启动：{seconds(run.enqueue_to_worker_ms)}（恢复时含此前执行和等待）。
              </p>
              {run.dropped_spans > 0 && <p>超出采集上限，省略 {run.dropped_spans} 个阶段。</p>}
              {!actorFilter && !stepFilter && (
                <PerformanceLanes spans={run.spans} duration={run.duration_ms} names={actorNames} />
              )}
              <details>
                <summary>所有阶段与嵌套明细</summary>
                <ol className="timing-rows">
                  {run.spans
                    .filter((span) => inTraceScope(run.spans, span.id, actorFilter, stepFilter))
                    .map((span) => (
                      <li key={span.id}>
                        <div>
                          {span.parent_id ? '↳ ' : ''}
                          {labels[span.name] || span.name}
                          {span.attributes.role ? ` · ${span.attributes.role}` : ''} · {span.status}
                          {span.attributes.cache_hit ? ' · 缓存命中' : ''}
                          {span.attributes.candidate_status && ` · 候选 ${span.attributes.candidate_status}`}
                          {span.attributes.audio_status && ` · 音频 ${span.attributes.audio_status}`}
                          {span.attributes.assignment_authorization &&
                            ` · ${span.attributes.assignment_authorization === 'grounded_response' ? '运行时核验真实发言来源' : '模型语义授权'}`}
                          {span.attributes.assignment_source_event_id &&
                            ` · 来源 ${span.attributes.assignment_source_event_id}`}
                          {span.attributes.assignment_grounded_rejected && ' · 结构化来源不成立'}
                          {span.attributes.query_cache_hit && ' · 查询向量已缓存'}
                          {span.attributes.query_inflight_joined && ' · 复用进行中的查询请求'}
                          {span.attributes.query_has_indexed_memory === false && ' · 本角色无已索引记忆'}
                          {performanceEvidence(span).length > 0 &&
                            ` · ${performanceEvidence(span).join(' · ')}`}
                        </div>
                        <div className="timing-track" aria-hidden="true">
                          <div
                            style={{
                              marginLeft: `${Math.min(100, (span.start_ms / Math.max(1, run.duration_ms)) * 100)}%`,
                              width: `${Math.min(100, ((span.duration_ms || 0) / Math.max(1, run.duration_ms)) * 100)}%`,
                            }}
                          />
                        </div>
                        <small>
                          开始 +{seconds(span.start_ms)} · 持续{' '}
                          {span.duration_ms === null ? '未知' : seconds(span.duration_ms)}
                          {(
                            [
                              ['tts_connection_ms', '语音连接就绪'],
                              ['tts_task_ready_ms', '供应商任务就绪'],
                              ['tts_first_pcm_ms', '首段PCM'],
                              ['tts_complete_ms', '供应商完成'],
                            ] as const
                          ).map(([key, label]) =>
                            typeof span.attributes[key] === 'number' ? (
                              <span key={key}>
                                {' '}
                                · {label} +{seconds(span.attributes[key]!)}（相对语音适配器启动）
                              </span>
                            ) : null,
                          )}
                          {Object.entries(span.marks).map(([name, ms]) => (
                            <span key={name}>
                              {' '}
                              · {marks[name] || name} +{seconds(ms)}（相对此阶段）
                            </span>
                          ))}
                        </small>
                        <VoiceFailureDetails
                          diagnostic={{
                            failure_type: span.attributes.audio_error_type,
                            failure_phase: span.attributes.audio_failure_phase,
                            received_close_code: span.attributes.audio_received_close_code,
                            sent_close_code: span.attributes.audio_sent_close_code,
                            received_close_first: span.attributes.audio_received_close_first,
                          }}
                        />
                      </li>
                    ))}
                </ol>
              </details>
              <details>
                <summary>上下文组装来源（不含后续阶段提示与检索补充）</summary>
                {!!run.dropped_worksets && <p>采集达到上限，省略 {run.dropped_worksets} 份组装记录。</p>}
                {!run.worksets?.length && <p>未采集来源清单。</p>}
                {run.worksets
                  ?.filter((set) => inTraceScope(run.spans, set.span_id, actorFilter, stepFilter))
                  .map((set, index) => (
                    <div key={index} id={`workset-${run.id}-${set.id}`}>
                      {set.id && <p>组装记录：{set.id}</p>}
                      {set.selection && (
                        <p>
                          容量筛选前估算 {set.selection.before_tokens} tokens；归档回忆{' '}
                          {set.selection.recall_before} → {set.selection.recall_after}，新观察{' '}
                          {set.selection.tail_before} → {set.selection.tail_after}。
                          {set.selection.archive_capacity_trim &&
                            ' 容量不足：仅保留固定来源回忆并精简归档字段。'}
                          {set.selection.tail_capacity_selection &&
                            ' 容量不足：保留必要观察后按相关性选择其余观察。'}
                        </p>
                      )}
                      <p>
                        角色 {set.actor_id} · 版本 {set.revision} · 估算 {set.estimated_tokens} tokens
                      </p>
                      <p>摘要版本：{set.snapshot_id || '无'}</p>
                      <p>
                        观察来源：{set.observation_ids.join(', ') || '无'}；记忆：
                        {set.memory_ids.join(', ') || '无'}
                      </p>
                      {set.omitted_id_count > 0 && <p>清单达到上限，省略 {set.omitted_id_count} 个标识。</p>}
                    </div>
                  ))}
              </details>
              <RequestAudit
                requests={run.requests?.filter((r) =>
                  inTraceScope(run.spans, r.span_id, actorFilter, stepFilter),
                )}
                dropped={run.dropped_requests}
                worksetPrefix={`workset-${run.id}-`}
                filtered={!!actorFilter || !!stepFilter}
              />
              {!!run.dropped_stage_origins && (
                <p>阶段来源标记达到采集上限，省略 {run.dropped_stage_origins} 条；部分消息因此未归因。</p>
              )}
            </section>
          ))}
          {(trace.events || []).flatMap((event) =>
            event.playback.map((playback) => (
              <div key={playback.playback_id}>
                <h4>语音播放 · {playback.status}</h4>
                {typeof playback.timings.approved_audio_cache_hit === 'boolean' && (
                  <p>
                    {playback.timings.approved_audio_cache_hit
                      ? '使用已批准的预生成音频；以下准备耗时为缓存读取与发送，不是重新合成。'
                      : '本片段未命中预生成音频，按需合成。'}
                  </p>
                )}
                {Object.entries(voiceMarks).map(([key, label]) =>
                  typeof playback.timings[key] === 'number' ? (
                    <p key={key}>
                      {label}：{seconds(playback.timings[key] as number)}
                    </p>
                  ) : null,
                )}
                <VoiceFailureDetails diagnostic={playback.timings.failure} />
              </div>
            )),
          )}
          <p className="muted">
            尚未拆分：供应商排队与纯推理、DNS/TCP/TLS、纯数据库事务、跨端时钟和实时阶段更新。非流式调用没有首token测量；实际扬声器首声未在此图采集。语音指标沿用各自起点，不与服务端横条强行对齐。
          </p>
        </>
      )}
    </details>
  )
}
