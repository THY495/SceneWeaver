import { useEffect, useRef, useState } from 'react'

type Request = (path: string, init?: RequestInit) => Promise<unknown>
export function Sprite({
  id,
  name,
  request,
  background = false,
}: {
  id: string
  name: string
  request: Request
  background?: boolean
}) {
  const [url, setUrl] = useState('')
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let live = true
    setUrl('')
    setFailed(false)
    void request('/role/presentation/sprites/' + id)
      .then((value) => {
        if (live) setUrl((value as { data_url: string }).data_url)
      })
      .catch(() => {
        if (live) setFailed(true)
      })
    return () => {
      live = false
    }
  }, [id])
  return url && !failed ? (
    <img
      className={background ? 'scene-background' : 'character-sprite'}
      src={url}
      alt={background ? `${name}的背景` : `${name}的常态立绘`}
      onError={() => setFailed(true)}
    />
  ) : (
    <span>{failed ? '图片暂不可用' : '读取图片…'}</span>
  )
}

export function SpriteEditor({
  value,
  request,
  onChange,
  background = false,
  choices = [],
}: {
  value?: string | null
  request: Request
  onChange: (id: string | null) => void
  background?: boolean
  choices?: { id: string; label: string }[]
}) {
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [browse, setBrowse] = useState(false)
  const [query, setQuery] = useState('')
  const [limit, setLimit] = useState(6)
  const assetName = background ? '背景' : '立绘'
  const usageName = background ? '场景' : '角色'
  const matches = choices.filter((item) =>
    item.label.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()),
  )
  const callback = useRef(onChange)
  callback.current = onChange
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])
  return (
    <div className="sprite-editor">
      <section aria-label={`工程${assetName}选择`}>
        <button aria-expanded={browse} onClick={() => setBrowse(!browse)}>
          {browse ? `收起${assetName}选择` : `选择工程${assetName} · ${choices.length}`}
        </button>
        {browse && (
          <>
            {choices.length ? (
              <>
                <label>
                  按使用{usageName}筛选
                  <input
                    type="search"
                    value={query}
                    onChange={(e) => {
                      setQuery(e.target.value)
                      setLimit(6)
                    }}
                  />
                </label>
                <div className="project-background-grid">
                  {matches.slice(0, limit).map((item) => (
                    <button
                      key={item.id}
                      className="project-background-choice"
                      disabled={busy}
                      aria-label={`使用${assetName}：${item.label}`}
                      aria-pressed={value === item.id}
                      onClick={() => onChange(item.id)}
                    >
                      <Sprite id={item.id} name={item.label} request={request} background={background} />
                      <span>
                        {value === item.id ? `当前${assetName} · ` : ''}
                        {item.label}
                      </span>
                    </button>
                  ))}
                </div>
                {!matches.length && (
                  <p role="status">
                    没有匹配的使用{usageName}。可清空筛选或上传新{assetName}。
                  </p>
                )}
                {matches.length > limit && <button onClick={() => setLimit(limit + 6)}>再显示 6 张</button>}
              </>
            ) : (
              <p>
                本工程还没有可复用的{assetName}。上传第一张后，可在其他{usageName}中选择。
              </p>
            )}
            <small>这里展示工程已引用的图片；替换或移除此处引用不会修改其他{usageName}。</small>
          </>
        )}
      </section>
      <label>
        {background ? '场景背景' : '常态立绘'}（PNG，最多 4 MB）
        <input
          type="file"
          accept="image/png"
          disabled={busy}
          onChange={async (e) => {
            const file = e.target.files?.[0]
            if (!file) return
            e.target.value = ''
            if (file.size > 4 * 1024 * 1024) {
              setError('图片不能超过 4 MB')
              return
            }
            setBusy(true)
            setError('')
            try {
              const form = new FormData()
              form.append('file', file)
              const data = (await request('/role/presentation/sprites', { method: 'POST', body: form })) as {
                id: string
              }
              if (mounted.current) callback.current(data.id)
            } catch (e) {
              setError(String(e))
            } finally {
              setBusy(false)
            }
          }}
        />
      </label>
      {value && (
        <>
          <Sprite id={value} name={background ? '场景' : '角色'} request={request} background={background} />
          <button disabled={busy} onClick={() => onChange(null)}>
            {background ? '移除背景引用' : '移除此立绘引用'}
          </button>
        </>
      )}
      {busy && <small>正在上传…</small>}
      {error && <p role="alert">{error}</p>}
    </div>
  )
}

type Stage = {
  characters: {
    id: string
    name: string
    base_sprite?: string | null
    position?: 'left' | 'center' | 'right'
  }[]
  last_speech: {
    speaker_id: string | null
    text: string
    segments: { expression: string; pose: string }[]
  } | null
}
const expressions: Record<string, string> = {
  neutral: '平静',
  happy: '愉快',
  sad: '难过',
  angry: '生气',
  surprised: '惊讶',
  concerned: '担忧',
}
const poses: Record<string, string> = { neutral: '', arms_crossed: '抱臂', thinking: '思考', nodding: '点头' }
export function CharacterStage({ sid, request }: { sid: string; request: Request }) {
  const [stage, setStage] = useState<Stage | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    let live = true
    let timer: ReturnType<typeof setTimeout>
    setStage(null)
    async function refresh() {
      try {
        const result = (await request('/role/presentation/stage/' + sid)) as Stage
        if (live) {
          setStage(result)
          setError('')
        }
      } catch (e) {
        if (live) setError(String(e))
      } finally {
        if (live) timer = setTimeout(() => void refresh(), 4000)
      }
    }
    void refresh()
    return () => {
      live = false
      clearTimeout(timer)
    }
  }, [sid])
  // Existing text-only stories retain their compact layout.
  if (!stage?.characters.some((c) => c.base_sprite || c.position))
    return error ? <small role="alert">{error}</small> : null
  return (
    <section
      className={
        stage.characters.some((c) => c.position) ? 'character-stage arranged-stage' : 'character-stage'
      }
      aria-label="画面人物"
    >
      {stage.characters.map((c) => {
        const performance = stage.last_speech?.speaker_id === c.id ? stage.last_speech.segments.at(-1) : null
        return (
          <figure
            key={c.id}
            style={c.position ? { gridColumn: { left: 1, center: 2, right: 3 }[c.position] } : undefined}
            className={stage.last_speech?.speaker_id === c.id ? 'speaking' : ''}
          >
            {c.base_sprite && <Sprite id={c.base_sprite} name={c.name} request={request} />}
            <figcaption>
              {c.name}
              {performance && (
                <small>
                  {' '}
                  · {expressions[performance.expression]} {poses[performance.pose]}
                </small>
              )}
            </figcaption>
          </figure>
        )
      })}
    </section>
  )
}
