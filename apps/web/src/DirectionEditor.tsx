export type Direction = {
  version: 1
  objective: string
  constraints: string[]
  beats: {
    id: string
    title: string
    objective: string
    participants: string[]
    constraints: string[]
    completion: string
    handoff: 'after_beat' | 'on_question' | 'continuous'
    max_segments: number
  }[]
}

export function DirectionEditor({
  value,
  actors,
  backstage = false,
  onChange,
}: {
  value?: Direction | null
  actors: { alias: string; name: string }[]
  backstage?: boolean
  onChange: (value: Direction | null) => void
}) {
  const createBeat = (): Direction['beats'][number] => ({
    id: crypto.randomUUID(),
    title: '澄清分歧',
    objective: '让相关角色交换各自知道的线索，说明分歧。',
    participants: actors.map((a) => a.alias),
    constraints: backstage
      ? ['只依据各自知情，不将猜测表述为确认事实。']
      : ['不替玩家作决定，不将猜测表述为确认事实。'],
    completion: backstage ? '参与角色在本轮明确推进了段落目标。' : '出现可供玩家追问或行动的信息。',
    handoff: backstage ? 'continuous' : 'after_beat',
    max_segments: 3,
  })
  function edit(index: number, patch: Partial<Direction['beats'][number]>) {
    if (value)
      onChange({ ...value, beats: value.beats.map((b, i) => (i === index ? { ...b, ...patch } : b)) })
  }
  return (
    <section aria-label="场景导演与互动段落">
      <h3>场景导演与互动段落</h3>
      <p>
        {backstage
          ? '配置幕后 NPC 的目标与可核验完成条件。独立游戏会逐段审查，完成后沿唯一出口转场；玩家不在场。'
          : '配置下一小段戏的目标、角色互动和玩家介入点。角色根据各自知情与立场回应，目标不保证一定达成。'}
      </p>
      {!value ? (
        <button
          disabled={!actors.length}
          onClick={() =>
            onChange({
              version: 1,
              objective: '通过角色互动推进本场剧情。',
              constraints: [],
              beats: [createBeat()],
            })
          }
        >
          添加 AI 互动编排
        </button>
      ) : (
        <>
          <label>
            本场戏的目标
            <textarea
              value={value.objective}
              maxLength={2000}
              onChange={(e) => onChange({ ...value, objective: e.target.value })}
            />
          </label>
          <label>
            导演约束（仅供导演，每行一项）
            <textarea
              value={value.constraints.join('\n')}
              onChange={(e) =>
                onChange({ ...value, constraints: e.target.value.split('\n').filter((x) => x.trim()) })
              }
            />
          </label>
          {value.beats.map((b, i) => (
            <fieldset key={b.id}>
              <legend>
                互动段落 {i + 1} · {b.title}
              </legend>
              <label>
                名称
                <input value={b.title} maxLength={160} onChange={(e) => edit(i, { title: e.target.value })} />
              </label>
              <label>
                角色需要推动什么
                <textarea
                  value={b.objective}
                  maxLength={2000}
                  onChange={(e) => edit(i, { objective: e.target.value })}
                />
              </label>
              <div role="group" aria-label={`${b.title}参与角色`}>
                {actors.map((a) => (
                  <label key={a.alias}>
                    <input
                      type="checkbox"
                      checked={b.participants.includes(a.alias)}
                      onChange={(e) =>
                        edit(i, {
                          participants: e.target.checked
                            ? [...b.participants, a.alias]
                            : b.participants.filter((x) => x !== a.alias),
                        })
                      }
                    />
                    {a.name}
                  </label>
                ))}
              </div>
              <label>
                完成条件
                <textarea
                  value={b.completion}
                  maxLength={2000}
                  onChange={(e) => edit(i, { completion: e.target.value })}
                />
              </label>
              {backstage ? (
                <p>幕后不交还玩家；每次 NPC 行动后检查完成条件，并在本场段落全部完成时转场。</p>
              ) : (
                <label>
                  交还玩家
                  <select
                    value={b.handoff}
                    onChange={(e) => edit(i, { handoff: e.target.value as typeof b.handoff })}
                  >
                    <option value="after_beat">本段完成后交还玩家</option>
                    <option value="on_question">向玩家提出问题时交还</option>
                    <option value="continuous">允许继续下一段（仍可中断）</option>
                  </select>
                </label>
              )}
              <details>
                <summary>段落约束与节奏</summary>
                <label>
                  约束（每行一项）
                  <textarea
                    value={b.constraints.join('\n')}
                    onChange={(e) =>
                      edit(i, { constraints: e.target.value.split('\n').filter((x) => x.trim()) })
                    }
                  />
                </label>
                <label>
                  最多连续演出片段
                  <input
                    type="number"
                    min={1}
                    max={6}
                    value={b.max_segments}
                    onChange={(e) => edit(i, { max_segments: Number(e.target.value) })}
                  />
                </label>
              </details>
              <button
                disabled={!i}
                onClick={() => {
                  const beats = [...value.beats]
                  ;[beats[i - 1], beats[i]] = [beats[i], beats[i - 1]]
                  onChange({ ...value, beats })
                }}
              >
                上移
              </button>
              <button
                disabled={i === value.beats.length - 1}
                onClick={() => {
                  const beats = [...value.beats]
                  ;[beats[i + 1], beats[i]] = [beats[i], beats[i + 1]]
                  onChange({ ...value, beats })
                }}
              >
                下移
              </button>
              <button
                disabled={value.beats.length === 1}
                onClick={() => onChange({ ...value, beats: value.beats.filter((x) => x.id !== b.id) })}
              >
                删除段落
              </button>
            </fieldset>
          ))}
          <button
            disabled={value.beats.length >= 32 || !actors.length}
            onClick={() => onChange({ ...value, beats: [...value.beats, createBeat()] })}
          >
            添加互动段落
          </button>
          <button onClick={() => onChange(null)}>移除 AI 互动编排</button>
        </>
      )}
    </section>
  )
}
