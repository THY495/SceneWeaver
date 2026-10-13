import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const source = await readFile(new URL('../src/gameMenuPause.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
}).outputText
const { pauseForMenu } = await import(
  'data:text/javascript;base64,' + Buffer.from(compiled).toString('base64')
)

for (const outcome of ['success', 'rejected', 'uncertain']) {
  const events = []
  let release, reject
  const response = new Promise((resolve, fail) => {
    release = resolve
    reject = fail
  })
  let requests = 0
  const failure = new Error(outcome)
  const paused = pauseForMenu(
    'save',
    async (path, options) => {
      requests++
      assert.deepEqual(events, ['pause_requested'], 'local stop precedes sending the mutation')
      assert.equal(path, '/game/pause')
      assert.equal(options.method, 'PUT')
      assert.deepEqual(JSON.parse(options.body), { paused: true })
      return response
    },
    (kind) => events.push(kind),
  )
  const settled = paused.then(
    () => events.push('may_navigate'),
    (error) => {
      assert.equal(error, failure, 'preserve the actual failure for the player UI')
    },
  )
  await Promise.resolve()
  assert.deepEqual(events, ['pause_requested'], 'a pending response cannot confirm pause or navigate')
  if (outcome === 'success') release({})
  else reject(failure)
  await settled
  assert.deepEqual(
    events,
    outcome === 'success'
      ? ['pause_requested', 'paused', 'may_navigate']
      : ['pause_requested', 'pause_failed'],
  )
  assert.equal(requests, 1, 'never retry a mutation automatically when its result is uncertain')
}
console.log(
  'Game main menu: immediate local stop, server-confirmed navigation, rejected/uncertain response isolation passed',
)
