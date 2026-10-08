import { useEffect, useRef, useState } from 'react'
import { AudioPreparation } from './audioPreparation'
import type { VisibleEvent } from './stageReading'
import { shouldQueueUtterance } from './voiceQueue'

type Utterance = {
  id: string
  task_id: string
  seq: number
  actor_id: string
  actor_name: string
  epoch: number
  voice: string
  chunks: string[]
  acknowledgement?: boolean
  text_fallback?: boolean
}
type Reply = { epoch: number; utterances: Utterance[] }
type Props = {
  onRelease?: (eventId: string, notice?: string, event?: VisibleEvent) => void
  onEnabledChange?: (enabled: boolean) => void
  autoReplyEventId?: string
  currentEventId?: string
  visibleEventIds?: string[]
  sid: string
  request: <T>(path: string, init?: RequestInit) => Promise<T>
}
type Job = { utterance: Utterance; index: number; replay: boolean }
type Piece = { start: number; samples: number }
const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))

export function VoicePanel({
  sid,
  request,
  currentEventId,
  visibleEventIds,
  autoReplyEventId,
  onRelease,
  onEnabledChange,
}: Props) {
  const callbacks = useRef({ onRelease, onEnabledChange })
  callbacks.current = { onRelease, onEnabledChange }
  const reading = useRef({ currentEventId, visibleEventIds })
  reading.current = { currentEventId, visibleEventIds }
  const [visible, setVisible] = useState(false),
    [available, setAvailable] = useState(false),
    [enabled, setEnabled] = useState(false)
  const [notice, setNotice] = useState(''),
    [speaking, setSpeaking] = useState(false),
    [rows, setRows] = useState<Utterance[]>([])
  const [voices, setVoices] = useState<Record<string, string>>({}),
    [profiles, setProfiles] = useState<Record<string, string>>({})
  const active = useRef(true),
    on = useRef(false),
    seen = useRef(0),
    epoch = useRef<number | null>(null),
    loading = useRef(false)
  const queue = useRef<Job[]>([]),
    working = useRef(false),
    context = useRef<AudioContext | null>(null),
    nodes = useRef<AudioBufferSourceNode[]>([])
  const generation = useRef(0)
  const playing = useRef<{
    id: string
    seq: number
    pieces: Piece[]
    received: number
    acknowledgement: boolean
    stoppedSamples?: number
  } | null>(null)
  const prepared = useRef<Map<string, AudioPreparation>>(new Map()),
    submitted = useRef<number | null>(null),
    seenJobs = useRef(new Set<string>()),
    clientId = useRef(crypto.randomUUID())
  const submittedTask = useRef<string | null>(null),
    pendingInput = useRef(false),
    awaitingFirstAudio = useRef(false)
  const key = (j: Job) => j.utterance.id + ':' + j.index
  async function releaseText(id: string) {
    const reply = await request<{ release?: { event: VisibleEvent } }>(
      `/voice/sessions/${sid}/utterances/${id}/visible`,
      { method: 'POST' },
    )
    callbacks.current.onRelease?.(id, '本段暂用文字呈现，可在菜单中手动重听。', reply.release?.event)
  }
  function prepare(j: Job) {
    const k = key(j)
    let p = prepared.current.get(k)
    if (!p) {
      p = new AudioPreparation(request, `/voice/sessions/${sid}/playbacks`, {
        request_id: crypto.randomUUID(),
        event_id: j.utterance.id,
        chunk_index: j.index,
        epoch: epoch.current,
        replay: j.replay,
        client_id: clientId.current,
        prefetch: true,
      })
      prepared.current.set(k, p)
    }
    return p
  }
  function prime() {
    if (queue.current[0] && prepared.current.size < 2) prepare(queue.current[0])
  }
  function played() {
    const p = playing.current,
      ctx = context.current
    if (!p || !ctx) return 0
    if (p.stoppedSamples !== undefined) return p.stoppedSamples
    return p.pieces.reduce(
      (n, x) => n + Math.min(x.samples, Math.max(0, Math.floor((ctx.currentTime - x.start) * 24000))),
      0,
    )
  }
  async function receipt(status: string) {
    const p = playing.current
    if (!p) return
    const prep = prepared.current.get(p.id)
    if (prep) await prep.receipt(status, played())
  }
  function focus(actorId?: string, active = false) {
    window.dispatchEvent(new CustomEvent('sceneweaver-audio-focus', { detail: { sid, actorId, active } }))
  }
  function stop() {
    window.dispatchEvent(new CustomEvent('sceneweaver-acknowledgement', { detail: { sid, text: '' } }))
    focus()
    generation.current++
    queue.current = []
    if (playing.current) playing.current.stoppedSamples = played()
    void receipt('interrupted').catch(() => {})
    for (const node of nodes.current) {
      try {
        node.stop()
      } catch {}
    }
    nodes.current = []
    for (const prep of prepared.current.values()) prep.cancel()
    prepared.current.clear()
    if (active.current) {
      setSpeaking(false)
      setNotice('播放已停止；字幕和剧情记录保留')
    }
  }
  async function pump() {
    if (working.current) return
    working.current = true
    try {
      while (active.current && queue.current.length) {
        const job = queue.current.shift()!,
          version = generation.current,
          k = key(job),
          prep = prepare(job)
        let timer: ReturnType<typeof setInterval> | undefined
        try {
          const ctx = context.current
          if (!ctx || ctx.state !== 'running') throw Error('请点击开启语音，允许浏览器播放声音')
          setSpeaking(true)
          setNotice('正在准备语音…')
          let timeout: ReturnType<typeof setTimeout> | undefined
          try {
            await Promise.race([
              prep.ready,
              new Promise<never>((_, reject) => {
                timeout = setTimeout(() => reject(Error('语音准备超时，已显示文字')), 8000)
              }),
            ])
          } finally {
            clearTimeout(timeout)
          }
          if (version !== generation.current || prep.controller.signal.aborted) continue
          if (prep.error) throw prep.error
          if (job.utterance.acknowledgement && queue.current.some((j) => !j.utterance.acknowledgement)) {
            prep.cancel()
            continue
          }
          const startedReply = await request<{
            release?: { release_id: string; epoch: number; event: VisibleEvent }
          }>(`/voice/playbacks/${prep.id}/start`, {
            method: 'POST',
            body: JSON.stringify({
              send_to_release_ms:
                job.replay || submitted.current === null || submittedTask.current !== job.utterance.task_id
                  ? undefined
                  : performance.now() - submitted.current,
              output_latency_ms: (ctx.outputLatency || ctx.baseLatency || 0) * 1000,
            }),
          })
          const released = startedReply.release
          if (released && (released.release_id !== job.utterance.id || released.epoch !== epoch.current))
            throw Error('本段播放状态已更新，请重新进入当前剧情')
          if (version !== generation.current || prep.controller.signal.aborted) continue
          playing.current = {
            id: k,
            seq: 0,
            pieces: [],
            received: 0,
            acknowledgement: Boolean(job.utterance.acknowledgement),
          }
          let next = ctx.currentTime + 0.015,
            started = false
          timer = setInterval(() => receipt('playing').catch(() => {}), 500)
          prime()
          while (!prep.done || prep.packets.length) {
            if (version !== generation.current || prep.controller.signal.aborted) break
            if (prep.error) throw prep.error
            const packet = prep.shift()
            if (!packet) {
              await wait(10)
              continue
            }
            while (next - ctx.currentTime > 0.3 && version === generation.current) await wait(10)
            if (version !== generation.current) break
            const length = packet.length / 2,
              buffer = ctx.createBuffer(1, length, 24000),
              channel = buffer.getChannelData(0),
              data = new DataView(packet.buffer, packet.byteOffset, packet.byteLength)
            for (let i = 0; i < length; i++) channel[i] = data.getInt16(i * 2, true) / 32768
            const source = ctx.createBufferSource()
            source.buffer = buffer
            source.connect(ctx.destination)
            const start = Math.max(next, ctx.currentTime + 0.015)
            next = start + length / 24000
            playing.current!.pieces.push({ start, samples: length })
            playing.current!.received += length
            nodes.current.push(source)
            source.onended = () => {
              nodes.current = nodes.current.filter((n) => n !== source)
              source.disconnect()
            }
            source.start(start)
            if (!started) {
              started = true
              if (submittedTask.current === job.utterance.task_id) awaitingFirstAudio.current = false
              window.dispatchEvent(
                new CustomEvent('sceneweaver-acknowledgement', {
                  detail: { sid, text: job.utterance.acknowledgement ? job.utterance.chunks[job.index] : '' },
                }),
              )
              const startGeneration = version
              setTimeout(
                () => {
                  if (active.current && generation.current === startGeneration && playing.current?.id === k) {
                    if (!job.utterance.acknowledgement)
                      callbacks.current.onRelease?.(job.utterance.id, undefined, released?.event)
                    focus(job.utterance.actor_id, true)
                  }
                },
                Math.max(0, (start - ctx.currentTime) * 1000),
              )
              setNotice(`${job.utterance.actor_name}正在朗读`)
              if (!job.replay)
                void prep
                  .receipt(
                    'playing',
                    0,
                    submitted.current !== null && submittedTask.current === job.utterance.task_id
                      ? performance.now() - submitted.current + (start - ctx.currentTime) * 1000
                      : undefined,
                    true,
                  )
                  .catch(() => {})
            }
          }
          if (prep.error) throw prep.error
          while (ctx.currentTime < next && version === generation.current) await wait(20)
          if (version === generation.current) {
            await receipt('completed')
            setNotice('本段播放完成')
          }
        } catch (e) {
          if (version === generation.current && !job.utterance.acknowledgement) {
            await releaseText(job.utterance.id).catch(() => {})
            queue.current = queue.current.filter((j) => j.utterance.id !== job.utterance.id)
          }
          if (playing.current) playing.current.stoppedSamples = played()
          await prep.receipt('failed', played()).catch(() => {})
          for (const node of nodes.current) {
            try {
              node.stop()
            } catch {}
          }
          nodes.current = []
          if (version === generation.current && active.current) setNotice(String(e))
        } finally {
          window.dispatchEvent(new CustomEvent('sceneweaver-acknowledgement', { detail: { sid, text: '' } }))
          if (timer) clearInterval(timer)
          prep.cancel()
          prepared.current.delete(k)
          focus()
          playing.current = null
          if (active.current) setSpeaking(false)
        }
      }
    } finally {
      working.current = false
    }
  }
  async function load(initial = false) {
    if (loading.current) return
    loading.current = true
    try {
      const data = await request<Reply>(`/voice/sessions/${sid}/utterances`)
      if (!active.current) return
      if (epoch.current !== null && epoch.current !== data.epoch) {
        stop()
        seenJobs.current.clear()
      }
      epoch.current = data.epoch
      setRows(data.utterances)
      const selected = reading.current.currentEventId
      const fallback = data.utterances.some((u) => u.id === selected && u.text_fallback)
      if (
        on.current &&
        selected &&
        (fallback || !data.utterances.some((u) => u.id === selected && u.chunks.length))
      )
        callbacks.current.onRelease?.(
          selected,
          fallback ? '本段暂用文字呈现，可在菜单中手动重听。' : undefined,
        )
      if (on.current && fallback && !working.current && !queue.current.length)
        setNotice('本段语音未能准备好，已改用文字；可手动重听。')
      const latest = Math.max(0, ...data.utterances.map((r) => r.seq))
      if (!initial && on.current) {
        for (const u of data.utterances.filter((r) =>
          shouldQueueUtterance(r, {
            epoch: data.epoch,
            seen: seen.current,
            submittedTask: submittedTask.current,
            currentEventId: reading.current.currentEventId,
          }),
        ))
          u.chunks.forEach((_, index) => {
            const job = { utterance: u, index, replay: false },
              k = key(job)
            if (!seenJobs.current.has(k)) {
              seenJobs.current.add(k)
              queue.current.push(job)
            }
          })
        const hasMain = queue.current.some((j) => !j.utterance.acknowledgement)
        if (hasMain) {
          queue.current = queue.current.filter((j) => {
            if (!j.utterance.acknowledgement) return true
            const p = prepared.current.get(key(j))
            p?.cancel()
            prepared.current.delete(key(j))
            return false
          })
          if (playing.current === null)
            for (const [k, p] of prepared.current) {
              if (data.utterances.some((u) => u.acknowledgement && k.startsWith(u.id))) {
                p.cancel()
                prepared.current.delete(k)
              }
            }
        }
        if (working.current) prime()
        if (queue.current.length > 12) {
          stop()
          on.current = false
          sessionStorage.removeItem('voice-enabled:' + sid)
          setEnabled(false)
          callbacks.current.onEnabledChange?.(false)
          setNotice('待播放内容较多，已停止自动朗读，可手动重听')
        }
        void pump()
      }
      seen.current = latest
    } catch (e) {
      if (reading.current.currentEventId) await releaseText(reading.current.currentEventId).catch(() => {})
      if (!initial && active.current) setNotice(String(e))
    } finally {
      loading.current = false
    }
  }
  useEffect(() => {
    active.current = true
    sessionStorage.removeItem('voice-enabled:' + sid)
    request<{ available: boolean; voices: Record<string, string> }>('/voice/config')
      .then(async (config) => {
        const data = await request<Reply>(`/voice/sessions/${sid}/utterances`)
        if (!active.current) return
        setVisible(true)
        setAvailable(config.available)
        setVoices(config.voices)
        setRows(data.utterances)
        epoch.current = data.epoch
        seen.current = Math.max(0, ...data.utterances.map((r) => r.seq))
      })
      .catch(() => {})
    const event = (raw: Event) => {
      const data = (raw as CustomEvent).detail
      if (data.sid !== sid) return
      if (['segment_committed', 'ack_ready'].includes(data.kind)) void load()
      else {
        if (data.kind === 'input_started') {
          submitted.current = performance.now()
          pendingInput.current = true
          awaitingFirstAudio.current = true
          submittedTask.current = null
        } else if (data.kind === 'accepted') {
          if (pendingInput.current) {
            submittedTask.current = data.task_id
            pendingInput.current = false
          } else {
            submitted.current = null
            submittedTask.current = null
          }
        } else {
          submitted.current = null
          submittedTask.current = null
          pendingInput.current = false
          awaitingFirstAudio.current = false
        }
        if (data.kind !== 'accepted') stop()
        // The server can finish the quick acknowledgement during submission.
        // Do not advance the sequence without queuing it at acceptance.
        if (data.kind !== 'input_started') void load(data.kind !== 'accepted')
      }
    }
    const hidden = () => {
      if (document.visibilityState === 'hidden') {
        stop()
        on.current = false
        sessionStorage.removeItem('voice-enabled:' + sid)
        setEnabled(false)
        callbacks.current.onEnabledChange?.(false)
      }
    }
    window.addEventListener('role-voice-event', event)
    const servicesChanged = () => {
      stop()
      on.current = false
      setEnabled(false)
      callbacks.current.onEnabledChange?.(false)
      sessionStorage.removeItem('voice-enabled:' + sid)
      void request<{ available: boolean; voices: Record<string, string> }>('/voice/config')
        .then((config) => {
          if (!active.current) return
          setAvailable(config.available)
          setVoices(config.voices)
          setProfiles({})
        })
        .catch(() => setAvailable(false))
    }
    window.addEventListener('sceneweaver-services-changed', servicesChanged)
    document.addEventListener('visibilitychange', hidden)
    const warmer = setInterval(() => {
      if (on.current && document.visibilityState === 'visible')
        void request(`/voice/sessions/${sid}/warm`, { method: 'POST' }).catch(() => {})
    }, 20000)
    let pollTimer: ReturnType<typeof setTimeout>
    const poll = () => {
      if (document.visibilityState === 'visible') void load()
      const waiting =
        on.current &&
        awaitingFirstAudio.current &&
        submitted.current !== null &&
        performance.now() - submitted.current < 30000
      pollTimer = setTimeout(poll, waiting ? 300 : 2000)
    }
    pollTimer = setTimeout(poll, 2000)
    return () => {
      sessionStorage.removeItem('voice-enabled:' + sid)
      active.current = false
      stop()
      clearTimeout(pollTimer)
      clearInterval(warmer)
      window.removeEventListener('role-voice-event', event)
      window.removeEventListener('sceneweaver-services-changed', servicesChanged)
      document.removeEventListener('visibilitychange', hidden)
      void context.current?.close()
    }
  }, [sid])
  useEffect(() => {
    if (currentEventId !== undefined) {
      if (!(playing.current?.acknowledgement && currentEventId === autoReplyEventId)) stop()
      void load()
    }
  }, [currentEventId])
  async function enable() {
    try {
      if (on.current) {
        on.current = false
        sessionStorage.removeItem('voice-enabled:' + sid)
        setEnabled(false)
        callbacks.current.onEnabledChange?.(false)
        stop()
        return
      }
      context.current ??= new AudioContext()
      await context.current.resume()
      const config = await request<{ available: boolean }>('/voice/config')
      if (!config.available) throw new Error('开启语音前必须配置 TTS API Key、模型与可用音色。')
      await load(true)
      setNotice('正在准备语音连接…')
      await request(`/voice/sessions/${sid}/warm`, { method: 'POST' }).catch(() => {})
      on.current = true
      sessionStorage.setItem('voice-enabled:' + sid, '1')
      setEnabled(true)
      callbacks.current.onEnabledChange?.(true)
      setNotice('已开启语音，将朗读后续新台词')
      if (reading.current.currentEventId !== undefined) void load()
    } catch (e) {
      setNotice(String(e))
    }
  }
  async function replay(u: Utterance) {
    try {
      stop()
      context.current ??= new AudioContext()
      await context.current.resume()
      await load(true)
      queue.current = u.chunks.map((_, index) => ({ utterance: u, index, replay: true }))
      void pump()
    } catch (e) {
      setNotice(String(e))
    }
  }
  const visibleRows = visibleEventIds ? rows.filter((r) => visibleEventIds.includes(r.id)) : rows
  const cast = Object.values(Object.fromEntries(visibleRows.map((r) => [r.actor_id, r])))
  if (!visible) return null
  return (
    <div className="voice-panel">
      <button disabled={!available} onClick={() => void enable()}>
        {enabled ? '关闭自动朗读' : '开启语音'}
      </button>
      <button
        disabled={!speaking && !queue.current.length}
        onClick={() => {
          stop()
          if (reading.current.currentEventId) callbacks.current.onRelease?.(reading.current.currentEventId)
        }}
      >
        停止播放
      </button>
      <small>
        {available
          ? '字幕为主 · 使用已配置的语音服务'
          : '开启语音需要配置 TTS API Key、模型与音色；文字对话不受影响'}
      </small>
      <details>
        <summary>音色与重听</summary>
        {cast.map((actor) => (
          <label className="field" key={actor.actor_id}>
            {actor.actor_name}
            <select
              aria-label={`${actor.actor_name}音色`}
              value={profiles[actor.actor_id] || actor.voice}
              onChange={(e) => {
                const voice = e.target.value
                request(`/voice/actors/${actor.actor_id}/profile`, {
                  method: 'PUT',
                  body: JSON.stringify({ voice }),
                })
                  .then(() => setProfiles((p) => ({ ...p, [actor.actor_id]: voice })))
                  .catch((e) => setNotice(String(e)))
              }}
            >
              {Object.entries(voices).map(([id, name]) => (
                <option key={id} value={id}>
                  {name}
                </option>
              ))}
            </select>
          </label>
        ))}
        {visibleRows
          .filter((r) => r.chunks.length)
          .slice(-6)
          .map((row) => (
            <p key={row.id}>
              {row.actor_name}：{row.chunks.join('').slice(0, 70)}{' '}
              <button disabled={!available} onClick={() => void replay(row)}>
                重听
              </button>
            </p>
          ))}
      </details>
      {notice && <p role="status">{notice}</p>}
    </div>
  )
}
