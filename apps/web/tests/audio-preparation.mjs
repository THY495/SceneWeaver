import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const source = await readFile(new URL('../src/audioPreparation.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 },
}).outputText
const { AudioPreparation } = await import(
  'data:text/javascript;base64,' + Buffer.from(compiled).toString('base64')
)
globalThis.sessionStorage = { getItem: () => null }
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
const receipts = []
const request = async (path, options) => {
  if (path.endsWith('/receipt')) {
    receipts.push(JSON.parse(options.body))
    return {}
  }
  if (options?.method === 'POST') return { id: 'test' }
  return { stream_done: true, sent_samples: 96000 }
}
globalThis.fetch = async () =>
  new Response(
    new ReadableStream({
      start(c) {
        c.enqueue(new Uint8Array(192000))
        c.close()
      },
    }),
  )
const prep = new AudioPreparation(request, '/prepare', {})
await prep.ready
await wait(35)
assert(prep.buffered <= 48000, 'producer must stop at the buffer limit')
assert(!prep.done, 'producer must exert backpressure until consumer drains')
let total = 0
while (!prep.done || prep.packets.length) {
  const p = prep.shift()
  if (p) total += p.length
  else await wait(5)
}
assert.equal(total, 192000)
assert.equal(prep.error, null)
assert(!receipts.some((x) => x.status === 'completed'), 'download is never playback completion')
await prep.receipt('playing', 1200)
prep.cancel()
await wait(5)
assert.equal(receipts.at(-1).played_samples, 1200, 'cancellation must not overwrite already heard samples')

let release
const delayed = new AudioPreparation(
  async (path, opts) => {
    if (path === '/prepare')
      return new Promise((r) => {
        release = r
      })
    return request(path, opts)
  },
  '/prepare',
  {},
)
delayed.cancel()
release({ id: 'late' })
await wait(10)
assert(delayed.controller.signal.aborted && delayed.done)
assert(receipts.some((x) => x.status === 'interrupted'))

globalThis.fetch = async () =>
  new Response(
    new ReadableStream({
      start(c) {
        c.enqueue(new Uint8Array(3))
        c.close()
      },
    }),
  )
const odd = new AudioPreparation(request, '/prepare', {})
await odd.ready
await wait(10)
assert(odd.error, 'incomplete PCM sample must fail explicitly')

let releaseFirst
const orderedReceipts = []
const ordered = Object.create(AudioPreparation.prototype)
ordered.id = 'ordered'
ordered.seq = 0
ordered.lastPlayed = 0
ordered.receiptTail = Promise.resolve()
ordered.heartbeatPending = false
ordered.firstAudioScheduledPending = false
ordered.firstAudioMsPending = undefined
ordered.request = async (_path, options) => {
  orderedReceipts.push(JSON.parse(options.body))
  if (orderedReceipts.length === 1)
    await new Promise((resolve) => {
      releaseFirst = resolve
    })
  return {}
}
const scheduled = ordered.receipt('playing', 0, 1234, true)
const heartbeat = ordered.receipt('playing', 1200)
await ordered.receipt('playing', 1500)
const interrupted = ordered.receipt('interrupted', 1500)
await wait(0)
assert.equal(orderedReceipts.length, 1, 'later receipts wait for the first network response')
releaseFirst()
await Promise.all([scheduled, heartbeat, interrupted])
assert.deepEqual(
  orderedReceipts.map(({ seq, status, played_samples, first_audio_scheduled }) => ({
    seq,
    status,
    played_samples,
    first_audio_scheduled,
  })),
  [
    { seq: 1, status: 'playing', played_samples: 0, first_audio_scheduled: true },
    { seq: 2, status: 'playing', played_samples: 1500, first_audio_scheduled: false },
    { seq: 3, status: 'interrupted', played_samples: 1500, first_audio_scheduled: false },
  ],
  'receipt order keeps the first-audio marker and latest played watermark',
)
const retried = Object.create(AudioPreparation.prototype)
Object.assign(retried, {
  id: 'retried',
  seq: 0,
  lastPlayed: 0,
  receiptTail: Promise.resolve(),
  heartbeatPending: false,
  firstAudioScheduledPending: false,
  firstAudioMsPending: undefined,
})
const attempts = []
retried.request = async (_path, options) => {
  const body = JSON.parse(options.body)
  attempts.push(body)
  if (attempts.length === 1) throw Error('response lost')
  return {}
}
await assert.rejects(retried.receipt('playing', 0, 800, true))
await retried.receipt('playing', 100)
assert.equal(attempts[1].first_audio_scheduled, true)
assert.equal(attempts[1].first_audio_ms, 800)
assert.equal(attempts[1].seq, 2)
console.log(
  '5 audio producer/receipt tests passed: bounded buffer, cancellation, incomplete sample, ordered receipts, marker retry',
)
