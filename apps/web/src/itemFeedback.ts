// API rule failures can arrive as a JSON-stringified FastAPI detail object.
export function itemError(error: unknown): string {
  if (error && typeof error === 'object' && 'uncertain' in error && error.uncertain)
    return '未能确认操作结果，操作可能已经生效。请先刷新状态，确认物品与交接事项后再操作。'
  const text = error instanceof Error ? error.message : String(error)
  try {
    const detail = JSON.parse(text)
    if (typeof detail?.reason === 'string') return detail.reason
  } catch {
    // Ordinary server messages are already suitable for display.
  }
  if (/Failed to fetch|NetworkError|Load failed|fetch failed/i.test(text))
    return '连接中断，暂时无法确认操作结果。请先刷新状态，确认物品与交接事项后再操作。'
  return text
}
