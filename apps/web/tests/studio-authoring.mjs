import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
async function module(path, replacements = {}) {
  let source = await readFile(new URL(path, import.meta.url), 'utf8')
  for (const [a, b] of Object.entries(replacements)) source = source.replace(a, b)
  return (
    'data:text/javascript;base64,' +
    Buffer.from(
      ts.transpileModule(source, {
        compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
      }).outputText,
    ).toString('base64')
  )
}
const {
  graphReadiness,
  graphDiagnostics,
  releaseReadiness,
  itemReferences,
  openingRecipientWarnings,
  removeActorFromGraph,
  transitionWarnings,
} = await import(await module('../src/authoringValidation.ts'))
const g = {
  entry: 'a',
  nodes: [
    {
      id: 'a',
      title: '港口',
      stage: [{ actor: 'npc' }],
      observers: ['npc', 'player'],
      participants: ['npc'],
      opening: [{ speaker: 'npc', text: 'hello' }],
    },
    { id: 'b', title: '灯塔', text: '灯亮了。', observers: ['player'] },
  ],
  edges: [],
}
const recipientGraph = structuredClone(g)
recipientGraph.nodes[0].opening = [{ speaker: 'another', target: 'npc', text: 'hello' }]
removeActorFromGraph(recipientGraph, 'npc')
assert.equal(recipientGraph.nodes[0].opening[0].target, null)
const ambiguousOpening = structuredClone(g)
ambiguousOpening.nodes[0].observers.push('another')
ambiguousOpening.nodes[0].opening = [
  { id: 'question', kind: 'dialogue', speaker: 'npc', text: '你愿意同行吗？' },
]
assert.equal(openingRecipientWarnings(ambiguousOpening).length, 1)
ambiguousOpening.nodes[0].opening[0].target = 'player'
assert.equal(openingRecipientWarnings(ambiguousOpening).length, 0)
const directed = structuredClone(g)
directed.nodes[0].direction = {
  version: 1,
  objective: '讨论',
  constraints: [],
  beats: [
    {
      id: 'beat',
      participants: ['npc'],
      title: '讨论',
      objective: '讨论',
      completion: '结束',
      constraints: [],
      handoff: 'after_beat',
      max_segments: 3,
    },
  ],
}
assert.ok(!graphDiagnostics(directed).some((issue) => issue.code.startsWith('director-')))
const requiredExchange = structuredClone(directed)
requiredExchange.nodes[0].direction.beats[0].npc_exchange_required = true
assert.ok(graphDiagnostics(requiredExchange).some((issue) => issue.code.startsWith('director-npc-exchange:')))
requiredExchange.nodes[0].direction.beats[0].participants.push('another')
assert.ok(
  !graphDiagnostics(requiredExchange).some((issue) => issue.code.startsWith('director-npc-exchange:')),
)
const companionRoute = structuredClone(directed)
companionRoute.nodes[1].entry_locations = [{ actor: 'npc', location: '灯塔' }]
companionRoute.edges = [
  { id: 'climb', source: 'a', target: 'b', label: '上楼', trigger: 'ai', condition: '玩家想上楼' },
]
assert.equal(transitionWarnings(companionRoute, 'player').length, 1)
companionRoute.edges[0].requires_agreement = ['npc']
assert.ok(
  transitionWarnings(companionRoute, 'player').some((issue) =>
    issue.code.startsWith('agreement-no-alternative:'),
  ),
)
const twoConsentRoutes = structuredClone(companionRoute)
twoConsentRoutes.nodes.push({ id: 'b2', title: '码头', text: '抵达码头', observers: ['player'] })
twoConsentRoutes.edges.push({
  id: 'sail',
  source: 'a',
  target: 'b2',
  label: '一起去码头',
  trigger: 'ai',
  condition: '玩家想去码头，NPC 同意',
  requires_agreement: ['npc'],
})
assert.equal(
  transitionWarnings(twoConsentRoutes, 'player').filter((issue) =>
    issue.code.startsWith('agreement-no-alternative:'),
  ).length,
  1,
  'Two agreement-only exits still leave the player stuck if NPC refuses both',
)
companionRoute.nodes.push({ id: 'c', title: '留在原地', text: '暂时不走', observers: ['player'] })
companionRoute.edges.push({
  id: 'stay',
  source: 'a',
  target: 'c',
  label: '留在原地',
  trigger: 'ai',
  condition: 'NPC 拒绝同行',
})
assert.deepEqual(transitionWarnings(companionRoute, 'player'), [])
const duplicateConditions = structuredClone(companionRoute)
duplicateConditions.edges[1].condition = '  玩家想上楼  '
assert.ok(
  transitionWarnings(duplicateConditions, 'player').some((issue) =>
    issue.code.startsWith('ai-condition-duplicate:'),
  ),
  'Identical AI exit conditions can both be met and block automatic routing',
)
duplicateConditions.edges[1].condition = 'NPC 拒绝同行'
assert.ok(
  !transitionWarnings(duplicateConditions, 'player').some((issue) =>
    issue.code.startsWith('ai-condition-duplicate:'),
  ),
  'Distinct branch conditions should not trigger duplicate warnings',
)
companionRoute.nodes[0].observers = ['player']
assert.ok(
  transitionWarnings(companionRoute, 'player').some((issue) =>
    issue.code.startsWith('agreement-unreachable:'),
  ),
)
removeActorFromGraph(companionRoute, 'npc')
assert.deepEqual(companionRoute.edges[0].requires_agreement, [])
directed.nodes[0].direction.beats[0].role_focus = { npc: '关注自己的选择' }
removeActorFromGraph(directed, 'npc')
assert.ok(graphDiagnostics(directed).some((issue) => issue.code.startsWith('director-incomplete:')))
assert.deepEqual(directed.nodes[0].direction.beats[0].participants, [])
assert.deepEqual(directed.nodes[0].direction.beats[0].role_focus, {})
assert.equal(
  directed.nodes[0].direction.beats.length,
  1,
  'Preserve author intent when its last actor is removed',
)
assert.equal(graphReadiness(g).length, 1)
assert.equal(graphDiagnostics(g)[0].scene, 'b')
assert.equal(graphDiagnostics(g)[0].panel, 'exits')
const backstage = structuredClone(g)
backstage.nodes[0].mode = 'backstage'
backstage.nodes[0].observers = []
assert.equal(graphDiagnostics(backstage)[0].panel, 'rules')
assert.match(releaseReadiness(backstage)[0], /尚不能在独立游戏中自动运行/)
assert.equal(releaseReadiness(g).length, 0)
const scripted = structuredClone(g)
scripted.nodes[0].mode = 'backstage'
scripted.nodes[0].observers = ['npc']
scripted.edges = [{ id: 'exit', source: 'a', target: 'b', label: '结束会议' }]
assert.equal(
  releaseReadiness(scripted, [{ alias: 'npc', location: '档案室' }], { alias: 'player', location: '门厅' })
    .length,
  0,
)
scripted.edges[0].trigger = 'ai'
assert.match(
  releaseReadiness(scripted, [{ alias: 'npc', location: '档案室' }], {
    alias: 'player',
    location: '门厅',
  })[0],
  /唯一无条件出口/,
)
scripted.edges[0].trigger = 'manual'
scripted.nodes[0].direction = structuredClone(directed.nodes[0].direction)
scripted.nodes[0].direction.beats[0].participants = ['npc']
assert.equal(
  releaseReadiness(scripted, [{ alias: 'npc', location: '档案室' }], { alias: 'player', location: '门厅' })
    .length,
  0,
)
scripted.nodes[0].entry_locations = [{ actor: 'npc', location: '档案室' }]
assert.equal(
  releaseReadiness(scripted, [{ alias: 'npc', location: '门厅' }], { alias: 'player', location: '门厅' })
    .length,
  0,
)
scripted.nodes[0].entry_locations[0].location = ''
assert.ok(graphDiagnostics(scripted).some((issue) => issue.code.startsWith('entry-location:')))
removeActorFromGraph(scripted, 'npc')
assert.deepEqual(scripted.nodes[0].entry_locations, [])
g.edges.push({
  source: 'a',
  target: 'b',
  label: '到灯塔',
  trigger: 'ai',
  condition: '',
  requires_holding: { actor: 'npc', item: 'letter' },
})
assert.match(graphReadiness(g)[0], /AI 触发条件/)
assert.equal(graphDiagnostics(g)[0].scene, 'a')
assert.equal(graphDiagnostics(g)[0].panel, 'exits')
g.nodes[0].objects = [{ kind: 'item', item: 'letter' }]
assert.equal(itemReferences(g).letter.length, 2, 'both stage and condition must block accidental deletion')
g.edges[0].condition = '玩家请求出发'
assert.equal(graphReadiness(g).length, 0)
removeActorFromGraph(g, 'npc')
assert.deepEqual(g.nodes[0].stage, [])
assert.deepEqual(g.nodes[0].observers, ['player'])
assert.equal(g.nodes[0].opening, null)
assert.equal(g.edges[0].requires_holding, null)
assert.equal(graphDiagnostics(g)[0].panel, 'opening')
assert.equal(itemReferences(g).letter.length, 1, 'stage reference survives actor removal')
const objectsModule = await module('../src/sceneObjectModel.ts')
const { reorderComposition, compositionEntries } = await import(
  await module('../src/sceneComposition.ts', { "'./sceneObjectModel'": JSON.stringify(objectsModule) })
)
const slots = [{ actor: 'a' }, { actor: 'b' }],
  objects = [
    { id: 'letter', layer: 'entities', order: 3 },
    { id: 'rail', layer: 'foreground', order: 0 },
  ]
let result = reorderComposition(slots, objects, 'object:letter', 'person:a')
assert.deepEqual(
  compositionEntries(result.slots, result.objects).map((e) => e.key),
  ['object:rail', 'person:b', 'person:a', 'object:letter'],
)
assert.equal(slots[0].draw_order, undefined, 'source stays immutable for undo')
assert.deepEqual(
  reorderComposition(slots, objects, 'person:a', 'object:rail'),
  { slots, objects },
  'semantic layer cannot be crossed by accidental reorder',
)
const cgGraph = {
  entry: 'cg',
  nodes: [{ id: 'cg', title: '重逢', text: '夜色', observers: ['player'], visual_mode: 'cg' }],
  edges: [],
}
assert.equal(graphDiagnostics(cgGraph)[0].panel, 'stage')
assert.match(graphReadiness(cgGraph)[0], /CG 主画面/)
cgGraph.nodes[0].background = 'a'.repeat(64)
assert.deepEqual(graphDiagnostics(cgGraph), [])
console.log(
  'Authoring readiness, CG image diagnostics, reference cleanup, mixed ordering and undo immutability passed',
)
