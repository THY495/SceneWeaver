import React, { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { StagePlayer } from '../src/StagePlayer'
import { HandoffItem } from '../src/HandoffItem'

// Isolated manual UI fixture: real rendering/gesture components, no backend or paid services.
const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="900"><rect width="1600" height="900" fill="#234352"/><rect x="20" y="20" width="1560" height="860" fill="none" stroke="#edc278" stroke-width="20"/><circle cx="800" cy="390" r="170" fill="#b18b59"/><text x="800" y="670" text-anchor="middle" font-size="60" fill="white">CG · Full frame</text></svg>'
const request = async () => ({data_url: 'data:image/svg+xml,' + encodeURIComponent(svg)})
function Fixture() {
  const [cg, setCg] = useState(true)
  const [selected, setSelected] = useState(false)
  const [result, setResult] = useState('尚未交互')
  const deliver = (id: string) => {setResult(`递交请求：${id}`); setSelected(false)}
  return <main className="vn-app" style={{maxWidth: 960, margin: 'auto'}}>
    <button onClick={() => setCg(!cg)}>{cg ? '切换普通舞台' : '切换 CG'}</button>
    <output aria-label="交互结果">{result}</output>
    <StagePlayer title="雨夜重逢" visualMode={cg ? 'cg' : 'stage'} background="fixture"
      request={request} people={[{id:'npc',name:'林舟'}]}
      line={{id:'line',speaker:'林舟',text:'这封信，你愿意交给我吗？'}}
      onSelect={(id) => selected ? deliver(id) : setResult(`交谈：${id}`)}
      itemTarget={selected ? () => ({label:'向林舟提出递交',enabled:true}) : undefined}
      tray={<HandoffItem name="信封" selected={selected} disabled={false}
        onSelect={() => setSelected(!selected)} onDragSelect={() => setSelected(true)}
        onDrop={deliver} onCancel={() => setSelected(false)} />} />
  </main>
}
createRoot(document.getElementById('root')!).render(<Fixture />)
