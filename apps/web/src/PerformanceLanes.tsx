import { performanceLanes, type PerformanceSpan } from './performanceTimeline'

export function PerformanceLanes({
  spans,
  duration,
  names,
  axisStart = 0,
  clockLabel = '同一横轴从本次执行开始计时',
}: {
  spans: PerformanceSpan[]
  duration: number
  names: Record<string, string>
  axisStart?: number
  clockLabel?: string
}) {
  return (
    <section aria-label="按实例划分的演出时间线">
      <h4>导演与角色实例</h4>
      <p className="muted">
        {clockLabel}；审查与首句语音分行呈现，重叠即并行。仅展示已采集的工作，不把空白解释为模型推理。
      </p>
      {performanceLanes(spans).map(({ owner, entries }) => (
        <section key={owner}>
          <h5>
            {owner === 'director'
              ? '场景导演'
              : owner === 'shared'
                ? '共享或归属未知'
                : `${names[owner] || '角色'} · ${owner.slice(0, 8)}`}
          </h5>
          <ol className="timing-rows">
            {entries.map(({ span, label }) => (
              <li key={span.id}>
                <div>
                  {label} · {span.status}
                </div>
                <div className="timing-track" aria-hidden="true">
                  <div
                    style={{
                      marginLeft: `${Math.max(0, Math.min(100, ((span.start_ms - axisStart) / Math.max(1, duration)) * 100))}%`,
                      width: `${(Math.max(0, Math.min(duration - (span.start_ms - axisStart), span.duration_ms || 0)) / Math.max(1, duration)) * 100}%`,
                    }}
                  />
                </div>
                <small>
                  开始 +{(span.start_ms / 1000).toFixed(3)}秒 · 持续{' '}
                  {span.duration_ms === null ? '未知' : `${(span.duration_ms / 1000).toFixed(3)}秒`}
                </small>
              </li>
            ))}
          </ol>
        </section>
      ))}
    </section>
  )
}
