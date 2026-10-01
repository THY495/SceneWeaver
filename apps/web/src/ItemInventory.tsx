import { useEffect, useRef, useState } from 'react'
import { itemError } from './itemFeedback'
export type InventoryItem = {
  id: string
  name: string
  version: number
  holder_id: string | null
  location: string
  container_id: string | null
  is_container: boolean
  open: boolean | null
}
type Request = (path: string, init?: RequestInit) => Promise<unknown>
export type InventoryTransfer = {
  proposal_id: string
  version: number
  item_id: string
  object_id: string
  expected_revision: number
  sender_id: string
  recipient_id: string
  description: string
  state: string
}
export type DetailedInventory = {
  enabled: boolean
  items: InventoryItem[]
  recipients?: { id: string; name: string }[]
  transfers?: InventoryTransfer[]
}
export function ItemInventory({
  actorId,
  request,
  onTask,
  disabledReason = '',
  supplied,
  onRefresh,
}: {
  actorId: string
  request: Request
  onTask?: (id: string) => void
  disabledReason?: string
  supplied?: DetailedInventory | null
  onRefresh?: () => Promise<void>
}) {
  const [local, setLocal] = useState<DetailedInventory | null>(null)
  const [recipient, setRecipient] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [targets, setTargets] = useState<Record<string, string>>({})
  const inFlight = useRef(false)
  const controlled = supplied !== undefined
  const inventory = controlled ? supplied : local
  const { items = [], recipients = [], transfers = [], enabled = false } = inventory || {}
  useEffect(() => {
    setRecipient('')
    setTargets({})
    setError('')
    setStatus('')
    if (controlled) return
    let live = true
    setLocal(null)
    const refresh = () =>
      request(`/role/items/actors/${actorId}`)
        .then((v) => {
          if (live) {
            setLocal(v as DetailedInventory)
            setError('')
          }
        })
        .catch((e) => {
          if (live) setError(itemError(e))
        })
    void refresh()
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible') void refresh()
    }, 8000)
    return () => {
      live = false
      clearInterval(timer)
    }
  }, [actorId, request, controlled])
  async function refreshInventory() {
    if (controlled) await onRefresh?.()
    else setLocal((await request(`/role/items/actors/${actorId}`)) as DetailedInventory)
  }
  async function perform(send: () => Promise<{ task_id?: string } | unknown>, success: string) {
    if (inFlight.current || disabledReason || !inventory) return
    inFlight.current = true
    setBusy(true)
    setError('')
    setStatus('')
    let taskId: string | undefined
    try {
      const result = (await send()) as { task_id?: string }
      taskId = result?.task_id
      setStatus(success)
    } catch (e) {
      setError(itemError(e))
    }
    try {
      await refreshInventory()
    } catch {
      if (!controlled) setLocal(null)
      setError((previous) => [previous, '状态刷新失败，请刷新确认后再操作。'].filter(Boolean).join(' '))
    } finally {
      inFlight.current = false
      setBusy(false)
    }
    if (taskId) onTask?.(taskId)
  }
  async function offer(item: InventoryItem) {
    await perform(
      () =>
        request(`/role/items/actors/${actorId}/offers`, {
          method: 'POST',
          body: JSON.stringify({
            request_id: crypto.randomUUID(),
            item_id: item.id,
            expected_revision: item.version,
            recipient_id: recipient,
          }),
        }),
      '已提出递交，物品仍由你持有。',
    )
  }
  async function transfer(t: InventoryTransfer, decision: 'accept' | 'refuse' | 'withdraw' | 'give') {
    await perform(
      () =>
        decision === 'give'
          ? request(`/role/items/actors/${actorId}/actions`, {
              method: 'POST',
              body: JSON.stringify({
                request_id: crypto.randomUUID(),
                item_id: t.object_id,
                expected_revision: t.expected_revision,
                action: 'give',
                destination: { kind: 'actor', target: t.recipient_id },
                agreement: { proposal_id: t.proposal_id, version: t.version, item_id: t.item_id },
              }),
            })
          : request(`/role/actors/${actorId}/agreements/${t.proposal_id}/decisions`, {
              method: 'POST',
              body: JSON.stringify({
                request_id: crypto.randomUUID(),
                expected_version: t.version,
                item_ids: [t.item_id],
                decision,
              }),
            }),
      {
        give: '物品已递交。',
        accept: '已同意接收，等待实际递交。',
        refuse: '已拒绝接收。',
        withdraw: '已撤回同意。',
      }[decision],
    )
  }
  async function action(
    item: InventoryItem,
    action: 'move' | 'open' | 'close',
    destination?: { kind: string; target: string },
  ) {
    await perform(
      () =>
        request(`/role/items/actors/${actorId}/actions`, {
          method: 'POST',
          body: JSON.stringify({
            request_id: crypto.randomUUID(),
            item_id: item.id,
            expected_revision: item.version,
            action,
            ...(destination ? { destination } : {}),
          }),
        }),
      action === 'open' ? '已打开容器。' : action === 'close' ? '已关闭容器。' : '物品位置已更新。',
    )
  }
  if (!inventory && !error) return <p role="status">正在读取物品状态…</p>
  if (!enabled && !error) return <p>本故事未启用物品互动。</p>
  return (
    <section aria-label="随身与场景物品">
      <h3>随身与场景物品</h3>
      {disabledReason && <p role="status">{disabledReason}</p>}
      <fieldset disabled={busy || !inventory || Boolean(disabledReason)} className="vn-item-controls">
        <p>显示当前看得见的物品。打开容器可能发现内容；能看见不代表能取出。查看列表不会推进剧情。</p>
        <label>
          递交给
          <select aria-label="递交接收者" value={recipient} onChange={(e) => setRecipient(e.target.value)}>
            <option value="">选择在场人物</option>
            {recipients.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <p>提出递交会请求人物作出决定，并调用模型。对方同意后，你可以完成递交；在此之前物品仍由你持有。</p>
        {items.length === 0 && <p>当前没有可见物品。</p>}
        {items.map((item) => (
          <fieldset key={item.id}>
            <legend>{item.name}</legend>
            <p>
              {item.holder_id === actorId ? '随身' : item.holder_id ? '由其他角色持有' : item.location}
              {item.container_id
                ? ` · 位于${items.find((x) => x.id === item.container_id)?.name || '容器'}内`
                : ''}
            </p>
            {item.holder_id === actorId && (
              <button
                disabled={busy || !recipient || !recipients.some((p) => p.id === recipient)}
                onClick={() => void offer(item)}
              >
                提出递交
              </button>
            )}
            {(!item.holder_id || item.holder_id === actorId) && (
              <>
                {(item.holder_id !== actorId || item.container_id) && (
                  <button
                    disabled={busy}
                    onClick={() => void action(item, 'move', { kind: 'actor', target: actorId })}
                  >
                    拿取
                  </button>
                )}
                <button
                  disabled={busy || (!item.holder_id && !item.container_id)}
                  onClick={() => void action(item, 'move', { kind: 'location', target: item.location })}
                >
                  放在此处
                </button>
                {item.is_container && (
                  <button disabled={busy} onClick={() => void action(item, item.open ? 'close' : 'open')}>
                    {item.open ? '关闭' : '打开'}
                  </button>
                )}
                <label className="field">
                  放入容器
                  <select
                    value={targets[item.id] || ''}
                    onChange={(e) => setTargets({ ...targets, [item.id]: e.target.value })}
                  >
                    <option value="">选择可见容器</option>
                    {items
                      .filter((x) => x.id !== item.id && x.is_container && x.open)
                      .map((x) => (
                        <option key={x.id} value={x.id}>
                          {x.name}
                        </option>
                      ))}
                  </select>
                </label>
                <button
                  disabled={busy || !targets[item.id]}
                  onClick={() => void action(item, 'move', { kind: 'container', target: targets[item.id] })}
                >
                  放入
                </button>
              </>
            )}
          </fieldset>
        ))}
        {transfers.length > 0 && (
          <section aria-label="交接事项">
            <h4>交接事项</h4>
            {transfers.map((t) => (
              <fieldset key={`${t.proposal_id}:${t.item_id}`}>
                <legend>{t.description}</legend>
                <p>
                  {(
                    {
                      wait_reply: '等待决定',
                      ready: '双方已同意，可以递交',
                      success: '已完成',
                      refused: '已拒绝',
                      cancelled: '已撤回',
                      wait_conditions: '等待条件',
                      failed: '执行失败',
                      expired: '物品已变化，请重新提出交接',
                    } as Record<string, string>
                  )[t.state] || t.state}
                </p>
                {t.state !== 'success' && (
                  <>
                    {t.sender_id === actorId && t.state === 'ready' && (
                      <button disabled={busy} onClick={() => void transfer(t, 'give')}>
                        完成递交
                      </button>
                    )}
                    {t.recipient_id === actorId && t.state === 'wait_reply' && (
                      <>
                        <button disabled={busy} onClick={() => void transfer(t, 'accept')}>
                          同意接收
                        </button>
                        <button disabled={busy} onClick={() => void transfer(t, 'refuse')}>
                          拒绝接收
                        </button>
                      </>
                    )}
                    {(t.sender_id === actorId || t.recipient_id === actorId) &&
                      !['cancelled', 'refused'].includes(t.state) && (
                        <button disabled={busy} onClick={() => void transfer(t, 'withdraw')}>
                          撤回同意
                        </button>
                      )}
                  </>
                )}
              </fieldset>
            ))}
          </section>
        )}
      </fieldset>
      {status && <p role="status">{status}</p>}
      {error && <p role="alert">{error}</p>}
      <button
        disabled={busy}
        onClick={() => {
          setError('')
          void refreshInventory().catch((e) => setError(itemError(e)))
        }}
      >
        刷新物品状态
      </button>
    </section>
  )
}
