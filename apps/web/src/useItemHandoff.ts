import { useEffect, useRef, useState } from 'react'
import { handoffPlan, sameHandoffPlan, type InventorySnapshot } from './itemHandoff'
import { itemError } from './itemFeedback'

const empty: InventorySnapshot = { enabled: false, items: [], recipients: [], transfers: [] }
export function useItemHandoff({
  actorId,
  request,
  open,
  blocked,
  onTask,
  supplied,
}: {
  actorId: string
  request: (path: string, init?: RequestInit) => Promise<unknown>
  open: boolean
  blocked: boolean
  onTask: (id: string) => void
  supplied: InventorySnapshot | null
}) {
  const [snapshot, setSnapshot] = useState(empty)
  const [selected, setSelected] = useState('')
  const [pending, setPending] = useState(false)
  const [message, setMessage] = useState('')
  const [loaded, setLoaded] = useState(false)
  const inFlight = useRef(false)
  const generation = useRef(0)
  const gated = useRef(blocked || !open)
  gated.current = blocked || !open
  const unavailable = !open || blocked || pending || !loaded
  useEffect(() => {
    ++generation.current
    setSelected('')
    setLoaded(false)
    setMessage('')
    setSnapshot(empty)
  }, [actorId, open])
  useEffect(() => {
    if (open && !inFlight.current) {
      setSnapshot(supplied || empty)
      setLoaded(Boolean(supplied))
    }
  }, [supplied, open])
  useEffect(() => {
    if (blocked) setSelected('')
  }, [blocked])
  const plan = (id: string) => handoffPlan(snapshot, actorId, selected, id)
  async function deliver(recipientId: string, itemId = selected) {
    if (unavailable || inFlight.current || !itemId) return
    const before = handoffPlan(snapshot, actorId, itemId, recipientId)
    if (before.kind === 'blocked') {
      setMessage(before.label)
      return
    }
    const item = snapshot.items.find((i) => i.id === itemId)!
    const current = generation.current
    inFlight.current = true
    setPending(true)
    setMessage('正在确认交接状态…')
    try {
      const fresh = (await request(`/role/items/actors/${actorId}`)) as InventorySnapshot
      if (current !== generation.current || gated.current) return
      setSnapshot(fresh)
      const after = handoffPlan(fresh, actorId, item.id, recipientId)
      if (
        fresh.items.find((i) => i.id === item.id)?.version !== item.version ||
        !sameHandoffPlan(before, after)
      )
        throw new Error('交接状态已变化，请重新选择物品和人物。')
      const body =
        before.kind === 'give'
          ? {
              request_id: crypto.randomUUID(),
              item_id: item.id,
              expected_revision: item.version,
              action: 'give',
              destination: { kind: 'actor', target: recipientId },
              agreement: {
                proposal_id: before.agreement.proposal_id,
                version: before.agreement.version,
                item_id: before.agreement.item_id,
              },
            }
          : {
              request_id: crypto.randomUUID(),
              item_id: item.id,
              expected_revision: item.version,
              recipient_id: recipientId,
            }
      const result = (await request(
        `/role/items/actors/${actorId}/${before.kind === 'give' ? 'actions' : 'offers'}`,
        {
          method: 'POST',
          body: JSON.stringify(body),
        },
      )) as { task_id?: string }
      if (current !== generation.current) return
      setSelected('')
      setLoaded(false)
      setMessage(before.kind === 'give' ? '物品已递交，等待人物回应。' : '已提出递交，物品仍由你持有。')
      if (result.task_id) onTask(result.task_id)
    } catch (e) {
      if (current === generation.current) {
        setMessage(itemError(e))
        setSelected('')
        setLoaded(false)
      }
    } finally {
      inFlight.current = false
      setPending(false)
    }
  }
  return { snapshot, selected, setSelected, pending, message, loaded, unavailable, plan, deliver }
}
