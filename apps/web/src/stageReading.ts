export type VisibleEvent = {
  presentation_pending?: boolean
  acknowledgement?: boolean
  id: string
  seq: number
  kind: string
  speaker: string
  actor_id?: string
  text: string
  performance?: {
    text: string
    expression?: string
    pose?: string
    appearance?: { sprite?: string | null; outfit?: string | null; variant?: string | null }
  }[]
  appearance?: { sprite?: string | null; outfit?: string | null; variant?: string | null }
  stage?: { mode: 'narration'; text: string } | { mode: 'redundant'; with_event_id: string }
}
export type ReadingLine = {
  presentationPending?: boolean
  id: string
  speaker: string
  actorId?: string
  text: string
  performance?: { expression?: string; pose?: string }
  appearance?: VisibleEvent['appearance']
}
export function speakingFocus(
  line: ReadingLine | undefined,
  visibleIds: string[],
  audio: { actorId?: string; active: boolean } | null,
) {
  const id = audio?.active ? audio.actorId : line?.actorId
  return id && visibleIds.includes(id) ? id : null
}
export function holdingPlayerTurn(input: string, busy: boolean, readingOrPanel: boolean) {
  return Boolean(input.trim()) || (!busy && readingOrPanel)
}
export function firstReplyLine(events: VisibleEvent[], after: number, player: string) {
  return readingLines(
    events.filter(
      (event) =>
        event.seq > after &&
        event.kind === 'speech' &&
        !event.acknowledgement &&
        event.actor_id &&
        event.actor_id !== player,
    ),
  )[0]
}
export function readingLines(events: VisibleEvent[]): ReadingLine[] {
  const narrated = new Set(events.filter((e) => e.stage?.mode === 'narration').map((e) => e.id))
  return (
    events
      .filter((e) => ['speech', 'host'].includes(e.kind))
      // Quick acknowledgements are audible, transient feedback. They remain
      // canonical speech records but do not become a second gated story page.
      .filter((e) => !e.acknowledgement)
      .filter((e) => e.stage?.mode !== 'redundant' || !narrated.has(e.stage.with_event_id))
      .flatMap((e) => {
        if (e.presentation_pending)
          return [
            {
              id: `${e.id}:0`,
              speaker: e.speaker,
              actorId: e.actor_id,
              text: '',
              presentationPending: true,
            },
          ]
        const spoken = e.kind === 'speech' && e.stage?.mode !== 'narration'
        const text = e.stage?.mode === 'narration' ? e.stage.text : e.text
        const parts = Array.isArray(e.performance) ? e.performance : []
        const normalized = (v: string) => v.replace(/\s/g, '')
        const validParts =
          spoken && parts.length && normalized(parts.map((p) => p.text).join('')) === normalized(text)
        const segments = validParts
          ? parts.map((p) => p.text.trim())
          : text
              .split(/\n\s*\n/)
              .map((t) => t.trim())
              .filter(Boolean)
        return segments.map((text, i) => {
          const part = validParts ? parts[i] : undefined
          return {
            id: `${e.id}:${i}`,
            speaker: spoken ? e.speaker || '未辨认的声音' : '旁白',
            actorId: spoken ? e.actor_id : undefined,
            ...(spoken && e.actor_id
              ? { performance: part, appearance: part?.appearance || e.appearance }
              : {}),
            text,
          }
        })
      })
  )
}
