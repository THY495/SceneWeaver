import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const code = ts.transpileModule(await readFile(new URL('../src/readingStore.ts', import.meta.url), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
}).outputText
const { readingWriter } = await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'))
const calls = []
const write = readingWriter(
  async (_, init) => {
    const body = JSON.parse(init.body)
    calls.push(body)
    await new Promise((resolve) => setTimeout(resolve, 5))
    return { revision: body.expected_revision + 1 }
  },
  '/reading',
  { epoch: 4, revision: 8 },
)
await Promise.all([write({ selected: 'a' }), write({ selected: 'b' }), write({ selected: 'c' })])
assert.deepEqual(
  calls.map((c) => c.expected_revision),
  [8, 9, 10],
)
assert.deepEqual(
  calls.map((c) => c.epoch),
  [4, 4, 4],
)
let attempts = 0
const failed = readingWriter(
  async () => {
    attempts++
    throw Error('response lost')
  },
  '/reading',
  { epoch: 4, revision: 8 },
)
await assert.rejects(failed({ selected: 'a' }))
await assert.rejects(failed({ selected: 'b' }))
assert.equal(attempts, 1)
console.log('Reading store serialization and uncertain-response checks passed')
