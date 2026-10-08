import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { writeFileSync } from 'node:fs'
import { build } from 'vite'

const result = await build({ mode: 'game', configLoader: 'runner', build: { write: false } })
const outputs = (Array.isArray(result) ? result.flatMap((part) => part.output) : result.output) || []
const chunks = outputs.filter((part) => part.type === 'chunk')
const sourceModules = [...new Set(chunks.flatMap((chunk) => Object.keys(chunk.modules || {})))]
  .map((path) => path.replaceAll('\\', '/'))
  .filter((path) => path.includes('/src/'))
  .map((path) => path.split('/src/')[1])
  .sort()
const code = chunks.map((chunk) => chunk.code).join('\n')
const forbiddenText = ['打开作者诊断', '作者推进入口', '返回编辑', '返回作品库与创作', '这一段暂由作者推进']

assert(
  outputs.some((part) => part.fileName === 'game.html'),
  'Missing game entry',
)
assert(sourceModules.includes('game.tsx'), 'Missing player entry')
assert(!sourceModules.includes('main.tsx'), 'Studio entry reached the game bundle')
for (const module of ['TimingTrace.tsx', 'StoryTiming.tsx', 'PerformanceLanes.tsx', 'RequestAudit.tsx'])
  assert(!sourceModules.includes(module), `Creator diagnostics reached the game bundle: ${module}`)
for (const phrase of forbiddenText) assert(!code.includes(phrase), `Author text in game: ${phrase}`)

const report = {
  scope: 'Vite game web bundle; not a signed Windows release or native launch',
  source_modules: sourceModules,
  chunks: chunks.map((chunk) => ({
    file: chunk.fileName,
    sha256: createHash('sha256').update(chunk.code).digest('hex'),
    bytes: Buffer.byteLength(chunk.code),
  })),
  forbidden_author_text_absent: true,
}
if (process.argv[2]) writeFileSync(process.argv[2], JSON.stringify(report, null, 2))
console.log(`Game bundle boundary passed: ${sourceModules.length} player/shared source modules`)
