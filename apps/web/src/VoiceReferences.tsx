import { useEffect, useState } from 'react'

type Item = {
  id: string
  name: string
  character: string
  duration_seconds: number
  enrollment_status: string
  voice_id?: string
}
export function VoiceReferences({
  project,
  request,
}: {
  project: string
  request: (path: string, init?: RequestInit) => Promise<unknown>
}) {
  const [items, setItems] = useState<Item[]>([]),
    [notice, setNotice] = useState(''),
    [busy, setBusy] = useState(false)
  const [name, setName] = useState(''),
    [character, setCharacter] = useState(''),
    [transcript, setTranscript] = useState('')
  const [file, setFile] = useState<File | null>(null),
    [rights, setRights] = useState(false),
    [redistribute, setRedistribute] = useState(false)
  const [selected, setSelected] = useState(''),
    [url, setUrl] = useState(''),
    [consent, setConsent] = useState(false)
  const base = `/role/projects/${project}/voice-references`
  async function refresh() {
    setItems((await request(base)) as Item[])
  }
  useEffect(() => {
    void refresh().catch(() => setNotice('请先保存为本机工程，再管理参考音频。'))
  }, [project, request])
  async function upload() {
    if (!file) return
    setBusy(true)
    try {
      if (file.size > 3000000) throw new Error('文件超过 3MB')
      const data = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader()
        reader.onload = () => resolve(String(reader.result).split(',')[1])
        reader.onerror = reject
        reader.readAsDataURL(file)
      })
      await request(base, {
        method: 'POST',
        body: JSON.stringify({
          name,
          character,
          transcript,
          wav_base64: data,
          rights_confirmed: rights,
          redistribution_allowed: redistribute,
        }),
      })
      await refresh()
      setNotice('已保存到本机工程，没有上传至语音供应商。')
    } catch {
      setNotice('保存失败：请检查工程是否已保存，以及音频格式和授权信息。')
    } finally {
      setBusy(false)
    }
  }
  async function enroll() {
    setBusy(true)
    try {
      const result = (await request(`${base}/${selected}/enroll`, {
        method: 'POST',
        body: JSON.stringify({ audio_url: url, consent_aliyun: consent, same_audio_confirmed: consent }),
      })) as { voice_id: string }
      setNotice(`注册成功，音色 ID：${result.voice_id}。可在音色配置中使用。`)
      await refresh()
    } catch {
      setNotice('注册未确认成功。请核实供应商音色列表，勿反复提交导致重复收费。')
    } finally {
      setBusy(false)
    }
  }
  return (
    <details>
      <summary>角色参考音频与阿里云音色注册</summary>
      <p>
        参考音频存于当前工程的 voice-references 目录；当前故事包导出尚不携带它。请使用 3–30
        秒、16–48kHz、单声道 PCM16 WAV。
      </p>
      <label>
        音色名称
        <input value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label>
        关联角色
        <input value={character} onChange={(e) => setCharacter(e.target.value)} />
      </label>
      <label>
        逐字稿
        <textarea value={transcript} onChange={(e) => setTranscript(e.target.value)} />
      </label>
      <label>
        参考录音
        <input type="file" accept=".wav,audio/wav" onChange={(e) => setFile(e.target.files?.[0] || null)} />
      </label>
      <label>
        <input type="checkbox" checked={rights} onChange={(e) => setRights(e.target.checked)} />
        我有权用此录音制作角色音色
      </label>
      <label>
        <input type="checkbox" checked={redistribute} onChange={(e) => setRedistribute(e.target.checked)} />
        授权允许原始录音随作品分发
      </label>
      <button
        disabled={busy || !file || !rights || !name.trim() || !character.trim() || !transcript.trim()}
        onClick={() => void upload()}
      >
        保存参考录音到工程
      </button>
      <ul>
        {items.map((item) => (
          <li key={item.id}>
            {item.character} · {item.name} · {item.duration_seconds.toFixed(1)} 秒 · {item.enrollment_status}
            {item.voice_id ? ` · ${item.voice_id}` : ''}
          </li>
        ))}
      </ul>
      <p>
        阿里云此模型要求可访问的 HTTPS 录音地址，不接受直接提交本地
        WAV。这里不会自动把录音公开到互联网。注册默认每次预留 0.05
        美元（可由部署配置调整），实际以供应商账单为准。
      </p>
      <label>
        选择参考音频
        <select value={selected} onChange={(e) => setSelected(e.target.value)}>
          <option value="">请选择</option>
          {items.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        同一录音的 HTTPS 地址
        <input type="url" value={url} onChange={(e) => setUrl(e.target.value)} />
      </label>
      <label>
        <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
        该地址对应上面的授权录音；同意将地址发送给阿里云用于复刻并产生费用
      </label>
      <button
        disabled={busy || !selected || !url.startsWith('https://') || !consent}
        onClick={() => void enroll()}
      >
        在当前阿里云账号注册音色
      </button>
      <p role="status">{notice}</p>
    </details>
  )
}
