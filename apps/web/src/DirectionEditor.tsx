export type Direction = {
  version: 1
  objective: string
  constraints: string[]
  beats: {
    id: string
    title: string
    objective: string
    participants: string[]
    role_focus?: Record<string, string>
    constraints: string[]
    completion: string
    handoff: 'after_beat' | 'on_question' | 'continuous'
    max_segments: number
    npc_exchange_required?: boolean
    source_response_tasks?: boolean
    direct_response_actor?: string | null
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
    objective: '让参与角色围绕本场目标表达各自判断；后发言者可承接前人的具体观点。',
    participants: actors.map((a) => a.alias),
    role_focus: {},
    constraints: backstage
      ? ['只依据各自知情，不将猜测表述为确认事实。']
      : ['不替玩家作决定，不将猜测表述为确认事实。'],
    completion: backstage ? '参与角色在本轮明确推进了段落目标。' : '出现可供玩家追问或行动的信息。',
    handoff: backstage ? 'continuous' : 'after_beat',
    max_segments: 3,
    source_response_tasks: false,
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
          <p>
            这里写全场共同遵守的事实边界和演出规则；需要第一次告诉玩家的内容，请明确安排在开场或相应段落目标中。
          </p>
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
                交流要求
                <select
                  value={b.npc_exchange_required ? 'required' : 'free'}
                  onChange={(e) => edit(i, { npc_exchange_required: e.target.value === 'required' })}
                >
                  <option value="free">由导演自由编排</option>
                  <option value="required">本轮至少有一次 NPC 直接向另一 NPC 发言</option>
                </select>
              </label>
              {b.npc_exchange_required && (
                <p role="note">
                  需至少两位角色实际在场并能感知对方。只向玩家报告不能满足要求；发问仍需预留答复位置。
                </p>
              )}
              <label>
                角色需要推动什么
                <textarea
                  value={b.objective}
                  maxLength={2000}
                  onChange={(e) => edit(i, { objective: e.target.value })}
                />
              </label>
              <p>
                多人对话可写清各角色关注的不同问题。共通知识通常由一人交代，再让后续角色回应；首次告知或纠错时仍要说清关键事实。
              </p>
              {b.participants.length > 1 && (
                <details>
                  <summary>多人对话目标示例</summary>
                  <p>
                    例如：甲先说明已确认的消息并表达自己的判断；乙回应甲的决定，提出自己真正关心的问题；丙承接乙的顾虑，再交还玩家。若人物设定没有不同关注点，可减少发言人数或先补充人物设定。
                  </p>
                </details>
              )}
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
                          role_focus: e.target.checked
                            ? b.role_focus || {}
                            : Object.fromEntries(
                                Object.entries(b.role_focus || {}).filter(([alias]) => alias !== a.alias),
                              ),
                          direct_response_actor:
                            !e.target.checked && b.direct_response_actor === a.alias
                              ? null
                              : b.direct_response_actor,
                        })
                      }
                    />
                    {a.name}
                  </label>
                ))}
              </div>
              {b.participants.length > 1 && (
                <div role="group" aria-label={`${b.title}角色关注点`}>
                  <p>
                    可选：写本段每位角色关心的不同问题或选择。导演会参考，角色仍按自身知情和立场决定怎么说。
                  </p>
                  {actors
                    .filter((a) => b.participants.includes(a.alias))
                    .map((a) => (
                      <label key={a.alias}>
                        {a.name}的关注点
                        <input
                          value={b.role_focus?.[a.alias] || ''}
                          maxLength={500}
                          placeholder="例如：是否愿意冒险开船；不要填写未确认的经历"
                          onChange={(e) => {
                            const role_focus = { ...b.role_focus }
                            if (e.target.value.trim()) role_focus[a.alias] = e.target.value
                            else delete role_focus[a.alias]
                            edit(i, { role_focus })
                          }}
                        />
                      </label>
                    ))}
                </div>
              )}
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
                  <small>
                    本段完成后交还：普通问句可等其余角色说完再回答；需要玩家先执行动作时仍会提前等待。
                  </small>
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
              <details>
                <summary>按实际发言安排回应</summary>
                <label>
                  <input
                    type="checkbox"
                    checked={b.source_response_tasks === true}
                    onChange={(e) => edit(i, { source_response_tasks: e.target.checked })}
                  />
                  允许导演引用已发表的原话安排回应（默认关闭）
                </label>
                <p>
                  适合角色回应玩家或其他角色刚说过的话。角色只能使用本人实际听闻的内容，
                  可以同意、质疑、拒绝或承认未知。听见一句话不代表这句话已经成为世界事实；
                  最终台词仍须审查。需要额外演出目标的任务继续走通常审查。
                </p>
                {b.source_response_tasks && (
                  <p role="status">
                    {value.constraints.length ||
                    b.constraints.length ||
                    Object.keys(b.role_focus || {}).length
                      ? '已在草稿中勾选，但本场导演约束、段落约束或角色关注点使本段暂不使用此方式。已有设置会完整保留；请按剧情需要选择，不必为了速度删掉约束。'
                      : '本段草稿允许这种回应方式，导演会按当前互动选择是否使用。它不会强制所有角色发言，也不保证每轮更快。'}
                  </p>
                )}
                <small>此处修改需要通过工作室的保存操作写入工程。</small>
              </details>
              {b.participants.length > 1 && b.max_segments <= b.participants.length && (
                <p role="note">
                  {b.max_segments < b.participants.length
                    ? '当前片段数不足以让每位参与角色都发言一次。'
                    : '若每位参与角色都发言一次，当前片段数将全部用完。'}
                  NPC 追问与回复各占一次；若需要往返讨论，
                  可增加片段上限或拆成多个段落。较长演出会增加玩家等待和模型开销。
                </p>
              )}
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
