import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const source = await readFile(new URL('../src/scenePreviewLocations.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
}).outputText
const { scenePreviewLocations, unstagedColocatedActors } = await import(
  'data:text/javascript;base64,' + Buffer.from(compiled).toString('base64')
)
const actors = [
  { alias: 'npc', location: '档案室' },
  { alias: 'player', location: '门厅' },
]
const graph = {
  entry: 'private',
  nodes: [
    { id: 'private' },
    { id: 'meeting', entry_locations: [{ actor: 'npc', location: '门厅' }] },
    { id: 'ending' },
  ],
  edges: [
    { source: 'private', target: 'meeting' },
    { source: 'meeting', target: 'ending' },
  ],
}
assert.equal(scenePreviewLocations(graph, actors, 'ending').locations.get('npc'), '门厅')

const branched = structuredClone(graph)
branched.nodes.splice(2, 0, { id: 'other' })
branched.edges.push({ source: 'private', target: 'other' }, { source: 'other', target: 'ending' })
const uncertain = scenePreviewLocations(branched, actors, 'ending')
assert.equal(uncertain.locations.get('npc'), undefined)
assert.deepEqual(uncertain.ambiguous, ['npc'])
branched.nodes.find((node) => node.id === 'ending').entry_locations = [{ actor: 'npc', location: '门厅' }]
const resolved = scenePreviewLocations(branched, actors, 'ending')
assert.equal(resolved.locations.get('npc'), '门厅')
assert.deepEqual(resolved.ambiguous, [])

branched.nodes.push({ id: 'after' })
branched.edges.push({ source: 'ending', target: 'after' })
assert.deepEqual(scenePreviewLocations(branched, actors, 'after').ambiguous, [])
const cast = [
  { alias: 'npc', name: '许遥' },
  { alias: 'player', name: '旅人' },
]
assert.deepEqual(unstagedColocatedActors(cast, 'player', resolved.locations, []), [
  { alias: 'npc', name: '许遥' },
])
assert.deepEqual(unstagedColocatedActors(cast, 'player', resolved.locations, [{ actor: 'npc' }]), [])
assert.deepEqual(unstagedColocatedActors(cast, 'player', resolved.locations, null), [])
assert.deepEqual(unstagedColocatedActors(cast, 'player', uncertain.locations, []), [])
console.log('Scene preview inherits entry moves across a DAG and flags differing branch locations')
