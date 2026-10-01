import { useEffect, useState } from 'react'
import { PlayerCredentials } from './PlayerCredentials'

type Status = { llm: { configured: boolean; verified: boolean; message: string }; tts_configured: boolean }
export function ServiceReadiness({
  request,
  onConfigured,
}: {
  request: (path: string, init?: RequestInit) => Promise<unknown>
  onConfigured?: (configured: boolean) => void
}) {
  const [status, setStatus] = useState<Status | null>(null)
  const [notice, setNotice] = useState(''),
    [busy, setBusy] = useState(false)
  useEffect(() => {
    let live = true
    const refresh = () =>
      request('/runtime/readiness')
        .then((v) => {
          if (live) setStatus(v as Status)
        })
        .catch(() => {
          if (live) setNotice('无法读取服务状态，请检查本地服务。')
        })
    void refresh()
    window.addEventListener('sceneweaver-services-changed', refresh)
    return () => {
      live = false
      window.removeEventListener('sceneweaver-services-changed', refresh)
    }
  }, [request])
  useEffect(() => {
    if (status) onConfigured?.(status.llm.configured)
  }, [status, onConfigured])
  async function check() {
    setBusy(true)
    try {
      await request('/runtime/check', { method: 'POST' })
      setStatus((await request('/runtime/readiness')) as Status)
      setNotice('对话模型连接正常，可以开始游戏。')
    } catch (e) {
      setNotice(String(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <section aria-label="游戏服务状态">
      <h3>连接状态</h3>
      <PlayerCredentials request={request} />
      <p>{status?.llm.message || '正在读取模型配置…'}</p>
      <p>
        {status?.tts_configured ? '语音已配置。' : '语音可选，未配置时仍可使用文字游玩。'}
        连接检查会发送简短测试文本，计入预算；更换密钥后需重新检查。
      </p>
      <button disabled={busy || !status?.llm.configured} onClick={() => void check()}>
        {busy ? '正在检查…' : '检查对话连接'}
      </button>
      <p role="status">{notice}</p>
    </section>
  )
}
