import { useEffect, useRef, useState } from 'react'

type Decision = {
  id: string
  actor: string
  status: string
  outcome?: string
  created_at: string
  events: { id: string; text: string }[]
  cost_usd: number
  reserved_usd: number
}
type Note = { id: string; task_id: string | null; note: string; story_seq: number; created_at: string }
const active = (d: Decision) => ['queued', 'running', 'waiting'].includes(d.status)
const statusName: Record<string, string> = {
  completed: '已完成',
  failed: '失败',
  cancelled: '已撤销',
  queued: '排队中',
  running: '执行中',
  waiting: '等待处理',
}

export function RunNotebook({
  story,
  refresh,
  request,
  onInspect,
  onDiagnose,
}: {
  story: string
  refresh: number
  request: (path: string, init?: RequestInit) => Promise<unknown>
  onInspect: (id: string) => void
  onDiagnose: (id: string) => void
}) {
  const [decisions, setDecisions] = useState<Decision[]>([])
  const [notes, setNotes] = useState<Note[]>([])
  const [target, setTarget] = useState(''),
    [note, setNote] = useState('')
  const [error, setError] = useState(''),
    [saved, setSaved] = useState(''),
    [busy, setBusy] = useState(false)
  const pending = useRef<string | null>(null)
  const editor = useRef<HTMLFieldSetElement>(null)
  const base = `/role/experiments/runs/${story}`
  const generation = useRef(0)
  async function load() {
    const ticket = ++generation.current
    try {
      const [d, n] = await Promise.all([request(base + '/decisions'), request(base + '/notes')])
      if (ticket !== generation.current) return
      setDecisions((d as { decisions: Decision[] }).decisions)
      setNotes((n as { notes: Note[] }).notes)
      setError('')
    } catch (e) {
      if (ticket === generation.current) setError(String(e))
    }
  }
  useEffect(() => {
    void load()
    return () => {
      generation.current++
    }
  }, [story, refresh])
  async function save() {
    setBusy(true)
    setSaved('')
    setError('')
    pending.current ??= JSON.stringify({ request_id: crypto.randomUUID(), task_id: target || null, note })
    try {
      await request(base + '/notes', { method: 'POST', body: pending.current })
      pending.current = null
      setNote('')
      setSaved('观察笔记已保存，离开或刷新页面后仍可查看。')
      await load()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <section className="run-panel" aria-label="决策结果与观察笔记">
      <h2>决策结果与观察笔记</h2>
      <p>查看最近 50 次决策。等待也值得记录；这里的笔记仅供创作者查看，不会成为人物记忆。</p>
      <button onClick={() => void load()}>刷新决策与笔记</button>
      {!decisions.length && <p>尚无单步决策。先选择角色，执行一次决策，再在这里检查结果。</p>}
      {decisions.map((d, index) => (
        <details key={d.id} className="decision-card" open={index === 0 ? true : undefined}>
          <summary>
            {d.actor} · {statusName[d.status] || d.status} ·{' '}
            {d.outcome === 'wait'
              ? '本次未提交发言或行动'
              : d.outcome === 'committed'
                ? '已提交片段'
                : '尚无有效结果'}
          </summary>
          <p>
            {new Date(d.created_at).toLocaleString()} · 可见新事件 {d.events.length} 条 · 本任务已结算 $
            {d.cost_usd.toFixed(5)} · 预留 ${d.reserved_usd.toFixed(5)}
          </p>
          {d.outcome === 'wait' && (
            <p>
              系统记录了等待结果，没有保存可直接展示的具体原因。可先检查该人物的目标、知情与实际输入，再补充事件或换另一位角色决策。
            </p>
          )}
          {['failed', 'cancelled'].includes(d.status) && (
            <p>本次没有正常完成。请先查看诊断与现有事件，确认结果后再重试。</p>
          )}
          {d.events.map((e) => (
            <p key={e.id}>
              {e.text} <button onClick={() => onInspect(e.id)}>查看接收者与关联记忆</button>
            </p>
          ))}
          <button onClick={() => onDiagnose(d.id)}>查看本次实际输入与诊断</button>
          <button
            disabled={active(d) || busy || !!pending.current}
            onClick={() => {
              setTarget(d.id)
              setSaved('')
              editor.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
            }}
          >
            为这次决策写笔记
          </button>
          <details>
            <summary>技术编号</summary>
            <code>{d.id}</code>
          </details>
        </details>
      ))}
      <h3>保存观察笔记</h3>
      <fieldset ref={editor} disabled={busy || !!pending.current}>
        <label>
          笔记关联对象
          <select value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="">本次运行（整体观察）</option>
            {decisions
              .filter((d) => !active(d))
              .map((d) => (
                <option key={d.id} value={d.id}>
                  {d.actor} · {new Date(d.created_at).toLocaleString()} ·{' '}
                  {d.outcome === 'wait' ? '等待' : statusName[d.status] || d.status}
                </option>
              ))}
          </select>
        </label>
        <label>
          观察结论
          <textarea
            value={note}
            maxLength={2000}
            placeholder="例如：角色知道钥匙位置却选择等待，需要核对目标和实际输入。"
            onChange={(e) => setNote(e.target.value)}
          />
        </label>
      </fieldset>
      <button disabled={busy || (!pending.current && !note.trim())} onClick={() => void save()}>
        {pending.current ? '重试保存同一笔记' : '保存观察笔记'}
      </button>
      {pending.current && !busy && (
        <button
          onClick={() => {
            pending.current = null
            setSaved('已解除重试锁定。请先刷新已保存笔记，确认上次是否成功，避免重复记录。')
            void load()
          }}
        >
          核对记录并重新编辑
        </button>
      )}
      {saved && <p role="status">{saved}</p>}
      {error && <p role="alert">{error}</p>}
      <h3>已保存笔记（最近 100 条）</h3>
      {!notes.length && <p>还没有保存笔记。临时比较基线不会显示在这里。</p>}
      {notes.map((n) => (
        <article key={n.id} className="decision-card">
          <p>{n.note}</p>
          <small>
            {new Date(n.created_at).toLocaleString()} · 保存时事件序号 {n.story_seq} ·{' '}
            {n.task_id ? '关联单次决策' : '整体观察'}
          </small>
          {n.task_id && <button onClick={() => onDiagnose(n.task_id!)}>查看关联决策诊断</button>}
        </article>
      ))}
    </section>
  )
}
