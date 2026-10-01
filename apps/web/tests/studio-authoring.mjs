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
const { graphReadiness, graphDiagnostics, releaseReadiness, itemReferences, removeActorFromGraph } =
  await import(await module('../src/authoringValidation.ts'))
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
removeActorFromGraph(directed, 'npc')
assert.ok(graphDiagnostics(directed).some((issue) => issue.code.startsWith('director-incomplete:')))
assert.deepEqual(directed.nodes[0].direction.beats[0].participants, [])
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
