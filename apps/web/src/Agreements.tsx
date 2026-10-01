import { useEffect, useState } from 'react'

type Item = { id: string; name: string; version: number; holder_id: string | null }
type Person = { id: string; name: string; player: boolean; location: string }
type Step = {
  id: string
  description: string
  executor_id: string
  action: { object_id: string; verb: string; target_id: string | null }
  depends_on: string[]
}
type Proposal = {
  proposal_id: string
  version: number
  initiator_id: string
  items: Step[]
  decisions: Record<string, Record<string, { decision: string }>>
  completed: Record<string, string>
  item_states?: Record<string, { state: string }>
}
type Props = {
  story: string
  actorId: string
  creator: boolean
  request: (path: string, init?: RequestInit) => Promise<unknown>
}

export function Agreements({ story, actorId, creator, request }: Props) {
  const [proposals, setProposals] = useState<Proposal[]>([]),
    [items, setItems] = useState<Item[]>([]),
    [people, setPeople] = useState<Person[]>([])
  const [objectId, setObjectId] = useState(''),
    [target, setTarget] = useState(''),
    [notice, setNotice] = useState(''),
    [busy, setBusy] = useState(false)
  async function load() {
    const query = `?actor_id=${encodeURIComponent(actorId)}`
    const [a, b, c] = await Promise.all([
      request(`/role/stories/${story}/agreements${query}`),
      request(`/role/stories/${story}/objects${query}`),
      request(`/role/stories/${story}/roster${query}`),
    ])
    setProposals(a as Proposal[])
    setItems(b as Item[])
    setPeople(c as Person[])
  }
  useEffect(() => {
    setProposals([])
    setItems([])
    setPeople([])
    setNotice('')
    setObjectId('')
    setTarget('')
    if (!actorId) return
    let active = true
    const refresh = () => {
      if (active && document.visibilityState === 'visible')
        load().catch((e) => {
          if (active) setNotice(String(e))
        })
    }
    const notified = (event: Event) => {
      if ((event as CustomEvent).detail.sid === actorId) refresh()
    }
    window.addEventListener('agreement-updated', notified)
    refresh()
    const timer = setInterval(refresh, 8000)
    return () => {
      active = false
      clearInterval(timer)
      window.removeEventListener('agreement-updated', notified)
    }
  }, [story, actorId])
  async function run(work: () => Promise<unknown>) {
    setBusy(true)
    setNotice('')
    try {
      await work()
      await load()
    } catch (e) {
      setNotice(String(e))
    } finally {
      setBusy(false)
    }
  }
  const post = (path: string, body: unknown) => request(path, { method: 'POST', body: JSON.stringify(body) })
  const actor = people.find((p) => p.id === actorId),
    editable = creator || actor?.player
  async function propose() {
    const timeline = (await request(
      `/role/stories/${story}/timeline?actor_id=${encodeURIComponent(actorId)}`,
    )) as { events: { id: string; kind: string; actor_id: string }[] }
    const source = timeline.events.find((e) => e.kind === 'speech' && e.actor_id === actorId)
    const item = items.find((i) => i.id === objectId)
    if (!source || !item) throw new Error('请先以此角色发言，并选择自己持有的物品。')
    return post(`/role/actors/${actorId}/agreements`, {
      request_id: crypto.randomUUID(),
      source_event_id: source.id,
      items: [
        {
          key: 'handover',
          executor_id: actorId,
          action: { object_id: objectId, expected_version: item.version, verb: 'give', target_id: target },
        },
      ],
    })
  }
  if (!actorId) return null
  return (
    <details>
      <summary>行动提案与明确同意</summary>
      <p className="muted">
        仅显示此角色实际获知的版本和回应。普通台词中的“好”不会自动授权交接。
        {creator ? '以下按钮属于创作控制。' : ''}
      </p>
      {editable && (
        <fieldset>
          <legend>提出交接提案</legend>
          <label className="field">
            自己持有的物品
            <select value={objectId} onChange={(e) => setObjectId(e.target.value)}>
              <option value="">选择物品</option>
              {items
                .filter((i) => i.holder_id === actorId)
                .map((i) => (
                  <option key={i.id} value={i.id}>
                    {i.name}
                  </option>
                ))}
            </select>
          </label>
          <label className="field">
            接收者
            <select value={target} onChange={(e) => setTarget(e.target.value)}>
              <option value="">选择同场 NPC</option>
              {people
                .filter((p) => p.id !== actorId && !p.player && p.location === actor?.location)
                .map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
            </select>
          </label>
          <button disabled={busy || !objectId || !target} onClick={() => run(propose)}>
            提出提案
          </button>
        </fieldset>
      )}
      {proposals.slice(-20).map((p) => (
        <fieldset key={p.proposal_id}>
          <legend>提案第 {p.version} 版</legend>
          {p.items.map((step) => {
            const own = p.decisions[step.id]?.[actorId]?.decision,
              done = !!p.completed[step.id]
            return (
              <div key={step.id}>
                <p>
                  {step.description} ·{' '}
                  {done
                    ? '已观察到完成'
                    : (
                        { accept: '我已同意', refuse: '我已拒绝', withdraw: '我已撤回同意' } as Record<
                          string,
                          string
                        >
                      )[own] || '我尚未明确同意'}
                </p>
                <p className="muted">
                  事项：
                  {(
                    {
                      success: '完成',
                      failed: '上次尝试未完成',
                      refused: '已拒绝',
                      cancelled: '同意已撤回',
                      wait_reply: '等待明确回应',
                      wait_conditions: '等待条件核对',
                      ready: '可尝试执行，现场仍需检查',
                    } as Record<string, string>
                  )[p.item_states?.[step.id]?.state || ''] || '待核对'}
                </p>
                {Object.entries(p.decisions[step.id] || {})
                  .filter(([aid]) => aid !== actorId)
                  .map(([aid, d]) => (
                    <p className="muted" key={aid}>
                      {people.find((x) => x.id === aid)?.name || '已知参与者'}：
                      {
                        ({ accept: '同意', refuse: '拒绝', withdraw: '撤回同意' } as Record<string, string>)[
                          d.decision
                        ]
                      }
                    </p>
                  ))}
                {editable &&
                  !done &&
                  ['accept', 'refuse', 'withdraw'].map((decision) => (
                    <button
                      key={decision}
                      disabled={busy || own === decision}
                      onClick={() =>
                        run(() =>
                          post(`/role/actors/${actorId}/agreements/${p.proposal_id}/decisions`, {
                            request_id: crypto.randomUUID(),
                            expected_version: p.version,
                            item_ids: [step.id],
                            decision,
                          }),
                        )
                      }
                    >
                      {
                        (
                          { accept: '同意此项', refuse: '拒绝此项', withdraw: '撤回同意' } as Record<
                            string,
                            string
                          >
                        )[decision]
                      }
                    </button>
                  ))}
                {actor?.player && step.executor_id === actorId && !done && (
                  <button
                    disabled={busy}
                    onClick={() =>
                      run(() => {
                        const item = items.find((i) => i.id === step.action.object_id)
                        if (!item) throw new Error('当前没有此物品的观察。')
                        return post(`/role/actors/${actorId}/object-actions`, {
                          request_id: crypto.randomUUID(),
                          ...step.action,
                          expected_version: item.version,
                          agreement: { proposal_id: p.proposal_id, version: p.version, item_id: step.id },
                        })
                      })
                    }
                  >
                    尝试执行此项
                  </button>
                )}
              </div>
            )
          })}
        </fieldset>
      ))}
      {proposals.length === 0 && <p>暂无已知提案。</p>}
      {notice && <p role="status">{notice}</p>}
    </details>
  )
}
