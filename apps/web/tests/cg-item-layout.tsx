import React, { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { HandoffItem } from '../src/HandoffItem'
import { StagePlayer } from '../src/StagePlayer'
import '../src/game.css'
import '../src/game-play.css'

declare global {
  interface Window {
    __handoffDrops: string[]
  }
}

window.__handoffDrops = []

function Probe() {
  const [selected, setSelected] = useState(false)
  const [recipient, setRecipient] = useState('')
  return (
    <div className="vn-app">
      <StagePlayer
        title="CG 物品交互测试"
        visualMode="cg"
        people={[
          { id: 'captain', name: '阿岚' },
          { id: 'keeper', name: '沈舟' },
          { id: 'clerk', name: '林澈' },
        ]}
        line={{ id: 'test', speaker: '旁白', text: '请把便条交给阿岚。' }}
        request={async () => ({})}
        onSelect={(id) => {
          if (selected) {
            window.__handoffDrops.push(id)
            setRecipient(id)
          }
        }}
        itemTarget={selected ? () => ({ label: '递交给此人', enabled: true }) : undefined}
        tray={
          <section className="vn-item-tray" aria-label="递交物品栏">
            <div className="vn-item-heading">
              <strong>随身物品</strong>
              <button type="button">物品详情与交接事项</button>
              <button type="button">收起物品栏</button>
            </div>
            <p>拖到人物身上，或先点物品、再点人物。</p>
            <div className="vn-item-cards">
              <HandoffItem
                name="渡口便条"
                selected={selected}
                disabled={false}
                onSelect={() => setSelected(!selected)}
                onDragSelect={() => setSelected(true)}
                onDrop={(id) => {
                  window.__handoffDrops.push(id)
                  setRecipient(id)
                }}
                onCancel={() => setSelected(false)}
              />
            </div>
            <p role="status">{recipient ? `已选择 ${recipient}` : '请选择接收人'}</p>
          </section>
        }
      />
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<Probe />)
