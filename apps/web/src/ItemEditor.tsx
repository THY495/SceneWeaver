import { useEffect, useState } from 'react'

type Limit = { mode: 'unset' | 'unlimited' | 'limited'; value: number | null }
type Definition = {
  id: string
  name: string
  tags?: string[]
  requires_container_tags?: string[]
  mass_g: number | null
  external_volume_ml: number | null
  capabilities: { movable: boolean; can_contain: boolean; can_be_contained: boolean }
  container: null | {
    capacity_ml: Limit
    max_contents_mass_g: Limit
    max_items: Limit
    accept_any_tags?: string[]
    reject_any_tags?: string[]
  }
}
type Item = {
  id: string
  definition: Definition
  placement: { kind: 'location' | 'actor' | 'container'; target: string }
  state: { open: boolean; locked: boolean; opaque: boolean }
  owner_id: string | null
}
type Actor = { id: string; location: string; lift_limit_g: Limit; carry_limit_g: Limit }
export type ItemWorld = {
  items: Item[]
  actors: Actor[]
  policy: { check_volume: boolean; check_mass: boolean; max_nesting: number }
}
type Person = { alias: string; name: string; location: string }
type Request = (path: string, init?: RequestInit) => Promise<unknown>
export function syncItemActors(world: ItemWorld, people: Person[]) {
  world.actors = people.map((p) => ({
    ...world.actors.find((a) => a.id === p.alias),
    id: p.alias,
    location: p.location,
    lift_limit_g: world.actors.find((a) => a.id === p.alias)?.lift_limit_g || { mode: 'unset', value: null },
    carry_limit_g: world.actors.find((a) => a.id === p.alias)?.carry_limit_g || {
      mode: 'unset',
      value: null,
    },
  }))
}
function LimitField({ label, value, change }: { label: string; value: Limit; change: (v: Limit) => void }) {
  return (
    <label className="field">
      {label}
      <select
        value={value.mode}
        onChange={(e) =>
          change({ mode: e.target.value as Limit['mode'], value: e.target.value === 'limited' ? 0 : null })
        }
      >
        <option value="unset">未配置</option>
        <option value="unlimited">不限</option>
        <option value="limited">指定数值</option>
      </select>
      {value.mode === 'limited' && (
        <input
          type="number"
          min={0}
          step={1}
          value={value.value ?? 0}
          onChange={(e) => change({ ...value, value: Number(e.target.value) })}
        />
      )}
    </label>
  )
}
export function ItemEditor({
  value,
  people,
  request,
  onChange,
  references = {},
}: {
  value?: ItemWorld | null
  people: Person[]
  request: Request
  onChange: (v: ItemWorld) => void
  references?: Record<string, string[]>
}) {
  const [presets, setPresets] = useState<Definition[]>([]),
    [preset, setPreset] = useState('paper-sheet'),
    [error, setError] = useState(''),
    [busy, setBusy] = useState(false)
  useEffect(() => {
    let live = true
    request('/role/authoring/items/presets')
      .then((v) => {
        if (live) setPresets((v as { presets: Definition[] }).presets)
      })
      .catch((e) => {
        if (live) setError(String(e))
      })
    return () => {
      live = false
    }
  }, [request])
  function edit(fn: (w: ItemWorld) => void) {
    const w = structuredClone(
      value || { items: [], actors: [], policy: { check_mass: false, check_volume: true, max_nesting: 8 } },
    )
    syncItemActors(w, people)
    fn(w)
    onChange(w)
    setError('')
  }
  async function add() {
    setBusy(true)
    try {
      const item = (await request('/role/authoring/items/instantiate', {
        method: 'POST',
        body: JSON.stringify({
          preset_id: preset,
          instance_id: crypto.randomUUID(),
          placement: { kind: 'location', target: people.at(-1)?.location || '大厅' },
        }),
      })) as Item
      edit((w) => w.items.push(item))
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }
  return (
    <section aria-label="初始物品配置">
      <h3>初始物品与容器</h3>
      <p>从模板添加物品，设置开局位置。保存并重新开局后生效。容器内容不会自动成为角色已知信息。</p>
      <label className="field">
        物品模板
        <select value={preset} onChange={(e) => setPreset(e.target.value)}>
          {presets.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </label>
      <button disabled={busy || !presets.length} onClick={() => void add()}>
        添加预设物品
      </button>
      {value && (
        <>
          <label>
            <input
              type="checkbox"
              checked={value.policy.check_mass}
              onChange={(e) =>
                edit((w) => {
                  w.policy.check_mass = e.target.checked
                })
              }
            />
            启用重量与角色负重检查
          </label>
          <label>
            <input
              type="checkbox"
              checked={value.policy.check_volume}
              onChange={(e) =>
                edit((w) => {
                  w.policy.check_volume = e.target.checked
                })
              }
            />
            启用容器容量检查
          </label>
          {value.items.map((item, i) => (
            <fieldset key={item.id}>
              <legend>{item.definition.name}</legend>
              <label className="field">
                名称
                <input
                  value={item.definition.name}
                  onChange={(e) =>
                    edit((w) => {
                      w.items[i].definition.name = e.target.value
                    })
                  }
                />
              </label>
              <label className="field">
                初始位置
                <select
                  value={item.placement.kind}
                  onChange={(e) =>
                    edit((w) => {
                      const kind = e.target.value as Item['placement']['kind']
                      w.items[i].placement = {
                        kind,
                        target:
                          kind === 'actor'
                            ? people[0].alias
                            : kind === 'container'
                              ? w.items.find((x) => x.id !== item.id && x.definition.capabilities.can_contain)
                                  ?.id || ''
                              : people[0].location,
                      }
                    })
                  }
                >
                  <option value="location">场景中</option>
                  <option value="actor">角色持有</option>
                  <option value="container">容器内部</option>
                </select>
              </label>
              {item.placement.kind === 'location' ? (
                <label className="field">
                  地点
                  <input
                    value={item.placement.target}
                    onChange={(e) =>
                      edit((w) => {
                        w.items[i].placement.target = e.target.value
                      })
                    }
                  />
                </label>
              ) : (
                <label className="field">
                  {item.placement.kind === 'actor' ? '持有人' : '所在容器'}
                  <select
                    value={item.placement.target}
                    onChange={(e) =>
                      edit((w) => {
                        w.items[i].placement.target = e.target.value
                      })
                    }
                  >
                    <option value="">请选择</option>
                    {(item.placement.kind === 'actor'
                      ? people.map((p) => ({ id: p.alias, name: p.name }))
                      : value.items
                          .filter((x) => x.id !== item.id && x.definition.capabilities.can_contain)
                          .map((x) => ({ id: x.id, name: x.definition.name }))
                    ).map((x) => (
                      <option key={x.id} value={x.id}>
                        {x.name}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <label className="field">
                所有人（不等于持有人）
                <select
                  value={item.owner_id || ''}
                  onChange={(e) =>
                    edit((w) => {
                      w.items[i].owner_id = e.target.value || null
                    })
                  }
                >
                  <option value="">未指定</option>
                  {people.map((p) => (
                    <option key={p.alias} value={p.alias}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </label>
              {item.definition.container && (
                <>
                  <label>
                    <input
                      type="checkbox"
                      checked={item.state.open}
                      disabled={item.state.locked}
                      onChange={(e) =>
                        edit((w) => {
                          w.items[i].state.open = e.target.checked
                        })
                      }
                    />
                    初始打开
                  </label>
                  <label>
                    <input
                      type="checkbox"
                      checked={item.state.locked}
                      onChange={(e) =>
                        edit((w) => {
                          w.items[i].state.locked = e.target.checked
                          if (e.target.checked) w.items[i].state.open = false
                        })
                      }
                    />
                    初始上锁（本版尚无解锁操作）
                  </label>
                  <label>
                    <input
                      type="checkbox"
                      checked={item.state.opaque}
                      onChange={(e) =>
                        edit((w) => {
                          w.items[i].state.opaque = e.target.checked
                        })
                      }
                    />
                    不透明
                  </label>
                </>
              )}
              <details>
                <summary>数值与能力</summary>
                <label>
                  <input
                    type="checkbox"
                    checked={item.definition.capabilities.can_contain}
                    disabled={value.items.some(
                      (x) => x.placement.kind === 'container' && x.placement.target === item.id,
                    )}
                    onChange={(e) =>
                      edit((w) => {
                        const target = w.items[i]
                        target.definition.capabilities.can_contain = e.target.checked
                        target.definition.container = e.target.checked
                          ? {
                              capacity_ml: { mode: 'unset', value: null },
                              max_contents_mass_g: { mode: 'unset', value: null },
                              max_items: { mode: 'unset', value: null },
                              accept_any_tags: [],
                              reject_any_tags: [],
                            }
                          : null
                        if (!e.target.checked) {
                          target.state.open = false
                          target.state.locked = false
                        }
                      })
                    }
                  />
                  可容纳其它物品（有内容物时请先移出）
                </label>
                <p>
                  标签用于限制哪些物品能放入容器，不代表角色自动知道这些属性。多个允许标签满足任意一个即可，禁止标签优先。
                </p>
                {(['tags', 'requires_container_tags'] as const).map((key) => (
                  <label className="field" key={key}>
                    {key === 'tags'
                      ? '物品标签（逗号分隔）'
                      : '容器必须具备以下全部标签（逗号分隔，留空不限）'}
                    <input
                      defaultValue={(item.definition[key] || []).join(', ')}
                      key={(item.definition[key] || []).join(', ')}
                      onBlur={(e) =>
                        edit((w) => {
                          w.items[i].definition[key] = [
                            ...new Set(
                              e.target.value
                                .split(/[,，]/)
                                .map((s) => s.trim())
                                .filter(Boolean),
                            ),
                          ]
                        })
                      }
                    />
                  </label>
                ))}
                {item.definition.container &&
                  (['accept_any_tags', 'reject_any_tags'] as const).map((key) => (
                    <label className="field" key={key}>
                      {key === 'accept_any_tags'
                        ? '仅接受带以下任一标签的物品（留空不限）'
                        : '拒绝带以下任一标签的物品'}
                      <input
                        defaultValue={(item.definition.container![key] || []).join(', ')}
                        key={(item.definition.container![key] || []).join(', ')}
                        onBlur={(e) =>
                          edit((w) => {
                            w.items[i].definition.container![key] = [
                              ...new Set(
                                e.target.value
                                  .split(/[,，]/)
                                  .map((s) => s.trim())
                                  .filter(Boolean),
                              ),
                            ]
                          })
                        }
                      />
                    </label>
                  ))}
                {(['mass_g', 'external_volume_ml'] as const).map((k) => (
                  <label className="field" key={k}>
                    {k === 'mass_g' ? '自身重量（g）' : '外部体积（ml）'}
                    <input
                      type="number"
                      min={0}
                      step={1}
                      value={item.definition[k] ?? ''}
                      placeholder="未配置"
                      onChange={(e) =>
                        edit((w) => {
                          w.items[i].definition[k] = e.target.value === '' ? null : Number(e.target.value)
                        })
                      }
                    />
                  </label>
                ))}
                <label>
                  <input
                    type="checkbox"
                    checked={item.definition.capabilities.movable}
                    onChange={(e) =>
                      edit((w) => {
                        w.items[i].definition.capabilities.movable = e.target.checked
                      })
                    }
                  />
                  可携带
                </label>
                <label>
                  <input
                    type="checkbox"
                    checked={item.definition.capabilities.can_be_contained}
                    onChange={(e) =>
                      edit((w) => {
                        w.items[i].definition.capabilities.can_be_contained = e.target.checked
                      })
                    }
                  />
                  可被容纳
                </label>
                {item.definition.container &&
                  (['capacity_ml', 'max_contents_mass_g', 'max_items'] as const).map((k) => (
                    <LimitField
                      key={k}
                      label={
                        {
                          capacity_ml: '内部容量（ml）',
                          max_contents_mass_g: '内容物承重（g）',
                          max_items: '内容物件数',
                        }[k]
                      }
                      value={item.definition.container![k]}
                      change={(v) =>
                        edit((w) => {
                          w.items[i].definition.container![k] = v
                        })
                      }
                    />
                  ))}
              </details>
              <button
                disabled={
                  !!references[item.id]?.length ||
                  value.items.some((x) => x.placement.kind === 'container' && x.placement.target === item.id)
                }
                onClick={() =>
                  edit((w) => {
                    w.items.splice(i, 1)
                  })
                }
              >
                移除物品
              </button>
              {!!references[item.id]?.length && (
                <small>请先解除引用：{references[item.id].join('、')}。移除物品不会自动放宽剧情条件。</small>
              )}
              {value.items.some(
                (x) => x.placement.kind === 'container' && x.placement.target === item.id,
              ) && <small>请先移出内容物再移除容器。</small>}
            </fieldset>
          ))}
          {value.policy.check_mass &&
            value.actors.map((a, i) => (
              <fieldset key={a.id}>
                <legend>{people.find((p) => p.alias === a.id)?.name}的搬运限制</legend>
                <LimitField
                  label="单次搬运上限（g）"
                  value={a.lift_limit_g}
                  change={(v) =>
                    edit((w) => {
                      w.actors[i].lift_limit_g = v
                    })
                  }
                />
                <LimitField
                  label="总负重上限（g）"
                  value={a.carry_limit_g}
                  change={(v) =>
                    edit((w) => {
                      w.actors[i].carry_limit_g = v
                    })
                  }
                />
              </fieldset>
            ))}
        </>
      )}
      <p role="status">{error}</p>
    </section>
  )
}
