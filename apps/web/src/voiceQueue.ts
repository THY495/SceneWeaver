type QueueUtterance = {
  id: string
  task_id: string
  seq: number
  epoch: number
  acknowledgement?: boolean
  text_fallback?: boolean
}

export function shouldQueueUtterance(
  row: QueueUtterance,
  state: { epoch: number; seen: number; submittedTask: string | null; currentEventId?: string },
) {
  if (row.epoch !== state.epoch || row.text_fallback) return false
  // Unspoken candidates have no world sequence. Only the input just accepted
  // by this client may auto-play one; idempotency is handled by the job set.
  if (row.acknowledgement) return row.task_id === state.submittedTask && state.submittedTask !== null
  return state.currentEventId !== undefined ? row.id === state.currentEventId : row.seq > state.seen
}
