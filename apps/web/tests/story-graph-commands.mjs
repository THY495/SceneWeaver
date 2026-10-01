import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const source = await readFile(new URL('../src/storyGraphCommands.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
}).outputText
const { connectionProblem, copySubgraph, pasteSubgraph, removeSelection } = await import(
  'data:text/javascript;base64,' + Buffer.from(compiled).toString('base64')
)
const graph = {
  entry: 'a',
  nodes: [
    { id: 'a', title: 'A', stage: [{ actor: 'npc', position: 'left' }] },
    { id: 'b', title: 'B' },
    { id: 'c', title: 'C' },
  ],
  edges: [
    { id: 'ab', source: 'a', target: 'b' },
    { id: 'bc', source: 'b', target: 'c' },
  ],
}
assert.match(connectionProblem(graph, 'c', 'a'), /循环/)
assert.match(connectionProblem(graph, 'a', 'a'), /自身/)
assert.match(connectionProblem(graph, 'a', 'b'), /已有/)
assert.equal(connectionProblem(graph, 'a', 'c'), '')
const copy = copySubgraph(graph, ['a', 'b'])
assert.deepEqual(
  copy.edges.map((e) => e.id),
  ['ab'],
)
let nextId = 0
const pasted = pasteSubgraph(graph, copy, () => `new-${++nextId}`)
assert.equal(pasted.nodes.length, 5)
assert.equal(pasted.edges.length, 3)
assert.equal(pasted.edges[2].source, 'new-1')
assert.equal(pasted.edges[2].target, 'new-2')
assert.equal(pasted.nodes[3].stage[0].actor, 'npc')
pasted.nodes[3].stage[0].position = 'right'
assert.equal(graph.nodes[0].stage[0].position, 'left')
assert.throws(() => removeSelection(graph, ['a'], []), /入口/)
const removed = removeSelection(graph, ['b'], [])
assert.equal(removed.edges.length, 0)
assert.equal(graph.edges.length, 2)
console.log(
  'Graph commands: cycles, duplicate links, internal subgraph copy, stable actor references and atomic deletion passed.',
)
