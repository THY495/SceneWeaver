import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const code = ts.transpileModule(await readFile(new URL('../src/stageReading.ts', import.meta.url), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
}).outputText
const { readingLines, holdingPlayerTurn, firstReplyLine } = await import(
  'data:text/javascript;base64,' + Buffer.from(code).toString('base64')
)
assert.equal(holdingPlayerTurn('', true, true), false, 'waiting for a model must not pause that model turn')
const pending = readingLines([
  {
    id: 'pending',
    seq: 8,
    kind: 'speech',
    speaker: 'NPC',
    actor_id: 'npc',
    text: '',
    presentation_pending: true,
  },
])
assert.equal(pending.length, 1, 'pending publication keeps a selectable reading slot')
assert.equal(pending[0].id, 'pending:0')
assert.equal(pending[0].text, '')
assert.equal(pending[0].presentationPending, true)
assert.equal(pending[0].performance, undefined)
assert.deepEqual(
  readingLines([
    { id: 'input', seq: 8, kind: 'speech', speaker: '玩家', actor_id: 'player', text: '选音乐。' },
    {
      id: 'ack',
      seq: 9,
      kind: 'speech',
      speaker: 'NPC',
      actor_id: 'npc',
      text: '嗯。',
      acknowledgement: true,
    },
    { id: 'main', seq: 10, kind: 'speech', speaker: 'NPC', actor_id: 'npc', text: '那就聊音乐。' },
  ]).map((line) => line.id),
  ['input:0', 'main:0'],
  'played acknowledgement must not become a second gated story page',
)
assert.equal(holdingPlayerTurn('改一下', true, false), true, 'new player input still interrupts')
assert.equal(holdingPlayerTurn('', false, true), true, 'reading or browsing holds idle progression')
assert.equal(
  firstReplyLine(
    [
      { id: 'old', seq: 1, kind: 'speech', actor_id: 'npc', text: 'Old', speaker: 'NPC' },
      { id: 'input', seq: 2, kind: 'speech', actor_id: 'player', text: 'Hi', speaker: 'Player' },
      {
        id: 'ack',
        seq: 3,
        kind: 'speech',
        actor_id: 'npc',
        text: 'Hmm.',
        speaker: 'NPC',
        acknowledgement: true,
      },
      {
        id: 'reply',
        seq: 4,
        kind: 'speech',
        actor_id: 'npc',
        text: 'Hello.\n\nSecond page.',
        speaker: 'NPC',
      },
    ],
    1,
    'player',
  ).id,
  'reply:0',
  'follow the first new NPC page, not old text, player input, or the last page',
)
const events = [
  { id: 'open', seq: 1, kind: 'host', speaker: '作者', text: '雨还在下。\n\n有人推门进来。' },
  { id: 'private-control', seq: 2, kind: 'memory_change', speaker: '', text: '不属于演出的记录' },
  { id: 'heard', seq: 3, kind: 'speech', speaker: '', actor_id: null, text: '听不清后半句。' },
]
const initial = readingLines(events)
assert.deepEqual(
  initial.map((l) => l.id),
  ['open:0', 'open:1', 'heard:0'],
)
assert.equal(initial[2].speaker, '未辨认的声音')
assert.equal(initial[2].actorId, null)
assert.equal(initial[0].speaker, '旁白')
const later = readingLines([
  ...events,
  { id: 'next', seq: 4, kind: 'speech', speaker: '阿岚', text: '你好。' },
])
assert.deepEqual(later.slice(0, 3), initial)
assert.equal(readingLines(events)[0].id, initial[0].id)
const handoff = [
  {
    id: 'offer',
    seq: 5,
    kind: 'speech',
    speaker: '旅人',
    actor_id: 'p',
    text: '原始提议',
    stage: { mode: 'narration', text: '旅人提出递交旧信。' },
  },
  {
    id: 'proposal',
    seq: 6,
    kind: 'speech',
    speaker: '旅人',
    text: '提出行动提案：旧信',
    stage: { mode: 'redundant', with_event_id: 'offer' },
  },
  {
    id: 'decision',
    seq: 7,
    kind: 'speech',
    speaker: '沈舟',
    actor_id: 'n',
    text: '同意提案事项：旧信',
    stage: { mode: 'narration', text: '沈舟同意接收旧信，此时尚未交接。' },
  },
]
assert.deepEqual(
  readingLines(handoff).map((l) => l.id),
  ['offer:0', 'decision:0'],
)
assert.equal(readingLines(handoff)[1].speaker, '旁白', 'summary must not impersonate exact character speech')
assert.equal(readingLines(handoff)[1].actorId, undefined)
assert.equal(
  readingLines(handoff.slice(1))[0].text,
  '提出行动提案：旧信',
  'pagination must not hide the only visible record',
)
assert.equal(handoff[2].text, '同意提案事项：旧信', 'canonical event text remains intact')
console.log(
  'Stage reading: paragraph order, stable cursor IDs, anonymous speech and non-performance filtering passed',
)
