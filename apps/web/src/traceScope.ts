export type ScopedSpan = {
  id: string
  parent_id: string | null
  attributes: { actor_id?: string; step?: number }
}

// Walk only this execution's parent chain. Missing ancestry stays unknown.
export function traceScope(spans: ScopedSpan[], id: string | undefined) {
  return traceScopeResolver(spans)(id)
}

export function traceScopeResolver(spans: ScopedSpan[]) {
  const byId = new Map(spans.map((span) => [span.id, span]))
  return (id: string | undefined) => {
    const seen = new Set<string>()
    let actor: string | undefined, step: number | undefined
    while (id && !seen.has(id)) {
      seen.add(id)
      const span = byId.get(id)
      if (!span) break
      actor ??= span.attributes.actor_id
      step ??= span.attributes.step
      id = span.parent_id || undefined
    }
    return { actor, step }
  }
}

export function inTraceScope(spans: ScopedSpan[], id: string | undefined, actor: string, step: string) {
  const scope = traceScope(spans, id)
  return (!actor || scope.actor === actor) && (!step || String(scope.step) === step)
}
