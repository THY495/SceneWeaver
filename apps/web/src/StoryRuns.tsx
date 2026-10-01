import { useEffect, useState } from 'react'
type Run = {
  id: string
  title: string
  created_at: string
  paused: boolean
  finished_at?: string | null
  note_count: number
}
export function StoryRuns({
  blueprint,
  request,
  onOpen,
}: {
  blueprint: string
  request: (path: string, init?: RequestInit) => Promise<unknown>
  onOpen: (id: string) => void
}) {
  const [runs, setRuns] = useState<Run[]>([]),
    [next, setNext] = useState<string | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    [loaded, setLoaded] = useState(false)
  async function load(older = false) {
    setBusy(true)
    setError('')
    try {
      const data = (await request(
        `/role/experiments/runs?blueprint_id=${encodeURIComponent(blueprint)}${older && next ? '&before=' + encodeURIComponent(next) : ''}`,
      )) as { runs: Run[]; next_before: string | null }
      setRuns((old) => (older ? [...old, ...data.runs] : data.runs))
      setNext(data.next_before)
      setLoaded(true)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }
  async function open(run: Run) {
    setBusy(true)
    try {
      const data = (await request(`/role/experiments/runs/${run.id}/record`)) as {
        people: { id: string; player: boolean }[]
      }
      const player = data.people.find((p) => p.player)
      if (!player) throw Error('该运行没有可打开的玩家会话')
      onOpen(player.id)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }
  useEffect(() => {
    void load()
  }, [blueprint])
  return (
    <>
      {runs[0] && (
        <button className="primary" disabled={busy} onClick={() => void open(runs[0])}>
          {runs[0].finished_at ? '回看最近结局' : '继续最近运行'}
          {runs[0].note_count ? ` · ${runs[0].note_count} 条观察笔记` : ''}
        </button>
      )}
      <details>
        <summary>这个故事的运行历史{runs.length ? ` · ${runs.length}${next ? '+' : ''} 次` : ''}</summary>
        <p>打开已有运行不会重新生成。每次启动保存当时的设定版本。</p>
        {runs.map((run) => (
          <p key={run.id}>
            <strong>{run.title}</strong>
            <br />
            {new Date(run.created_at).toLocaleString()} ·{' '}
            {run.finished_at ? '已结束' : run.paused ? '已暂停' : '运行中'} · {run.note_count} 条观察笔记
            <br />
            <button disabled={busy} onClick={() => void open(run)}>
              打开此运行
            </button>
          </p>
        ))}
        {loaded && !runs.length && <p>尚未启动过实验。</p>}
        <button disabled={busy} onClick={() => void load()}>
          刷新历史
        </button>
        {next && (
          <button disabled={busy} onClick={() => void load(true)}>
            更早的运行
          </button>
        )}
        {error && <p role="alert">{error}</p>}
      </details>
    </>
  )
}
