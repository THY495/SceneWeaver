import { useState } from 'react'
import { PerformanceLanes } from './PerformanceLanes'
import type { PerformanceSpan } from './performanceTimeline'

type Overview = {
  origin_at: string
  captured_at: string
  finished_at?: string | null
  ending_event_id?: string | null
  clock_note: string
  truncated: Record<string, boolean>
  tasks: {
    id: string
    parent_id: string | null
    kind: string
    status: string
    created_at: string
    overdue?: boolean
  }[]
  traces: {
    id: string
    task_id: string
    started_at?: string
    duration_ms: number
    spans: PerformanceSpan[]
    dropped_spans?: number
  }[]
  events: { id: string; seq: number; kind: string; actor_id: string | null; committed_at: string }[]
  playbacks: {
    id: string
    event_id: string
    status: string
    requested_at: string
    first_audio_schedule_receipt_at: string | null
    last_receipt_at: string | null
    played_samples: number
    cache_hit: boolean | null
  }[]
}

export function StoryTiming({
  story,
  request,
  names,
}: {
  story: string
  request: (path: string, init?: RequestInit) => Promise<unknown>
  names: Record<string, string>
}) {
  const [data, setData] = useState<Overview | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [windowId, setWindowId] = useState('')
  async function load() {
    setBusy(true)
    setError('')
    try {
      setData((await request(`/role/stories/${story}/timing-overview`)) as Overview)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }
  const origin = data ? Date.parse(data.origin_at) : 0
  const offset = (at: string) => Date.parse(at) - origin
  const relative = (at: string) => `${(offset(at) / 1000).toFixed(3)}秒`
  const selected = data?.traces.find((run) => run.id === windowId && run.started_at)
  const axisStart = selected ? offset(selected.started_at!) : 0
  const spans =
    data?.traces
      .filter((run) => !selected || run.id === selected.id)
      .flatMap((run) => {
        const start = run.started_at ? offset(run.started_at) : NaN
        if (!Number.isFinite(start)) return []
        return run.spans.map((span) => ({
          ...span,
          id: `${run.id}:${span.id}`,
          parent_id: span.parent_id ? `${run.id}:${span.parent_id}` : null,
          start_ms: start + span.start_ms,
        }))
      }) || []
  const end = [
    ...spans.map((s) => s.start_ms + (s.duration_ms || 0)),
    ...(data?.events.map((e) => offset(e.committed_at)) || []),
    ...(data?.playbacks.map((p) =>
      offset(p.last_receipt_at || p.first_audio_schedule_receipt_at || p.requested_at),
    ) || []),
  ].reduce((latest, value) => (Number.isFinite(value) ? Math.max(latest, value) : latest), 1)
  return (
    <details>
      <summary>整场运行时间线（创作者）</summary>
      <p>串联本故事的回合、后台任务和播放回执。读取不会推进剧情或调用模型。</p>
      <button disabled={busy} onClick={() => void load()}>
        {busy ? '正在读取…' : '读取 / 刷新整场记录'}
      </button>
      {error && <p role="status">{error}</p>}
      {data && (
        <>
          <p>
            起点：{data.origin_at} · 采集：{data.captured_at}
          </p>
          <p>{data.clock_note}</p>
          {Object.values(data.truncated).some(Boolean) && (
            <p role="status">
              记录超过读取上限，当前图不完整：
              {Object.entries(data.truncated)
                .filter(([, v]) => v)
                .map(([k]) => k)
                .join('、')}
              。
            </p>
          )}
          <p>
            {data.finished_at
              ? `故事已结束：+${relative(data.finished_at)}；收尾事件 ${data.ending_event_id?.slice(0, 8)}。后台任务状态另列。`
              : '无结束标记的末场景不能证明游戏已结束；仍排队的后台任务没有执行耗时。'}
          </p>
          <label>
            时间窗
            <select value={windowId} onChange={(e) => setWindowId(e.target.value)}>
              <option value="">整场（保留阅读与等待间隔）</option>
              {data.traces
                .filter((run) => run.started_at)
                .map((run) => (
                  <option key={run.id} value={run.id}>
                    回合 {run.task_id.slice(0, 8)} · +{relative(run.started_at!)}
                  </option>
                ))}
            </select>
          </label>
          <PerformanceLanes
            spans={spans}
            duration={selected ? selected.duration_ms : end}
            axisStart={axisStart}
            clockLabel={
              selected
                ? `当前时间窗从故事创建后 +${relative(selected.started_at!)} 开始，下面的数值仍相对故事创建时间`
                : '同一横轴从故事创建开始计时'
            }
            names={names}
          />
          {data.traces.some((t) => !t.started_at || t.dropped_spans) && (
            <p>部分执行缺少起点或省略阶段，不能据此推断空白区发生了什么。</p>
          )}
          <h4>回合与后台收尾</h4>
          <ul>
            {data.tasks.map((t) => (
              <li key={t.id}>
                +{relative(t.created_at)} · {t.kind} · {t.status} · {t.id.slice(0, 8)}
                {t.overdue && ' · 已过截止时间，尚未执行完毕'}
                {t.parent_id && ` ← ${t.parent_id.slice(0, 8)}`}
                {!data.traces.some((r) => r.task_id === t.id) && ' · 无已保存执行追踪'}
              </li>
            ))}
          </ul>
          <details>
            <summary>世界事件提交</summary>
            <ul>
              {data.events.map((e) => (
                <li key={e.id}>
                  +{relative(e.committed_at)} · #{e.seq} · {e.kind} ·{' '}
                  {e.actor_id ? names[e.actor_id] || e.actor_id.slice(0, 8) : '旁白/世界'} ·{' '}
                  {e.id.slice(0, 8)}
                </li>
              ))}
            </ul>
          </details>
          <details>
            <summary>播放请求与服务端回执（非物理播放区间）</summary>
            <ul>
              {data.playbacks.map((p) => (
                <li key={p.id}>
                  +{relative(p.requested_at)} 请求 · 事件 {p.event_id.slice(0, 8)} · {p.status} ·{' '}
                  {p.cache_hit === true ? '预生成缓存' : p.cache_hit === false ? '按需合成' : '缓存信息缺失'}{' '}
                  · 已回执 {(p.played_samples / 24000).toFixed(2)} 秒音频
                  {p.first_audio_schedule_receipt_at
                    ? ` · 首次排程回执 +${relative(p.first_audio_schedule_receipt_at)}（服务端收到，非物理首音）`
                    : ' · 未收到首次排程标记'}
                  {p.last_receipt_at ? ` · 最后回执 +${relative(p.last_receipt_at)}` : ' · 回执时间未采集'}
                </li>
              ))}
            </ul>
          </details>
        </>
      )}
    </details>
  )
}
