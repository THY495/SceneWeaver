import { useEffect, useRef, useState } from 'react'
import type { ExperimentPerson } from './ExperimentObservation'

type Goal = { id: string; content: string; status: string; version: number }
export function ExperimentPreparation({
  people,
  refresh,
  request,
  onChanged,
}: {
  people: ExperimentPerson[]
  refresh: number
  request: (path: string, init?: RequestInit) => Promise<unknown>
  onChanged: () => void
}) {
  const [actor, setActor] = useState(''),
    [location, setLocation] = useState(''),
    [goal, setGoal] = useState('')
  const [goals, setGoals] = useState<Goal[]>([]),
    [notice, setNotice] = useState(''),
    [busy, setBusy] = useState(false)
  const [more, setMore] = useState(false)
  const pending = useRef<{ path: string; method: string; body: string } | null>(null)
  const person = people.find((p) => p.id === actor)
  useEffect(() => {
    if (!actor && people.length) setActor(people[0].id)
  }, [people, actor])
  useEffect(() => {
    if (person && !pending.current) setLocation(person.location)
  }, [actor, person?.revision])
  useEffect(() => {
    if (!actor) return
    let live = true
    setGoals([])
    request(`/role/actors/${actor}/memories?kind=goal`)
      .then((data) => {
        if (!live) return
        const value = data as { memories: Goal[]; next_before: string | null }
        setGoals(value.memories)
        setMore(!!value.next_before)
      })
      .catch((e) => {
        if (live) setNotice(String(e))
      })
    return () => {
      live = false
    }
  }, [actor, refresh])
  async function mutate(path?: string, method?: string, body?: unknown) {
    if (busy) return
    if (!pending.current && path && method) pending.current = { path, method, body: JSON.stringify(body) }
    if (!pending.current) return
    setBusy(true)
    setNotice('')
    try {
      await request(pending.current.path, { method: pending.current.method, body: pending.current.body })
      pending.current = null
      setGoal('')
      onChanged()
      setNotice('准备条件已保存。请核对观察变化后再执行。')
    } catch (e) {
      setNotice(String(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <details className="experiment-preparation">
      <summary>实验准备：位置、察觉与目标</summary>
      <p>这些是主持人修改，会影响角色记忆与尚未提交的单步许可。建议先暂停剧情；不会替你自动恢复运行。</p>
      <fieldset disabled={busy || !!pending.current}>
        <label className="field">
          准备角色
          <select
            aria-label="准备角色"
            value={actor}
            onChange={(e) => {
              setActor(e.target.value)
              setNotice('')
              setGoal('')
            }}
          >
            {people.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name} · {p.location}
                {p.player ? '（玩家）' : ''}
              </option>
            ))}
          </select>
        </label>
        {person && (
          <>
            <label className="field">
              所在场景
              <input
                aria-label="准备场景"
                value={location}
                maxLength={100}
                onChange={(e) => setLocation(e.target.value)}
              />
            </label>
            <button
              disabled={!location.trim() || location.trim() === person.location}
              onClick={() =>
                void mutate(`/role/actors/${actor}/location`, 'PATCH', {
                  location,
                  expected_revision: person.revision,
                  request_id: crypto.randomUUID(),
                })
              }
            >
              保存角色位置
            </button>
            <p>位置变更会写入事件，并按已有感知规则通知相应角色。</p>
            <h4>此角色是否察觉其他角色</h4>
            <p>单向关系，设为未察觉不会撤销已有记忆；重新察觉也不会补发此前漏听的内容。</p>
            {people
              .filter((p) => p.id !== actor)
              .map((p) => (
                <label className="preparation-notice" key={p.id}>
                  <input
                    aria-label={`准备察觉 ${p.name}`}
                    type="checkbox"
                    checked={!person.unnoticed_ids?.includes(p.id)}
                    onChange={(e) =>
                      void mutate(`/role/actors/${actor}/awareness/${p.id}`, 'PATCH', {
                        noticed: e.target.checked,
                        expected_revision: person.revision,
                      })
                    }
                  />{' '}
                  察觉 {p.name} · {p.location}
                </label>
              ))}
            {!person.player && (
              <>
                <label className="field">
                  新增个人目标
                  <textarea
                    aria-label="准备目标"
                    value={goal}
                    maxLength={800}
                    onChange={(e) => setGoal(e.target.value)}
                  />
                </label>
                <button
                  disabled={!goal.trim()}
                  onClick={() =>
                    void mutate(`/role/actors/${actor}/memories`, 'POST', {
                      kind: 'goal',
                      content: goal,
                      request_id: crypto.randomUUID(),
                    })
                  }
                >
                  保存个人目标
                </button>
                <p>仅写入此角色；属于主持人设定，不是模型自行形成的目标。</p>
              </>
            )}
            <h4>个人目标</h4>
            {goals.map((item) => (
              <details key={item.id}>
                <summary>
                  {item.status} · {item.content}
                </summary>
                {['active', 'completed', 'archived']
                  .filter((status) => status !== item.status)
                  .map((status) => (
                    <button
                      key={status}
                      onClick={() =>
                        void mutate(`/role/actors/${actor}/memories/${item.id}`, 'PATCH', {
                          expected_version: item.version,
                          status,
                        })
                      }
                    >
                      {
                        (
                          { active: '恢复目标', completed: '标记完成', archived: '归档目标' } as Record<
                            string,
                            string
                          >
                        )[status]
                      }
                    </button>
                  ))}
              </details>
            ))}
            {more && <p>仅显示最近100条目标，更早条目可在个人记忆编辑器查询。</p>}
          </>
        )}
      </fieldset>
      {pending.current && (
        <>
          <button disabled={busy} onClick={() => void mutate()}>
            重试同一准备请求
          </button>
          <button
            disabled={busy}
            onClick={() => {
              pending.current = null
              onChanged()
              setNotice('已刷新准备条件，请先核对原请求是否已保存，再进行修改。')
            }}
          >
            刷新核对并解除输入锁定
          </button>
        </>
      )}
      <button disabled={busy || !!pending.current} onClick={onChanged}>
        刷新准备条件
      </button>
      {notice && <p role="status">{notice}</p>}
    </details>
  )
}
