import type { ReadingLine } from './stageReading'

export type ReadingProgress = {
  version: 1
  entry: string
  selected: string
  acknowledged: boolean
  fingerprint: string
}
export function lineFingerprint(line: ReadingLine): string {
  let hash = 2166136261
  const text = `${line.speaker}\n${line.text}`
  for (let i = 0; i < text.length; i++) hash = Math.imul(hash ^ text.charCodeAt(i), 16777619)
  return (hash >>> 0).toString(16)
}
export function parseProgress(raw: string | null): ReadingProgress | null {
  try {
    const value = JSON.parse(raw || 'null')
    return value?.version === 1 &&
      typeof value.entry === 'string' &&
      typeof value.selected === 'string' &&
      typeof value.acknowledged === 'boolean' &&
      typeof value.fingerprint === 'string'
      ? value
      : null
  } catch {
    return null
  }
}
export function restoreProgress(lines: ReadingLine[], entry: string, saved: ReadingProgress | null) {
  const fallback = lines.find((l) => l.id === `${entry}:0`) || lines[0]
  const selected = saved?.entry === entry ? lines.find((l) => l.id === saved.selected) : undefined
  const unchanged = selected && saved?.fingerprint === lineFingerprint(selected)
  return {
    selected: selected?.id || fallback?.id || '',
    readThrough: unchanged && saved?.acknowledged ? selected!.id : '',
    notice:
      saved?.entry === entry && (!selected || !unchanged)
        ? '原阅读位置或内容已变化，请重新确认当前段落。'
        : '',
  }
}
