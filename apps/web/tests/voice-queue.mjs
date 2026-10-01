import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const source = await readFile(new URL('../src/voiceQueue.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
}).outputText
const { shouldQueueUtterance } = await import(
  'data:text/javascript;base64,' + Buffer.from(compiled).toString('base64')
)

const state = { epoch: 3, seen: 12, submittedTask: 'player-task', currentEventId: 'old-page' }
const ack = { id: 'ack', task_id: 'player-task', seq: 0, epoch: 3, acknowledgement: true }
assert(shouldQueueUtterance(ack, state), 'current input acknowledgement does not need a world seq')
assert(!shouldQueueUtterance(ack, { ...state, submittedTask: null }), 'old candidates cannot auto-play')
assert(!shouldQueueUtterance({ ...ack, task_id: 'other-task' }, state))
assert(!shouldQueueUtterance({ ...ack, epoch: 2 }, state))
assert(!shouldQueueUtterance({ ...ack, text_fallback: true }, state))
assert(
  shouldQueueUtterance(
    { id: 'new', task_id: 'player-task', seq: 13, epoch: 3 },
    {
      ...state,
      currentEventId: undefined,
    },
  ),
)
