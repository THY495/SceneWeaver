import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const code = ts.transpileModule(
  await readFile(new URL('../src/readingProgress.ts', import.meta.url), 'utf8'),
  {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
  },
).outputText
const { lineFingerprint, parseProgress, restoreProgress } = await import(
  'data:text/javascript;base64,' + Buffer.from(code).toString('base64')
)
const lines = [
  { id: 'entry:0', speaker: '旁白', text: '开场' },
  { id: 'reply:0', speaker: '沈舟', text: '请说。' },
]
const saved = {
  version: 1,
  entry: 'entry',
  selected: 'reply:0',
  acknowledged: true,
  fingerprint: lineFingerprint(lines[1]),
}
assert.deepEqual(restoreProgress(lines, 'entry', saved), {
  selected: 'reply:0',
  readThrough: 'reply:0',
  notice: '',
})
assert.equal(restoreProgress(lines, 'entry', { ...saved, acknowledged: false }).readThrough, '')
assert.equal(
  restoreProgress([...lines, { id: 'new:0', speaker: '沈舟', text: '下一句。' }], 'entry', saved).selected,
  'reply:0',
  'new responses must not be skipped',
)
const changed = restoreProgress([lines[0], { ...lines[1], text: '内容已改变' }], 'entry', saved)
assert.equal(changed.readThrough, '')
assert.ok(changed.notice)
assert.ok(restoreProgress(lines.slice(0, 1), 'entry', saved).notice, 'deleted/hidden bookmark needs a notice')
assert.equal(restoreProgress(lines, 'different-scene', saved).readThrough, '')
assert.equal(
  restoreProgress([{ id: 'next:0', speaker: '旁白', text: '下一场' }, ...lines], 'next', saved).selected,
  'next:0',
)
assert.equal(parseProgress('{bad'), null)
assert.equal(parseProgress(JSON.stringify({ ...saved, acknowledged: 'true' })), null)
assert.equal(parseProgress(JSON.stringify({ ...saved, version: 2 })), null)
assert.deepEqual(parseProgress(JSON.stringify(saved)), saved)
assert.equal(restoreProgress([], 'entry', saved).selected, '')
console.log(
  'Reading progress: unread/acknowledged recovery, new events, scene changes, hidden/changed content and invalid storage passed',
)
