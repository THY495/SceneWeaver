import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const source = await readFile(new URL('../src/backstagePlacement.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
}).outputText
const { backstagePlacement } = await import(
  'data:text/javascript;base64,' + Buffer.from(compiled).toString('base64')
)
const actors = [
  { alias: 'a', name: '阿岚', location: '书房' },
  { alias: 'b', name: '沈舟', location: '书房' },
  { alias: 'player', name: '玩家', location: '花园' },
]
assert.match(backstagePlacement([], actors), /至少选择/)
assert.match(backstagePlacement(['missing'], actors), /缺少/)
assert.match(backstagePlacement(['a', 'b'], actors), /没有其他角色/)
assert.match(backstagePlacement(['a'], actors), /沈舟也在书房/)
assert.match(
  backstagePlacement(
    ['a', 'b'],
    actors.map((a) => (a.alias === 'b' ? { ...a, location: '花园' } : a)),
  ),
  /不在同一地点/,
)
assert.match(
  backstagePlacement(
    ['a', 'b'],
    actors.map((a) => (a.alias === 'player' ? { ...a, location: '书房' } : a)),
  ),
  /玩家也在书房/,
)
console.log('6 backstage placement checks passed')
