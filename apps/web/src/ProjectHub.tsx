import { useState } from 'react'

export type ProjectSummary = {
  id: string
  version: number
  path?: string
  recipe: { title: string; description: string; people: unknown[]; narrative?: { nodes: unknown[] } | null }
}

export function ProjectHub<T extends ProjectSummary>({
  projects,
  onOpen,
  onCopy,
  onCreate,
  onImport,
  onTemplate,
  busy,
}: {
  projects: T[]
  onOpen: (project: T) => void
  onCopy: (project: T) => void
  onCreate: () => void
  onImport: () => void
  onTemplate: (id: string) => void
  busy: boolean
}) {
  const [query, setQuery] = useState('')
  const matches = projects.filter((p) =>
    `${p.recipe.title} ${p.id}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()),
  )
  return (
    <div className="project-hub">
      <aside className="project-hub-create">
        <h2>创建工程</h2>
        <button className="primary" disabled={busy} onClick={onCreate}>
          空白工程
        </button>
        <button disabled={busy} onClick={onImport}>
          导入可编辑故事包
        </button>
        <h3>从模板开始</h3>
        <div className="project-template">
          <button disabled={busy} onClick={() => onTemplate('ferry-narrative')}>
            末班渡船 · 剧情分支
          </button>
          <small>自带 6 个场景和手动分支，可直接编辑、检查和构建。</small>
        </div>
        <div className="project-template">
          <button disabled={busy} onClick={() => onTemplate('partial-hearing')}>
            雨夜档案馆 · 秘密与旁听
          </button>
          <small>预设 3 名角色及不同知情；尚无场景，需创建开场与结局。</small>
        </div>
        <div className="project-template">
          <button disabled={busy} onClick={() => onTemplate('offstage-dialogue')}>
            闭馆前 · NPC 幕后互动
          </button>
          <small>预设幕后人物关系；尚无场景，需配置可发行的演出链。</small>
        </div>
        <p>每次创建独立工程。模板修改不会影响原件。</p>
      </aside>
      <section aria-label="本机工程">
        <h2>本机工程</h2>
        <label>
          查找工程
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="工程名称或标识"
          />
        </label>
        <p className="project-storage-note">
          工程自动保存在本机，包含故事、图片和文字资料。要分发给玩家，请在工程中构建 Windows
          游戏；可编辑故事包用于备份或迁移。
        </p>
        <div className="project-hub-list">
          {matches.map((p) => (
            <article key={p.id}>
              <div className="project-avatar" aria-hidden="true">
                {p.recipe.title.slice(0, 1) || '新'}
              </div>
              <div>
                <h3>{p.recipe.title}</h3>
                <p>{p.recipe.description || '尚未填写工程简介'}</p>
                <small>
                  {p.recipe.narrative?.nodes.length || 0} 个场景 · {p.recipe.people.length} 位角色 · 版本{' '}
                  {p.version}
                </small>
                <code>工程 {p.id}</code>
                {p.path && <code>{p.path}</code>}
              </div>
              <div className="project-hub-buttons">
                <button className="primary" disabled={busy} onClick={() => onOpen(p)}>
                  打开工程
                </button>
                <button disabled={busy} onClick={() => onCopy(p)}>
                  复制工程
                </button>
              </div>
            </article>
          ))}
        </div>
        {!matches.length && (
          <p role="status">{projects.length ? '没有匹配工程。' : '还没有工程。从左侧新建或导入。'}</p>
        )}
      </section>
    </div>
  )
}
