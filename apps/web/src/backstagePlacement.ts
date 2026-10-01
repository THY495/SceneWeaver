export function backstagePlacement(
  participants: string[],
  actors: { alias: string; name: string; location?: string }[],
): string {
  if (!participants.length) return '请至少选择一名 NPC。'
  const selected = participants.map((id) => actors.find((a) => a.alias === id))
  if (selected.some((a) => !a?.location)) return '有参与者缺少角色或地点设置，请检查人物设定。'
  const locations = new Set(selected.map((a) => a!.location))
  if (locations.size !== 1)
    return '参与者不在同一地点：' + selected.map((a) => `${a!.name}（${a!.location}）`).join('、') + '。'
  const location = selected[0]!.location
  const others = actors.filter((a) => a.location === location && !participants.includes(a.alias))
  if (others.length)
    return `${others.map((a) => a.name).join('、')}也在${location}，需要移到别处或调整参与名单；玩家不能加入幕后。`
  return `参与者均在${location}，没有其他角色同处此地。`
}
