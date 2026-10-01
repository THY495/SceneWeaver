import { useState } from 'react'
import { Sprite } from './CharacterStage'
import {
  convertLegacyPortrait,
  resolveAppearance,
  type CharacterPresentation,
  type AppearanceSelection,
} from './outfitModel'

export function OutfitSelector({
  presentation,
  outfit,
  variant,
  onChange,
}: AppearanceSelection & {
  presentation?: CharacterPresentation
  onChange: (v: AppearanceSelection) => void
}) {
  const group =
    (presentation?.outfits || []).find((o) => o.id === outfit) ||
    (presentation?.outfits || []).find((o) => o.id === presentation?.default_outfit) ||
    presentation?.outfits?.[0]
  if (!group) return null
  return (
    <>
      <label>
        造型
        <select
          value={outfit || ''}
          onChange={(e) => onChange({ outfit: e.target.value || null, variant: null })}
        >
          <option value="">沿用默认造型</option>
          {presentation?.outfits?.map((o) => (
            <option key={o.id} value={o.id}>
              {o.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        表情／动作
        <select
          value={variant || ''}
          onChange={(e) => onChange({ outfit: outfit || null, variant: e.target.value || null })}
        >
          <option value="">造型默认表情／动作</option>
          {group.variants.map((v) => (
            <option key={v.id} value={v.id}>
              {v.name}
            </option>
          ))}
        </select>
      </label>
    </>
  )
}

export function OutfitEditor({
  presentation,
  assets = [],
  request,
  onChange,
}: {
  presentation: CharacterPresentation
  assets?: { content_hash: string; name: string }[]
  request: (path: string, init?: RequestInit) => Promise<unknown>
  onChange: (p: CharacterPresentation) => void
}) {
  const [selected, setSelected] = useState('')
  const groups = presentation.outfits || [],
    group = groups.find((o) => o.id === selected) || groups[0]
  function edit(fn: (p: CharacterPresentation) => void) {
    const next = structuredClone(presentation)
    next.version = 1
    fn(next)
    onChange(next)
  }
  const images = [...assets]
  for (const hash of [presentation.base_sprite, ...groups.flatMap((o) => o.variants.map((v) => v.sprite))])
    if (hash && !images.some((a) => a.content_hash === hash))
      images.push({ content_hash: hash, name: '已有立绘 ' + hash.slice(0, 8) })
  return (
    <fieldset aria-label="人物造型与表情">
      <legend>造型与表情／动作</legend>
      <p>不同服装建立不同造型；同一服装的表情和动作放在同一造型中。没有覆盖时使用默认设置。</p>
      {!groups.length && presentation.base_sprite && (
        <button onClick={() => onChange(convertLegacyPortrait(presentation))}>
          将旧单立绘转换为常态造型
        </button>
      )}
      <label>
        默认造型
        <select
          value={presentation.default_outfit || ''}
          onChange={(e) =>
            edit((p) => {
              p.default_outfit = e.target.value || null
            })
          }
        >
          <option value="">第一个造型</option>
          {groups.map((o) => (
            <option key={o.id} value={o.id}>
              {o.name}
            </option>
          ))}
        </select>
      </label>
      <label>
        编辑造型
        <select value={group?.id || ''} onChange={(e) => setSelected(e.target.value)}>
          {groups.map((o) => (
            <option key={o.id} value={o.id}>
              {o.name}
            </option>
          ))}
        </select>
      </label>
      <button
        disabled={!images.length || groups.length >= 16}
        onClick={() => {
          const id = crypto.randomUUID()
          edit((p) => {
            p.outfits = [
              ...groups,
              {
                id,
                name: '新造型',
                default_variant: 'neutral',
                variants: [
                  {
                    id: 'neutral',
                    name: '常态',
                    sprite: resolveAppearance(presentation).sprite || images[0].content_hash,
                  },
                ],
              },
            ]
          })
          setSelected(id)
        }}
      >
        新增造型
      </button>
      {!images.length && <p>先在工程素材中导入立绘图片。</p>}
      {group && (
        <>
          <label>
            造型名称
            <input
              value={group.name}
              maxLength={80}
              onChange={(e) =>
                edit((p) => {
                  p.outfits!.find((o) => o.id === group.id)!.name = e.target.value
                })
              }
            />
          </label>
          <label>
            默认表情／动作
            <select
              value={group.default_variant}
              onChange={(e) =>
                edit((p) => {
                  p.outfits!.find((o) => o.id === group.id)!.default_variant = e.target.value
                })
              }
            >
              {group.variants.map((v) => (
                <option key={v.id} value={v.id}>
                  {v.name}
                </option>
              ))}
            </select>
          </label>
          {group.variants.map((v) => (
            <fieldset key={v.id}>
              <legend>{v.name}</legend>
              <div style={{ maxWidth: 120, height: 120, overflow: 'hidden' }}>
                <Sprite id={v.sprite} name={v.name} request={request} />
              </div>
              <label>
                变体名称
                <input
                  value={v.name}
                  maxLength={80}
                  onChange={(e) =>
                    edit((p) => {
                      p.outfits!.find((o) => o.id === group.id)!.variants.find((x) => x.id === v.id)!.name =
                        e.target.value
                    })
                  }
                />
              </label>
              <label>
                图片
                <select
                  value={v.sprite}
                  onChange={(e) =>
                    edit((p) => {
                      p.outfits!.find((o) => o.id === group.id)!.variants.find((x) => x.id === v.id)!.sprite =
                        e.target.value
                    })
                  }
                >
                  {images.map((a) => (
                    <option key={a.content_hash} value={a.content_hash}>
                      {a.name}
                    </option>
                  ))}
                </select>
              </label>
              {(['expression', 'pose'] as const).map((key) => (
                <label key={key}>
                  {key === 'expression' ? 'AI 表情映射' : 'AI 动作映射'}
                  <select
                    value={v[key] || ''}
                    onChange={(e) =>
                      edit((p) => {
                        p.outfits!.find((o) => o.id === group.id)!.variants.find((x) => x.id === v.id)![key] =
                          e.target.value
                      })
                    }
                  >
                    <option value="">不限制此项</option>
                    {(key === 'expression'
                      ? [
                          ['neutral', '自然'],
                          ['happy', '开心'],
                          ['sad', '悲伤'],
                          ['angry', '生气'],
                          ['surprised', '惊讶'],
                          ['concerned', '担忧'],
                        ]
                      : [
                          ['neutral', '常态'],
                          ['arms_crossed', '抱臂'],
                          ['thinking', '思考'],
                          ['nodding', '点头'],
                        ]
                    ).map(([value, label]) => (
                      <option key={value} value={value}>
                        {label}
                      </option>
                    ))}
                    {v[key] &&
                      !(
                        key === 'expression'
                          ? ['neutral', 'happy', 'sad', 'angry', 'surprised', 'concerned']
                          : ['neutral', 'arms_crossed', 'thinking', 'nodding']
                      ).includes(v[key]!) && (
                        <option value={v[key]}>{v[key]}（旧自定义值，请重新映射）</option>
                      )}
                  </select>
                </label>
              ))}
              <button
                disabled={group.variants.length === 1}
                onClick={() =>
                  edit((p) => {
                    const o = p.outfits!.find((o) => o.id === group.id)!
                    o.variants = o.variants.filter((x) => x.id !== v.id)
                    if (o.default_variant === v.id) o.default_variant = o.variants[0].id
                  })
                }
              >
                删除变体
              </button>
            </fieldset>
          ))}
          <button
            disabled={group.variants.length >= 32 || !images.length}
            onClick={() =>
              edit((p) => {
                p.outfits!.find((o) => o.id === group.id)!.variants.push({
                  id: crypto.randomUUID(),
                  name: '新表情／动作',
                  sprite:
                    resolveAppearance(presentation, { outfit: group.id }).sprite || images[0].content_hash,
                })
              })
            }
          >
            新增表情／动作
          </button>
          <button
            onClick={() =>
              edit((p) => {
                p.outfits = groups.filter((o) => o.id !== group.id)
                if (p.default_outfit === group.id) p.default_outfit = p.outfits[0]?.id || null
              })
            }
          >
            删除当前造型
          </button>
        </>
      )}
      <small>
        当前默认立绘：{resolveAppearance(presentation).sprite ? '已配置' : '未配置'}
        。删除仍被场景引用的造型后，需要重新选择场景造型才能运行。
      </small>
    </fieldset>
  )
}
