import { useEffect, useState } from 'react'
import { Agreements } from './Agreements'

type Person = { id: string; name: string; player: boolean; location: string }
type Item = {
  id: string
  name: string
  holder_id: string | null
  location: string
  version: number
  protocol?: number
}
type Exchange = { question_id: string; question: string; target_id: string; asker_id: string; status: string }
type Props = {
  story: string
  actorId: string
  creator?: boolean
  request: (path: string, init?: RequestInit) => Promise<unknown>
}
const labels: Record<string, string> = {
  partial: '部分完成',
  cancelled: '已撤回',
  open: '待回应',
  answer: '已回应',
  refuse: '明确拒答',
  defer: '暂缓',
  silence: '明确沉默',
}

export function SceneObjects({ story, actorId, creator = false, request }: Props) {
  const [items, setItems] = useState<Item[]>([]),
    [people, setPeople] = useState<Person[]>([]),
    [exchanges, setExchanges] = useState<Exchange[]>([])
  const [name, setName] = useState(''),
    [holder, setHolder] = useState(''),
    [location, setLocation] = useState(''),
    [target, setTarget] = useState('')
  const [busy, setBusy] = useState(false),
    [notice, setNotice] = useState('')
  async function load() {
    const [a, b, c] = await Promise.all([
      request(`/role/stories/${story}/objects${actorId ? '?actor_id=' + encodeURIComponent(actorId) : ''}`),
      request(
        `/role/stories/${story}/roster${actorId && !creator ? '?actor_id=' + encodeURIComponent(actorId) : ''}`,
      ),
      actorId
        ? request(`/role/stories/${story}/exchanges?actor_id=${encodeURIComponent(actorId)}`)
        : Promise.resolve([]),
    ])
    setItems((a as Item[]).filter((item) => item.protocol !== 2))
    setPeople(b as Person[])
    setLocation(
      (old) =>
        old || (b as Person[]).find((p) => p.player)?.location || (b as Person[])[0]?.location || 'main',
    )
    setExchanges(c as Exchange[])
  }
  useEffect(() => {
    let active = true
    setItems([])
    setExchanges([])
    setNotice('')
    const refresh = () => {
      if (active && document.visibilityState === 'visible')
        load().catch((e) => {
          if (active) setNotice(String(e))
        })
    }
    refresh()
    const timer = setInterval(refresh, 8000)
    return () => {
      active = false
      clearInterval(timer)
    }
  }, [story, actorId])
  async function mutate(path: string, body: unknown) {
    setBusy(true)
    setNotice('')
    try {
      await request(path, { method: 'POST', body: JSON.stringify(body) })
      await load()
    } catch (e) {
      setNotice(String(e))
    } finally {
      setBusy(false)
    }
  }
  const actor = people.find((p) => p.id === actorId),
    present = people.filter((p) => !p.player && p.location === actor?.location)
  return (
    <>
      <Agreements story={story} actorId={actorId} creator={creator} request={request} />
      <details>
        <summary>关键物品与待回应事项</summary>
        <p className="muted">
          {creator && !actorId
            ? '创作者查看全部已登记物品。'
            : '显示此角色最后观察到的物品状态；未观察期间可能已有变化。'}
        </p>
        <button onClick={() => load().catch((e) => setNotice(String(e)))}>刷新物品与回应</button>
        {actorId && (
          <button
            disabled={busy}
            onClick={() => mutate(`/role/actors/${actorId}/observe`, { request_id: crypto.randomUUID() })}
          >
            观察当前场景物品
          </button>
        )}
        {creator && (
          <fieldset>
            <legend>登记关键物品</legend>
            <label className="field">
              物品名称
              <input value={name} maxLength={100} onChange={(e) => setName(e.target.value)} />
            </label>
            <label className="field">
              初始持有人
              <select value={holder} onChange={(e) => setHolder(e.target.value)}>
                <option value="">放在场景中</option>
                {people.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
            {!holder && (
              <label className="field">
                初始场景
                <input
                  list={`object-locations-${story}`}
                  value={location}
                  maxLength={100}
                  onChange={(e) => setLocation(e.target.value)}
                />
                <datalist id={`object-locations-${story}`}>
                  {[...new Set(people.map((p) => p.location))].map((place) => (
                    <option key={place} value={place} />
                  ))}
                </datalist>
              </label>
            )}
            <button
              disabled={busy || !name.trim() || !location.trim()}
              onClick={() =>
                mutate(`/role/stories/${story}/objects`, {
                  request_id: crypto.randomUUID(),
                  name,
                  holder_id: holder || null,
                  location,
                })
              }
            >
              登记物品
            </button>
          </fieldset>
        )}
        {actor?.player && (
          <label className="field">
            交给在场NPC
            <select value={target} onChange={(e) => setTarget(e.target.value)}>
              <option value="">选择对象</option>
              {present.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
        )}
        {items.length === 0 && <p>暂无已知关键物品。</p>}
        {items.map((item) => (
          <div key={item.id}>
            <strong>{item.name}</strong> ·{' '}
            {item.holder_id
              ? people.find((p) => p.id === item.holder_id)?.name || '已知持有人'
              : item.location}
            {actor?.player && (
              <span>
                {(item.holder_id === actorId
                  ? ['put', 'give']
                  : !item.holder_id && item.location === actor.location
                    ? ['take']
                    : []
                ).map((verb) => (
                  <button
                    key={verb}
                    disabled={busy || (verb === 'give' && !present.some((p) => p.id === target))}
                    onClick={() =>
                      mutate(`/role/actors/${actorId}/object-actions`, {
                        request_id: crypto.randomUUID(),
                        object_id: item.id,
                        expected_version: item.version,
                        verb,
                        target_id: verb === 'give' ? target : null,
                      })
                    }
                  >
                    {({ take: '拿起', put: '放下', give: '交给NPC' } as Record<string, string>)[verb]}
                  </button>
                ))}
              </span>
            )}
          </div>
        ))}
        {exchanges.slice(-30).map((item) => (
          <p key={item.question_id}>
            <strong>{labels[item.status] || item.status}</strong> ·{' '}
            {people.find((p) => p.id === item.target_id)?.name}：{item.question}
            {item.asker_id === actorId && ['open', 'partial', 'defer', 'cancelled'].includes(item.status) && (
              <button
                disabled={busy}
                onClick={() =>
                  mutate(`/role/actors/${actorId}/exchanges/${item.question_id}`, {
                    request_id: crypto.randomUUID(),
                    status: item.status === 'cancelled' ? 'open' : 'cancelled',
                  })
                }
              >
                {item.status === 'cancelled' ? '重新提出' : '撤回请求'}
              </button>
            )}
          </p>
        ))}
        {notice && <p role="status">{notice}</p>}
      </details>
    </>
  )
}
