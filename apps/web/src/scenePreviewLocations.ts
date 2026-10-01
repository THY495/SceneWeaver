import type { Narrative } from './NarrativeEditor'

type Actor = { alias: string; location?: string }
type Locations = Map<string, string | undefined>

/** Preview authored arrivals along a DAG. A merge with different locations stays unknown. */
export function scenePreviewLocations(graph: Narrative, actors: Actor[], selected: string) {
  const initial: Locations = new Map(actors.map((actor) => [actor.alias, actor.location]))
  const nodes = new Map(graph.nodes.map((node) => [node.id, node]))
  const reachable = new Set<string>([graph.entry])
  let changed = true
  while (changed) {
    changed = false
    for (const edge of graph.edges)
      if (reachable.has(edge.source) && nodes.has(edge.target) && !reachable.has(edge.target)) {
        reachable.add(edge.target)
        changed = true
      }
  }
  const pending = new Set(reachable)
  const incoming = new Map<string, Locations[]>([[graph.entry, [initial]]])
  const states = new Map<string, Locations>()
  const ambiguous = new Map<string, string[]>()
  while (pending.size) {
    let advanced = false
    for (const id of pending) {
      if (
        id !== graph.entry &&
        graph.edges.some(
          (edge) => edge.target === id && reachable.has(edge.source) && pending.has(edge.source),
        )
      )
        continue
      const paths = incoming.get(id) || [initial]
      const state: Locations = new Map()
      const uncertain: string[] = []
      for (const actor of actors) {
        const choices = paths.map((path) => path.get(actor.alias))
        const agreed = choices.every((place) => place === choices[0])
        state.set(actor.alias, agreed ? choices[0] : undefined)
        if (!agreed || (choices[0] === undefined && initial.get(actor.alias) !== undefined))
          uncertain.push(actor.alias)
      }
      for (const entry of nodes.get(id)?.entry_locations || []) {
        state.set(entry.actor, entry.location)
        const index = uncertain.indexOf(entry.actor)
        if (index >= 0) uncertain.splice(index, 1)
      }
      states.set(id, state)
      ambiguous.set(id, uncertain)
      pending.delete(id)
      advanced = true
      for (const edge of graph.edges.filter((edge) => edge.source === id && reachable.has(edge.target))) {
        const queued = incoming.get(edge.target) || []
        queued.push(state)
        incoming.set(edge.target, queued)
      }
    }
    if (!advanced) break
  }
  const fallback = new Map(initial)
  for (const entry of nodes.get(selected)?.entry_locations || []) fallback.set(entry.actor, entry.location)
  return { locations: states.get(selected) || fallback, ambiguous: ambiguous.get(selected) || [] }
}
