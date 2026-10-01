import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const compile = (source) =>
  ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
  }).outputText
const uri = (source) => 'data:text/javascript;base64,' + Buffer.from(compile(source)).toString('base64')
const scope = uri(await readFile(new URL('../src/traceScope.ts', import.meta.url), 'utf8'))
const source = (await readFile(new URL('../src/performanceTimeline.ts', import.meta.url), 'utf8')).replace(
  "'./traceScope'",
  JSON.stringify(scope),
)
const { performanceLanes } = await import(uri(source))
const span = (id, parent_id, name, start_ms, attributes = {}) => ({
  id,
  parent_id,
  name,
  start_ms,
  duration_ms: 800,
  status: 'ok',
  attributes,
})
const spans = [
  span('a', null, 'npc.perform', 0, { actor_id: 'a' }),
  span('b', null, 'npc.perform', 0, { actor_id: 'b' }),
  span('tts-a', 'a', 'voice.candidate_prepare', 100),
  span('review-a', 'a', 'model.call', 90, { role: 'actor_review' }),
  span('review-b', 'b', 'model.call', 90, { role: 'actor_review' }),
  span('http', 'review-a', 'model.http', 95),
  span('dir', null, 'model.call', 0, { role: 'director' }),
  span('orphan', 'missing', 'model.call', 200),
]
const lanes = performanceLanes(spans)
assert.deepEqual(
  lanes.find((l) => l.owner === 'a').entries.map((e) => e.span.id),
  ['review-a', 'tts-a'],
)
assert.equal(lanes.find((l) => l.owner === 'b').entries[0].span.id, 'review-b')
assert.equal(lanes.find((l) => l.owner === 'director').entries[0].span.id, 'dir')
assert.equal(lanes.find((l) => l.owner === 'shared').entries[0].span.id, 'orphan')
assert.equal(lanes.flatMap((l) => l.entries).length, 5)
assert.equal(lanes.find((l) => l.owner === 'a').entries[1].span.start_ms, 100)
console.log(
  'Performance lanes: parallel instances, preserved clock, missing ancestry and no nested HTTP duplication passed.',
)
