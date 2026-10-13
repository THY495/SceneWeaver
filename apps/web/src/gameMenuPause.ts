import type { RequestJSON } from './audioPreparation'

/** Stop local playback before waiting for the server to pause a save. */
export async function pauseForMenu(
  sid: string,
  request: RequestJSON,
  notify = (kind: string) =>
    window.dispatchEvent(new CustomEvent('role-voice-event', { detail: { sid, kind } })),
) {
  notify('pause_requested')
  try {
    await request('/game/pause', { method: 'PUT', body: JSON.stringify({ paused: true }) })
  } catch (error) {
    notify('pause_failed')
    throw error
  }
  notify('paused')
}
