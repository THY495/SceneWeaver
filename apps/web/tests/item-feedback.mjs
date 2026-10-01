import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const code = ts.transpileModule(await readFile(new URL('../src/itemFeedback.ts', import.meta.url), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
}).outputText
const { itemError } = await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'))
assert.equal(itemError(new Error('{"code":"capacity","reason":"容器剩余容量不足"}')), '容器剩余容量不足')
assert.equal(itemError(new Error('物品状态已变化')), '物品状态已变化')
assert.match(itemError(new TypeError('Failed to fetch')), /无法确认操作结果/)
assert.match(itemError(new TypeError('NetworkError when attempting to fetch resource.')), /先刷新状态/)
assert.equal(itemError(new Error('{"unknown":"detail"}')), '{"unknown":"detail"}')
console.log('Item feedback: rule reasons and uncertain network outcomes passed')
