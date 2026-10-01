import { useEffect, useRef, useState } from 'react'
import { VoicePanel } from './VoicePanel'
import { VideoPlayer } from './VideoPlayer'
import { ServiceReadiness } from './ServiceReadiness'
import { performanceSprite } from './outfitModel'
import { StagePlayer, type StagePerson } from './StagePlayer'
import type { SceneObject } from './sceneObjectModel'
import {
  firstReplyLine,
  holdingPlayerTurn,
  readingLines,
  speakingFocus,
  type VisibleEvent,
} from './stageReading'
import { ItemInventory, type DetailedInventory } from './ItemInventory'
import { useItemHandoff } from './useItemHandoff'
import { HandoffItem } from './HandoffItem'
import type { InventorySnapshot } from './itemHandoff'
import { lineFingerprint, parseProgress, restoreProgress, type ReadingProgress } from './readingProgress'
import type { Communication } from './PlayerBar'
import { readingWriter, type SavedReading } from './readingStore'

type Request = (path: string, init?: RequestInit) => Promise<unknown>
type Frame = {
  enabled: boolean
  ending?: boolean
  finished_at?: string | null
  node_id?: string
  entry_event_id?: string
  mode?: string
  auto_status?: 'running' | 'error' | 'stalled'
  title: string
  text: string
  background?: string
  visual_mode?: 'stage' | 'cg' | 'video'
  video?: { asset: string; allow_skip: boolean } | null
  revision: number
  auto_ready?: { through_seq: number } | null
  execution?: {
    phase: 'opening' | 'interactive' | 'finished' | 'video'
    index: number
    version: number
    has_next: boolean
    event_id: string
  }
  choices: { id: string; label: string }[]
}
type Settings = { paused: boolean; [key: string]: unknown }
type PlayerSnapshot = {
  epoch: number
  frame: Frame
  settings: Settings
  events: VisibleEvent[]
  characters: StagePerson[]
  objects?: SceneObject[]
  inventory: InventorySnapshot & DetailedInventory
  interaction: { allowed: boolean; reason: string; responding: boolean }
  history_truncated: boolean
  history_more_after: boolean
  history_next_anchor?: string | null
}
export function NovelPlay({
  sid,
  request,
  input,
  onInput,
  onSend,
  busy: clientBusy,
  error,
  communication,
  onCommunication,
  onReady,
  onLibrary,
  returnToEditor = false,
  standalone = false,
  onLab,
  onTask,
}: {
  sid: string
  request: Request
  input: string
  onInput: (text: string) => void
  onSend: () => void
  busy: boolean
  error: string
  communication: Communication
  onCommunication: (value: Communication) => void
  onReady: (ready: boolean) => void
  onLibrary: () => void
  returnToEditor?: boolean
  standalone?: boolean
  onLab: () => void
  onTask: (taskId: string) => void
}) {
  // A published game has no author surface, even if a caller supplies the shared component props.
  const playerOnly = import.meta.env.MODE === 'game' ? true : standalone
  const [story, setStory] = useState(''),
    [frame, setFrame] = useState<Frame | null>(null),
    [settings, setSettings] = useState<Settings | null>(null)
  const [sceneObjects, setSceneObjects] = useState<SceneObject[]>([])
  const [people, setPeople] = useState<StagePerson[]>([]),
    [events, setEvents] = useState<VisibleEvent[]>([])
  const [selected, setSelected] = useState(''),
    [panel, setPanel] = useState(''),
    [failure, setFailure] = useState(''),
    [working, setWorking] = useState(false)
  const [readThrough, setReadThrough] = useState('')
  const [snapshot, setSnapshot] = useState<PlayerSnapshot | null>(null)
  const presenceClient = useRef(crypto.randomUUID())
  const busy = clientBusy || Boolean(snapshot?.interaction.responding)
  const [progressReady, setProgressReady] = useState(false)
  const [progressNotice, setProgressNotice] = useState('')
  const savedProgress = useRef<ReadingProgress | null>(null)
  const saveProgress = useRef<((value: ReadingProgress) => Promise<void>) | null>(null)
  const [progressSaving, setProgressSaving] = useState(false)
  const saveSequence = useRef(0)
  const restoredEntry = useRef<string | null>(null)
  const emptyHistoryAnchor = useRef('')
  const followReply = useRef<{ after: number; entry: string } | null>(null)
  const [autoReplyEventId, setAutoReplyEventId] = useState('')
  const lastReplyScroll = useRef('')
  const refreshRef = useRef<() => Promise<void>>(async () => {})
  const [acknowledgement, setAcknowledgement] = useState('')
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined
    const receive = (raw: Event) => {
      const data = (raw as CustomEvent).detail
      if (data?.sid !== sid) return
      if (['segment_committed', 'ack_ready'].includes(data.kind)) {
        clearTimeout(timer)
        timer = setTimeout(() => void refreshRef.current(), 50)
      }
      if (['cancelled', 'failed'].includes(data.kind)) followReply.current = null
    }
    const ack = (raw: Event) => {
      const data = (raw as CustomEvent).detail
      if (data?.sid === sid) setAcknowledgement(data.text || '')
    }
    window.addEventListener('role-voice-event', receive)
    window.addEventListener('sceneweaver-acknowledgement', ack)
    return () => {
      clearTimeout(timer)
      window.removeEventListener('role-voice-event', receive)
      window.removeEventListener('sceneweaver-acknowledgement', ack)
    }
  }, [sid])
  const [audioFocus, setAudioFocus] = useState<{ actorId?: string; active: boolean } | null>(null)
  const [voiceEnabled, setVoiceEnabled] = useState(false)
  const [releasedAudio, setReleasedAudio] = useState<Set<string>>(() => new Set())
  const [presentationNotice, setPresentationNotice] = useState<{ id: string; text: string } | null>(null)
  useEffect(() => {
    setVoiceEnabled(false)
    setReleasedAudio(new Set())
  }, [sid])
  useEffect(() => {
    setAudioFocus(null)
    const receive = (event: Event) => {
      const detail = (event as CustomEvent).detail
      if (detail?.sid === sid) setAudioFocus(detail.active ? { actorId: detail.actorId, active: true } : null)
    }
    window.addEventListener('sceneweaver-audio-focus', receive)
    return () => window.removeEventListener('sceneweaver-audio-focus', receive)
  }, [sid])
  const lines = readingLines(events),
    index = Math.max(
      0,
      lines.findIndex((l) => l.id === selected),
    ),
    line = lines[index]
  const lineEventId = line?.id.replace(/:\d+$/, '') || ''
  const waitingForVoice = Boolean(
    line?.presentationPending ||
    (voiceEnabled && line?.actorId && line.actorId !== sid && !releasedAudio.has(lineEventId)),
  )
  const displayedLine = waitingForVoice ? undefined : line
  useEffect(() => {
    if (!playerOnly || !autoReplyEventId || lineEventId !== autoReplyEventId || panel) return
    if (lastReplyScroll.current === autoReplyEventId) return
    lastReplyScroll.current = autoReplyEventId
    window.scrollTo({ top: 0, behavior: 'auto' })
  }, [playerOnly, autoReplyEventId, lineEventId, panel])
  const readingRef = useRef<ReadingProgress | null>(null)
  readingRef.current =
    frame && line && !line.presentationPending
      ? {
          version: 1,
          entry: frame.entry_event_id || '',
          selected: line.id,
          acknowledged: readThrough === line.id,
          fingerprint: lineFingerprint(line),
        }
      : null
  const unread = index < lines.length - 1,
    awaitingRead = Boolean(line && readThrough !== line.id),
    backstage = frame?.mode === 'backstage',
    opening = frame?.execution?.phase === 'opening',
    videoActive = frame?.execution?.phase === 'video'
  const blocked =
    !snapshot?.interaction.allowed ||
    !progressReady ||
    snapshot?.history_more_after ||
    busy ||
    working ||
    !settings ||
    settings.paused ||
    backstage ||
    opening ||
    unread ||
    awaitingRead ||
    panel !== ''
  const itemBlockedReason = !progressReady
    ? '正在恢复阅读位置。'
    : busy || working
      ? '请等待当前回应完成。'
      : !settings || !frame
        ? '正在读取场景。'
        : settings.paused
          ? '继续游玩后才能递交。'
          : backstage
            ? '当前不在玩家场景。'
            : opening || unread || awaitingRead || snapshot?.history_more_after
              ? '读完当前剧情后才能递交。'
              : input.trim()
                ? '请先发送或清空正在输入的内容。'
                : snapshot?.interaction.reason || ''
  const handoff = useItemHandoff({
    actorId: sid,
    request,
    open: panel === 'items',
    blocked: Boolean(itemBlockedReason),
    supplied: snapshot?.inventory || null,
    onTask: (id) => {
      setPanel('')
      onTask(id)
    },
  })
  const presence = useRef({ visible: false, typing: true, voice_enabled: false })
  const attemptedAuto = useRef('')
  useEffect(() => {
    const ready = frame?.auto_ready
    if (
      !ready ||
      !progressReady ||
      progressSaving ||
      !snapshot?.interaction.allowed ||
      snapshot?.history_more_after ||
      busy ||
      working ||
      !settings ||
      settings.paused ||
      unread ||
      awaitingRead ||
      panel ||
      !story ||
      !line
    )
      return
    const key = `${frame.node_id}:${ready.through_seq}`
    if (attemptedAuto.current === key) return
    const lastSeq = events.reduce((n, event) => Math.max(n, event.seq), 0)
    if (lastSeq < ready.through_seq) return
    attemptedAuto.current = key
    setWorking(true)
    void request(`/role/experiments/stories/${story}/narrative/auto-continue`, {
      method: 'POST',
      body: JSON.stringify({ through_seq: ready.through_seq }),
    })
      .then(async (raw) => {
        await refreshRef.current()
        const next = raw as Frame
        if (next.entry_event_id) setSelected(next.entry_event_id + ':0')
      })
      .catch((e) => setFailure(String(e)))
      .finally(() => setWorking(false))
  }, [
    frame,
    busy,
    working,
    settings,
    unread,
    awaitingRead,
    panel,
    story,
    line?.id,
    progressReady,
    progressSaving,
  ])
  presence.current = {
    visible: document.visibilityState === 'visible',
    voice_enabled: voiceEnabled,
    typing:
      !progressReady ||
      holdingPlayerTurn(
        input,
        busy,
        unread || awaitingRead || Boolean(panel) || Boolean(snapshot?.history_more_after),
      ),
  }
  useEffect(() => {
    if (!progressReady || !line || selected !== line.id || !saveProgress.current || !frame) return
    const value: ReadingProgress = {
      version: 1,
      entry: frame.entry_event_id || '',
      selected: line.id,
      acknowledged: readThrough === line.id,
      fingerprint: lineFingerprint(line),
    }
    savedProgress.current = value
    const sequence = ++saveSequence.current
    setProgressSaving(true)
    void saveProgress
      .current(value)
      .then(() => {
        if (saveSequence.current === sequence) {
          setProgressSaving(false)
        }
      })
      .catch(() => {
        if (saveSequence.current === sequence)
          setProgressNotice('阅读位置尚未确认保存，已暂停自动推进。请重新进入试玩后继续。')
      })
  }, [progressReady, selected, readThrough, line?.text, line?.speaker, frame?.entry_event_id])
  useEffect(() => {
    onReady(!blocked)
  }, [blocked])
  useEffect(() => {
    if (communication.addressees.some((id) => !people.some((p) => p.id === id))) {
      onCommunication({ channel: 'public', addressees: [] })
    }
  }, [people, communication])
  useEffect(() => {
    let active = true
    let timer: ReturnType<typeof setTimeout>
    let id = ''
    let generation = 0
    onReady(false)
    setProgressReady(false)
    followReply.current = null
    saveProgress.current = null
    savedProgress.current = null
    saveSequence.current++
    setProgressSaving(false)
    setProgressNotice('')
    restoredEntry.current = null
    onCommunication({ channel: 'public', addressees: [] })
    async function refresh() {
      if (!id) return
      const current = ++generation
      try {
        let bookmark = readingRef.current
        if (!bookmark) bookmark = savedProgress.current
        const anchor = bookmark?.selected.split(':')[0] || emptyHistoryAnchor.current
        const next = (await request(
          `/role/players/${sid}/snapshot?audio=${presence.current.voice_enabled}${anchor ? `&anchor=${encodeURIComponent(anchor)}` : ''}`,
        )) as PlayerSnapshot
        const f = next.frame,
          c = next.settings,
          t = { events: next.events },
          p = { characters: next.characters }
        if (active && current === generation) {
          const entry = f.entry_event_id || ''
          const restoring = restoredEntry.current !== entry
          let saved: ReadingProgress | null = readingRef.current
          if (restoring) {
            saved = savedProgress.current
          }
          if (!active || current !== generation) return
          if (restoring) {
            const restored = restoreProgress(readingLines(t.events), entry, saved)
            setSelected(restored.selected)
            setReadThrough(restored.readThrough)
            if (restored.notice) setProgressNotice(restored.notice)
            restoredEntry.current = entry
            setProgressReady(true)
          } else {
            const latest = readingRef.current
            const checked = restoreProgress(readingLines(t.events), entry, latest)
            if (checked.notice) {
              setSelected(checked.selected)
              setReadThrough('')
              setProgressNotice(checked.notice)
            }
            const follow = followReply.current
            if (follow && follow.entry === entry) {
              const reply = firstReplyLine(t.events, follow.after, sid)
              if (reply) {
                setAutoReplyEventId(reply.id.replace(/:\d+$/, ''))
                setSelected(reply.id)
                setReadThrough('')
                followReply.current = null
              }
            } else if (follow) followReply.current = null
          }
          setFrame(f)
          setSnapshot(next)
          setSettings(c)
          setEvents(t.events)
          setPeople(p.characters)
          setSceneObjects(next.objects || [])
          setFailure('')
        }
      } catch (e) {
        if (active && current === generation) {
          setFailure(String(e))
          setSettings(null)
          setSnapshot(null)
          onReady(false)
        }
      }
    }
    refreshRef.current = refresh
    const pulse = () => {
      if (id)
        void request(`/role/stories/${id}/presence`, {
          method: 'POST',
          body: JSON.stringify({
            ...presence.current,
            client_id: presenceClient.current,
            visible: document.visibilityState === 'visible',
          }),
        }).catch(() => {})
    }
    async function poll() {
      await refresh()
      if (active) {
        pulse()
        timer = setTimeout(() => void poll(), 2500)
      }
    }
    void request(`/sessions/${sid}`)
      .then(async (raw) => {
        if (!active) return
        const session = raw as { story_id: string; epoch: number }
        id = session.story_id
        const path = `/role/players/${sid}/reading`
        const saved = (await request(path)) as SavedReading
        if (!active) return
        savedProgress.current = parseProgress(JSON.stringify(saved.progress))
        saveProgress.current = readingWriter(request, path, saved)
        setStory(id)
        await poll()
      })
      .catch((e) => setFailure(String(e)))
    document.addEventListener('visibilitychange', pulse)
    return () => {
      active = false
      saveSequence.current++
      saveProgress.current = null
      clearTimeout(timer)
      document.removeEventListener('visibilitychange', pulse)
      if (id)
        void request(`/role/stories/${id}/presence`, {
          method: 'POST',
          body: JSON.stringify({ visible: false, typing: false, client_id: presenceClient.current }),
        }).catch(() => {})
      onReady(false)
    }
  }, [sid])
  useEffect(() => {
    if (!story) return
    void request(`/role/stories/${story}/presence`, {
      method: 'POST',
      body: JSON.stringify({ ...presence.current, client_id: presenceClient.current }),
    }).catch(() => {})
  }, [story, unread, awaitingRead, panel, busy, input, voiceEnabled])
  async function toggle() {
    if (!settings || backstage) return
    setWorking(true)
    try {
      await request(`/role/stories/${story}/settings`, {
        method: 'PUT',
        body: JSON.stringify({ ...settings, paused: !settings.paused }),
      })
      if (!settings.paused)
        window.dispatchEvent(new CustomEvent('role-voice-event', { detail: { sid, kind: 'paused' } }))
      await refreshRef.current()
    } catch (e) {
      setFailure(String(e))
    } finally {
      setWorking(false)
    }
  }
  async function retryBackstage() {
    setWorking(true)
    try {
      await request('/game/backstage/retry', { method: 'POST' })
      await refreshRef.current()
    } catch (e) {
      setFailure(String(e))
    } finally {
      setWorking(false)
    }
  }
  async function choose(edge: string) {
    if (!frame || blocked) return
    setWorking(true)
    onReady(false)
    try {
      await request(`/role/experiments/stories/${story}/narrative/choose`, {
        method: 'POST',
        body: JSON.stringify({
          request_id: crypto.randomUUID(),
          expected_revision: frame.revision,
          edge_id: edge,
        }),
      })
      await refreshRef.current()
    } catch (e) {
      setFailure(String(e))
    } finally {
      setWorking(false)
    }
  }
  const advancing = useRef(false)
  async function advanceVideo(outcome: 'ended' | 'skipped') {
    if (!frame?.execution || !videoActive || settings?.paused) throw new Error('当前不能推进片段。')
    await request(`/role/experiments/stories/${story}/narrative/advance`, {
      method: 'POST',
      body: JSON.stringify({
        request_id: `video:${frame.node_id}:${frame.execution.version}`,
        node_id: frame.node_id,
        expected_version: frame.execution.version,
        video_result: outcome,
      }),
    })
    await refreshRef.current()
  }
  async function advanceOpening() {
    if (advancing.current || !frame?.execution || !opening || busy || settings?.paused) return
    advancing.current = true
    setWorking(true)
    try {
      const result = (await request(`/role/experiments/stories/${story}/narrative/advance`, {
        method: 'POST',
        body: JSON.stringify({
          request_id: `opening:${frame.node_id}:${frame.execution.version}`,
          node_id: frame.node_id,
          expected_version: frame.execution.version,
        }),
      })) as Frame
      if (result.execution?.phase === 'opening') setSelected(result.execution.event_id + ':0')
      await refreshRef.current()
    } catch (e) {
      setFailure(String(e))
    } finally {
      advancing.current = false
      setWorking(false)
    }
  }
  return (
    <div className="vn-app">
      <header className="vn-toolbar">
        <button disabled={handoff.pending} onClick={onLibrary}>
          {playerOnly ? '主菜单' : returnToEditor ? '返回编辑' : '作品库'}
        </button>
        <span>{frame?.title || '读取故事…'}</span>
        <button disabled={handoff.pending} onClick={() => setPanel(panel === 'history' ? '' : 'history')}>
          回看
        </button>
        <button disabled={handoff.pending} onClick={() => setPanel(panel === 'items' ? '' : 'items')}>
          随身物品
        </button>
        <button disabled={handoff.pending} onClick={() => setPanel(panel === 'menu' ? '' : 'menu')}>
          菜单
        </button>
      </header>
      {videoActive && frame?.node_id && frame.video ? (
        <VideoPlayer
          key={frame.entry_event_id}
          actor={sid}
          node={frame.node_id}
          title={frame.title}
          epoch={snapshot?.epoch || 0}
          paused={!!settings?.paused}
          allowSkip={frame.video.allow_skip}
          request={request}
          onToggle={toggle}
          onComplete={advanceVideo}
        />
      ) : (
        <StagePlayer
          objects={sceneObjects}
          onObjectSelect={() => setPanel('item-details')}
          title={frame?.title || '当前场景'}
          background={backstage ? undefined : frame?.background}
          visualMode={frame?.visual_mode === 'cg' ? 'cg' : 'stage'}
          focusActorId={
            backstage
              ? null
              : speakingFocus(
                  displayedLine,
                  people.map((p) => p.id),
                  audioFocus,
                )
          }
          people={
            backstage
              ? []
              : people.map((p) =>
                  p.id === displayedLine?.actorId
                    ? {
                        ...p,
                        base_sprite:
                          displayedLine.appearance?.sprite ||
                          performanceSprite(p.variants, displayedLine.performance) ||
                          p.base_sprite,
                      }
                    : p,
                )
          }
          line={
            backstage
              ? {
                  id: 'backstage',
                  speaker: '旁白',
                  text: playerOnly ? '另一处的故事尚在进行。' : '另一处的故事尚在进行。这一段暂由作者推进。',
                }
              : displayedLine
          }
          emptyText={waitingForVoice ? '正在准备播放…' : undefined}
          request={request}
          onSelect={
            backstage || frame?.finished_at
              ? undefined
              : (id) => {
                  if (handoff.selected) void handoff.deliver(id)
                  else onCommunication({ channel: 'public', addressees: [id] })
                }
          }
          itemTarget={
            handoff.selected && !frame?.finished_at
              ? (id) => {
                  const plan = handoff.plan(id)
                  return { label: plan.label, enabled: !handoff.unavailable && plan.kind !== 'blocked' }
                }
              : undefined
          }
          tray={
            panel === 'items' && (
              <section
                className="vn-item-tray"
                aria-label="递交物品栏"
                onKeyDown={(e) => {
                  if (e.key === 'Escape') handoff.setSelected('')
                }}
              >
                <div className="vn-item-heading">
                  <strong>随身物品</strong>
                  <button
                    disabled={handoff.pending}
                    onClick={() => {
                      handoff.setSelected('')
                      setPanel('item-details')
                    }}
                  >
                    物品详情与交接事项
                  </button>
                  <button disabled={handoff.pending} onClick={() => setPanel('')}>
                    收起物品栏
                  </button>
                </div>
                <p>拖到人物身上，或先点物品、再点人物。未同意时仅提出递交；双方同意后再次操作才交出。</p>
                <div className="vn-item-cards">
                  {handoff.snapshot.items
                    .filter((i) => i.holder_id === sid)
                    .map((item) => (
                      <HandoffItem
                        key={item.id}
                        name={item.name}
                        disabled={handoff.unavailable}
                        selected={handoff.selected === item.id}
                        onDragSelect={() => handoff.setSelected(item.id)}
                        onCancel={() => handoff.setSelected('')}
                        onSelect={() => handoff.setSelected(handoff.selected === item.id ? '' : item.id)}
                        onDrop={(id) => void handoff.deliver(id, item.id)}
                      />
                    ))}
                  {handoff.loaded && !handoff.snapshot.items.some((i) => i.holder_id === sid) && (
                    <span>当前没有随身物品。</span>
                  )}
                </div>
                {handoff.selected && <button onClick={() => handoff.setSelected('')}>取消选择</button>}
                <p role="status">
                  {itemBlockedReason ||
                    handoff.message ||
                    (!handoff.loaded
                      ? '正在读取物品…'
                      : handoff.selected
                        ? '选择人物，查看并执行对应操作。'
                        : '对方可以拒绝；放到舞台空白处不会交出物品。')}
                </p>
              </section>
            )
          }
        >
          {!backstage && (unread || awaitingRead || snapshot?.history_more_after) && (
            <button
              className="vn-next"
              disabled={!progressReady || waitingForVoice}
              onClick={() => {
                if (unread) setSelected(lines[index + 1].id)
                else if (line && awaitingRead) setReadThrough(line.id)
                else {
                  if (!line && snapshot?.history_next_anchor)
                    emptyHistoryAnchor.current = snapshot.history_next_anchor
                  void refreshRef.current()
                }
              }}
            >
              继续阅读 ▸
            </button>
          )}
          {busy && <span role="status">人物正在回应…</span>}
        </StagePlayer>
      )}
      {acknowledgement && (
        <p role="status" aria-label="人物接话">
          {acknowledgement}
        </p>
      )}
      {presentationNotice?.id === lineEventId && <p role="status">{presentationNotice.text}</p>}
      <div hidden={panel !== 'menu'} aria-label="语音设置">
        {panel === 'menu' && <ServiceReadiness request={request} />}
        <VoicePanel
          autoReplyEventId={videoActive ? undefined : autoReplyEventId}
          onRelease={(id, notice, event) => {
            if (event) setEvents((old) => old.map((row) => (row.id === id ? event : row)))
            setReleasedAudio((old) => new Set([...old, id]))
            if (notice) setPresentationNotice({ id, text: notice })
          }}
          onEnabledChange={(enabled) => {
            if (enabled && lineEventId) setReleasedAudio((old) => new Set([...old, lineEventId]))
            setVoiceEnabled(enabled)
          }}
          sid={sid}
          request={request as <T>(path: string, init?: RequestInit) => Promise<T>}
          currentEventId={backstage || videoActive ? '' : line?.id.replace(/:\d+$/, '') || ''}
          visibleEventIds={videoActive ? [] : lines.slice(0, index + 1).map((l) => l.id.replace(/:\d+$/, ''))}
        />
      </div>
      {panel && panel !== 'items' && (
        <section
          className="vn-drawer"
          aria-label={panel === 'history' ? '对白回看' : panel === 'item-details' ? '随身物品' : '游戏菜单'}
        >
          <button onClick={() => setPanel('')}>关闭</button>
          {panel === 'history' && (
            <>
              <h2>已读对白</h2>
              {lines
                .slice(0, index + 1)
                .filter((l) => !waitingForVoice || l.id !== line?.id)
                .map((l) => (
                  <p key={l.id}>
                    <b>{l.speaker}：</b>
                    {l.text}
                  </p>
                ))}
            </>
          )}
          {panel === 'item-details' && (
            <ItemInventory
              actorId={sid}
              request={request}
              disabledReason={itemBlockedReason}
              supplied={snapshot?.inventory || null}
              onRefresh={() => refreshRef.current()}
              onTask={(id) => {
                setPanel('')
                onTask(id)
              }}
            />
          )}
          {panel === 'menu' && (
            <>
              <h2>本次游玩</h2>
              <p>当前进度由服务器保存；语音只朗读当前读到的事件，可在语音设置中开启或停止。</p>
              <p>阅读位置自动保存在当前运行中，重新进入时会恢复。</p>
              <button disabled={working || backstage || !!frame?.finished_at} onClick={() => void toggle()}>
                {frame?.finished_at ? '故事已结束' : settings?.paused ? '继续游玩' : '暂停游玩'}
              </button>
              {!playerOnly && <button onClick={onLab}>打开作者诊断（含剧透）</button>}
              <button onClick={onLibrary}>
                {playerOnly ? '返回主菜单' : returnToEditor ? '返回编辑' : '返回作品库与创作'}
              </button>
            </>
          )}
        </section>
      )}
      {!panel && !videoActive && (
        <section className="vn-interaction" aria-label="当前可做的事">
          {backstage ? (
            playerOnly ? (
              <div>
                <p>
                  {frame?.auto_status === 'error'
                    ? '另一处的剧情暂时中断。已发生的事会保留。'
                    : frame?.auto_status === 'stalled'
                      ? '另一处的剧情无法继续。请返回主菜单另开存档，或联系故事创作者。'
                      : '另一处的故事尚在进行。'}
                </p>
                {frame?.auto_status === 'error' && (
                  <button disabled={working} onClick={() => void retryBackstage()}>
                    重试幕后演出
                  </button>
                )}
              </div>
            ) : (
              <button onClick={onLab}>作者推进入口（含剧透）</button>
            )
          ) : (
            <>
              {settings?.paused && (
                <>
                  <button disabled={working} onClick={() => void toggle()}>
                    开始 / 继续游玩
                  </button>
                  <p>
                    当前已暂停。继续游玩后可推进后续段落；固定开场不调用模型，进入自由互动后人物可能产生模型请求。
                  </p>
                </>
              )}
              {!unread && !awaitingRead && opening && (
                <button
                  disabled={working || busy || !settings || settings.paused}
                  onClick={() => void advanceOpening()}
                >
                  {frame?.execution?.has_next
                    ? '推进下一段剧情'
                    : frame?.ending
                      ? '结束故事'
                      : '进入自由互动'}
                </button>
              )}
              {frame?.finished_at && (
                <p role="status">故事已结束。你可以回看剧情，或返回主菜单开始新的游玩。</p>
              )}
              {!opening && !videoActive && !frame?.finished_at && (
                <>
                  <div className="vn-choices">
                    {frame?.choices?.map((c) => (
                      <button key={c.id} disabled={blocked} onClick={() => void choose(c.id)}>
                        {c.label}
                      </button>
                    ))}
                  </div>
                  <div className="vn-address">
                    <span>交谈对象</span>
                    <button
                      aria-pressed={!communication.addressees.length}
                      onClick={() => onCommunication({ channel: 'public', addressees: [] })}
                    >
                      在场人物
                    </button>
                    {people.map((p) => (
                      <button
                        key={p.id}
                        aria-pressed={communication.addressees.includes(p.id)}
                        onClick={() => onCommunication({ channel: 'public', addressees: [p.id] })}
                      >
                        {p.name}
                      </button>
                    ))}
                  </div>
                  <form
                    onSubmit={(e) => {
                      e.preventDefault()
                      if (!blocked && input.trim()) {
                        followReply.current = {
                          after: Math.max(0, ...events.map((event) => event.seq)),
                          entry: frame?.entry_event_id || '',
                        }
                        onSend()
                      }
                    }}
                  >
                    <label htmlFor="vn-input">说些什么，或描述你的行动</label>
                    {(busy || working || unread || awaitingRead) && (
                      <p role="status">可以先写下想说的话，读完当前剧情后再发送。</p>
                    )}
                    <div className="vn-input">
                      <textarea
                        id="vn-input"
                        value={input}
                        onChange={(e) => onInput(e.target.value)}
                        rows={2}
                        placeholder="用自己的话参与故事…"
                      />
                      <button disabled={blocked || !input.trim()} type="submit">
                        发言
                      </button>
                    </div>
                  </form>
                </>
              )}
            </>
          )}
        </section>
      )}
      {(failure || error) && (
        <p className="vn-error" role="alert">
          {failure || error}
          <button
            onClick={() => {
              attemptedAuto.current = ''
              void refreshRef.current()
            }}
          >
            重新读取
          </button>
        </p>
      )}
      {progressNotice && (
        <p className="vn-error" role="status">
          {progressNotice}
          <button onClick={() => setProgressNotice('')}>知道了</button>
        </p>
      )}
    </div>
  )
}
