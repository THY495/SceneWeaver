import { useEffect, useRef, useState } from 'react'

type Person = { id: string; name: string; player: boolean; location: string }
type Result = {
  task_id: string
  status: string
  outcome?: string
  stop_reason?: string
  error?: string
  events: { id: string; kind: string; text: string; item_action_status?: string; item_action_code?: string }[]
}
type Settings = { paused: boolean; [key: string]: unknown }
type PublicConfig = {
  dialogue_model: string
  generation_configured: boolean
  daily_budget_usd: number
  turn_budget_usd: number
}
const active = (result: Result | null) => !result || ['queued', 'running', 'waiting'].includes(result.status)

export function ExperimentStep({
  story,
  request,
  onFinished,
  refresh = 0,
}: {
  story: string
  request: (path: string, init?: RequestInit) => Promise<unknown>
  onFinished: (id: string) => void
  refresh?: number
}) {
  const [people, setPeople] = useState<Person[]>([]),
    [settings, setSettings] = useState<Settings | null>(null)
  const [limits, setLimits] = useState<PublicConfig | null>(null)
  const [actor, setActor] = useState(''),
    [tid, setTid] = useState(''),
    [busy, setBusy] = useState(false)
  const [result, setResult] = useState<Result | null>(null),
    [error, setError] = useState('')
  const current = useRef<Result | null>(null),
    pending = useRef<{ request_id: string; actor_id: string } | null>(null)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])
  const base = `/role/stories/${story}`
  useEffect(() => {
    let live = true
    Promise.all([request(base + '/roster'), request(base + '/settings'), request('/config')])
      .then(([roster, config, limits]) => {
        if (!live) return
        const npcs = (roster as Person[]).filter((p) => !p.player)
        setPeople(npcs)
        setActor((old) => (npcs.some((p) => p.id === old) ? old : npcs[0]?.id || ''))
        setSettings(config as Settings)
        setLimits(limits as PublicConfig)
      })
      .catch((e) => {
        if (live) setError(String(e))
      })
    return () => {
      live = false
    }
  }, [story, refresh])
  useEffect(() => {
    if (!tid) return
    let live = true,
      polling = false
    const presence = (visible: boolean) =>
      request(`${base}/steps/${tid}/presence`, {
        method: 'POST',
        body: JSON.stringify({ visible }),
        keepalive: !visible,
      })
    const refresh = async () => {
      if (polling || !active(current.current)) return
      polling = true
      try {
        await presence(document.visibilityState === 'visible')
        const value = (await request(`${base}/steps/${tid}`)) as Result
        if (live) {
          current.current = value
          setResult(value)
          if (!active(value)) onFinished(tid)
        }
      } catch (e) {
        if (live) setError(String(e))
      } finally {
        polling = false
      }
    }
    const visibility = () => {
      if (document.visibilityState !== 'visible' && active(current.current))
        void presence(false).catch(() => {})
      void refresh()
    }
    void refresh()
    const timer = setInterval(refresh, 2500)
    document.addEventListener('visibilitychange', visibility)
    return () => {
      live = false
      clearInterval(timer)
      document.removeEventListener('visibilitychange', visibility)
      if (active(current.current)) void presence(false).catch(() => {})
    }
  }, [story, tid])
  async function pause() {
    if (!settings) return
    setBusy(true)
    setError('')
    try {
      setSettings(
        (await request(base + '/settings', {
          method: 'PUT',
          body: JSON.stringify({ ...settings, paused: true }),
        })) as Settings,
      )
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }
  async function start() {
    if (!actor || busy || (tid && active(current.current))) return
    setBusy(true)
    setError('')
    pending.current ??= { request_id: crypto.randomUUID(), actor_id: actor }
    try {
      const value = (await request(base + '/steps', {
        method: 'POST',
        body: JSON.stringify(pending.current),
      })) as { task_id: string }
      if (!mounted.current) {
        void request(`${base}/steps/${value.task_id}/presence`, {
          method: 'POST',
          body: JSON.stringify({ visible: false }),
          keepalive: true,
        }).catch(() => {})
        return
      }
      current.current = null
      setResult(null)
      setTid(value.task_id)
      pending.current = null
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }
  async function cancel() {
    try {
      await request(`${base}/steps/${tid}/presence`, {
        method: 'POST',
        body: JSON.stringify({ visible: false }),
      })
      const value = (await request(`${base}/steps/${tid}`)) as Result
      current.current = value
      setResult(value)
      onFinished(tid)
    } catch (e) {
      setError(String(e))
    }
  }
  return (
    <section className="experiment-step">
      <h3>单步实验</h3>
      <p>
        剧情保持暂停，只允许选中NPC做一次决策；结果可以是等待或最多一个发言/行动片段。可能调用付费模型，沿用日预算和任务预算。关闭面板或隐藏页面会撤销尚未提交的许可；已经提交的事件保留。
      </p>
      {limits && (
        <p className="step-budget">
          使用模型：{limits.dialogue_model}。本次单步任务费用硬上限 ${limits.turn_budget_usd.toFixed(2)}
          ，实际按调用结算；全局日上限 ${limits.daily_budget_usd.toFixed(2)}
          。点击执行即授权本次单步在该上限内调用模型，不会修改预算设置。
        </p>
      )}
      {limits && !limits.generation_configured && <p role="alert">生成模型尚未配置，当前不能执行单步。</p>}
      <p>剧情：{settings ? (settings.paused ? '已暂停' : '尚未暂停') : '读取中'}</p>
      {settings && !settings.paused && (
        <button disabled={busy} onClick={pause}>
          暂停剧情以准备实验
        </button>
      )}
      <label>
        执行角色{' '}
        <select
          aria-label="单步执行角色"
          value={actor}
          disabled={busy || !!pending.current || (!!tid && active(result))}
          onChange={(e) => setActor(e.target.value)}
        >
          {people.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name} · {p.location}
            </option>
          ))}
        </select>
      </label>
      <button
        disabled={
          busy ||
          !settings?.paused ||
          !actor ||
          limits?.generation_configured === false ||
          (!!tid && active(result))
        }
        onClick={start}
      >
        {pending.current ? '重试同一单步请求' : '执行一次决策'}
      </button>
      {tid && active(result) && <button onClick={cancel}>撤销本次单步</button>}
      {result && (
        <div role="status">
          <p>
            本次决策 ·{' '}
            {(
              {
                completed: '已完成',
                failed: '失败',
                cancelled: '已撤销',
                queued: '排队中',
                running: '执行中',
                waiting: '等待处理',
              } as Record<string, string>
            )[result.status] || result.status}{' '}
            ·{' '}
            {result.stop_reason === 'safety_rejected'
              ? '候选内容未通过校验，未发布'
              : result.outcome === 'wait'
                ? '角色选择等待'
                : result.events.some((e) => e.item_action_status === 'rejected')
                  ? '物品操作被规则拒绝，未改变物品状态'
                  : result.outcome === 'committed'
                    ? '已提交一个片段'
                    : '尚无有效决策结果'}
          </p>
          {!active(result) && <p>在下方“决策结果与观察笔记”查看结果、关联诊断并保存结论。</p>}
          {result.stop_reason === 'safety_rejected' && (
            <p>
              这不代表角色决定沉默。待回应问题保持原状态；查看诊断或调整设定后，可再次执行一次决策，新执行会产生模型费用。
            </p>
          )}
          {result.error && <p>{result.error}</p>}
          {result.events.map((event) => (
            <p key={event.id}>
              {event.kind}：{event.text}
            </p>
          ))}
        </div>
      )}
      {error && <p role="alert">{error}</p>}
    </section>
  )
}
