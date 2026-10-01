import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const source = await readFile(new URL('../src/stageTransform.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
}).outputText
const { defaultTransform, moveTransform, transformStyle, dragTransform, moveStageSlot } = await import(
  'data:text/javascript;base64,' + Buffer.from(compiled).toString('base64')
)
const left = defaultTransform('left')
assert.equal(left.x, 320)
const moved = moveTransform(left, 160, -90)
assert.equal(left.x, 320, 'drag preview must not mutate saved transform')
assert.equal(moved.x, 480)
assert.equal(moved.y, 720)
assert.deepEqual(transformStyle(moved), {
  left: '30%',
  top: '80%',
  transform: 'translate(-50%, -100%) rotate(0deg) scale(1, 1)',
})
assert.equal(moveTransform(left, 99999, -99999).x, 3200)
assert.equal(moveTransform(left, 99999, -99999).y, -900)
console.log('stage transform tests passed')
const uneven = { ...left, scale_x: 0.8, scale_y: 1 }
for (const delta of [-100000, -100, 0, 100, 100000]) {
  const scaled = dragTransform(uneven, 'scale', 0, delta)
  assert.ok(Math.abs(scaled.scale_x / scaled.scale_y - 0.8) < 1e-10)
  assert.ok(scaled.scale_x >= 0.1 && scaled.scale_y <= 4)
}
assert.equal(dragTransform(left, 'rotate', 1000, 0).rotation, 180)
assert.equal(dragTransform(left, 'rotate', -1000, 0).rotation, -180)
assert.deepEqual(dragTransform(left, 'move', 160, -90), moved)
console.log('canvas move, uniform scale and rotation bounds passed')
const slots = [
  { actor: 'a', position: 'left', editor_hidden: true },
  { actor: 'b', position: 'right', editor_locked: true },
]
const reordered = moveStageSlot(slots, 'a', 1)
assert.deepEqual(
  reordered.map((s) => s.actor),
  ['b', 'a'],
)
assert.equal(reordered[1], slots[0], 'reorder preserves instance data and identity')
assert.deepEqual(
  slots.map((s) => s.actor),
  ['a', 'b'],
)
assert.equal(moveStageSlot(slots, 'a', -1), slots)
assert.equal(moveStageSlot(slots, 'absent', 1), slots)
console.log('stage ordering preserves identity and editor flags')
