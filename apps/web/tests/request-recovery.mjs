import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

async function load(name) {
  const code = ts.transpileModule(await readFile(new URL(`../src/${name}.ts`, import.meta.url), 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
  }).outputText
  return import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'))
}
const { requestJSON } = await load('apiRequest')
const { itemError } = await load('itemFeedback')
let commits = 0,
  posts = 0
const server = createServer(async (req, res) => {
  for await (const _chunk of req) {
    /* consume the complete request */
  }
  if (req.method === 'GET') {
    if (req.url === '/offline') return res.destroy()
    return res.end(JSON.stringify({ commits }))
  }
  posts++
  if (req.url === '/before') return res.destroy()
  if (req.url === '/rejected') {
    res.statusCode = 409
    return res.end(JSON.stringify({ detail: { code: 'stale', reason: '物品状态已变化' } }))
  }
  commits++
  if (req.url === '/lost') return res.destroy()
  if (req.url === '/partial') return res.end('{"ok":')
  if (req.url === '/late-error') {
    res.statusCode = 502
    return res.end(JSON.stringify({ detail: 'upstream closed' }))
  }
  if (req.url === '/slow') await new Promise((resolve) => setTimeout(resolve, 80))
  res.end(JSON.stringify({ ok: true, task_id: 'receipt', commits }))
})
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
const base = `http://127.0.0.1:${server.address().port}`
const send = (path) => requestJSON(base + path, { method: 'POST', body: '{}' })
try {
  for (const path of ['/lost', '/partial', '/late-error']) {
    const before = commits,
      attempts = posts
    await assert.rejects(send(path), (error) => {
      assert.equal(error.uncertain, true)
      assert.match(itemError(error), /可能已经生效/)
      return true
    })
    const snapshot = await requestJSON(base + '/snapshot')
    assert.equal(snapshot.commits, before + 1)
    assert.equal(posts, attempts + 1, 'response loss must never automatically resend a mutation')
  }
  const before = commits
  await assert.rejects(send('/before'), (e) => e.uncertain === true)
  assert.equal((await requestJSON(base + '/snapshot')).commits, before)
  await assert.rejects(send('/rejected'), (e) => {
    assert.equal(e.uncertain, false)
    assert.equal(itemError(e), '物品状态已变化')
    return true
  })
  assert.equal(commits, before)
  const accepted = await send('/slow')
  assert.equal(accepted.task_id, 'receipt')
  await assert.rejects(requestJSON(base + '/offline'), (e) => e.uncertain === false)
  assert.equal(accepted.ok, true, 'a failed subsequent read does not change the mutation result')
  assert.equal((await requestJSON(base + '/snapshot')).commits, before + 1)
  console.log(
    'HTTP recovery: lost/truncated/502 responses, pre-commit disconnect, rejection, slow success and failed refresh passed; no automatic POST retries',
  )
} finally {
  server.closeAllConnections()
  await new Promise((resolve) => server.close(resolve))
}
