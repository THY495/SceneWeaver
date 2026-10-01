import { useEffect, useState } from 'react'

type Build = {
  id: string
  revision: number
  created_at: string
  path: string
  status: string
  runtime_sha256?: string | null
}
export function BuildHistory({
  project,
  revision,
  refresh,
  request,
}: {
  project: string
  revision: number
  refresh: number
  request: (path: string, init?: RequestInit) => Promise<unknown>
}) {
  const [builds, setBuilds] = useState<Build[]>([])
  const [error, setError] = useState('')
  useEffect(() => {
    let live = true
    setBuilds([])
    setError('')
    if (revision > 0)
      request(`/role/projects/${project}/builds`)
        .then((value) => {
          if (live) setBuilds(value as Build[])
        })
        .catch(() => {
          if (live) setError('无法读取构建记录，请重新打开工程后重试。')
        })
    return () => {
      live = false
    }
  }, [project, revision, refresh, request])
  return (
    <details>
      <summary>Windows 构建记录（{builds.length}）</summary>
      {error && <p role="alert">{error}</p>}
      {!error && !builds.length && <p>尚无可读取的构建记录。旧版构建目录仍保留在工程 builds 中。</p>}
      {builds.map((build) => (
        <div key={build.id}>
          <p>
            工程版本 {build.revision} · {new Date(build.created_at).toLocaleString()} ·{' '}
            {build.status === 'completed'
              ? '已构建（此处仅核对故事文件）'
              : build.status === 'damaged'
                ? '产物缺失或损坏，请重新构建'
                : '未完成，请发起新构建'}
            {build.runtime_sha256 && ` · 运行库 ${build.runtime_sha256.slice(0, 12)}`}
          </p>
          <p>
            <code>{build.path}</code>
          </p>
        </div>
      ))}
    </details>
  )
}
