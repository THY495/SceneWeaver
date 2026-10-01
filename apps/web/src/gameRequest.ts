import { requestJSON } from './apiRequest'

/** Translate shared player widgets to the independent game's bounded API. */
export async function gameRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  let target = path
  if (/^\/sessions\/[^/]+$/.test(path)) target = '/game/session'
  else if (/^\/role\/stories\/[^/]+\/settings$/.test(path)) {
    if (init.method !== 'PUT') throw new Error('不支持此游戏操作。')
    target = '/game/pause'
    init = { ...init, body: JSON.stringify({ paused: JSON.parse(String(init.body)).paused }) }
  } else if (/^\/role\/stories\/[^/]+\/presence$/.test(path)) {
    target = path.replace('/role/stories/', '/game/stories/')
  } else if (/^\/role\/experiments\/stories\/[^/]+\/narrative\/(choose|advance|auto-continue)$/.test(path)) {
    target = path.replace('/role/experiments/stories/', '/game/stories/')
  } else if (path.startsWith('/role/presentation/sprites/')) {
    target = path.replace('/role/presentation/sprites/', '/game/assets/')
  }
  const headers = new Headers(init.headers)
  headers.set('Content-Type', 'application/json')
  const token = sessionStorage.getItem('continuum-token')
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const base = /^\/runs\/[a-f0-9-]{36}(?=\/|$)/.exec(location.pathname)?.[0] || ''
  return requestJSON<T>(base + '/api' + target, { ...init, headers })
}
