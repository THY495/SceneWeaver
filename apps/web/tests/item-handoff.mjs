import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const code = ts.transpileModule(await readFile(new URL('../src/itemHandoff.ts', import.meta.url), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
}).outputText
const { handoffPlan, sameHandoffPlan } = await import(
  'data:text/javascript;base64,' + Buffer.from(code).toString('base64')
)
const snapshot = {
  enabled: true,
  items: [{ id: 'letter', name: '信', version: 2, holder_id: 'player' }],
  recipients: [{ id: 'npc', name: '沈舟' }],
  transfers: [],
}
const consent = {
  proposal_id: 'p',
  version: 3,
  item_id: 'entry',
  object_id: 'letter',
  expected_revision: 2,
  sender_id: 'player',
  recipient_id: 'npc',
  state: 'ready',
}
const plan = (s = snapshot, recipient = 'npc') => handoffPlan(s, 'player', 'letter', recipient)
assert.equal(plan().kind, 'offer')
assert.equal(plan(snapshot, 'absent').kind, 'blocked')
assert.equal(plan({ ...snapshot, items: [{ ...snapshot.items[0], holder_id: 'npc' }] }).kind, 'blocked')
assert.equal(plan({ ...snapshot, enabled: false }).kind, 'blocked')
const ready = plan({ ...snapshot, transfers: [consent] })
assert.equal(ready.kind, 'give')
for (const state of ['wait_reply', 'wait_conditions', 'failed']) {
  assert.equal(plan({ ...snapshot, transfers: [{ ...consent, state }] }).kind, 'blocked')
}
for (const state of ['refused', 'cancelled', 'expired', 'success']) {
  assert.equal(plan({ ...snapshot, transfers: [{ ...consent, state }] }).kind, 'offer')
}
assert.equal(plan({ ...snapshot, transfers: [{ ...consent, expected_revision: 1 }] }).kind, 'offer')
assert.equal(plan({ ...snapshot, transfers: [{ ...consent, recipient_id: 'someone-else' }] }).kind, 'offer')
assert.equal(
  plan({ ...snapshot, transfers: [consent, { ...consent, proposal_id: 'second' }] }).kind,
  'blocked',
)
assert.equal(
  sameHandoffPlan(plan(), ready),
  false,
  'do not promote a pending offer gesture to a physical transfer',
)
assert.equal(sameHandoffPlan(ready, plan()), false, 'withdrawn consent cannot be reused')
assert.equal(sameHandoffPlan(ready, plan({ ...snapshot, transfers: [{ ...consent, version: 4 }] })), false)
assert.equal(sameHandoffPlan(ready, ready), true)
console.log('Item handoff: consent, version, recipient, pending, ambiguity and gesture revalidation passed')
