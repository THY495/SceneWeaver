/** A missing response is not evidence that the server rolled back a mutation. */
export class RequestFailure extends Error {
  constructor(
    message: string,
    readonly uncertain: boolean,
    readonly status?: number,
    readonly code?: string,
  ) {
    super(message)
    this.name = 'RequestFailure'
  }
}

export async function requestJSON<T>(url: string, init: RequestInit = {}): Promise<T> {
  const mutation = !['GET', 'HEAD'].includes((init.method || 'GET').toUpperCase())
  let response: Response
  try {
    response = await fetch(url, init)
  } catch {
    throw new RequestFailure('连接中断，请检查连接后刷新状态。', mutation)
  }
  let value: unknown
  try {
    value = await response.json()
  } catch {
    throw new RequestFailure('未能读取完整响应，请刷新状态。', mutation, response.status)
  }
  if (!response.ok) {
    const detail = (value as { detail?: unknown } | null)?.detail ?? '请求失败'
    throw new RequestFailure(
      typeof detail === 'string' ? detail : JSON.stringify(detail),
      mutation && response.status >= 500,
      response.status,
      typeof detail === 'object' && detail !== null && typeof (detail as { code?: unknown }).code === 'string'
        ? (detail as { code: string }).code
        : undefined,
    )
  }
  return value as T
}
