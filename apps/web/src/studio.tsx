import React, { useCallback, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { requestJSON } from './apiRequest'
import { StoryStudio, type StudioReturnPosition } from './StoryStudio'
import { RunStudio } from './RunStudio'
import { NovelPlay } from './NovelPlay'
import type { Communication } from './PlayerBar'
import './style.css'
import './creator-shell.css'

type SessionState = {
  tasks: { id: string; status: string; error?: string }[]
}

function App() {
  const [mode, setMode] = useState<'studio' | 'preview' | 'diagnostics'>('studio')
  const [sid, setSid] = useState<string | null>(null)
  const [returnPosition, setReturnPosition] = useState<StudioReturnPosition | null>(null)
  const [input, setInput] = useState('')
  const [task, setTask] = useState<string | null>(null)
  const [ready, setReady] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [token, setToken] = useState(sessionStorage.getItem('sceneweaver-token') || '')
  const [needsToken, setNeedsToken] = useState(false)
  const [communication, setCommunication] = useState<Communication>({ channel: 'public', addressees: [] })

  const request = useCallback(
    async <T,>(path: string, init: RequestInit = {}): Promise<T> => {
      const headers = new Headers(init.headers)
      if (token) headers.set('Authorization', `Bearer ${token}`)
      if (init.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json')
      try {
        return await requestJSON<T>('/api' + path, { ...init, headers })
      } catch (cause) {
        if (String(cause).includes('401')) setNeedsToken(true)
        throw cause
      }
    },
    [token],
  )

  useEffect(() => {
    if (!task || !sid) return
    let active = true
    const poll = async () => {
      try {
        const state = await request<SessionState>(`/sessions/${sid}`)
        const current = state.tasks.find((row) => row.id === task)
        if (
          !active ||
          !current ||
          !['completed', 'failed', 'cancelled', 'superseded'].includes(current.status)
        )
          return
        if (current.status === 'failed') setError(current.error || '本次回应未成功，请重试。')
        setTask(null)
      } catch (cause) {
        if (active) setError(String(cause))
      }
    }
    void poll()
    const interval = window.setInterval(() => void poll(), 800)
    return () => {
      active = false
      window.clearInterval(interval)
    }
  }, [sid, task, token])

  async function send() {
    if (!sid || !ready || !input.trim() || busy || task) return
    setBusy(true)
    setError('')
    window.dispatchEvent(new CustomEvent('role-voice-event', { detail: { sid, kind: 'input_started' } }))
    try {
      const result = await request<{ task_id: string }>(`/sessions/${sid}/messages`, {
        method: 'POST',
        body: JSON.stringify({
          content: input,
          client_message_id: crypto.randomUUID(),
          mode: 'auto',
          acknowledgement: sessionStorage.getItem('voice-enabled:' + sid) === '1',
          ...communication,
        }),
      })
      setTask(result.task_id)
      setInput('')
    } catch (cause) {
      setError(String(cause))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="creator-shell">
      <header className="creator-chrome">
        <strong>Sceneweaver</strong>
        <span>{mode === 'studio' ? '开发工具' : mode === 'diagnostics' ? '工程调试' : '工程预览'}</span>
      </header>
      {needsToken && (
        <form
          onSubmit={(event) => {
            event.preventDefault()
            sessionStorage.setItem('sceneweaver-token', token)
            setNeedsToken(false)
            setError('')
          }}
        >
          <label htmlFor="local-token">本地访问令牌</label>
          <input
            id="local-token"
            type="password"
            value={token}
            onChange={(event) => setToken(event.target.value)}
          />
          <button type="submit">连接</button>
        </form>
      )}
      {error && <p role="alert">{error}</p>}
      {mode === 'studio' ? (
        <StoryStudio
          sid={sid}
          returnPosition={returnPosition}
          onPreview={setReturnPosition}
          request={request}
          onClose={() => setMode('studio')}
          onOpen={(id) => {
            setSid(id)
            setReady(false)
            setTask(null)
            setMode('preview')
          }}
        />
      ) : !sid ? (
        <p role="status">正在打开工程预览…</p>
      ) : mode === 'diagnostics' ? (
        <section className="creator-diagnostics">
          <button onClick={() => setMode('studio')}>返回工程编辑</button>
          <RunStudio key={sid} sid={sid} request={request} onClose={() => setMode('preview')} />
        </section>
      ) : (
        <NovelPlay
          key={sid}
          sid={sid}
          request={request}
          input={input}
          onInput={setInput}
          onSend={() => void send()}
          busy={busy || Boolean(task)}
          error={error}
          communication={communication}
          onCommunication={setCommunication}
          onReady={setReady}
          onLibrary={() => setMode('studio')}
          returnToEditor
          onLab={() => setMode('diagnostics')}
          onTask={setTask}
        />
      )}
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
