import { useEffect, useState } from 'react'

export function PlayerCredentials({
  request,
}: {
  request: (path: string, init?: RequestInit) => Promise<unknown>
}) {
  const [available, setAvailable] = useState(false)
  const [open, setOpen] = useState(false)
  const [clearLlm, setClearLlm] = useState(false),
    [clearTts, setClearTts] = useState(false)
  const [llm, setLlm] = useState(''),
    [tts, setTts] = useState('')
  const [region, setRegion] = useState('ap-southeast-1'),
    [voice, setVoice] = useState('longanfengyue')
  const [budget, setBudget] = useState(2),
    [notice, setNotice] = useState(''),
    [busy, setBusy] = useState(false)
  useEffect(() => {
    let live = true
    request('/player/credentials')
      .then((v) => {
        const result = v as {
          available: boolean
          llm_configured: boolean
          tts_region: string
          tts_voice: string
          daily_budget_usd: number
        }
        if (live) {
          setAvailable(result.available)
          setOpen(!result.llm_configured)
          setRegion(result.tts_region)
          setVoice(result.tts_voice)
          setBudget(result.daily_budget_usd)
        }
      })
      .catch(() => {})
    return () => {
      live = false
    }
  }, [request])
  if (!available) return null
  async function save() {
    setBusy(true)
    try {
      await request('/player/credentials', {
        method: 'PATCH',
        body: JSON.stringify({
          ...(clearLlm ? { llm_key: '' } : llm.trim() ? { llm_key: llm.trim() } : {}),
          ...(clearTts ? { tts_key: '' } : tts.trim() ? { tts_key: tts.trim() } : {}),
          tts_region: region,
          tts_voice: voice,
          daily_budget_usd: budget,
        }),
      })
      setLlm('')
      setTts('')
      setClearLlm(false)
      setClearTts(false)
      window.dispatchEvent(new Event('sceneweaver-services-changed'))
      setNotice('已加密保存。请点击检查 LLM 连接，检查通过后再开始游戏。')
    } catch {
      setNotice('保存失败。请先暂停剧情、停止语音并等待生成结束，再检查输入和本机存储。')
    } finally {
      setBusy(false)
    }
  }
  return (
    <details open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>对话模型与预算</summary>
      <p>
        DeepSeek 密钥仅在本机加密保存。对话请求使用你的账户额度，并受下方每日预算限制；留空会保留已有密钥。
      </p>
      <label>
        DeepSeek API Key
        <input
          type="password"
          autoComplete="off"
          disabled={clearLlm}
          value={llm}
          onChange={(e) => setLlm(e.target.value)}
        />
      </label>
      <label>
        <input type="checkbox" checked={clearLlm} onChange={(e) => setClearLlm(e.target.checked)} />
        清除已保存的对话模型密钥
      </label>
      <label>
        每日 API 预算（美元，UTC 日界线）
        <input
          type="number"
          min="0.01"
          max="100"
          step="0.01"
          value={budget}
          onChange={(e) => setBudget(Number(e.target.value))}
        />
      </label>
      <details className="player-credentials__voice">
        <summary>角色语音（可选）</summary>
        <p>语音使用阿里云。未配置时可以只用文字游玩；启用语音前需要填写语音密钥。</p>
        <label>
          阿里云 TTS API Key
          <input
            type="password"
            autoComplete="off"
            disabled={clearTts}
            value={tts}
            onChange={(e) => setTts(e.target.value)}
          />
        </label>
        <label>
          <input type="checkbox" checked={clearTts} onChange={(e) => setClearTts(e.target.checked)} />
          清除已保存的语音密钥
        </label>
        <label>
          语音地域
          <select value={region} onChange={(e) => setRegion(e.target.value)}>
            <option value="ap-southeast-1">新加坡</option>
            <option value="cn-beijing">北京</option>
          </select>
        </label>
        <label>
          系统或已注册音色 ID
          <input value={voice} onChange={(e) => setVoice(e.target.value)} />
        </label>
      </details>
      <button
        disabled={busy || !voice.trim() || !Number.isFinite(budget) || budget <= 0 || budget > 100}
        onClick={() => void save()}
      >
        {busy ? '保存中…' : '加密保存设置'}
      </button>
      <p role="status">{notice}</p>
    </details>
  )
}
