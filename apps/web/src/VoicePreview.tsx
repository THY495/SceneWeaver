import { useEffect, useState } from 'react'

export function VoicePreview({
  request,
}: {
  request: (path: string, init?: RequestInit) => Promise<unknown>
}) {
  const [voices, setVoices] = useState<Record<string, string>>({}),
    [voice, setVoice] = useState('')
  const [text, setText] = useState('你终于回来了。我一直在这里等你。'),
    [direction, setDirection] = useState('')
  const [available, setAvailable] = useState(false),
    [supports, setSupports] = useState(false)
  const [busy, setBusy] = useState(false),
    [notice, setNotice] = useState(''),
    [audio, setAudio] = useState('')
  useEffect(() => {
    let live = true
    request('/voice/config')
      .then((v) => {
        if (!live) return
        const c = v as {
          available: boolean
          voices: Record<string, string>
          capabilities: { natural_language_direction: boolean }
        }
        setAvailable(c.available)
        setVoices(c.voices)
        setVoice(Object.keys(c.voices)[0] || '')
        setSupports(c.capabilities.natural_language_direction)
      })
      .catch(() => {
        if (live) setNotice('无法读取语音配置')
      })
    return () => {
      live = false
    }
  }, [request])
  async function preview() {
    setBusy(true)
    setNotice('正在合成试听…')
    setAudio('')
    try {
      const result = (await request('/voice/preview', {
        method: 'POST',
        body: JSON.stringify({ voice, text, direction: supports ? direction : '' }),
      })) as { audio: string; mime: string }
      setAudio('data:' + result.mime + ';base64,' + result.audio)
      setNotice('合成完成，点击播放试听。')
    } catch (e) {
      setNotice(String(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <section aria-label="音色与语气试听">
      <h3>音色与语气试听</h3>
      <label className="field">
        音色
        <select value={voice} onChange={(e) => setVoice(e.target.value)}>
          {Object.entries(voices).map(([id, name]) => (
            <option key={id} value={id}>
              {name}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        试听台词
        <input maxLength={120} value={text} onChange={(e) => setText(e.target.value)} />
      </label>
      <label className="field">
        语气描述
        <input
          maxLength={160}
          disabled={!supports}
          value={direction}
          onChange={(e) => setDirection(e.target.value)}
          placeholder={supports ? '轻声，松了一口气，尾句略带责备' : '当前适配不支持语气指令'}
        />
      </label>
      <p>合成会将以上台词和语气发送给当前配置的 TTS 服务并计入预算；不会写入剧情。试听最长 30 秒。</p>
      <button disabled={!available || !voice || !text.trim() || busy} onClick={() => void preview()}>
        {busy ? '正在合成…' : '生成试听（消耗 API 额度）'}
      </button>
      {audio && <audio controls src={audio} aria-label="音色试听音频" />}
      <p role="status">{notice}</p>
    </section>
  )
}
