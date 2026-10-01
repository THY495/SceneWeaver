import type { StageSlot } from './stageTransform'
import { objectZIndex, type SceneObject } from './sceneObjectModel'

export function compositionEntries(slots: StageSlot[], objects: SceneObject[]) {
  return [
    ...objects.map((o, i) => ({
      key: 'object:' + o.id,
      id: o.id,
      kind: 'object' as const,
      layer: o.layer,
      z: objectZIndex(o),
      tie: i,
    })),
    ...slots.map((s, i) => ({
      key: 'person:' + s.actor,
      id: s.actor,
      kind: 'person' as const,
      layer: 'entities' as const,
      z: 4000 + (s.draw_order ?? i),
      tie: objects.length + i,
    })),
  ].sort((a, b) => b.z - a.z || b.tie - a.tie)
}
// Reorder within a semantic layer; foreground/background remain intentional boundaries.
export function reorderComposition(slots: StageSlot[], objects: SceneObject[], from: string, to: string) {
  const entries = compositionEntries(slots, objects)
  const source = entries.find((e) => e.key === from),
    target = entries.find((e) => e.key === to)
  if (!source || !target || source.layer !== target.layer || from === to) return { slots, objects }
  const group = entries.filter((e) => e.layer === source.layer)
  const a = group.findIndex((e) => e.key === from),
    b = group.findIndex((e) => e.key === to)
  group.splice(a, 1)
  group.splice(b, 0, source)
  const ranks = new Map(group.map((e, i) => [e.key, group.length - i - 1]))
  return {
    slots: slots.map((s) =>
      ranks.has('person:' + s.actor) ? { ...s, draw_order: ranks.get('person:' + s.actor)! } : s,
    ),
    objects: objects.map((o) =>
      ranks.has('object:' + o.id) ? { ...o, order: ranks.get('object:' + o.id)! } : o,
    ),
  }
}
