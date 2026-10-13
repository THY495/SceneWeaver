import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ts from 'typescript'

const require = createRequire(import.meta.url)
const source = await readFile(new URL('../src/DirectionEditor.tsx', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.React },
}).outputText
const exported = {}
new Function('require', 'exports', 'React', compiled)(require, exported, React)
const { DirectionEditor } = exported
const actors = [{ alias: 'captain', name: '阿岚' }]
const direction = {
  version: 1,
  objective: '讨论是否开船',
  constraints: ['作者秘密须保留'],
  beats: [
    {
      id: 'talk',
      title: '交谈',
      objective: '讨论',
      completion: '回应玩家',
      participants: ['captain'],
      role_focus: { captain: '优先考虑安全' },
      constraints: ['保留关切'],
      handoff: 'after_beat',
      max_segments: 1,
    },
  ],
}
function find(element, predicate) {
  if (!React.isValidElement(element)) return null
  if (predicate(element)) return element
  for (const child of React.Children.toArray(element.props.children)) {
    const found = find(child, predicate)
    if (found) return found
  }
  return null
}
let changed
const editor = (value) =>
  DirectionEditor({
    value,
    actors,
    onChange: (v) => {
      changed = v
    },
  })
const before = editor(direction)
const permissionLabel = find(
  before,
  (e) =>
    e.type === 'label' &&
    React.Children.toArray(e.props.children).some(
      (child) => typeof child === 'string' && child.includes('允许导演引用已发表的原话'),
    ),
)
const checkbox = find(permissionLabel, (e) => e.type === 'input')
assert.equal(checkbox.props.checked, false)
checkbox.props.onChange({ target: { checked: true } })
assert.deepEqual(changed, { ...direction, beats: [{ ...direction.beats[0], source_response_tasks: true }] })
assert.match(renderToStaticMarkup(editor(changed)), /暂不使用此方式/)
const clear = {
  ...changed,
  constraints: [],
  beats: [{ ...changed.beats[0], constraints: [], role_focus: {} }],
}
assert.match(renderToStaticMarkup(editor(clear)), /导演会按当前互动选择是否使用/)
assert.doesNotMatch(renderToStaticMarkup(editor(clear)), /暂不使用此方式/)
const empty = editor(null)
find(empty, (e) => e.type === 'button').props.onClick()
assert.equal(changed.beats[0].source_response_tasks, false)
const selected = { ...direction, beats: [{ ...direction.beats[0], direct_response_actor: 'captain' }] }
const participant = find(
  editor(selected),
  (e) => e.type === 'div' && e.props.role === 'group' && e.props['aria-label'] === '交谈参与角色',
)
find(participant, (e) => e.type === 'input').props.onChange({ target: { checked: false } })
assert.equal(changed.beats[0].direct_response_actor, null)
assert.deepEqual(changed.beats[0].role_focus, {})
assert.deepEqual(changed.beats[0].participants, [])
console.log('Direction authoring permission and preserved constraints passed')
