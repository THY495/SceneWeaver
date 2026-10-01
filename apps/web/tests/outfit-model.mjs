import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
async function load(name) {
  const code = ts.transpileModule(await readFile(new URL(`../src/${name}.ts`, import.meta.url), 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
  }).outputText
  return import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'))
}
const { resolveAppearance, convertLegacyPortrait, performanceSprite } = await load('outfitModel')
const { readingLines, speakingFocus } = await load('stageReading')
const legacy = { base_sprite: 'legacy' },
  converted = convertLegacyPortrait(legacy)
assert.equal(legacy.outfits, undefined)
assert.equal(resolveAppearance(converted).sprite, 'legacy')
assert.equal(convertLegacyPortrait(converted), converted)
const p = {
  default_outfit: 'rain',
  outfits: [
    {
      id: 'rain',
      default_variant: 'neutral',
      variants: [
        { id: 'neutral', sprite: 'coat', expression: 'calm' },
        { id: 'angry', sprite: 'angry-coat', expression: 'angry' },
      ],
    },
    { id: 'casual', default_variant: 'neutral', variants: [{ id: 'neutral', sprite: 'shirt' }] },
  ],
}
assert.equal(resolveAppearance(p, { outfit: 'missing', variant: 'missing' }).sprite, 'coat')
assert.equal(resolveAppearance(p, { outfit: 'casual' }).sprite, 'shirt')
assert.equal(performanceSprite(p.outfits[0].variants, { expression: 'angry' }), 'angry-coat')
assert.equal(
  performanceSprite(
    [
      { id: 'pose', sprite: 'thinking', pose: 'thinking' },
      { id: 'expression', sprite: 'concerned', expression: 'concerned' },
      { id: 'both', sprite: 'concerned-thinking', expression: 'concerned', pose: 'thinking' },
    ],
    { expression: 'concerned', pose: 'thinking' },
  ),
  'concerned-thinking',
)
assert.equal(
  performanceSprite([{ id: 'expression', sprite: 'concerned', expression: 'concerned' }], {
    expression: 'concerned',
    pose: 'thinking',
  }),
  'concerned',
)
assert.equal(performanceSprite([{ id: 'plain', sprite: 'plain' }], { expression: 'concerned' }), undefined)
assert.equal(
  performanceSprite(p.outfits[1].variants, { expression: 'angry' }),
  undefined,
  'emotion never changes outfit',
)
assert.equal(performanceSprite(p.outfits[0].variants, { expression: 'angry', pose: 'running' }), 'angry-coat')
const line = { id: '1', speaker: 'A', actorId: 'a', text: 'hi' }
assert.equal(speakingFocus(line, ['a', 'b'], null), 'a')
assert.equal(speakingFocus(line, ['a', 'b'], { actorId: 'b', active: true }), 'b')
assert.equal(speakingFocus(line, ['a'], { actorId: 'hidden', active: true }), null)
assert.equal(speakingFocus(line, ['a'], { active: true }), null)
assert.equal(speakingFocus(line, ['a'], { active: false }), 'a')
const lines = readingLines([
  {
    id: 'e',
    kind: 'speech',
    actor_id: 'a',
    text: 'x',
    appearance: { sprite: 'coat' },
    performance: [{ text: 'x', expression: 'angry' }],
  },
  { id: 'n', kind: 'host', text: 'n', appearance: { sprite: 'secret' } },
])
assert.equal(lines[0].appearance.sprite, 'coat')
assert.equal(lines[1].appearance, undefined)
console.log('Outfit migration, selection fallback, bounded expression and visible playback focus passed')
