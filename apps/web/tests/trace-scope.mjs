import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const source = await readFile(new URL('../src/traceScope.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
}).outputText
const { traceScope, inTraceScope } = await import(
  'data:text/javascript;base64,' + Buffer.from(compiled).toString('base64')
)
const spans = [
  { id: 'root', parent_id: null, attributes: {} },
  { id: 'a', parent_id: 'root', attributes: { actor_id: 'alice', step: 0 } },
  { id: 'b', parent_id: 'root', attributes: { actor_id: 'bob', step: 1 } },
  { id: 'http-a', parent_id: 'a', attributes: {} },
  { id: 'http-b', parent_id: 'b', attributes: {} },
]
assert.deepEqual(traceScope(spans, 'http-a'), { actor: 'alice', step: 0 })
assert.equal(inTraceScope(spans, 'http-a', 'alice', '0'), true)
assert.equal(inTraceScope(spans, 'http-b', 'alice', ''), false)
assert.equal(inTraceScope(spans, 'root', 'alice', ''), false)
assert.equal(inTraceScope(spans, 'missing', '', ''), true)
assert.equal(inTraceScope(spans, 'missing', '', '0'), false)
assert.deepEqual(traceScope([{ id: 'cycle', parent_id: 'cycle', attributes: {} }], 'cycle'), {
  actor: undefined,
  step: undefined,
})
console.log('Trace scope: parallel actors, zero step, unknown ancestors and cycles passed.')
