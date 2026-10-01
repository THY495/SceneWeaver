import { useEffect, useRef, useState } from 'react'
import { ExperimentPreparation } from './ExperimentPreparation'
import { ExperimentInjection } from './ExperimentInjection'
import { ExperimentObservation, type ExperimentPerson } from './ExperimentObservation'
import { ExperimentStep } from './ExperimentStep'
import { ExperimentMemorySnapshot } from './ExperimentMemorySnapshot'
import { TimingTrace } from './TimingTrace'
import { RunRelationship } from './RunRelationship'
import { EventInspector } from './EventInspector'
import { RunNotebook } from './RunNotebook'
import './story-studio.css'

type Props = {
  sid: string
  request: (path: string, init?: RequestInit) => Promise<unknown>
  onClose: () => void
}
export function RunStudio({ sid, request, onClose }: Props) {
  const [inspection, setInspection] = useState<string | null>(null)
  const inspectionPanel = useRef<HTMLDivElement>(null)
  const diagnosticPanel = useRef<HTMLDetailsElement>(null)
  const [title, setTitle] = useState('')
  const [cost, setCost] = useState<{ settled_usd: number; reserved_usd: number } | null>(null)
  const [story, setStory] = useState(''),
    [people, setPeople] = useState<ExperimentPerson[]>([])
  const [refresh, setRefresh] = useState(0),
    [seq, setSeq] = useState(0),
    [paused, setPaused] = useState(false)
  const [task, setTask] = useState<string>(),
    [error, setError] = useState(''),
    [busy, setBusy] = useState(false)
  const [name, setName] = useState(''),
    [core, setCore] = useState(''),
    [location, setLocation] = useState('大厅'),
    [observers, setObservers] = useState<string[]>([])
  const pending = useRef<string | null>(null)
  useEffect(() => {
    let live = true
    request(`/sessions/${sid}`)
      .then((v) => {
        if (live) setStory((v as { story_id: string }).story_id)
      })
      .catch((e) => setError(String(e)))
    return () => {
      live = false
    }
  }, [sid])
  useEffect(() => {
    if (!story) return
    let live = true
    Promise.all([
      request(`/role/stories/${story}/roster`),
      request(`/role/stories/${story}/observation-frame`),
    ])
      .then(([p, f]) => {
        if (!live) return
        setPeople(p as ExperimentPerson[])
        setSeq((f as { story_seq: number }).story_seq)
        setPaused((f as { paused: boolean }).paused)
      })
      .catch((e) => setError(String(e)))
    request(`/role/experiments/runs/${story}/record`)
      .then((a) => {
        if (live) {
          setCost((a as { cost: { settled_usd: number; reserved_usd: number } }).cost)
          setTitle((a as { story: { title: string } }).story.title)
        }
      })
      .catch(() => {
        if (live) setCost(null)
      })
    return () => {
      live = false
    }
  }, [story, refresh])
  const changed = () => setRefresh((v) => v + 1)
  function inspect(id: string) {
    setInspection(id)
    requestAnimationFrame(() =>
      inspectionPanel.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }),
    )
  }
  function diagnose(id: string) {
    setTask(id)
    if (diagnosticPanel.current) {
      diagnosticPanel.current.open = true
      diagnosticPanel.current.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  }
  async function join() {
    setBusy(true)
    setError('')
    pending.current ??= JSON.stringify({
      request_id: crypto.randomUUID(),
      expected_seq: seq,
      profile: { name, core, world: '', relationships: [], document_ids: [] },
      location,
      observer_ids: observers,
    })
    try {
      await request(`/role/experiments/stories/${story}/characters`, {
        method: 'POST',
        body: pending.current,
      })
      pending.current = null
      setName('')
      setCore('')
      setObservers([])
      changed()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <section className="story-studio" aria-label="运行观察台">
      <header className="studio-header">
        <div>
          <p className="eyebrow">运行观察 · 创作者视角</p>
          <h1>{title || '人物正在经历什么'}</h1>
          <p>
            {!people.length ? '读取运行状态…' : paused ? '剧情已暂停' : '剧情未暂停'} · 最新事件序号 {seq} ·{' '}
            {people.filter((p) => !p.player).length} 位 NPC
          </p>
          {cost && (
            <p>
              本运行已结算 ${cost.settled_usd.toFixed(4)} · 预留 ${cost.reserved_usd.toFixed(4)}（刷新时更新）
            </p>
          )}
        </div>
        <button onClick={onClose}>返回玩家对话</button>
      </header>
      <p>这里包含玩家可能不知道的信息。查看不推进剧情；单步执行会调用模型。</p>
      {error && (
        <p className="studio-error" role="alert">
          {error}
        </p>
      )}
      {story && (
        <>
          <div className="run-grid">
            <aside className="run-panel">
              <h2>世界与人物</h2>
              {people.map((p) => (
                <p key={p.id}>
                  <strong>{p.name}</strong> · {p.player ? '玩家' : 'NPC'}
                  <br />
                  {p.location}
                </p>
              ))}
              <button onClick={changed}>刷新世界状态</button>
              <ExperimentPreparation
                people={people}
                refresh={refresh}
                request={request}
                onChanged={changed}
              />
            </aside>
            <section className="run-panel">
              <h2>推进与干预</h2>
              <ExperimentStep
                story={story}
                refresh={refresh}
                request={request}
                onFinished={(id) => {
                  setTask(id)
                  changed()
                }}
              />
              <ExperimentMemorySnapshot
                story={story}
                people={people}
                refresh={refresh}
                request={request}
                onFinished={(id) => {
                  setTask(id)
                  changed()
                }}
              />
              <ExperimentInjection
                story={story}
                people={people}
                refresh={refresh}
                request={request}
                onChanged={changed}
              />
              <RunRelationship people={people} paused={paused} request={request} onChanged={changed} />
              <details>
                <summary>加入新角色</summary>
                <p>只加入本次运行，不改写故事起点。新角色只知道下方填写的经历，不继承旧事件。</p>
                <fieldset disabled={busy || Boolean(pending.current)}>
                  <label>
                    姓名
                    <input value={name} onChange={(e) => setName(e.target.value)} />
                  </label>
                  <label>
                    身份、经历及已有知识
                    <textarea value={core} onChange={(e) => setCore(e.target.value)} />
                  </label>
                  <label>
                    出现地点
                    <input
                      value={location}
                      onChange={(e) => {
                        setLocation(e.target.value)
                        setObservers([])
                      }}
                    />
                  </label>
                  <p>谁察觉他的到来？</p>
                  {people
                    .filter((p) => p.location === location)
                    .map((p) => (
                      <label key={p.id}>
                        <input
                          type="checkbox"
                          checked={observers.includes(p.id)}
                          onChange={(e) =>
                            setObservers(
                              e.target.checked ? [...observers, p.id] : observers.filter((x) => x !== p.id),
                            )
                          }
                        />
                        {p.name}
                      </label>
                    ))}
                </fieldset>
                <p>
                  将在{location}新增{name || '一位角色'}；{observers.length}位现有角色收到到场事件。
                </p>
                <button
                  disabled={busy || !paused || !name.trim() || !core.trim()}
                  onClick={() => void join()}
                >
                  {pending.current ? '重试此次加入' : '确认加入本次运行'}
                </button>
                {pending.current && !busy && (
                  <button
                    onClick={() => {
                      pending.current = null
                      changed()
                      setError('已解除重试锁定，请先核对角色列表；若上次已成功，请勿重复添加。')
                    }}
                  >
                    刷新并重新编辑
                  </button>
                )}
              </details>
            </section>
          </div>
          <RunNotebook
            story={story}
            refresh={refresh}
            request={request}
            onInspect={inspect}
            onDiagnose={diagnose}
          />
          <section className="run-panel">
            <h2>事件与人物记忆</h2>
            <ExperimentObservation
              story={story}
              people={people}
              refresh={refresh}
              request={request}
              onInspect={inspect}
            />
          </section>
          <div ref={inspectionPanel}>
            {inspection && (
              <EventInspector
                key={inspection}
                story={story}
                eventId={inspection}
                request={request}
                onClose={() => setInspection(null)}
              />
            )}
          </div>
          <details className="run-panel" ref={diagnosticPanel}>
            <summary>高级诊断：实际输入、调用与审核</summary>
            <p>选取任务，检查本次真正读入的信息、审核与耗时。</p>
            <TimingTrace key={task} story={story} initialTask={task} request={request} />
          </details>
        </>
      )}
    </section>
  )
}
