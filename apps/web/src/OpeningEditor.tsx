import { useState } from 'react'
import { StagePlayer, type StagePerson } from './StagePlayer'
import { OutfitSelector } from './OutfitEditor'
import { resolveAppearance, type CharacterPresentation } from './outfitModel'
import type { SceneObject } from './sceneObjectModel'

export type OpeningBlock = {
  id: string
  kind: 'narration' | 'dialogue'
  text: string
  speaker?: string | null
  outfit?: string | null
  variant?: string | null
}

export function OpeningEditor({
  blocks,
  legacy,
  actors,
  title,
  background,
  visualMode,
  people,
  request,
  onChange,
  objects,
  directed = false,
}: {
  directed?: boolean
  objects?: SceneObject[]
  blocks?: OpeningBlock[] | null
  legacy: string
  actors: { alias: string; name: string; presentation?: CharacterPresentation }[]
  title: string
  background?: string | null
  visualMode?: 'stage' | 'cg'
  people: StagePerson[]
  request: (path: string, init?: RequestInit) => Promise<unknown>
  onChange: (blocks: OpeningBlock[]) => void
}) {
  const [selected, setSelected] = useState('')
  const list = blocks || [{ id: 'opening', kind: 'narration' as const, text: legacy }]
  const index = Math.max(
      0,
      list.findIndex((b) => b.id === selected),
    ),
    block = list[index]
  function edit(patch: Partial<OpeningBlock>) {
    onChange(list.map((b, i) => (i === index ? { ...b, ...patch } : b)))
  }
  function add(kind: OpeningBlock['kind']) {
    const id = crypto.randomUUID()
    onChange([
      ...list,
      {
        id,
        kind,
        text: kind === 'narration' ? '填写本段旁白。' : '填写人物台词。',
        ...(kind === 'dialogue' ? { speaker: actors[0]?.alias } : {}),
      },
    ])
    setSelected(id)
  }
  function move(delta: number) {
    const next = [...list]
    ;[next[index], next[index + delta]] = [next[index + delta], next[index]]
    onChange(next)
    setSelected(block.id)
  }
  return (
    <section className="opening-workspace" aria-label="场内开场编排">
      <h3>开场编排</h3>
      <small>
        选择下方段落，在右侧编辑。
        {directed ? '开场后按 AI 互动编排组织角色演出，玩家可以介入。' : '开场播放完毕后进入自由互动。'}
      </small>
      <div className="opening-layout">
        <nav aria-label="开场积木">
          {list.map((b, i) => (
            <button key={b.id} aria-pressed={b.id === block.id} onClick={() => setSelected(b.id)}>
              {i + 1}.{' '}
              {b.kind === 'narration'
                ? '旁白'
                : actors.find((a) => a.alias === b.speaker)?.name || '人物对白'}{' '}
              · {b.text.slice(0, 18)}
            </button>
          ))}
          <button disabled={list.length >= 16} onClick={() => add('narration')}>
            ＋旁白
          </button>
          <button disabled={list.length >= 16 || !actors.length} onClick={() => add('dialogue')}>
            ＋人物对白
          </button>
          <p>↓ {directed ? 'AI 互动编排与玩家介入' : '自由互动与场景出口'}</p>
        </nav>
        <div className="opening-stage-preview">
          <StagePlayer
            objects={objects}
            title={title}
            background={background}
            visualMode={visualMode}
            people={people.map((p) => {
              const actor = actors.find((a) => a.alias === p.id)
              if (!actor?.presentation) return p
              let selection = { outfit: p.outfit, variant: p.variant }
              for (const prior of list.slice(0, index + 1)) {
                if (prior.kind === 'dialogue' && prior.speaker === p.id && (prior.outfit || prior.variant))
                  selection = { outfit: prior.outfit || selection.outfit, variant: prior.variant }
              }
              return {
                ...p,
                base_sprite: resolveAppearance(actor.presentation, selection).sprite,
              }
            })}
            request={request}
            line={{
              id: block.id,
              text: block.text,
              actorId: block.kind === 'dialogue' ? block.speaker || undefined : undefined,
              speaker:
                block.kind === 'narration'
                  ? '旁白'
                  : actors.find((a) => a.alias === block.speaker)?.name || '未选择人物',
            }}
          >
            <button disabled={index === 0} onClick={() => setSelected(list[index - 1].id)}>
              上一段预览
            </button>
            <button disabled={index === list.length - 1} onClick={() => setSelected(list[index + 1].id)}>
              下一段预览
            </button>
          </StagePlayer>
        </div>
        <fieldset className="opening-inspector" aria-label="选中段落属性">
          <legend>
            第 {index + 1} 段 · {block.kind === 'narration' ? '旁白' : '人物对白'}
          </legend>
          <p>旁白记录已发生内容；对白记录人物言论。知情范围在“参与与知情”中配置。</p>
          {block.kind === 'dialogue' && (
            <label>
              说话人物
              <select
                value={block.speaker || ''}
                onChange={(e) => edit({ speaker: e.target.value, outfit: null, variant: null })}
              >
                <option value="" disabled>
                  选择人物
                </option>
                {actors.map((a) => (
                  <option key={a.alias} value={a.alias}>
                    {a.name}
                  </option>
                ))}
              </select>
            </label>
          )}
          {block.kind === 'dialogue' && (
            <OutfitSelector
              presentation={{
                ...actors.find((a) => a.alias === block.speaker)?.presentation,
                default_outfit:
                  list
                    .slice(0, index)
                    .filter((b) => b.speaker === block.speaker && b.outfit)
                    .at(-1)?.outfit ||
                  people.find((p) => p.id === block.speaker)?.outfit ||
                  actors.find((a) => a.alias === block.speaker)?.presentation?.default_outfit,
              }}
              outfit={block.outfit}
              variant={block.variant}
              onChange={edit}
            />
          )}
          <label>
            段落内容
            <textarea value={block.text} maxLength={4000} onChange={(e) => edit({ text: e.target.value })} />
          </label>
          <button disabled={index === 0} onClick={() => move(-1)}>
            上移
          </button>
          <button disabled={index === list.length - 1} onClick={() => move(1)}>
            下移
          </button>
          <button
            disabled={list.length === 1}
            onClick={() => {
              onChange(list.filter((b) => b.id !== block.id))
              setSelected(list[Math.max(0, index - 1)].id)
            }}
          >
            移除本段
          </button>
        </fieldset>
      </div>
    </section>
  )
}
