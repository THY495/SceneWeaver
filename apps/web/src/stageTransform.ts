export type StageTransform = {
  version: 1
  x: number
  y: number
  scale_x: number
  scale_y: number
  rotation: number
}
export type StageSlot = {
  actor: string
  draw_order?: number | null
  outfit?: string | null
  variant?: string | null
  position: 'left' | 'center' | 'right'
  transform?: StageTransform | null
  editor_locked?: boolean
  editor_hidden?: boolean
}
// Back-to-front order. Reordering never clones or modifies a world entity.
export function moveStageSlot(slots: StageSlot[], actor: string, direction: -1 | 1): StageSlot[] {
  const index = slots.findIndex((s) => s.actor === actor)
  const target = index + direction
  if (index < 0 || target < 0 || target >= slots.length) return slots
  const next = [...slots]
  ;[next[index], next[target]] = [next[target], next[index]]
  return next
}
// Coordinates use a 1600 × 900 design frame; the origin of each sprite is its bottom centre.
export function defaultTransform(position: StageSlot['position'] = 'center'): StageTransform {
  return {
    version: 1,
    x: { left: 320, center: 800, right: 1280 }[position],
    y: 810,
    scale_x: 1,
    scale_y: 1,
    rotation: 0,
  }
}
export function moveTransform(t: StageTransform, dx: number, dy: number): StageTransform {
  return { ...t, x: Math.max(-1600, Math.min(3200, t.x + dx)), y: Math.max(-900, Math.min(1800, t.y + dy)) }
}
export function transformStyle(t: StageTransform) {
  return {
    left: `${t.x / 16}%`,
    top: `${t.y / 9}%`,
    transform: `translate(-50%, -100%) rotate(${t.rotation}deg) scale(${t.scale_x}, ${t.scale_y})`,
  }
}
export type TransformTool = 'move' | 'scale' | 'rotate'
export function dragTransform(
  t: StageTransform,
  tool: TransformTool,
  dx: number,
  dy: number,
): StageTransform {
  if (tool === 'move') return moveTransform(t, dx, dy)
  if (tool === 'rotate') return { ...t, rotation: Math.max(-180, Math.min(180, t.rotation + dx / 4)) }
  // Uniform scaling keeps the existing aspect ratio; clamp the factor, not each axis.
  const min = Math.max(0.1 / t.scale_x, 0.1 / t.scale_y)
  const max = Math.min(4 / t.scale_x, 4 / t.scale_y)
  const factor = Math.max(min, Math.min(max, Math.exp(-dy / 400)))
  return { ...t, scale_x: t.scale_x * factor, scale_y: t.scale_y * factor }
}
