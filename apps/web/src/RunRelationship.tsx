import { useState } from 'react'
import type { ExperimentPerson } from './ExperimentObservation'

export function RunRelationship({
  people,
  paused,
  request,
  onChanged,
}: {
  people: ExperimentPerson[]
  paused: boolean
  request: (path: string, init?: RequestInit) => Promise<unknown>
  onChanged: () => void
}) {
  const [actor, setActor] = useState(''),
    [target, setTarget] = useState(''),
    [relation, setRelation] = useState(''),
    [basis, setBasis] = useState(''),
    [busy, setBusy] = useState(false),
    [error, setError] = useState('')
  async function submit() {
    const selected = people.find((p) => p.id === actor)
    if (!selected) return
    setBusy(true)
    setError('')
    try {
      await request(`/role/actors/${actor}/relationships`, {
        method: 'POST',
        body: JSON.stringify({
          expected_revision: selected.revision,
          target,
          relation,
          basis,
          status: 'current',
        }),
      })
      setRelation('')
      setBasis('')
      onChanged()
    } catch (e) {
      setError(String(e))
      onChanged()
    } finally {
      setBusy(false)
    }
  }
  return (
    <details>
      <summary>补充或更新人物关系</summary>
      <p>只改变所选角色对另一人的关系认知，并记录依据；对方不会自动得到相同关系。重复提交前请刷新核对。</p>
      <fieldset disabled={busy || !paused}>
        <label>
          谁的视角
          <select
            value={actor}
            onChange={(e) => {
              setActor(e.target.value)
              setTarget('')
            }}
          >
            <option value="">选择 NPC</option>
            {people
              .filter((p) => !p.player)
              .map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
          </select>
        </label>
        <label>
          关系对象
          <select value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="">选择对方</option>
            {people
              .filter((p) => p.id !== actor)
              .map((p) => (
                <option key={p.id} value={p.name}>
                  {p.name}
                </option>
              ))}
          </select>
        </label>
        <label>
          当前关系
          <input value={relation} maxLength={100} onChange={(e) => setRelation(e.target.value)} />
        </label>
        <label>
          该角色知道的依据
          <textarea value={basis} maxLength={500} onChange={(e) => setBasis(e.target.value)} />
        </label>
        <button
          disabled={!actor || !target || !relation.trim() || !basis.trim()}
          onClick={() => void submit()}
        >
          记录本次关系更新
        </button>
      </fieldset>
      {error && <p role="alert">{error}</p>}
    </details>
  )
}
