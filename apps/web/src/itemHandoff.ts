export type HeldItem = { id: string; name: string; version: number; holder_id: string | null }
export type Handoff = {
  proposal_id: string
  version: number
  item_id: string
  object_id: string
  expected_revision: number
  sender_id: string
  recipient_id: string
  state: string
}
export type InventorySnapshot = {
  enabled: boolean
  items: HeldItem[]
  recipients: { id: string; name: string }[]
  transfers: Handoff[]
}
export type HandoffPlan =
  | { kind: 'blocked'; label: string }
  | { kind: 'offer'; label: string }
  | { kind: 'give'; label: string; agreement: Handoff }

export function handoffPlan(
  snapshot: InventorySnapshot,
  actorId: string,
  itemId: string,
  recipientId: string,
): HandoffPlan {
  const item = snapshot.items.find((i) => i.id === itemId)
  const recipient = snapshot.recipients.find((p) => p.id === recipientId && p.id !== actorId)
  if (!snapshot.enabled || !item || item.holder_id !== actorId || !recipient)
    return { kind: 'blocked', label: '当前无法递交' }
  const relevant = snapshot.transfers.filter(
    (t) =>
      t.object_id === item.id &&
      t.sender_id === actorId &&
      t.recipient_id === recipientId &&
      t.expected_revision === item.version,
  )
  const ready = relevant.filter((t) => t.state === 'ready')
  if (ready.length > 1) return { kind: 'blocked', label: '有多项同意，请在物品详情中选择' }
  if (ready.length === 1)
    return { kind: 'give', label: `递交${item.name}给${recipient.name}`, agreement: ready[0] }
  if (relevant.some((t) => ['wait_reply', 'wait_conditions', 'failed'].includes(t.state)))
    return { kind: 'blocked', label: '已有交接事项，请等待或查看物品详情' }
  return { kind: 'offer', label: `向${recipient.name}提出递交${item.name}` }
}

// A refreshed consent must never silently turn an offer gesture into a physical handoff.
export function sameHandoffPlan(before: HandoffPlan, after: HandoffPlan): boolean {
  if (before.kind === 'blocked' || after.kind === 'blocked' || before.kind !== after.kind) return false
  if (before.kind === 'give' && after.kind === 'give') {
    const a = before.agreement,
      b = after.agreement
    return (
      a.proposal_id === b.proposal_id &&
      a.version === b.version &&
      a.item_id === b.item_id &&
      a.expected_revision === b.expected_revision
    )
  }
  return true
}
