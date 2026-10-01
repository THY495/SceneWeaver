import { useEffect, useRef, useState } from 'react'
import { SceneObjects } from './SceneObjects'

type Person = { id: string; name: string; player: boolean; location: string }
export type Communication = { channel: 'public' | 'private'; addressees: string[] }
type Props = {
  sid: string
  typing: boolean
  request: (path: string, init?: RequestInit) => Promise<unknown>
  value: Communication
  onChange: (value: Communication) => void
}

export function PlayerBar({ sid, typing, request, value, onChange }: Props) {
  const [story, setStory] = useState(''),
    [people, setPeople] = useState<Person[]>([]),
    [notice, setNotice] = useState('')
  const busyInput = useRef(typing),
    micBusy = useRef(false)
  busyInput.current = typing
  useEffect(() => {
    let live = true
    setStory('')
    setPeople([])
    onChange({ channel: 'public', addressees: [] })
    request(`/sessions/${sid}`)
      .then(async (raw) => {
        const id = (raw as { story_id?: string }).story_id
        if (!id) return
        const people = (await request(
          `/role/stories/${id}/roster?actor_id=${encodeURIComponent(sid)}`,
        )) as Person[]
        if (live && people.some((p) => p.id === sid && p.player)) {
          setPeople(people)
          setStory(id)
        }
      })
      .catch((e) => {
        if (live) setNotice(String(e))
      })
    return () => {
      live = false
    }
  }, [sid])
  useEffect(() => {
    if (!story) return
    const pulse = () =>
      request(`/role/stories/${story}/presence`, {
        method: 'POST',
        body: JSON.stringify({
          visible: document.visibilityState === 'visible',
          typing: busyInput.current || micBusy.current,
        }),
      }).catch(() => {})
    const mic = (e: Event) => {
      const d = (e as CustomEvent).detail
      if (d.sid === sid) {
        micBusy.current = d.busy
        void pulse()
      }
    }
    window.addEventListener('speech-input-state', mic)
    pulse()
    const timer = setInterval(pulse, 5000)
    document.addEventListener('visibilitychange', pulse)
    return () => {
      window.removeEventListener('speech-input-state', mic)
      clearInterval(timer)
      document.removeEventListener('visibilitychange', pulse)
      request(`/role/stories/${story}/presence`, {
        method: 'POST',
        body: JSON.stringify({ visible: false, typing: false }),
      }).catch(() => {})
    }
  }, [story])
  useEffect(() => {
    if (story)
      request(`/role/stories/${story}/presence`, {
        method: 'POST',
        body: JSON.stringify({
          visible: document.visibilityState === 'visible',
          typing: typing || micBusy.current,
        }),
      }).catch(() => {})
  }, [story, typing])
  if (!story) return notice ? <p role="status">{notice}</p> : null
  const location = people.find((p) => p.id === sid)?.location
  const present = people.filter((p) => !p.player && p.location === location)
  return (
    <div className="player-bar">
      <span>玩家模式 · {location === 'main' ? '当前场景' : location}</span>
      <button
        onClick={() =>
          request(`/role/stories/${story}/roster?actor_id=${encodeURIComponent(sid)}`)
            .then((data) => {
              const roster = data as Person[]
              setPeople(roster)
              const scene = roster.find((p) => p.id === sid)?.location
              onChange({
                ...value,
                addressees: value.addressees.filter((id) =>
                  roster.some((p) => p.id === id && p.location === scene),
                ),
              })
            })
            .catch((e) => setNotice(String(e)))
        }
      >
        刷新在场角色
      </button>
      <select
        aria-label="交流频道"
        value={value.channel}
        onChange={(e) => onChange({ channel: e.target.value as Communication['channel'], addressees: [] })}
      >
        <option value="public">公开交流</option>
        <option value="private">私下交流</option>
      </select>
      <select
        aria-label="交流对象"
        value={value.addressees[0] || ''}
        onChange={(e) => onChange({ ...value, addressees: e.target.value ? [e.target.value] : [] })}
      >
        <option value="">{value.channel === 'private' ? '请选择私下对象' : '所有在场角色'}</option>
        {present.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
          </option>
        ))}
      </select>
      <small>
        {value.channel === 'private' ? '仅你和指定角色感知本次交流。' : '公开发言可被同场角色听到。'}{' '}
        输入时暂停新的主动发言。
      </small>
      <SceneObjects key={story + sid} story={story} actorId={sid} request={request} />
    </div>
  )
}
