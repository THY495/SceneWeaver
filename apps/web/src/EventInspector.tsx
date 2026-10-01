import { useEffect, useRef, useState } from 'react'
import { TimingTrace } from './TimingTrace'
type Event = {
  id: string
  text: string
  speaker: string
  kind: string
  task_id: string | null
  observations: { observer_id: string; view: { text: string; speaker: string } }[]
}
type Archive = {
  events: Event[]
  people: { id: string; name: string }[]
  memories: { id: string; actor_id: string; content: string; source_ids: string[] }[]
}
type Notes = { source_hash: string; notes: { id: string; category: string; note: string }[] }
const categories: Record<string, string> = {
  boundary: '越界知情',
  character: '人物不符',
  repetition: '重复',
  initiative: '主动性问题',
  good: '表现符合预期',
  other: '其他',
}
export function EventInspector({
  story,
  eventId,
  request,
  onClose,
}: {
  story: string
  eventId: string
  request: (path: string, init?: RequestInit) => Promise<unknown>
  onClose: () => void
}) {
  const [archive, setArchive] = useState<Archive | null>(null),
    [notes, setNotes] = useState<Notes | null>(null),
    [error, setError] = useState(''),
    [category, setCategory] = useState('boundary'),
    [note, setNote] = useState(''),
    [busy, setBusy] = useState(false)
  const pending = useRef<string | null>(null)
  const path = `/role/experiments/runs/${story}/events/${eventId}/notes`
  useEffect(() => {
    let live = true
    Promise.all([request(`/role/experiments/runs/${story}/record`), request(path)])
      .then(([a, n]) => {
        if (live) {
          setArchive(a as Archive)
          setNotes(n as Notes)
        }
      })
      .catch((e) => {
        if (live) setError(String(e))
      })
    return () => {
      live = false
    }
  }, [story, eventId])
  async function save() {
    if (!notes) return
    setBusy(true)
    setError('')
    pending.current ??= JSON.stringify({
      request_id: crypto.randomUUID(),
      source_hash: notes.source_hash,
      category,
      note,
    })
    try {
      await request(path, { method: 'POST', body: pending.current })
      pending.current = null
      setNotes((await request(path)) as Notes)
      setNote('')
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }
  const event = archive?.events.find((e) => e.id === eventId)
  const name = (id: string) => archive?.people.find((p) => p.id === id)?.name || '未知角色'
  return (
    <section className="run-panel event-inspector" aria-label="事件联动诊断">
      <button onClick={onClose}>关闭事件诊断</button>
      <h2>这条事件是如何发生的</h2>
      {error && <p role="alert">{error}</p>}
      {event ? (
        <>
          <blockquote>
            {event.speaker}：{event.text}
          </blockquote>
          <h3>谁实际收到什么</h3>
          {event.observations.map((o) => (
            <p key={o.observer_id}>
              <strong>{name(o.observer_id)}</strong> · {o.view.speaker}：{o.view.text}
            </p>
          ))}
          {!event.observations.length && <p>没有角色收到此事件，仅创作者可见。</p>}
          <h3>关联的当前记忆</h3>
          <p>下面是以此事件为来源的当前保存记忆，不代表当时生成前的输入。</p>
          {archive?.memories
            .filter((m) => m.source_ids.includes(eventId))
            .map((m) => (
              <p key={m.id}>
                {name(m.actor_id)}：{m.content}
              </p>
            ))}
          <h3>观察标记</h3>
          <fieldset disabled={busy || Boolean(pending.current)}>
            <label>
              分类
              <select value={category} onChange={(e) => setCategory(e.target.value)}>
                {Object.entries(categories).map(([key, value]) => (
                  <option key={key} value={key}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
            <label>
              说明
              <textarea value={note} maxLength={1000} onChange={(e) => setNote(e.target.value)} />
            </label>
          </fieldset>
          <button disabled={busy || !notes} onClick={() => void save()}>
            {pending.current ? '重试保存标记' : '保存标记'}
          </button>
          {notes?.notes.map((n) => (
            <p key={n.id}>
              {categories[n.category]}：{n.note || '无补充说明'}
            </p>
          ))}
          {event.task_id ? (
            <>
              <h3>关联任务：实际上下文、审核与耗时</h3>
              <p>一个任务可能包含多名角色或多个片段，可继续按角色与步骤筛选。</p>
              <TimingTrace key={event.task_id} story={story} initialTask={event.task_id} request={request} />
            </>
          ) : (
            <p>此事件由创作或人工干预产生，没有对应模型生成任务。</p>
          )}
        </>
      ) : (
        !error && <p>正在读取事件及其依据…</p>
      )}
    </section>
  )
}
