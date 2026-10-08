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
const { performanceLanes, performanceEvidence } = await import(uri(source))
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
const rejected = span('rejected', 'a', 'npc.semantic_review_result', 900, {
  candidate_revision: 2,
  review_provider_accepted: true,
  review_accepted: false,
  response_review_binding: true,
  response_binding_accepted: false,
  response_task_status: 'not_addressed',
})
assert.deepEqual(performanceEvidence(rejected), [
  '稿次 2',
  '模型通过标记：通过',
  '本轮审核校验：拒绝',
  '回应绑定实验',
  '回应评估（模型）：未回应',
  '绑定结构门槛：拒绝',
])
const accepted = span('accepted', 'a', 'npc.semantic_review_result', 1400, {
  review_provider_accepted: true,
  review_accepted: true,
  review_issue_count: 0,
  review_unsupported_count: 0,
  response_review_binding: true,
  response_binding_accepted: true,
  response_task_status: 'responded',
})
assert.ok(performanceEvidence(accepted).includes('回应评估（模型）：已回应'))
assert.ok(!performanceEvidence(accepted).join(' ').includes('有效回答'))
assert.ok(performanceEvidence(accepted).includes('本轮审核校验：通过'))
assert.ok(
  performanceEvidence(
    span('old-flag', null, 'npc.semantic_review_result', 0, {
      review_accepted: true,
      review_issue_count: 0,
      review_unsupported_count: 1,
    }),
  ).includes('本轮审核校验：拒绝'),
)
assert.ok(
  performanceEvidence(
    span('missing-counts', null, 'npc.semantic_review_result', 0, {
      review_accepted: true,
    }),
  ).includes('本轮审核校验：通过标记（完整结论未记录）'),
)
assert.deepEqual(performanceEvidence(span('old', null, 'npc.semantic_review_result', 0)), [
  '回应绑定评估：未记录',
])
assert.deepEqual(
  performanceEvidence(
    span('private', null, 'model.call', 0, {
      response_review_binding: true,
      response_task_status: 'PRIVATE UNRECOGNIZED OUTPUT',
    }),
  ),
  ['回应绑定实验', '回应评估（模型）：未记录'],
)
const expanded = performanceLanes([...spans, rejected, accepted, span('commit', 'a', 'world.commit', 1500)])
assert.deepEqual(
  expanded.find((l) => l.owner === 'a').entries.map((e) => e.span.id),
  ['review-a', 'tts-a', 'rejected', 'accepted', 'commit'],
)
console.log(
  'Performance lanes: parallel instances, preserved clock, missing ancestry and no nested HTTP duplication passed.',
)
