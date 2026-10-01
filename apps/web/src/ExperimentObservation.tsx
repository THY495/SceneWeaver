import { useEffect, useRef, useState } from 'react'

export type ExperimentPerson = {
  id: string
  name: string
  player: boolean
  location: string
  revision: number
  unnoticed_ids?: string[]
}
type Item = {
  id: string
  kind: string
  text?: string
  content?: string
  status?: string
  version?: number
  source_ids?: string[]
  replaces_id?: string | null
  speaker?: string
  seq?: number
  source_class?: string
  detail?: { basis?: string }
}
const labels: Record<string, string> = {
  belief: '持有的信息或信念',
  episode: '获知或参与的经历',
  goal: '当前目标',
  speech: '言论',
  host: '创作者设定',
  authoring: '初始设定',
  action: '行动',
  active: '当前有效',
  superseded: '已被更新',
  disputed: '存在争议',
  archived: '已归档',
  completed: '已完成',
  relationship: '人物关系',
}
const label = (value: string) => labels[value] || value
type Frame = {
  actor_id: string | null
  captured_at: string
  story_seq: number
  paused: boolean
  actor: { name: string; location: string; revision: number } | null
  events: Item[]
  memories: Item[]
  event_total: number
  memory_total: number
  snapshot: {
    id: string
    covered_seq: number
    privacy_version: number
    summary: string
    source_ids: string[]
    created_at: string
  } | null
  exchanges: {
    question_id: string
    question: string
    asker_id: string
    target_id: string
    status: string
    response_event_id: string | null
    transitions: { status: string; event_id: string; seq: number }[]
  }[]
  latest_context_use: {
    task_id: string
    task_kind: string
    task_status: string
    recorded_at: string
    snapshot_id: string | null
    actor_revision: number
    privacy_version: number
    estimated_tokens: number
    observation_count: number
    memory_count: number
    omitted_id_count: number
    scope: string
  } | null
  exchange_matrix: {
    question_id: string
    seq: number
    question: string
    asker_id: string
    asker_name: string
    target_id: string
    target_name: string
    views: {
      actor_id: string
      actor_name: string
      player: boolean
      status: string
      response_event_id: string | null
    }[]
  }[]
  limit: number
}
const exchangeLabels: Record<string, string> = {
  open: '待回应',
  partial: '部分回应',
  defer: '延后',
  answer: '已回答',
  refuse: '已拒绝',
  silence: '以沉默回应',
  cancelled: '已撤回',
  unseen: '不可见',
}
export function ExperimentObservation({
  story,
  people,
  refresh,
  request,
  onInspect,
}: {
  story: string
  people: ExperimentPerson[]
  refresh: number
  request: (path: string, init?: RequestInit) => Promise<unknown>
  onInspect?: (eventId: string) => void
}) {
  const [actor, setActor] = useState(''),
    [frame, setFrame] = useState<Frame | null>(null)
  const [baseline, setBaseline] = useState<Frame | null>(null),
    [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const generation = useRef(0)
  async function load(capture = false) {
    const ticket = ++generation.current
    setLoading(true)
    setError('')
    try {
      const data = (await request(
        `/role/stories/${story}/observation-frame${actor ? `?actor_id=${encodeURIComponent(actor)}` : ''}`,
      )) as Frame
      if (ticket !== generation.current) return
      setFrame(data)
      if (capture) setBaseline(data)
    } catch (e) {
      if (ticket === generation.current) setError(String(e))
    } finally {
      if (ticket === generation.current) setLoading(false)
    }
  }
  useEffect(() => {
    setFrame(null)
    setBaseline(null)
    void load()
    return () => {
      generation.current++
    }
  }, [story, actor])
  useEffect(() => {
    if (refresh) void load()
  }, [refresh])
  function records(items: Item[], prior: Item[] | undefined, total: number, noun: string) {
    const old = new Map(prior?.map((item) => [item.id, item]))
    const missing = prior?.filter((item) => !items.some((current) => current.id === item.id)).length || 0
    return (
      <div>
        <p>
          {noun}：显示 {items.length} / {total} 条{total > items.length ? '（仅最近记录，范围不完整）' : ''}
        </p>
        {!!missing && (
          <p>基线中有 {missing} 条本次未返回，可能因删除、来源失效或展示范围变化；不显示旧内容。</p>
        )}
        {items.map((item) => {
          const previous = old.get(item.id)
          const mark = !prior
            ? ''
            : !previous
              ? '基线中未显示'
              : JSON.stringify(previous) !== JSON.stringify(item)
                ? '已变化'
                : '未变化'
          return (
            <details key={item.id} className="experiment-record">
              <summary>
                {mark && `${mark} · `}
                {label(item.kind)} {label(item.status || '')} ·{' '}
                {(item.text || item.content || '').slice(0, 65)}
              </summary>
              <p>
                {item.speaker && `${item.speaker}：`}
                {item.text || item.content}
              </p>
              {item.detail?.basis && (
                <p>
                  内容依据：
                  {item.detail.basis === 'confirmed'
                    ? '创作者确认的设定'
                    : item.detail.basis === 'hearsay'
                      ? '听闻或信念，未确认属实'
                      : item.detail.basis}
                </p>
              )}
              <small>
                记录 {item.id}
                {item.seq != null && ` · 顺序 ${item.seq}`}
                {item.source_class && ` · 来源类别 ${item.source_class}`}
                {item.version != null && ` · 版本 ${item.version}`}
              </small>
              {!!item.source_ids?.length && <p>来源事件：{item.source_ids.join('、')}</p>}
              {item.replaces_id && <p>替换的上一版本：{item.replaces_id}</p>}
              {onInspect && !actor && item.seq != null && (
                <button onClick={() => onInspect(item.id)}>查看此事件的依据与诊断</button>
              )}
            </details>
          )
        })}
      </div>
    )
  }
  return (
    <section className="experiment-observation">
      <h3>观察与记忆对比</h3>
      <p>
        创作者可切换全局、NPC与玩家的记录范围。角色视角是已保存的感知与记忆，不代表本次模型实际读入了全部内容；实际输入请看流程诊断。
      </p>
      <label className="field">
        实验观察视角
        <select aria-label="实验观察视角" value={actor} onChange={(e) => setActor(e.target.value)}>
          <option value="">创作者：全部事件</option>
          {people.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}（{p.player ? '玩家' : 'NPC'}）
            </option>
          ))}
        </select>
      </label>
      {actor && (
        <p className="memory-legend">
          <strong>怎样读个人记忆：</strong>
          “持有的信息或信念”回答“角色认为内容是什么”，本身不保证属实；“获知或参与的经历”回答“角色何时、如何得知或参与”。两者可以引用同一事件，不是重复确认。
        </p>
      )}
      <button disabled={loading} onClick={() => void load(true)}>
        设为临时比较基线
      </button>
      <p>
        基线只用于当前面板比较，切换视角即清除。
        {onInspect ? '需要留存结论，请使用本页“保存观察笔记”。' : '持久观察笔记位于故事运行观察台。'}
      </p>
      <button disabled={loading} onClick={() => void load()}>
        刷新观察对比
      </button>
      {baseline && (
        <p>
          基线：{new Date(baseline.captured_at).toLocaleTimeString()} · 事件水位 {baseline.story_seq}
          。仅保留在当前面板，切换视角会清除；不是可恢复的剧情快照。
        </p>
      )}
      {loading && <p role="status">读取观察中…</p>}
      {frame && (
        <>
          <p>
            本次：{new Date(frame.captured_at).toLocaleTimeString()} · 最新事件序号 {frame.story_seq} ·{' '}
            {frame.paused ? '已暂停' : '运行中'}
            {frame.actor &&
              ` · ${frame.actor.name} 位于 ${frame.actor.location} · 版本 ${frame.actor.revision}`}
          </p>
          <p>
            两次读取之间也可能有后台整理或其他创作者操作；差异不直接证明由某一次单步造成。发言经历不等于内容属实。
          </p>
          <div className="experiment-record">
            <h4>回应状态视角对照</h4>
            <p>
              这是创作者审计视图。同一请求在不同角色处可能仍是待回应、已经结束或完全不可见；“不可见”不会输入该角色上下文。
            </p>
            {frame.exchange_matrix.length === 0 ? (
              <p>当前运行尚无明确指定回应对象的请求。</p>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>请求</th>
                      {frame.exchange_matrix[0].views.map((view) => (
                        <th key={view.actor_id}>
                          {view.actor_name}
                          {view.player ? '（玩家）' : ''}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {frame.exchange_matrix.map((exchange) => (
                      <tr key={exchange.question_id}>
                        <th>
                          #{exchange.seq} {exchange.asker_name} → {exchange.target_name}
                          <br />
                          <small>{exchange.question.slice(0, 80)}</small>
                        </th>
                        {exchange.views.map((view) => (
                          <td key={view.actor_id}>{exchangeLabels[view.status] || view.status}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
          {actor && (
            <div className="experiment-record memory-snapshot">
              <h4>上下文整理状态</h4>
              {frame.snapshot ? (
                <details>
                  <summary>
                    最近快照覆盖至事件序号 {frame.snapshot.covered_seq} · {frame.snapshot.source_ids.length}{' '}
                    个来源
                    {baseline?.snapshot?.id === frame.snapshot.id ? ' · 与基线相同' : ''}
                  </summary>
                  <p>{frame.snapshot.summary || '快照没有可显示的摘要。'}</p>
                  <small>
                    快照 {frame.snapshot.id} · 隐私版本 {frame.snapshot.privacy_version} ·{' '}
                    {new Date(frame.snapshot.created_at).toLocaleString()}
                  </small>
                  <p>来源事件：{frame.snapshot.source_ids.join('、') || '无'}</p>
                </details>
              ) : (
                <p>当前没有来源有效的角色快照；系统会继续使用原始可见事件和个人记忆。</p>
              )}
              <p>
                快照是上下文整理结果，不是新的世界事实；来源被删除或隐私版本变化后，旧快照不会继续显示或参与上下文。
              </p>
              <h4>最近一次实际上下文使用</h4>
              {frame.latest_context_use ? (
                <details>
                  <summary>
                    {frame.latest_context_use.snapshot_id
                      ? `模型实际使用快照 ${frame.latest_context_use.snapshot_id.slice(0, 8)}`
                      : '模型本次未使用角色快照'}{' '}
                    · 约 {frame.latest_context_use.estimated_tokens} tokens
                  </summary>
                  <p>
                    任务 {frame.latest_context_use.task_kind} · {frame.latest_context_use.task_status} ·
                    角色版本 {frame.latest_context_use.actor_revision} · 隐私版本{' '}
                    {frame.latest_context_use.privacy_version}
                  </p>
                  <p>
                    实际组装引用 {frame.latest_context_use.observation_count} 个观察、
                    {frame.latest_context_use.memory_count} 条记忆
                    {frame.latest_context_use.omitted_id_count
                      ? `；另有 ${frame.latest_context_use.omitted_id_count} 个标识因审计上限省略`
                      : ''}
                    。
                  </p>
                  <small>
                    任务 {frame.latest_context_use.task_id} ·{' '}
                    {new Date(frame.latest_context_use.recorded_at).toLocaleString()}
                  </small>
                </details>
              ) : (
                <p>尚无该角色已落盘的模型上下文证据。生成快照本身不代表后续推理已经使用它。</p>
              )}
            </div>
          )}
          {actor && (
            <div className="experiment-record">
              <h4>回应请求生命周期</h4>
              <p>这里只显示该角色能够观察到的提问与状态变化；私下回应不会泄露给未在场角色。</p>
              {frame.exchanges.length === 0 && <p>当前没有可见的明确回应请求。</p>}
              {frame.exchanges.map((exchange) => (
                <details key={exchange.question_id}>
                  <summary>
                    {exchangeLabels[exchange.status] || exchange.status} · {exchange.question.slice(0, 80)}
                  </summary>
                  <p>{exchange.question}</p>
                  <ol>
                    {exchange.transitions.map((transition) => (
                      <li key={`${transition.event_id}:${transition.status}`}>
                        事件序号 {transition.seq} · {exchangeLabels[transition.status] || transition.status} ·{' '}
                        {transition.event_id}
                      </li>
                    ))}
                  </ol>
                  <small>
                    提问 {exchange.question_id}
                    {exchange.response_event_id && ` · 最近状态事件 ${exchange.response_event_id}`}
                  </small>
                </details>
              ))}
            </div>
          )}
          {records(frame.events, baseline?.events, frame.event_total, '可见事件')}
          {actor &&
            records(frame.memories, baseline?.memories, frame.memory_total, '个人记忆（含目标、信念及经历）')}
        </>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  )
}
