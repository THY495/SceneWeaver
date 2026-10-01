// Bounded producer; downloading never means that the user heard the utterance.
export type RequestJSON = <T>(path: string, init?: RequestInit) => Promise<T>
export class AudioPreparation {
  id = ''
  seq = 0
  done = false
  error: unknown = null
  received = 0
  buffered = 0
  private lastPlayed = 0
  private receiptTail: Promise<void> = Promise.resolve()
  private heartbeatPending = false
  private firstAudioScheduledPending = false
  private firstAudioMsPending: number | undefined
  packets: Uint8Array[] = []
  controller = new AbortController()
  ready: Promise<void>
  private wake!: () => void
  constructor(
    private request: RequestJSON,
    private path: string,
    private body: object,
  ) {
    this.ready = new Promise((resolve) => {
      this.wake = resolve
    })
    void this.run()
  }
  async receipt(status: string, played_samples = 0, first_audio_ms?: number, first_audio_scheduled = false) {
    played_samples = Math.max(this.lastPlayed, played_samples)
    this.lastPlayed = played_samples
    if (first_audio_scheduled) this.firstAudioScheduledPending = true
    if (first_audio_ms !== undefined && this.firstAudioMsPending === undefined)
      this.firstAudioMsPending = first_audio_ms
    if (!this.id) return
    const heartbeat = status === 'playing' && first_audio_ms === undefined && !first_audio_scheduled
    if (heartbeat && this.heartbeatPending) return
    if (heartbeat) this.heartbeatPending = true
    const send = this.receiptTail.then(async () => {
      try {
        const scheduled = this.firstAudioScheduledPending
        const latency = this.firstAudioMsPending
        await this.request(`/voice/playbacks/${this.id}/receipt`, {
          method: 'POST',
          body: JSON.stringify({
            seq: ++this.seq,
            played_samples: heartbeat ? this.lastPlayed : played_samples,
            status,
            first_audio_ms: latency,
            first_audio_scheduled: scheduled,
          }),
        })
        if (scheduled) this.firstAudioScheduledPending = false
        if (latency !== undefined) this.firstAudioMsPending = undefined
      } finally {
        if (heartbeat) this.heartbeatPending = false
      }
    })
    this.receiptTail = send.catch(() => {})
    await send
  }
  cancel() {
    this.controller.abort()
    this.packets = []
    this.buffered = 0
    this.wake()
    void this.receipt('interrupted').catch(() => {})
  }
  private async run() {
    try {
      const p = await this.request<{ id: string }>(this.path, {
        method: 'POST',
        body: JSON.stringify(this.body),
      })
      this.id = p.id
      if (this.controller.signal.aborted) {
        await this.receipt('interrupted')
        return
      }
      const token = sessionStorage.getItem('continuum-token')
      const base =
        typeof location === 'undefined'
          ? ''
          : /^\/runs\/[a-f0-9-]{36}(?=\/|$)/.exec(location.pathname)?.[0] || ''
      const r = await fetch(`${base}/api/voice/playbacks/${this.id}/audio`, {
        method: 'POST',
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        signal: this.controller.signal,
      })
      if (!r.ok) throw Error(`语音请求失败 (${r.status})`)
      const reader = r.body!.getReader()
      let carry = new Uint8Array(0)
      while (true) {
        if (this.controller.signal.aborted) throw Error('cancelled')
        while (this.buffered >= 48000 && !this.controller.signal.aborted)
          await new Promise((resolve) => setTimeout(resolve, 20))
        const { value, done } = await reader.read()
        if (done) break
        const joined = new Uint8Array(carry.length + value.length)
        joined.set(carry)
        joined.set(value, carry.length)
        const end = joined.length - (joined.length % 2)
        carry = joined.slice(end)
        for (let i = 0; i < end; i += 9600) {
          while (this.buffered >= 48000 && !this.controller.signal.aborted)
            await new Promise((resolve) => setTimeout(resolve, 20))
          if (this.controller.signal.aborted) throw Error('cancelled')
          const packet = joined.slice(i, Math.min(end, i + 9600))
          this.packets.push(packet)
          this.buffered += packet.length
          this.received += packet.length / 2
          this.wake()
        }
      }
      if (carry.length) throw Error('音频采样不完整')
      const state = await this.request<{ stream_done: boolean; sent_samples: number }>(
        `/voice/playbacks/${this.id}`,
      )
      if (!state.stream_done || !this.received || state.sent_samples !== this.received)
        throw Error('语音合成未完成，文字回答仍然有效')
    } catch (e) {
      this.error = e
      if (!this.controller.signal.aborted) await this.receipt('failed').catch(() => {})
    } finally {
      this.done = true
      this.wake()
    }
  }
  shift() {
    const p = this.packets.shift()
    if (p) this.buffered -= p.length
    return p
  }
}
