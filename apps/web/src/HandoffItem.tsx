import { useRef, useState } from 'react'

export function HandoffItem({
  name,
  selected,
  disabled,
  onSelect,
  onDragSelect,
  onDrop,
  onCancel,
}: {
  name: string
  selected: boolean
  disabled: boolean
  onSelect: () => void
  onDragSelect: () => void
  onDrop: (recipientId: string) => void
  onCancel: () => void
}) {
  const gesture = useRef<{ x: number; y: number; moved: boolean } | null>(null)
  const suppressClick = useRef(false)
  const [ghost, setGhost] = useState<{ x: number; y: number } | null>(null)
  return (
    <>
      <button
        disabled={disabled}
        aria-pressed={selected}
        className="vn-draggable-item"
        onKeyDown={(e) => {
          if (e.key === 'Escape') {
            suppressClick.current = Boolean(gesture.current)
            gesture.current = null
            setGhost(null)
            onCancel()
          }
        }}
        onPointerDown={(e) => {
          if (disabled || !e.isPrimary || e.button !== 0) return
          suppressClick.current = false
          gesture.current = { x: e.clientX, y: e.clientY, moved: false }
          e.currentTarget.setPointerCapture(e.pointerId)
        }}
        onPointerMove={(e) => {
          const start = gesture.current
          if (!start || disabled) return
          if (!start.moved && Math.hypot(e.clientX - start.x, e.clientY - start.y) >= 8) {
            start.moved = true
            onDragSelect()
          }
          if (start.moved) setGhost({ x: e.clientX, y: e.clientY })
        }}
        onPointerUp={(e) => {
          const start = gesture.current
          gesture.current = null
          setGhost(null)
          if (!start?.moved) return
          suppressClick.current = true
          const target = document
            .elementFromPoint(e.clientX, e.clientY)
            ?.closest<HTMLElement>('[data-handoff-person]')
          if (!disabled && target?.dataset.handoffPerson) onDrop(target.dataset.handoffPerson)
          onCancel()
        }}
        onPointerCancel={() => {
          gesture.current = null
          setGhost(null)
          onCancel()
        }}
        onLostPointerCapture={() => {
          gesture.current = null
          setGhost(null)
        }}
        onClick={() => {
          if (suppressClick.current) {
            suppressClick.current = false
            return
          }
          onSelect()
        }}
      >
        {name}
      </button>
      {ghost && (
        <span aria-hidden="true" className="vn-drag-ghost" style={{ left: ghost.x + 12, top: ghost.y + 12 }}>
          {name}
        </span>
      )}
    </>
  )
}
