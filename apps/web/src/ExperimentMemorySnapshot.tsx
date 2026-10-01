import { useEffect, useRef, useState } from 'react'
import type { ExperimentPerson } from './ExperimentObservation'

type Settings = { paused: boolean }
type Limits = {
  dialogue_model: string
  generation_configured: boolean
  daily_budget_usd: number
  turn_budget_usd: number
}
type TaskResult = {
  task_id: string
  actor_id: string
  status: string
  error?: string
  cut?: number
  notes?: number
  snapshot_id?: string
}

const running = (status?: string) => Boolean(status && ['queued', 'running', 'waiting'].includes(status))

export function ExperimentMemorySnapshot({
  story,
  people,
  refresh,
  request,
  onFinished,
}: {
  story: string
  people: ExperimentPerson[]
  refresh: number
  request: (path: string, init?: RequestInit) => Promise<unknown>
  onFinished: (id: string) => void
}) {
  const npcs = people.filter((person) => !person.player)
  const [actor, setActor] = useState('')
  const [settings, setSettings] = useState<Settings | null>(null)
  const [limits, setLimits] = useState<Limits | null>(null)
  const [task, setTask] = useState<TaskResult | null>(null)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const pending = useRef<{ request_id: string; actor_id: string } | null>(null)
  const base = `/role/stories/${story}`

  useEffect(() => {
    setActor((old) => (npcs.some((person) => person.id === old) ? old : npcs[0]?.id || ''))
  }, [people])
  useEffect(() => {
    let live = true
    Promise.all([request(base + '/settings'), request('/config')])
      .then(([nextSettings, nextLimits]) => {
        if (!live) return
        setSettings(nextSettings as Settings)
        setLimits(nextLimits as Limits)
      })
      .catch((reason) => {
        if (live) setError(String(reason))
      })
    return () => {
      live = false
    }
  }, [story, refresh])
  useEffect(() => {
    if (!task || !running(task.status)) return
    let live = true
    const poll = async () => {
      try {
        const next = (await request(`${base}/memory-snapshots/${task.task_id}`)) as TaskResult
        if (!live) return
        setTask(next)
        if (!running(next.status)) onFinished(next.task_id)
      } catch (reason) {
        if (live) setError(String(reason))
      }
    }
    void poll()
    const timer = setInterval(poll, 2000)
    return () => {
      live = false
      clearInterval(timer)
    }
  }, [story, task?.task_id, task?.status])

  async function start() {
    if (!actor || busy || running(task?.status)) return
    setBusy(true)
    setError('')
    setMessage('')
    pending.current ??= { request_id: crypto.randomUUID(), actor_id: actor }
    try {
      const result = (await request(base + '/memory-snapshots', {
        method: 'POST',
        body: JSON.stringify(pending.current),
      })) as { scheduled: boolean; task_id?: string; reason?: string }
      if (!result.scheduled || !result.task_id) {
        setMessage(result.reason || '目前不需要整理。')
        pending.current = null
        return
      }
      setTask({ task_id: result.task_id, actor_id: actor, status: 'queued' })
      pending.current = null
    } catch (reason) {
      setError(String(reason))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="experiment-snapshot">
      <h3>角色上下文整理实验</h3>
      <p>
        在剧情暂停时，把选中NPC已感知的较早事件整理成角色级快照，并保留最近两条原始事件。快照只压缩上下文，不确认传闻，也不改变世界状态。
      </p>
      {limits && (
        <p className="step-budget">
          使用模型：{limits.dialogue_model}。单任务费用硬上限 ${limits.turn_budget_usd.toFixed(2)}
          ，实际按调用结算；全局日上限 ${limits.daily_budget_usd.toFixed(2)}
          。点击整理即授权本次任务在该上限内调用模型。
        </p>
      )}
      <label>
        整理角色{' '}
        <select
          aria-label="上下文整理角色"
          value={actor}
          disabled={busy || running(task?.status)}
          onChange={(event) => setActor(event.target.value)}
        >
          {npcs.map((person) => (
            <option key={person.id} value={person.id}>
              {person.name} · {person.location}
            </option>
          ))}
        </select>
      </label>
      <button
        disabled={
          busy ||
          !actor ||
          !settings?.paused ||
          limits?.generation_configured === false ||
          running(task?.status)
        }
        onClick={() => void start()}
      >
        {pending.current ? '重试同一次整理' : '整理一次角色上下文'}
      </button>
      {settings && !settings.paused && <p>请先在单步实验中暂停剧情。</p>}
      {message && <p role="status">{message}</p>}
      {task && (
        <div role="status">
          <p>
            整理任务：
            {(
              {
                queued: '排队中',
                running: '执行中',
                waiting: '等待处理',
                completed: '已完成',
                failed: '失败',
                cancelled: '已撤销',
                superseded: '已失效',
              } as Record<string, string>
            )[task.status] || task.status}
            {task.cut != null && ` · 覆盖至事件序号 ${task.cut}`}
            {task.notes != null && ` · 新增 ${task.notes} 条个人记忆`}
          </p>
          {task.snapshot_id && <p>已生成快照 {task.snapshot_id}，下方刷新观察后可查看摘要和来源。</p>}
          {task.error && <p>{task.error}</p>}
        </div>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  )
}
