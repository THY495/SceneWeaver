import type { ReadingProgress } from './readingProgress'

type Request = (path: string, init?: RequestInit) => Promise<unknown>
export type SavedReading = { epoch: number; revision: number; progress: ReadingProgress | null }

/** Serialize writes, retaining the same revision on an uncertain response. */
export function readingWriter(request: Request, path: string, saved: SavedReading) {
  let revision = saved.revision
  let queue = Promise.resolve()
  return (progress: ReadingProgress): Promise<void> => {
    queue = queue.then(async () => {
      const body = JSON.stringify({ epoch: saved.epoch, expected_revision: revision, progress })
      const result = (await request(path, { method: 'PUT', body, keepalive: true })) as { revision: number }
      revision = result.revision
    })
    // A rejected chain stays stopped: later positions must not overwrite unknown state.
    return queue
  }
}
