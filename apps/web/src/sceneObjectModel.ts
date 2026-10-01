import type { StageTransform } from './stageTransform'
export type SceneObject = {
  version: 1
  id: string
  kind: 'decoration' | 'item'
  name: string
  item?: string | null
  item_id?: string
  sprite: string
  transform: StageTransform
  layer: 'decoration' | 'entities' | 'foreground'
  order: number
  editor_hidden?: boolean
  editor_locked?: boolean
}
export function objectZIndex(o: SceneObject) {
  return { decoration: 0, entities: 3000, foreground: 6000 }[o.layer] + o.order + 1000
}
