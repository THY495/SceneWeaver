import { lazy, Suspense, useEffect, useRef, useState } from 'react'
const StoryGraphWorkspace = lazy(() => import('./StoryGraphWorkspace'))
import { ProjectHub } from './ProjectHub'
import { BuildHistory } from './BuildHistory'
import { StoryRuns } from './StoryRuns'
import { ProjectResources } from './ProjectResources'
import { ProjectAssets, type ImageAsset } from './ProjectAssets'
import './story-studio.css'
import { NarrativeEditor, type Narrative } from './NarrativeEditor'
import type { VideoAsset } from './VideoSceneEditor'
import { OutfitEditor } from './OutfitEditor'
import { resolveAppearance, type CharacterPresentation } from './outfitModel'
import type { QuickResource } from './StudioQuickCreate'
import { defaultTransform } from './stageTransform'
import {
  graphDiagnostics,
  itemReferences,
  releaseReadiness,
  removeActorFromGraph,
} from './authoringValidation'
import { ServiceReadiness } from './ServiceReadiness'
import { VoicePreview } from './VoicePreview'
import { VoiceReferences } from './VoiceReferences'
import { RequestFailure } from './apiRequest'
import { ItemEditor, syncItemActors, type ItemWorld } from './ItemEditor'

type Relationship = {
  target: string
  relation: string
  status: 'current' | 'former' | 'uncertain'
  basis?: string
}
type Person = {
  alias: string
  profile: {
    name: string
    core: string
    world: string
    relationships: Relationship[]
    document_ids: string[]
  }
  location: string
  goals: string[]
  unnoticed: string[]
  presentation?: CharacterPresentation
}
type Knowledge = { text: string; kind: 'fact' | 'belief'; observers: string[] }
type Recipe = {
  schema_version: 1
  template_id: string
  template_version: number
  title: string
  description: string
  world: string
  people: Person[]
  player: { alias: string; name: string; location: string }
  speech: {
    speaker: string
    text: string
    heard: { actor: string; text: string; speaker_known: boolean }[]
    expects_reply?: boolean
    target?: string | null
  }[]
  knowledge: Knowledge[]
  narrative?: Narrative | null
  item_world?: ItemWorld | null
  image_assets?: ImageAsset[]
  video_assets?: VideoAsset[]
}
type Blueprint = { id: string; version: number; recipe: Recipe; storage?: string; path?: string }
export type StudioReturnPosition = { project: string; scene: string }
type Props = {
  returnPosition?: StudioReturnPosition | null
  onPreview?: (position: StudioReturnPosition | null) => void
  playMode?: boolean
  sid: string | null
  request: (path: string, init?: RequestInit) => Promise<unknown>
  onOpen: (id: string) => void
  onClose: () => void
}
const person = (): Person => ({
  alias: crypto.randomUUID(),
  profile: { name: '', core: '', world: '', relationships: [], document_ids: [] },
  location: '大厅',
  goals: [],
  unnoticed: [],
})
const empty = (): Blueprint => ({
  id: crypto.randomUUID(),
  version: 0,
  recipe: {
    schema_version: 1,
    template_id: 'custom',
    template_version: 1,
    title: '',
    description: '',
    world: '',
    people: [person()],
    player: { alias: 'player', name: '访客', location: '大厅' },
    speech: [],
    knowledge: [],
  },
})
const steps = [
  '项目设置',
  '角色与地点',
  '人物关系',
  '信息与秘密',
  '初始言论记录',
  '场景与故事图',
  '物品与容器',
  '检查与预览',
  '图片素材',
  '故事节点图',
]

export function StoryStudio({
  sid,
  request,
  onOpen,
  onClose,
  playMode = false,
  returnPosition,
  onPreview,
}: Props) {
  const [cards, setCards] = useState<(Person['profile'] & { id: string })[]>([])
  const [cardId, setCardId] = useState('')
  useEffect(() => {
    request('/characters')
      .then((data) => setCards(data as (Person['profile'] & { id: string })[]))
      .catch((e) => setError(String(e)))
  }, [])
  const [items, setItems] = useState<Blueprint[]>([]),
    [draft, setDraft] = useState<Blueprint | null>(null)
  const [legacy, setLegacy] = useState<Blueprint[]>([])
  const [broken, setBroken] = useState<{ id: string; path: string; error: string }[]>([])
  const [imported, setImported] = useState<Blueprint | null>(null)
  const [assetCount, setAssetCount] = useState(0)
  const [documentCount, setDocumentCount] = useState(0)
  const [step, setStep] = useState(0),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(''),
    [notice, setNotice] = useState('')
  const [dirty, setDirty] = useState(false)
  const [buildRefresh, setBuildRefresh] = useState(0)
  const buildPending = useRef<{ project: string; version: number; request: string } | null>(null)
  const [past, setPast] = useState<Recipe[]>([])
  const [future, setFuture] = useState<Recipe[]>([])
  const pending = useRef<{ body: string; player?: string } | null>(null)
  const upload = useRef<HTMLInputElement>(null)
  const sceneCursor = useRef('')
  const [selectedScene, setSelectedScene] = useState('')
  const [diagnosticFocus, setDiagnosticFocus] = useState<{
    scene: string
    panel: 'opening' | 'rules' | 'exits' | 'stage'
    revision: number
  } | null>(null)
  const [selectedCharacter, setSelectedCharacter] = useState('')
  const resume = useRef(returnPosition)
  async function refresh() {
    try {
      const [projects, old] = (await Promise.all([
        request('/role/projects'),
        request('/role/experiments/blueprints'),
      ])) as [Array<Blueprint & { error?: string }>, Blueprint[]]
      const loaded = projects.filter((p) => !p.error)
      setBroken(projects.filter((p) => p.error) as { id: string; path: string; error: string }[])
      setLegacy(old.filter((p) => !projects.some((disk) => disk.id === p.id)))
      setItems(loaded)
      if (resume.current) {
        const position = resume.current
        resume.current = null
        onPreview?.(null)
        const item = loaded.find((item) => item.id === position.project)
        if (item) {
          edit((await request('/role/projects/' + item.id)) as Blueprint)
          sceneCursor.current = position.scene
          setSelectedScene(position.scene)
          setStep(5)
          setNotice('已返回预览前的工程。测试运行不会改写工程设定。')
        }
      }
    } catch (e) {
      setError(String(e))
    }
  }
  useEffect(() => {
    void refresh()
  }, [])
  useEffect(() => {
    if (!draft || (step !== 5 && step !== 9)) return
    const frame = requestAnimationFrame(() => {
      document
        .querySelector(step === 5 ? '.scene-local-navigation' : '.story-graph-workspace')
        ?.scrollIntoView({ block: 'start' })
    })
    return () => cancelAnimationFrame(frame)
  }, [step, selectedScene, draft?.id])
  useEffect(() => {
    const protect = (event: BeforeUnloadEvent) => {
      if (dirty) {
        event.preventDefault()
        event.returnValue = ''
      }
    }
    window.addEventListener('beforeunload', protect)
    return () => window.removeEventListener('beforeunload', protect)
  }, [dirty])
  const recipe = draft?.recipe
  const actors = recipe
    ? [
        ...recipe.people.map((p) => ({
          alias: p.alias,
          name: p.profile.name || '未命名角色',
          base_sprite: resolveAppearance(p.presentation).sprite,
          presentation: p.presentation,
          location: p.location,
        })),
        recipe.player,
      ]
    : []
  function change(fn: (r: Recipe) => void) {
    if (!draft || busy || pending.current) return
    const next = structuredClone(draft)
    fn(next.recipe)
    if (next.recipe.item_world)
      syncItemActors(next.recipe.item_world, [
        ...next.recipe.people.map((p) => ({ alias: p.alias, name: p.profile.name, location: p.location })),
        next.recipe.player,
      ])
    setDraft(next)
    setPast((p) => [...p.slice(-79), structuredClone(draft.recipe)])
    setFuture([])
    setDirty(true)
    setNotice('')
  }
  function edit(item: Blueprint) {
    setPast([])
    setFuture([])
    sceneCursor.current = item.recipe.narrative?.entry || ''
    setSelectedScene(sceneCursor.current)
    setSelectedCharacter(item.recipe.people[0]?.alias || '')
    setDraft(structuredClone(item))
    setStep(item.version === 0 && !item.recipe.title ? 0 : 9)
    setDirty(item.version === 0)
    setError('')
    setNotice('')
  }
  function restoreRecipe(redo: boolean) {
    if (!draft || busy || pending.current) return
    const source = redo ? future : past
    if (!source.length) return
    const next = structuredClone(source[source.length - 1])
    if (redo) {
      setFuture(source.slice(0, -1))
      setPast((p) => [...p, structuredClone(draft.recipe)])
    } else {
      setPast(source.slice(0, -1))
      setFuture((p) => [...p, structuredClone(draft.recipe)])
    }
    setDraft({ ...draft, recipe: next })
    setDirty(true)
    setNotice('')
  }
  async function openProject(item: Blueprint) {
    setBusy(true)
    setError('')
    try {
      edit((await request('/role/projects/' + item.id)) as Blueprint)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }
  async function copyProject(item: Blueprint) {
    setBusy(true)
    setError('')
    try {
      const source =
        item.storage === 'directory' ? ((await request('/role/projects/' + item.id)) as Blueprint) : item
      edit({
        id: crypto.randomUUID(),
        version: 0,
        recipe: { ...structuredClone(source.recipe), title: source.recipe.title + ' · 副本' },
      })
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }
  function leave() {
    if (dirty && !window.confirm('尚有未保存的设定，确定放弃这些修改？')) return
    setDraft(null)
    setDirty(false)
    setNotice('')
    setError('')
    pending.current = null
    void refresh()
  }
  async function sample(id: string) {
    setBusy(true)
    try {
      const r = (await request('/role/experiments/templates/' + id)) as Recipe
      edit({ id: crypto.randomUUID(), version: 0, recipe: { ...r, knowledge: r.knowledge || [] } })
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }
  async function createSceneResource(sceneId: string, data: QuickResource) {
    if (!draft || !draft.recipe.narrative?.nodes.some((n) => n.id === sceneId)) throw Error('当前场景不存在')
    const id = crypto.randomUUID()
    let item: ItemWorld['items'][number] | undefined
    if (data.kind === 'item') {
      item = (await request('/role/authoring/items/instantiate', {
        method: 'POST',
        body: JSON.stringify({
          preset_id: data.container ? 'backpack' : 'paper-sheet',
          instance_id: id,
          placement: { kind: 'location', target: data.location },
        }),
      })) as ItemWorld['items'][number]
      item.definition.name = data.name
    }
    change((r) => {
      const node = r.narrative!.nodes.find((n) => n.id === sceneId)!
      if (data.sprite && !(r.image_assets || []).some((a) => a.content_hash === data.sprite))
        r.image_assets = [
          ...(r.image_assets || []),
          { id: crypto.randomUUID(), name: data.name, kind: 'png', content_hash: data.sprite },
        ]
      if (data.kind === 'person') {
        const placeholder = r.people.find((a) => !a.profile.name.trim() && !a.profile.core.trim())
        if (placeholder) {
          removeActorFromGraph(r.narrative, placeholder.alias)
          r.people = r.people.filter((a) => a.alias !== placeholder.alias)
        }
        const p = person()
        p.alias = id
        p.profile.name = data.name
        p.profile.core = data.core
        p.profile.world = r.world
        p.location = data.location
        p.presentation = { base_sprite: data.sprite || null }
        // Materialize automatic stage before adding a new placement.
        node.stage =
          node.stage ||
          r.people
            .filter((a) => a.location === r.player.location)
            .map((a) => ({ actor: a.alias, position: 'center' as const, transform: defaultTransform() }))
        r.people.push(p)
        node.stage.push({ actor: id, position: 'center', transform: defaultTransform() })
        if (!node.observers.includes(id)) node.observers.push(id)
      } else if (data.kind === 'background') node.background = data.sprite
      else {
        if (item) {
          r.item_world = r.item_world || {
            items: [],
            actors: [],
            policy: { check_volume: true, check_mass: false, max_nesting: 8 },
          }
          r.item_world.items.push(item)
        }
        if (data.sprite)
          node.objects = [
            ...(node.objects || []),
            {
              version: 1,
              id,
              kind: data.kind === 'item' ? 'item' : 'decoration',
              item: item ? id : null,
              name: data.name,
              sprite: data.sprite,
              transform: defaultTransform(),
              layer: 'entities',
              order: (node.objects?.length || 0) + r.people.length,
            },
          ]
      }
    })
  }
  async function save(): Promise<Blueprint | null> {
    if (!draft) return null
    if (basicIssues.length) {
      setStep(7)
      throw Error(basicIssues.join('；'))
    }
    const emptyObservers = draft.recipe.narrative?.nodes.find((node) => !node.observers.length)
    if (emptyObservers) {
      setStep(7)
      throw Error(`场景「${emptyObservers.title}」至少需要一位角色获知开场内容；请在“参与与知情”中勾选。`)
    }
    const recipe = structuredClone(draft.recipe)
    recipe.people.forEach((person) => {
      person.goals = person.goals.map((g) => g.trim()).filter(Boolean)
    })
    const result = (await request('/role/projects/' + draft.id, {
      method: 'PUT',
      body: JSON.stringify({ expected_version: draft.version, recipe }),
    })) as Blueprint
    setDraft(result)
    setDirty(false)
    setNotice('工程已保存到独立目录。已有运行不会改变。')
    return result
  }
  async function action(start: boolean) {
    if (start) setNotice('开始前将检查 LLM 配置并进行一次小额连通性请求，计入当前预算。')
    setBusy(true)
    setError('')
    try {
      if (start && pending.current) {
        const result = (await request('/role/experiments/start', {
          method: 'POST',
          body: pending.current.body,
        })) as { player_id: string }
        pending.current = null
        if (draft) onPreview?.({ project: draft.id, scene: sceneCursor.current })
        onOpen(result.player_id)
        return
      }
      const saved = await save()
      if (start && saved) {
        pending.current = {
          body: JSON.stringify({
            request_id: crypto.randomUUID(),
            blueprint_id: saved.id,
            blueprint_version: saved.version,
          }),
        }
        const result = (await request('/role/experiments/start', {
          method: 'POST',
          body: pending.current.body,
        })) as { player_id: string }
        pending.current = null
        onPreview?.({ project: saved.id, scene: sceneCursor.current })
        onOpen(result.player_id)
      }
    } catch (e) {
      if (e instanceof RequestFailure && !e.uncertain) pending.current = null
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }
  async function download() {
    if (!draft) return
    setBusy(true)
    setError('')
    try {
      const bundle = await request('/role/packages/export', {
        method: 'POST',
        body: JSON.stringify(draft.recipe),
      })
      const url = URL.createObjectURL(new Blob([JSON.stringify(bundle)], { type: 'application/json' }))
      const a = document.createElement('a')
      a.href = url
      a.download = 'sceneweaver-story.json'
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }
  async function buildGame() {
    if (!draft || busy) return
    setBusy(true)
    setError('')
    setNotice('正在保存工程并构建 Windows 游戏…')
    try {
      const retry =
        buildPending.current?.project === draft.id && !dirty && buildPending.current.version === draft.version
      const saved = retry || (!dirty && draft.version > 0) ? draft : await save()
      if (!saved) return
      if (!retry)
        buildPending.current = { project: saved.id, version: saved.version, request: crypto.randomUUID() }
      setNotice('正在检查并组装游戏运行库和作品资源，请稍候…')
      const result = (await request(`/role/projects/${saved.id}/builds`, {
        method: 'POST',
        body: JSON.stringify({ expected_version: saved.version, request_id: buildPending.current!.request }),
      })) as { path: string; revision: number }
      setNotice(
        `Windows 游戏文件已生成（工程版本 ${result.revision}）：${result.path}。发布前请在目标 Windows 验证可启动性与可信签名；玩家需配置自己的模型密钥。`,
      )
      buildPending.current = null
      setBuildRefresh((value) => value + 1)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
      if (e instanceof RequestFailure && e.uncertain) {
        setNotice(
          buildPending.current
            ? '尚未收到构建结果。再次点击构建会查询同一次请求，不会重复生成；也可查看构建记录。'
            : '尚未收到工程保存结果。请重试保存；在确认工程版本前不会发起构建。',
        )
      } else {
        buildPending.current = null
        setNotice('构建未完成。已保存的工程仍保留。')
      }
      setBuildRefresh((value) => value + 1)
    } finally {
      setBusy(false)
    }
  }
  async function importFile(file?: File) {
    if (!file) return
    setImported(null)
    setBusy(true)
    setError('')
    try {
      if (file.size > 32 * 1024 * 1024) throw Error('故事包不能超过 32 MB')
      const body = new FormData()
      body.append('file', file)
      const data = (await request('/role/packages/import', { method: 'POST', body })) as {
        recipe: Recipe
        asset_count: number
        document_count?: number
      }
      setImported({ id: crypto.randomUUID(), version: 0, recipe: data.recipe })
      setAssetCount(data.asset_count)
      setDocumentCount(data.document_count || 0)
      pending.current = null
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }
  async function playImported() {
    if (!imported) return
    setBusy(true)
    setError('')
    try {
      let saved = imported
      if (!pending.current) {
        if (!saved.version) {
          saved = (await request('/role/experiments/blueprints/' + saved.id, {
            method: 'PUT',
            body: JSON.stringify({ expected_version: 0, recipe: saved.recipe }),
          })) as Blueprint
          setImported(saved)
        }
        pending.current = {
          body: JSON.stringify({
            request_id: crypto.randomUUID(),
            blueprint_id: saved.id,
            blueprint_version: saved.version,
          }),
        }
      }
      const result = (await request('/role/experiments/start', {
        method: 'POST',
        body: pending.current.body,
      })) as { player_id: string }
      pending.current = null
      onOpen(result.player_id)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusy(false)
    }
  }
  const basicIssues = recipe
    ? [
        !recipe.title.trim() && '请填写故事名称',
        recipe.people.some((p) => !p.profile.name.trim() || !p.profile.core.trim()) && '请补齐角色名称与经历',
        new Set(actors.map((a) => a.name)).size !== actors.length && '角色名称不能重复',
        (!recipe.player.name.trim() || !recipe.player.location.trim()) && '故事概况：请填写玩家名称与地点',
        recipe.people.some((p) => !p.location.trim()) && '角色与地点：每位角色都需要开场地点',
        recipe.people.some((p) =>
          p.profile.relationships.some((r) => !r.target.trim() || !r.relation.trim()),
        ) && '人物关系：请选择对象并填写关系，或移除空白关系',
        recipe.people.some((p) =>
          p.profile.relationships.some((r) => !actors.some((a) => a.name === r.target)),
        ) && '人物关系：存在故事外的关系对象，请先加入该人物或移除这条关系',
        recipe.knowledge.some((k) => !k.text.trim()) && '信息与秘密：请填写内容，或移除空白信息',
        recipe.speech.some(
          (s) => !s.text.trim() || s.heard.some((h) => !h.text.trim() || !s.text.includes(h.text)),
        ) && '开场言论：原话不能为空，听闻片段必须逐字来自原话',
        recipe.speech.some(
          (s) =>
            Boolean(s.expects_reply) !== Boolean(s.target) ||
            (s.target && !s.heard.some((h) => h.actor === s.target && h.text === s.text && h.speaker_known)),
        ) && '开场言论：明确回应必须选择完整听到并认出说话者的角色',
      ].filter(Boolean)
    : []
  const graphIssues = graphDiagnostics(recipe?.narrative)
  const releaseIssues = releaseReadiness(recipe?.narrative, recipe?.people, recipe?.player)
  const issues = [...basicIssues, ...graphIssues.map((issue) => issue.message)]
  const buildIssues = [...issues, ...releaseIssues]
  return (
    <section className="story-studio" aria-label="故事工作室">
      <header className="studio-header">
        <div>
          <p className="eyebrow">SCENEWEAVER · 开发工作室</p>
          <h1>{draft ? recipe?.title || '新工程' : '项目管理器'}</h1>
          <p>
            {draft
              ? `编辑设定 · ${draft.version ? '版本 ' + draft.version : '尚未保存'}${dirty ? ' · 有未保存修改' : ''}`
              : '创建工程、编排场景与 AI 互动，在工具内预览并检查作品。'}
          </p>
        </div>
        {draft && (
          <button onClick={leave} disabled={busy}>
            关闭工程
          </button>
        )}
      </header>
      {error && (
        <p className="studio-error" role="alert">
          {error}
        </p>
      )}
      {notice && <p role="status">{notice}</p>}
      {!draft ? (
        <>
          <ProjectHub
            projects={items}
            busy={busy}
            onCreate={() => edit(empty())}
            onImport={() => upload.current?.click()}
            onTemplate={(id) => void sample(id)}
            onOpen={(item) => void openProject(item)}
            onCopy={(item) => void copyProject(item)}
          />
          {broken.map((p) => (
            <p role="alert" key={p.id}>
              {p.path}：{p.error}
            </p>
          ))}
          <details>
            <summary>旧数据库工程（{legacy.length}）</summary>
            <p>导入副本后保存为独立磁盘工程。原记录和已有测试保留。</p>
            {legacy.map((p) => (
              <article key={p.id}>
                <h3>{p.recipe.title}</h3>
                <code>{p.id}</code>
                <button disabled={busy} onClick={() => void copyProject(p)}>
                  导入为磁盘工程副本
                </button>
              </article>
            ))}
          </details>
          <input
            hidden
            type="file"
            ref={upload}
            accept=".json"
            onChange={(e) => {
              void importFile(e.target.files?.[0])
              e.target.value = ''
            }}
          />
          {imported && (
            <section aria-label="导入完成">
              <h2>{imported.recipe.title}</h2>
              <p>
                已导入 {imported.recipe.people.length} 位 NPC、{imported.recipe.narrative?.nodes.length || 0}{' '}
                个场景、{assetCount} 张图片。故事简介、秘密与剧情走向尚未展开。
              </p>
              {documentCount > 0 && (
                <p>
                  另含 {documentCount}{' '}
                  份角色资料，已恢复原有绑定并建立全文索引。未自动生成语义向量；可在世界资料中重建索引。资料内容未展开。
                </p>
              )}
              <button
                disabled={busy || Boolean(pending.current)}
                onClick={() => {
                  edit(imported)
                  setImported(null)
                }}
              >
                打开导入工程
              </button>
            </section>
          )}
        </>
      ) : (
        recipe && (
          <>
            <div
              className={`studio-layout${step === 5 ? ' studio-scene-workspace-full' : step === 9 ? ' studio-scene-focus' : ''}`}
            >
              {step !== 5 && (
                <ProjectResources
                  title={recipe.title}
                  scenes={recipe.narrative?.nodes.map((n) => ({ id: n.id, name: n.title })) || []}
                  characters={recipe.people.map((p) => ({ id: p.alias, name: p.profile.name }))}
                  scene={
                    recipe.narrative?.nodes.some((n) => n.id === selectedScene)
                      ? selectedScene
                      : recipe.narrative?.entry || ''
                  }
                  character={
                    recipe.people.some((p) => p.alias === selectedCharacter)
                      ? selectedCharacter
                      : recipe.people[0]?.alias || ''
                  }
                  page={step}
                  onPage={(page) => setStep(page === 5 ? 9 : page)}
                  onScene={(id) => {
                    setSelectedScene(id)
                    sceneCursor.current = id
                    setStep(5)
                  }}
                  onCharacter={(id) => {
                    setSelectedCharacter(id)
                    setStep(1)
                  }}
                />
              )}
              <fieldset className="studio-editor" disabled={busy || Boolean(pending.current)}>
                <h2>{steps[step]}</h2>
                <div className="scene-editor-toolbar">
                  <button disabled={!past.length} onClick={() => restoreRecipe(false)}>
                    撤销工程修改
                  </button>
                  <button disabled={!future.length} onClick={() => restoreRecipe(true)}>
                    重做工程修改
                  </button>
                </div>
                <div hidden={step !== 9}>
                  <Suspense fallback={<p>正在加载故事图…</p>}>
                    <StoryGraphWorkspace
                      graph={recipe.narrative}
                      player={recipe.player.alias}
                      request={request}
                      onChange={(graph) =>
                        change((r) => {
                          r.narrative = graph
                        })
                      }
                      onOpen={(id) => {
                        setSelectedScene(id)
                        sceneCursor.current = id
                        setStep(5)
                      }}
                    />
                  </Suspense>
                </div>
                {step === 0 && (
                  <>
                    <label>
                      故事名称
                      <input
                        value={recipe.title}
                        maxLength={160}
                        onChange={(e) =>
                          change((r) => {
                            r.title = e.target.value
                          })
                        }
                      />
                    </label>
                    <label>
                      简介（仅创作者可见）
                      <textarea
                        value={recipe.description}
                        onChange={(e) =>
                          change((r) => {
                            r.description = e.target.value
                          })
                        }
                      />
                    </label>
                    <label>
                      共同背景（所有 NPC 都知道）
                      <textarea
                        value={recipe.world}
                        maxLength={2000}
                        placeholder="时代、环境、公开常识。秘密请放到「信息与秘密」。"
                        onChange={(e) =>
                          change((r) => {
                            r.world = e.target.value
                          })
                        }
                      />
                    </label>
                    <h3>玩家身份</h3>
                    <label>
                      玩家名称
                      <input
                        value={recipe.player.name}
                        onChange={(e) =>
                          change((r) => {
                            r.player.name = e.target.value
                          })
                        }
                      />
                    </label>
                    <label>
                      玩家开场地点
                      <input
                        value={recipe.player.location}
                        onChange={(e) =>
                          change((r) => {
                            r.player.location = e.target.value
                          })
                        }
                      />
                    </label>
                    <p>纯观察时，将玩家放在独立地点，通过创作者视角观察 NPC。</p>
                  </>
                )}
                {step === 1 && (
                  <>
                    {recipe.people.map(
                      (p, i) =>
                        p.alias ===
                          (recipe.people.some((p) => p.alias === selectedCharacter)
                            ? selectedCharacter
                            : recipe.people[0]?.alias) && (
                          <article className="studio-person" key={p.alias}>
                            <h3>角色属性 · {p.profile.name || '未命名角色'}</h3>
                            <label>
                              角色名称
                              <input
                                value={p.profile.name}
                                onChange={(e) =>
                                  change((r) => {
                                    const old = r.people[i].profile.name
                                    r.people[i].profile.name = e.target.value
                                    r.people.forEach((a) =>
                                      a.profile.relationships.forEach((rel) => {
                                        if (rel.target === old) rel.target = e.target.value
                                      }),
                                    )
                                  })
                                }
                              />
                            </label>
                            <label>
                              身份、性格、经历与表达方式
                              <textarea
                                value={p.profile.core}
                                maxLength={6000}
                                onChange={(e) =>
                                  change((r) => {
                                    r.people[i].profile.core = e.target.value
                                  })
                                }
                              />
                            </label>
                            <label>
                              开场地点
                              <input
                                value={p.location}
                                onChange={(e) =>
                                  change((r) => {
                                    r.people[i].location = e.target.value
                                  })
                                }
                              />
                            </label>
                            <label>
                              当前目标（每行一项）
                              <textarea
                                value={p.goals.join('\n')}
                                onChange={(e) =>
                                  change((r) => {
                                    r.people[i].goals = e.target.value.split('\n')
                                  })
                                }
                              />
                            </label>
                            <OutfitEditor
                              presentation={p.presentation || {}}
                              assets={recipe.image_assets || []}
                              request={request}
                              onChange={(presentation) =>
                                change((r) => {
                                  r.people[i].presentation = presentation
                                })
                              }
                            />
                            <details>
                              <summary>高级：未察觉的角色</summary>
                              {actors
                                .filter((a) => a.alias !== p.alias)
                                .map((a) => (
                                  <label className="studio-check" key={a.alias}>
                                    <input
                                      type="checkbox"
                                      checked={p.unnoticed.includes(a.alias)}
                                      onChange={(e) =>
                                        change((r) => {
                                          r.people[i].unnoticed = e.target.checked
                                            ? [...p.unnoticed, a.alias]
                                            : p.unnoticed.filter((x) => x !== a.alias)
                                        })
                                      }
                                    />
                                    {a.name}
                                  </label>
                                ))}
                            </details>
                            <button
                              disabled={recipe.people.length < 2}
                              onClick={() =>
                                change((r) => {
                                  r.people.splice(i, 1)
                                  removeActorFromGraph(r.narrative, p.alias)
                                  r.item_world?.items.forEach((item) => {
                                    if (item.placement.kind === 'actor' && item.placement.target === p.alias)
                                      item.placement = { kind: 'location', target: p.location }
                                    if (item.owner_id === p.alias) item.owner_id = null
                                  })
                                  r.people.forEach((a) => {
                                    a.unnoticed = a.unnoticed.filter((x) => x !== p.alias)
                                    a.profile.relationships = a.profile.relationships.filter(
                                      (x) => x.target !== p.profile.name,
                                    )
                                  })
                                  r.knowledge.forEach((k) => {
                                    k.observers = k.observers.filter((x) => x !== p.alias)
                                  })
                                  r.speech = r.speech
                                    .filter((s) => s.speaker !== p.alias)
                                    .map((s) => ({ ...s, heard: s.heard.filter((h) => h.actor !== p.alias) }))
                                })
                              }
                            >
                              移除此角色及关联设定
                            </button>
                            <small>移除会清理舞台、开场对白及人物引用；携带物品留在其原地点。</small>
                          </article>
                        ),
                    )}
                    <button
                      onClick={() =>
                        change((r) => {
                          const added = person()
                          r.people.push(added)
                          setSelectedCharacter(added.alias)
                        })
                      }
                    >
                      再添加一名 NPC
                    </button>
                    <label>
                      从角色库复用模板
                      <select value={cardId} onChange={(e) => setCardId(e.target.value)}>
                        <option value="">选择角色模板</option>
                        {cards.map((card) => (
                          <option key={card.id} value={card.id}>
                            {card.name} · {card.id.slice(0, 8)}
                          </option>
                        ))}
                      </select>
                    </label>
                    <button
                      disabled={!cardId}
                      onClick={() =>
                        change((r) => {
                          const card = cards.find((c) => c.id === cardId)
                          if (!card) return
                          const added = person()
                          added.profile = {
                            name: card.name,
                            core: card.core,
                            world: r.world,
                            relationships: structuredClone(card.relationships),
                            document_ids: [...card.document_ids],
                          }
                          r.people.push(added)
                          setSelectedCharacter(added.alias)
                        })
                      }
                    >
                      复制角色到当前设定
                    </button>
                    <p>只复制模板。请核对同名角色、人物关系与绑定资料；原角色及已有故事不受影响。</p>
                  </>
                )}
                {step === 2 && (
                  <>
                    <p>关系有方向。甲的看法不会自动成为乙的看法；只填写当前角色知道的内容。</p>
                    {recipe.people.map((p, i) => (
                      <article className="studio-person" key={p.alias}>
                        <h3>{p.profile.name || '未命名角色'}眼中的关系</h3>
                        {p.profile.relationships.map((rel, j) => (
                          <div className="studio-relation" key={j}>
                            <label>
                              对方
                              <select
                                value={rel.target}
                                onChange={(e) =>
                                  change((r) => {
                                    r.people[i].profile.relationships[j].target = e.target.value
                                  })
                                }
                              >
                                <option value="">选择角色</option>
                                {actors
                                  .filter((a) => a.alias !== p.alias)
                                  .map((a) => (
                                    <option key={a.alias} value={a.name}>
                                      {a.name}
                                    </option>
                                  ))}
                              </select>
                            </label>
                            <label>
                              关系与态度
                              <input
                                value={rel.relation}
                                maxLength={100}
                                onChange={(e) =>
                                  change((r) => {
                                    r.people[i].profile.relationships[j].relation = e.target.value
                                  })
                                }
                              />
                            </label>
                            <label>
                              关系依据
                              <textarea
                                value={rel.basis || ''}
                                maxLength={500}
                                onChange={(e) =>
                                  change((r) => {
                                    r.people[i].profile.relationships[j].basis = e.target.value
                                  })
                                }
                              />
                            </label>
                            <button
                              onClick={() =>
                                change((r) => {
                                  r.people[i].profile.relationships.splice(j, 1)
                                })
                              }
                            >
                              移除关系
                            </button>
                          </div>
                        ))}
                        <button
                          onClick={() =>
                            change((r) => {
                              r.people[i].profile.relationships.push({
                                target: '',
                                relation: '',
                                status: 'current',
                              })
                            })
                          }
                        >
                          添加关系
                        </button>
                      </article>
                    ))}
                  </>
                )}
                {step === 3 && (
                  <>
                    <p>未勾选的角色不会收到这条信息。无人知晓的事实仅保存在创作者记录中。</p>
                    {recipe.knowledge.map((k, i) => (
                      <article className="studio-person" key={i}>
                        <label>
                          信息性质
                          <select
                            value={k.kind}
                            onChange={(e) =>
                              change((r) => {
                                r.knowledge[i].kind = e.target.value as Knowledge['kind']
                              })
                            }
                          >
                            <option value="fact">世界真实设定</option>
                            <option value="belief">角色信念或未证实说法</option>
                          </select>
                        </label>
                        <label>
                          内容
                          <textarea
                            value={k.text}
                            onChange={(e) =>
                              change((r) => {
                                r.knowledge[i].text = e.target.value
                              })
                            }
                          />
                        </label>
                        <p>哪些角色在开场时知道 / 持有此看法？</p>
                        {actors.map((a) => (
                          <label className="studio-check" key={a.alias}>
                            <input
                              type="checkbox"
                              checked={k.observers.includes(a.alias)}
                              onChange={(e) =>
                                change((r) => {
                                  r.knowledge[i].observers = e.target.checked
                                    ? [...k.observers, a.alias]
                                    : k.observers.filter((x) => x !== a.alias)
                                })
                              }
                            />
                            {a.name}
                          </label>
                        ))}
                        <button
                          onClick={() =>
                            change((r) => {
                              r.knowledge.splice(i, 1)
                            })
                          }
                        >
                          移除信息
                        </button>
                      </article>
                    ))}
                    <button
                      onClick={() =>
                        change((r) => {
                          r.knowledge.push({ text: '', kind: 'fact', observers: [] })
                        })
                      }
                    >
                      添加信息或秘密
                    </button>
                  </>
                )}
                {step === 4 && (
                  <>
                    <p>记录谁说过什么，不会自动认定内容属实。听闻者需显式勾选，可仅收到原话片段。</p>
                    {recipe.speech.map((s, i) => (
                      <article className="studio-person" key={i}>
                        <label>
                          说话者
                          <select
                            value={s.speaker}
                            onChange={(e) =>
                              change((r) => {
                                r.speech[i].speaker = e.target.value
                                r.speech[i].heard = s.heard.filter((h) => h.actor !== e.target.value)
                                if (r.speech[i].target === e.target.value) r.speech[i].target = null
                              })
                            }
                          >
                            {actors.map((a) => (
                              <option key={a.alias} value={a.alias}>
                                {a.name}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label>
                          完整原话
                          <textarea
                            value={s.text}
                            onChange={(e) =>
                              change((r) => {
                                r.speech[i].text = e.target.value
                                r.speech[i].target = null
                              })
                            }
                          />
                        </label>
                        <label className="studio-check">
                          <input
                            type="checkbox"
                            checked={Boolean(s.expects_reply)}
                            onChange={(e) =>
                              change((r) => {
                                r.speech[i].expects_reply = e.target.checked
                                r.speech[i].target = null
                              })
                            }
                          />
                          这句话明确期待一位角色回应
                        </label>
                        {s.expects_reply && (
                          <label>
                            回应角色
                            <select
                              value={s.target || ''}
                              onChange={(e) =>
                                change((r) => {
                                  r.speech[i].target = e.target.value || null
                                })
                              }
                            >
                              <option value="">选择完整听到并认出说话者的角色</option>
                              {actors
                                .filter((a) =>
                                  s.heard.some(
                                    (h) => h.actor === a.alias && h.text === s.text && h.speaker_known,
                                  ),
                                )
                                .map((a) => (
                                  <option key={a.alias} value={a.alias}>
                                    {a.name}
                                  </option>
                                ))}
                            </select>
                          </label>
                        )}
                        {actors
                          .filter((a) => a.alias !== s.speaker)
                          .map((a) => {
                            const h = s.heard.find((h) => h.actor === a.alias)
                            return (
                              <div key={a.alias}>
                                <label className="studio-check">
                                  <input
                                    type="checkbox"
                                    checked={Boolean(h)}
                                    onChange={(e) =>
                                      change((r) => {
                                        r.speech[i].heard = e.target.checked
                                          ? [
                                              ...s.heard,
                                              { actor: a.alias, text: s.text, speaker_known: true },
                                            ]
                                          : s.heard.filter((h) => h.actor !== a.alias)
                                        if (!e.target.checked && r.speech[i].target === a.alias)
                                          r.speech[i].target = null
                                      })
                                    }
                                  />
                                  {a.name}听到了
                                </label>
                                {h && (
                                  <>
                                    <label>
                                      实际听闻片段
                                      <input
                                        value={h.text}
                                        onChange={(e) =>
                                          change((r) => {
                                            r.speech[i].heard.find((h) => h.actor === a.alias)!.text =
                                              e.target.value
                                            if (r.speech[i].target === a.alias) r.speech[i].target = null
                                          })
                                        }
                                      />
                                    </label>
                                    <label className="studio-check">
                                      <input
                                        type="checkbox"
                                        checked={h.speaker_known}
                                        onChange={(e) =>
                                          change((r) => {
                                            r.speech[i].heard.find(
                                              (h) => h.actor === a.alias,
                                            )!.speaker_known = e.target.checked
                                            if (!e.target.checked && r.speech[i].target === a.alias)
                                              r.speech[i].target = null
                                          })
                                        }
                                      />
                                      能辨认说话者
                                    </label>
                                  </>
                                )}
                              </div>
                            )
                          })}
                        <button
                          onClick={() =>
                            change((r) => {
                              r.speech.splice(i, 1)
                            })
                          }
                        >
                          移除言论
                        </button>
                      </article>
                    ))}
                    <button
                      onClick={() =>
                        change((r) => {
                          r.speech.push({
                            speaker: r.people[0].alias,
                            text: '',
                            heard: [],
                            expects_reply: false,
                            target: null,
                          })
                        })
                      }
                    >
                      添加开场言论
                    </button>
                  </>
                )}
                <div hidden={step !== 5}>
                  <NarrativeEditor
                    videos={recipe.video_assets || []}
                    onUploadVideo={(scene, asset) =>
                      change((r) => {
                        const existing = (r.video_assets || []).find(
                          (a) => a.content_hash === asset.content_hash,
                        )
                        const selected = existing || asset
                        if (!existing) r.video_assets = [...(r.video_assets || []), asset]
                        const node = r.narrative?.nodes.find((n) => n.id === scene)
                        if (node)
                          node.video = { asset: selected.id, allow_skip: node.video?.allow_skip ?? true }
                      })
                    }
                    diagnosticFocus={diagnosticFocus}
                    onCreateResource={createSceneResource}
                    images={(recipe.image_assets || []).map((a) => ({ id: a.content_hash, label: a.name }))}
                    history={{
                      canUndo: past.length > 0,
                      canRedo: future.length > 0,
                      undo: () => restoreRecipe(false),
                      redo: () => restoreRecipe(true),
                    }}
                    key={draft.id}
                    initialScene={sceneCursor.current}
                    selectedScene={selectedScene}
                    externalSceneList
                    onSelectScene={(id) => {
                      sceneCursor.current = id
                      setSelectedScene(id)
                    }}
                    onBackToGraph={() => setStep(9)}
                    onSave={() => void action(false)}
                    request={request}
                    value={recipe.narrative}
                    items={recipe.item_world?.items.map((item) => ({
                      id: item.id,
                      name: item.definition.name,
                    }))}
                    actors={actors}
                    player={recipe.player.alias}
                    onChange={(value) =>
                      change((r) => {
                        r.narrative = value
                      })
                    }
                  />
                </div>
                {step === 6 && (
                  <ItemEditor
                    value={recipe.item_world}
                    references={itemReferences(recipe.narrative)}
                    people={[
                      ...recipe.people.map((p) => ({
                        alias: p.alias,
                        name: p.profile.name,
                        location: p.location,
                      })),
                      recipe.player,
                    ]}
                    request={request}
                    onChange={(value) =>
                      change((r) => {
                        r.item_world = value
                      })
                    }
                  />
                )}
                {step === 8 && (
                  <ProjectAssets
                    key={draft.id}
                    catalog={recipe.image_assets || []}
                    onUploadState={setBusy}
                    onRegister={(hash, name) =>
                      change((r) => {
                        const entries = r.image_assets || []
                        const prior = entries.find((a) => a.content_hash === hash)
                        r.image_assets = prior
                          ? entries.map((a) => (a.content_hash === hash ? { ...a, name } : a))
                          : [...entries, { id: crypto.randomUUID(), content_hash: hash, name, kind: 'png' }]
                      })
                    }
                    scenes={
                      recipe.narrative?.nodes.map((n) => ({
                        id: n.id,
                        name: n.title,
                        image: n.background,
                      })) || []
                    }
                    characters={recipe.people.map((p) => ({
                      id: p.alias,
                      name: p.profile.name,
                      image: p.presentation?.base_sprite,
                    }))}
                    request={request}
                    onLocate={(usage) => {
                      if (usage.kind === 'scene') {
                        setSelectedScene(usage.id)
                        sceneCursor.current = usage.id
                        setStep(5)
                      } else {
                        setSelectedCharacter(usage.id)
                        setStep(1)
                      }
                    }}
                    onApply={(usage, image) =>
                      change((r) => {
                        if (usage.kind === 'scene') {
                          const node = r.narrative?.nodes.find((n) => n.id === usage.id)
                          if (node) node.background = image
                        } else {
                          const actor = r.people.find((p) => p.alias === usage.id)
                          if (actor) actor.presentation = { ...actor.presentation, base_sprite: image }
                        }
                      })
                    }
                  />
                )}
                {step === 7 && (
                  <>
                    <ServiceReadiness request={request} />
                    <VoicePreview request={request} />
                    <VoiceReferences project={draft.id} request={request} />
                    {draft.version > 0 && (
                      <section aria-label="本工程测试记录">
                        <h3>本工程测试记录</h3>
                        <StoryRuns
                          blueprint={draft.id}
                          request={request}
                          onOpen={(id) => {
                            if (dirty && !window.confirm('打开历史测试会放弃尚未保存的修改，是否继续？'))
                              return
                            onPreview?.({ project: draft.id, scene: sceneCursor.current })
                            onOpen(id)
                          }}
                        />
                      </section>
                    )}
                    <p>
                      启动会保存设定并创建全新的独立运行，初始状态为暂停。玩家场景进入后可开始预览；幕后场景在
                      Studio 中单步调试，符合发行限制的独立游戏会自动推进。
                    </p>
                    {basicIssues.map((x) => (
                      <p className="studio-error" key={String(x)}>
                        {x}
                      </p>
                    ))}
                    {graphIssues.map((issue) => (
                      <div className="studio-error" key={issue.code}>
                        <p>{issue.message}</p>
                        <button
                          onClick={() => {
                            setSelectedScene(issue.scene)
                            sceneCursor.current = issue.scene
                            setDiagnosticFocus({
                              scene: issue.scene,
                              panel: issue.panel,
                              revision: Date.now(),
                            })
                            setStep(issue.code.startsWith('detached:') ? 9 : 5)
                          }}
                        >
                          {issue.code.startsWith('detached:') ? '打开故事图连线' : '定位到问题设定'}
                        </button>
                      </div>
                    ))}
                    {releaseIssues.map((issue) => (
                      <p className="studio-error" key={issue}>
                        {issue}
                      </p>
                    ))}
                    {recipe.people.map((p) => (
                      <article className="studio-person" key={p.alias}>
                        <h3>
                          {p.profile.name} · {p.location}
                        </h3>
                        <p>{p.profile.core}</p>
                        <p>共同背景：{recipe.world || '无'}</p>
                        <p>目标：{p.goals.join('；') || '无'}</p>
                        <p>
                          关系：
                          {p.profile.relationships.map((r) => `${r.target}：${r.relation}`).join('；') ||
                            '无'}
                        </p>
                        <h4>初始知情</h4>
                        {recipe.knowledge
                          .filter((k) => k.observers.includes(p.alias))
                          .map((k, i) => (
                            <p key={i}>
                              {k.kind === 'fact' ? '已知事实' : '未证实看法'}：{k.text}
                            </p>
                          ))}
                        {recipe.speech.flatMap((s, i) =>
                          s.speaker === p.alias
                            ? [<p key={i}>自己说过：{s.text}</p>]
                            : s.heard
                                .filter((h) => h.actor === p.alias)
                                .map((h) => (
                                  <p key={i}>
                                    {h.speaker_known
                                      ? actors.find((a) => a.alias === s.speaker)?.name
                                      : '未辨认的声音'}
                                    说：{h.text}
                                  </p>
                                )),
                        )}
                      </article>
                    ))}
                  </>
                )}
              </fieldset>
              <aside className="studio-guide">
                <h3>设定预览</h3>
                <p>
                  {recipe.people.length} 位 NPC · 玩家 {recipe.player.name}
                </p>
                <p>
                  {new Set([...recipe.people.map((p) => p.location), recipe.player.location]).size} 个开场地点
                </p>
                <p>
                  {recipe.knowledge.length} 条信息 · {recipe.speech.length} 条开场言论
                </p>
                <hr />
                <p>编辑的是下一次实验的起点。已经发生的故事不会被改写。</p>
                <p>秘密应按知情角色分配，避免写进共同背景。</p>
                <button disabled={busy} onClick={() => void download()}>
                  导出可编辑故事包
                </button>
                <p>故事包用于继续编辑。发布给玩家请构建 Windows 游戏并分发完整文件夹。</p>
              </aside>
            </div>
            <BuildHistory
              project={draft.id}
              revision={draft.version}
              refresh={buildRefresh}
              request={request}
            />
            <footer className="studio-footer">
              <button disabled={busy || Boolean(pending.current)} onClick={() => void action(false)}>
                保存项目
              </button>
              <button disabled={busy} onClick={() => void download()}>
                导出可编辑故事包
              </button>
              <button
                disabled={busy || buildIssues.length > 0 || Boolean(pending.current)}
                onClick={() => void buildGame()}
              >
                保存并构建 Windows 游戏
              </button>
              {step !== 7 ? (
                <button className="primary" onClick={() => setStep(7)}>
                  检查与预览{buildIssues.length > 0 ? ` · ${buildIssues.length} 项待处理` : ''}
                </button>
              ) : (
                <button
                  className="primary"
                  disabled={busy || issues.length > 0}
                  onClick={() => void action(true)}
                >
                  {busy ? '处理中…' : pending.current ? '重试本次启动' : '保存并从起点预览'}
                </button>
              )}
            </footer>
          </>
        )
      )}
    </section>
  )
}
