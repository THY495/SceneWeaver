import { useState } from 'react'

type Entry = { id: string; name: string }

export function ProjectResources({
  title,
  scenes,
  characters,
  scene,
  character,
  page,
  onPage,
  onScene,
  onCharacter,
}: {
  title: string
  scenes: Entry[]
  characters: Entry[]
  scene: string
  character: string
  page: number
  onPage: (page: number) => void
  onScene: (id: string) => void
  onCharacter: (id: string) => void
}) {
  const [query, setQuery] = useState('')
  const matches = (entry: Entry) => entry.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())
  return (
    <nav className="project-resources" aria-label="工程资源">
      <strong>{title || '新工程'}</strong>
      <label>
        查找场景或角色
        <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} />
      </label>
      <section aria-label="场景资源">
        <h3>场景 · {scenes.length}</h3>
        <button onClick={() => onPage(5)}>场景与故事图</button>
        {scenes.filter(matches).map((entry) => (
          <button
            key={entry.id}
            aria-current={page === 5 && scene === entry.id ? 'true' : undefined}
            onClick={() => onScene(entry.id)}
          >
            {entry.name || '未命名场景'}
          </button>
        ))}
        {!scenes.filter(matches).length && <small>{scenes.length ? '没有匹配场景' : '尚未创建场景'}</small>}
      </section>
      <section aria-label="角色资源">
        <h3>角色 · {characters.length}</h3>
        {characters.filter(matches).map((entry) => (
          <button
            key={entry.id}
            aria-current={page === 1 && character === entry.id ? 'true' : undefined}
            onClick={() => onCharacter(entry.id)}
          >
            {entry.name || '未命名角色'}
          </button>
        ))}
        {!characters.filter(matches).length && <small>没有匹配角色</small>}
        <button onClick={() => onPage(1)}>管理角色模板</button>
      </section>
      <section aria-label="世界设定">
        <h3>世界设定</h3>
        {[
          [2, '人物关系'],
          [6, '物品与容器'],
          [3, '信息与秘密'],
          [4, '初始言论记录'],
          [8, '图片素材'],
          [0, '项目设置'],
          [7, '检查与预览'],
        ].map(([id, label]) => (
          <button key={id} aria-current={page === id ? 'page' : undefined} onClick={() => onPage(Number(id))}>
            {label}
          </button>
        ))}
      </section>
    </nav>
  )
}
