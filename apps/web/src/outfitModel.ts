export type PortraitVariant = { id: string; name: string; sprite: string; expression?: string; pose?: string }
export type Outfit = { id: string; name: string; default_variant: string; variants: PortraitVariant[] }
export type CharacterPresentation = {
  version?: number
  base_sprite?: string | null
  default_outfit?: string | null
  outfits?: Outfit[]
}
export type AppearanceSelection = { outfit?: string | null; variant?: string | null }
export function performanceSprite(
  variants: Pick<PortraitVariant, 'id' | 'sprite' | 'expression' | 'pose'>[] | undefined,
  performance?: { expression?: string; pose?: string },
) {
  if (!performance || (!performance.expression && !performance.pose)) return undefined
  return variants
    ?.filter(
      (v) =>
        (v.expression || v.pose) &&
        (!v.expression || v.expression === performance.expression) &&
        (!v.pose || v.pose === performance.pose),
    )
    .sort(
      (a, b) =>
        Number(Boolean(b.expression)) +
        Number(Boolean(b.pose)) -
        Number(Boolean(a.expression)) -
        Number(Boolean(a.pose)),
    )[0]?.sprite
}
export function resolveAppearance(p: CharacterPresentation | undefined, selection: AppearanceSelection = {}) {
  const outfits = p?.outfits || []
  const outfit =
    outfits.find((o) => o.id === selection.outfit) ||
    outfits.find((o) => o.id === p?.default_outfit) ||
    outfits[0]
  const variant =
    outfit?.variants.find((v) => v.id === selection.variant) ||
    outfit?.variants.find((v) => v.id === outfit.default_variant) ||
    outfit?.variants[0]
  return { sprite: variant?.sprite || p?.base_sprite || null, outfit: outfit?.id, variant: variant?.id }
}
export function convertLegacyPortrait(p: CharacterPresentation): CharacterPresentation {
  if (!p.base_sprite || p.outfits?.length) return p
  return {
    ...p,
    version: 1,
    default_outfit: 'default',
    outfits: [
      {
        id: 'default',
        name: '常态造型',
        default_variant: 'neutral',
        variants: [
          { id: 'neutral', name: '常态', sprite: p.base_sprite, expression: 'neutral', pose: 'neutral' },
        ],
      },
    ],
  }
}
