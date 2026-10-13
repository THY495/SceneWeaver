import React, { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { NovelPlay } from './NovelPlay'
import { ServiceReadiness } from './ServiceReadiness'
import { gameRequest } from './gameRequest'
import { pauseForMenu } from './gameMenuPause'
import type { Communication } from './PlayerBar'
import './game.css'
import './game-play.css'

function Game() {
  const [library, setLibrary] = useState<{
    title: string
    saves: { id: string; label: string; created_at: string; finished: boolean }[]
  } | null>(null)
  const [label, setLabel] = useState('新的旅程')
  const [llmConfigured, setLlmConfigured] = useState<boolean | null>(null)
  const [settingsOpen, setSettingsOpen] = useState(false)
  useEffect(() => {
    if (llmConfigured === false) setSettingsOpen(true)
  }, [llmConfigured])
  const newRequest = useRef<{ label: string; id: string } | null>(null)
  const [session, setSession] = useState<{ id: string; title: string } | null>(null)
  const [playing, setPlaying] = useState(false)
  const [input, setInput] = useState(''),
    [error, setError] = useState('')
  const [ready, setReady] = useState(false),
    [sending, setSending] = useState(false)
  const [communication, setCommunication] = useState<Communication>({ channel: 'public', addressees: [] })
  const pending = useRef<{ fingerprint: string; id: string } | null>(null)
  const reportedFailures = useRef(new Set<string>())
  const menuPending = useRef(false)
  useEffect(() => {
    if (!playing || !session) return
    let live = true
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        const state = await gameRequest<{
          tasks: { id: string; status: string; stop_reason?: string | null }[]
        }>('/game/session')
        if (live) {
          const failures = state.tasks.filter((task) => task.status === 'failed')
          const incomplete = state.tasks.filter(
            (task) => task.status === 'completed' && task.stop_reason === 'safety_rejected',
          )
          const unseen = failures.some((task) => !reportedFailures.current.has(task.id))
          const unseenIncomplete = incomplete.some((task) => !reportedFailures.current.has(task.id))
          for (const task of failures) reportedFailures.current.add(task.id)
          for (const task of incomplete) reportedFailures.current.add(task.id)
          if (unseen)
            setError('有一段回应未能完成，已发生的剧情会保留。你可以继续发言；若反复出现，再检查模型设置。')
          else if (unseenIncomplete)
            setError('这段对话暂时停在这里。已发生的剧情会保留，你可以继续行动或发言。')
        }
      } catch {
        if (live) setError('暂时无法读取游玩状态，请检查本地游戏服务。')
      } finally {
        if (live) timer = setTimeout(() => void poll(), 2500)
      }
    }
    void poll()
    return () => {
      live = false
      clearTimeout(timer)
    }
  }, [playing, session])
  useEffect(() => {
    let live = true
    gameRequest<{ id: string; title: string }>('/game/session')
      .then((s) => {
        if (live) {
          setSession(s)
          document.title = s.title
          if (new URLSearchParams(location.search).get('start') === '1') {
            history.replaceState(history.state, '', location.pathname)
            void gameRequest('/runtime/check', { method: 'POST' })
              .then(() => {
                if (live) setPlaying(true)
              })
              .catch((e) => {
                if (live) setError(String(e))
              })
          }
        }
      })
      .catch(() => {
        gameRequest<{
          title: string
          saves: { id: string; label: string; created_at: string; finished: boolean }[]
        }>('/game/library')
          .then((value) => {
            if (live) {
              setLibrary(value)
              document.title = value.title
            }
          })
          .catch((e) => {
            if (live) setError(String(e))
          })
      })
    return () => {
      live = false
    }
  }, [])
  async function newGame() {
    if (sending || !label.trim()) return
    if (newRequest.current?.label !== label) newRequest.current = { label, id: crypto.randomUUID() }
    setSending(true)
    setError('')
    try {
      const save = await gameRequest<{ id: string }>('/game/saves', {
        method: 'POST',
        body: JSON.stringify({ label, request_id: newRequest.current.id }),
      })
      location.assign(`/runs/${encodeURIComponent(save.id)}/?start=1`)
    } catch (e) {
      setError(String(e))
    } finally {
      setSending(false)
    }
  }
  async function menu() {
    if (!location.pathname.startsWith('/runs/')) {
      setPlaying(false)
      return
    }
    if (!session || menuPending.current) return
    menuPending.current = true
    setSending(true)
    setError('')
    try {
      await pauseForMenu(session.id, gameRequest)
      setPlaying(false)
      location.assign('/')
    } catch (e) {
      setError(String(e))
    } finally {
      menuPending.current = false
      setSending(false)
    }
  }
  async function begin() {
    setSending(true)
    setError('')
    try {
      await gameRequest('/runtime/check', { method: 'POST' })
      if (session) {
        const snapshot = await gameRequest<{
          frame: { mode: string; finished_at?: string | null }
        }>(`/role/players/${session.id}/snapshot`)
        if (snapshot.frame.mode === 'player' && !snapshot.frame.finished_at) {
          await gameRequest('/game/pause', {
            method: 'PUT',
            body: JSON.stringify({ paused: false }),
          })
        }
      }
      setPlaying(true)
    } catch (e) {
      setError(String(e))
    } finally {
      setSending(false)
    }
  }
  async function send() {
    if (!session || !ready || sending || !input.trim()) return
    const payload = {
      content: input.trim(),
      mode: 'auto',
      acknowledgement: sessionStorage.getItem('voice-enabled:' + session.id) === '1',
      ...communication,
    }
    const fingerprint = JSON.stringify(payload)
    if (pending.current?.fingerprint !== fingerprint)
      pending.current = { fingerprint, id: crypto.randomUUID() }
    setSending(true)
    setError('')
    window.dispatchEvent(
      new CustomEvent('role-voice-event', { detail: { sid: session.id, kind: 'input_started' } }),
    )
    try {
      const accepted = await gameRequest<{ task_id: string }>(`/sessions/${session.id}/messages`, {
        method: 'POST',
        body: JSON.stringify({ ...payload, client_message_id: pending.current.id }),
      })
      window.dispatchEvent(
        new CustomEvent('role-voice-event', {
          detail: { sid: session.id, kind: 'accepted', task_id: accepted.task_id },
        }),
      )
      pending.current = null
      setInput('')
      setReady(false)
    } catch (e) {
      window.dispatchEvent(
        new CustomEvent('role-voice-event', { detail: { sid: session.id, kind: 'cancelled' } }),
      )
      setError(String(e))
    } finally {
      setSending(false)
    }
  }
  return playing && session ? (
    <NovelPlay
      sid={session.id}
      request={gameRequest}
      standalone
      input={input}
      onInput={setInput}
      onSend={() => void send()}
      busy={sending}
      error={error}
      communication={communication}
      onCommunication={setCommunication}
      onReady={setReady}
      onLibrary={() => void menu()}
      onLab={() => {}}
      onTask={() => setReady(false)}
    />
  ) : (
    <main className="game-title">
      <div className="game-title__light" aria-hidden="true" />
      <div className="game-title__shell">
        <header className="game-title__hero">
          <span className="game-title__eyebrow">互动叙事 · 由你的选择继续</span>
          <h1>{session?.title || library?.title || '正在读取作品…'}</h1>
          <p>走进故事，和人物交谈。你的话语与行动会影响接下来的情节。</p>
        </header>
        <div className="game-title__panel">
          {llmConfigured === false && (
            <p className="game-title__notice" role="status">
              开始前需要配置 DeepSeek API Key 并检查连接。语音可稍后添加。
            </p>
          )}
          {library && (
            <>
              <section className="game-title__start" aria-label="开始新的旅程">
                <span className="game-title__section-label">新的旅程</span>
                <label htmlFor="game-save-label">存档名称</label>
                <div className="game-title__start-row">
                  <input
                    id="game-save-label"
                    value={label}
                    maxLength={80}
                    onChange={(e) => setLabel(e.target.value)}
                  />
                  <button
                    className="game-title__primary"
                    disabled={sending || !label.trim() || llmConfigured === false}
                    onClick={() => void newGame()}
                  >
                    新游戏
                  </button>
                </div>
              </section>
              <section className="game-title__saves" aria-label="已有存档">
                <span className="game-title__section-label">继续旅程</span>
                {library.saves.length === 0 && <p>还没有存档。准备好后，开始一段新的旅程吧。</p>}
                {library.saves.map((save) => (
                  <a
                    className="game-title__save"
                    key={save.id}
                    href={`/runs/${encodeURIComponent(save.id)}/`}
                  >
                    <strong>{save.label}</strong>
                    <small>
                      {new Date(save.created_at).toLocaleString()}
                      {save.finished ? ' · 已结束' : ''}
                    </small>
                  </a>
                ))}
              </section>
            </>
          )}
          {session && (
            <section className="game-title__resume">
              <span className="game-title__section-label">回到故事</span>
              <button
                className="game-title__primary"
                disabled={sending || llmConfigured === false}
                onClick={() => void begin()}
              >
                {sending ? '正在检查连接…' : '继续游玩'}
              </button>
            </section>
          )}
          <details
            className="game-title__settings"
            open={settingsOpen}
            onToggle={(e) => setSettingsOpen(e.currentTarget.open)}
          >
            <summary>连接与语音设置</summary>
            <ServiceReadiness request={gameRequest} onConfigured={setLlmConfigured} />
          </details>
          {error && (
            <p className="game-title__error" role="alert">
              {error}
            </p>
          )}
        </div>
      </div>
    </main>
  )
}

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Game />
  </React.StrictMode>,
)
