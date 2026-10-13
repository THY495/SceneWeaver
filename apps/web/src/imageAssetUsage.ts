import type { Outfit } from './outfitModel'

export type SceneImageTarget = {
  id: string
  name: string
  image?: string | null
  objects?: { id: string; name: string; sprite: string }[]
}
export type CharacterImageTarget = {
  id: string
  name: string
  image?: string | null
  outfits?: Outfit[]
}
export type ImageUsage = {
  kind: 'scene' | 'character'
  purpose: 'background' | 'portrait' | 'object'
  id: string
  name: string
  referenceId: string
}

export function imageAssetUsages(
  catalog: { content_hash: string }[],
  scenes: SceneImageTarget[],
  characters: CharacterImageTarget[],
) {
  const result = new Map<string, ImageUsage[]>(catalog.map((a) => [a.content_hash, []]))
  function add(image: string | null | undefined, usage: ImageUsage) {
    if (image) result.set(image, [...(result.get(image) || []), usage])
  }
  for (const scene of scenes) {
    const base = { kind: 'scene' as const, id: scene.id }
    add(scene.image, {
      ...base,
      purpose: 'background',
      name: scene.name,
      referenceId: `scene:${scene.id}:background`,
    })
    for (const object of scene.objects || [])
      add(object.sprite, {
        ...base,
        purpose: 'object',
        name: `${scene.name} / ${object.name}`,
        referenceId: `scene:${scene.id}:object:${object.id}`,
      })
  }
  for (const character of characters) {
    const base = { kind: 'character' as const, purpose: 'portrait' as const, id: character.id }
    add(character.image, {
      ...base,
      name: character.name,
      referenceId: `character:${character.id}:base`,
    })
    for (const outfit of character.outfits || [])
      for (const variant of outfit.variants)
        add(variant.sprite, {
          ...base,
          name: `${character.name} / ${outfit.name} / ${variant.name}`,
          referenceId: `character:${character.id}:outfit:${outfit.id}:variant:${variant.id}`,
        })
  }
  return result
}
