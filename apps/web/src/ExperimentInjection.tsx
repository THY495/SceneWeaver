import { useEffect, useRef, useState } from 'react'
import type { ExperimentPerson } from './ExperimentObservation'

type Hearing = { enabled: boolean; text: string; speaker_known: boolean }
type ReplyKind = 'answer' | 'defer' | 'refuse' | 'silence'
type Exchange = {
  question_id: string
  seq: number
  question: string
  asker_id: string
  asker_name: string
  target_id: string
  target_name: string
  views: { actor_id: string; status: string }[]
}
type ObservationFrame = { exchange_matrix: Exchange[] }
const replyDefaults: Record<ReplyKind, string> = {
  answer: '',
  defer: '我需要再确认一下，稍后回答。',
  refuse: '我拒绝回答这个问题。',
  silence: '（沉默片刻，没有回答这个问题。）',
}
const openStatuses = new Set(['open', 'partial', 'defer'])

export function ExperimentInjection({
  story,
  people,
  refresh,
  request,
  onChanged,
}: {
  story: string
  people: ExperimentPerson[]
  refresh: number
  request: (path: string, init?: RequestInit) => Promise<unknown>
  onChanged: () => void
}) {
  const [mode, setMode] = useState('speech'),
    [speaker, setSpeaker] = useState('')
  const [text, setText] = useState(''),
    [channel, setChannel] = useState('public')
  const [expectsReply, setExpectsReply] = useState(false),
    [replyTarget, setReplyTarget] = useState('')
  const [exchanges, setExchanges] = useState<Exchange[]>([]),
    [question, setQuestion] = useState(''),
    [replyKind, setReplyKind] = useState<ReplyKind>('answer')
  const [hearings, setHearings] = useState<Record<string, Hearing>>({})
  const [key, setKey] = useState(''),
    [occurred, setOccurred] = useState(''),
    [learned, setLearned] = useState('')
  const [basis, setBasis] = useState('rumor'),
    [source, setSource] = useState('主持人')
  const [busy, setBusy] = useState(false),
    [notice, setNotice] = useState('')
  const pending = useRef<{ body: string; path: string } | null>(null)
  const selectedExchange = exchanges.find((item) => item.question_id === question)
  const effectiveSpeaker = mode === 'reply' ? selectedExchange?.target_id || '' : speaker
  const selected = people.filter((p) => hearings[p.id]?.enabled)
  const sameScene = people.filter(
    (p) =>
      p.id !== effectiveSpeaker &&
      p.location === people.find((person) => person.id === effectiveSpeaker)?.location,
  )
  const invalidExcerpt =
    (mode === 'speech' || mode === 'reply') &&
    selected.some((p) => hearings[p.id].text && !text.includes(hearings[p.id].text))
  const valid =
    text.trim() &&
    (mode === 'speech'
      ? speaker &&
        !invalidExcerpt &&
        (channel === 'public' || selected.length) &&
        (!expectsReply ||
          (replyTarget &&
            selected.some(
              (person) =>
                person.id === replyTarget && !hearings[person.id].text && hearings[person.id].speaker_known,
            )))
      : mode === 'reply'
        ? selectedExchange && !invalidExcerpt && (channel === 'public' || selected.length)
        : key.trim() && occurred.trim() && learned.trim() && source.trim() && selected.length)

  useEffect(() => {
    if (mode !== 'reply') return
    let live = true
    request(`/role/stories/${story}/observation-frame`)
      .then((data) => {
        if (!live) return
        const matrix = (data as ObservationFrame).exchange_matrix.filter((item) => {
          const target = item.views.find((view) => view.actor_id === item.target_id)
          return target && openStatuses.has(target.status)
        })
        setExchanges(matrix)
        setQuestion((current) => (matrix.some((item) => item.question_id === current) ? current : ''))
      })
      .catch((error) => {
        if (live) setNotice(String(error))
      })
    return () => {
      live = false
    }
  }, [mode, request, story, refresh])

  function resetRecipients() {
    setHearings({})
    setExpectsReply(false)
    setReplyTarget('')
  }

  function chooseQuestion(id: string) {
    const item = exchanges.find((exchange) => exchange.question_id === id)
    setQuestion(id)
    setChannel('private')
    setReplyKind('answer')
    setText('')
    setHearings(
      item &&
        people.find((person) => person.id === item.asker_id)?.location ===
          people.find((person) => person.id === item.target_id)?.location
        ? { [item.asker_id]: { enabled: true, text: '', speaker_known: true } }
        : {},
    )
  }

  async function submit() {
    if (busy) return
    setBusy(true)
    setNotice('')
    try {
      if (!pending.current) {
        const request_id = crypto.randomUUID()
        const observations = selected.map((p) => ({
          actor_id: p.id,
          text: hearings[p.id].text || null,
          speaker_known: hearings[p.id].speaker_known,
        }))
        const data =
          mode === 'speech'
            ? {
                request_id,
                actor_id: speaker,
                text,
                channel,
                addressees: channel === 'private' ? selected.map((p) => p.id) : [],
                observations,
                expects_reply: expectsReply,
                target_id: expectsReply ? replyTarget : null,
              }
            : mode === 'reply'
              ? {
                  request_id,
                  question_id: question,
                  actor_id: selectedExchange?.target_id,
                  response_kind: replyKind,
                  text,
                  channel,
                  addressees: channel === 'private' ? selected.map((p) => p.id) : [],
                  observations,
                }
              : {
                  request_id,
                  key,
                  value: text,
                  occurred_at: occurred,
                  recipients: selected.map((p) => ({ session_id: p.id, learned_at: learned, basis, source })),
                }
        pending.current = {
          body: JSON.stringify(data),
          path:
            mode === 'speech'
              ? `/role/stories/${story}/speech-events`
              : mode === 'reply'
                ? `/role/stories/${story}/reply-events`
                : `/stories/${story}/events`,
        }
      }
      await request(pending.current.path, { method: 'POST', body: pending.current.body })
      pending.current = null
      setNotice('注入已保存。可刷新各角色视角核对实际收到的记录。')
      onChanged()
    } catch (e) {
      setNotice(String(e))
    } finally {
      setBusy(false)
    }
  }

  const isSpeech = mode === 'speech'
  const isReply = mode === 'reply'
  return (
    <details className="experiment-injection">
      <summary>注入实验事件</summary>
      <p>
        创作者录入测试条件，不调用模型。建议先暂停剧情并记录观察基线；注入会使尚未提交的旧单步许可失效。不会替你恢复运行。
      </p>
      <fieldset disabled={busy || !!pending.current}>
        <label className="field">
          注入类型
          <select
            aria-label="注入类型"
            value={mode}
            onChange={(e) => {
              setMode(e.target.value)
              resetRecipients()
              setQuestion('')
            }}
          >
            <option value="speech">角色发言与实际听闻</option>
            <option value="reply">回应已有请求</option>
            <option value="host">主持人信息（确认／传闻）</option>
          </select>
        </label>
        {isSpeech && (
          <>
            <label className="field">
              发言角色
              <select
                aria-label="发言角色"
                value={speaker}
                onChange={(e) => {
                  setSpeaker(e.target.value)
                  resetRecipients()
                }}
              >
                <option value="">选择角色</option>
                {people.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name} · {p.location}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <input
                aria-label="这句话明确期待回应"
                type="checkbox"
                checked={expectsReply}
                onChange={(event) => {
                  setExpectsReply(event.target.checked)
                  if (!event.target.checked) setReplyTarget('')
                }}
              />{' '}
              这句话明确期待一位角色回应
            </label>
            {expectsReply && (
              <label className="field">
                回应角色
                <select
                  aria-label="明确回应角色"
                  value={replyTarget}
                  onChange={(event) => setReplyTarget(event.target.value)}
                >
                  <option value="">选择完整听到并认出说话者的角色</option>
                  {selected
                    .filter((person) => !hearings[person.id].text && hearings[person.id].speaker_known)
                    .map((person) => (
                      <option key={person.id} value={person.id}>
                        {person.name}
                      </option>
                    ))}
                </select>
              </label>
            )}
          </>
        )}
        {isReply && (
          <>
            <label className="field">
              待回应请求
              <select
                aria-label="待回应请求"
                value={question}
                onChange={(e) => chooseQuestion(e.target.value)}
              >
                <option value="">选择仍待回应的请求</option>
                {exchanges.map((item) => (
                  <option key={item.question_id} value={item.question_id}>
                    #{item.seq} {item.asker_name} → {item.target_name}：{item.question}
                  </option>
                ))}
              </select>
            </label>
            {selectedExchange && (
              <p>
                回应者固定为 <strong>{selectedExchange.target_name}</strong>
                。只有完整听到回应的角色才会获知请求状态变化。
              </p>
            )}
            <label className="field">
              回应结果
              <select
                aria-label="回应结果"
                value={replyKind}
                onChange={(event) => {
                  const next = event.target.value as ReplyKind
                  setReplyKind(next)
                  setText(replyDefaults[next])
                }}
              >
                <option value="answer">回答</option>
                <option value="defer">延后回答</option>
                <option value="refuse">拒绝回答</option>
                <option value="silence">以沉默回应</option>
              </select>
            </label>
          </>
        )}
        {(isSpeech || isReply) && (
          <>
            <label className="field">
              发言频道
              <select aria-label="发言频道" value={channel} onChange={(e) => setChannel(e.target.value)}>
                <option value="public">公开</option>
                <option value="private">私下（勾选角色为接收对象）</option>
              </select>
            </label>
            <p>
              发言者自动知道完整原文。其余同场角色只有勾选后才听到；公开不代表每个人都听到，记录发言也不会确认内容为事实。
            </p>
          </>
        )}
        {mode === 'host' && (
          <>
            <label className="field">
              信息键
              <input
                aria-label="信息键"
                value={key}
                maxLength={100}
                onChange={(e) => setKey(e.target.value)}
              />
            </label>
            <label className="field">
              发生时间
              <input
                aria-label="发生时间"
                value={occurred}
                maxLength={100}
                onChange={(e) => setOccurred(e.target.value)}
              />
            </label>
            <label className="field">
              获知时间
              <input
                aria-label="获知时间"
                value={learned}
                maxLength={100}
                onChange={(e) => setLearned(e.target.value)}
              />
            </label>
            <label className="field">
              信息来源
              <input
                aria-label="信息来源"
                value={source}
                maxLength={100}
                onChange={(e) => setSource(e.target.value)}
              />
            </label>
            <label className="field">
              获知方式
              <select aria-label="获知方式" value={basis} onChange={(e) => setBasis(e.target.value)}>
                <option value="rumor">传闻</option>
                <option value="hearsay">转述</option>
                <option value="witnessed">亲历</option>
                <option value="confirmed">已确认</option>
              </select>
            </label>
            <p>
              本次所选接收者采用同一种获知方式。确认信息冲突时需到原剧情编辑器显式纠正，此处不会自动覆盖。
            </p>
          </>
        )}
        <label className="field">
          注入内容
          <textarea
            aria-label="注入内容"
            value={text}
            maxLength={isSpeech || isReply ? 8000 : 1000}
            onChange={(e) => setText(e.target.value)}
          />
        </label>
        {(isSpeech || isReply ? sameScene : people).map((p) => {
          const hearing = hearings[p.id] || { enabled: false, text: '', speaker_known: true }
          const change = (patch: Partial<Hearing>) =>
            setHearings({ ...hearings, [p.id]: { ...hearing, ...patch } })
          return (
            <div key={p.id}>
              <label>
                <input
                  aria-label={`接收：${p.name}`}
                  type="checkbox"
                  checked={hearing.enabled}
                  onChange={(e) => change({ enabled: e.target.checked })}
                />{' '}
                接收：{p.name}
              </label>
              {(isSpeech || isReply) && hearing.enabled && (
                <>
                  <label className="field">
                    {p.name}听到的片段（留空为全文）
                    <input
                      aria-label={`${p.name}听到的片段（留空为全文）`}
                      value={hearing.text}
                      maxLength={8000}
                      onChange={(e) => change({ text: e.target.value })}
                    />
                  </label>
                  <label>
                    <input
                      aria-label={`${p.name}认出说话者`}
                      type="checkbox"
                      checked={hearing.speaker_known}
                      onChange={(e) => change({ speaker_known: e.target.checked })}
                    />{' '}
                    {p.name}认出说话者
                  </label>
                </>
              )}
            </div>
          )
        })}
      </fieldset>
      {invalidExcerpt && <p role="alert">听到的片段必须逐字包含在原文中。</p>}
      <button disabled={busy || (!pending.current && !valid)} onClick={() => void submit()}>
        {pending.current ? '重试同一注入请求' : '保存实验注入'}
      </button>
      {pending.current && (
        <button
          disabled={busy}
          onClick={() => {
            pending.current = null
            setNotice('已解除输入锁定，原请求结果仍可能已保存；请先刷新观察核对，避免重复注入。')
          }}
        >
          解除输入锁定
        </button>
      )}
      {notice && <p role="status">{notice}</p>}
    </details>
  )
}
