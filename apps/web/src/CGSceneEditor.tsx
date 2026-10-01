import { SpriteEditor } from './CharacterStage'
import { StagePlayer } from './StagePlayer'

export function CGSceneEditor({
  title,
  background,
  images,
  request,
  onChange,
}: {
  title: string
  background?: string | null
  images: { id: string; label: string }[]
  request: (path: string, init?: RequestInit) => Promise<unknown>
  onChange: (id: string | null) => void
}) {
  return (
    <section aria-label="CG 场景编辑">
      <h3>CG 主画面</h3>
      <p>整张图片配合旁白和人物对白播放。在场角色仍可互动；人物立绘与画面物品不叠加显示。</p>
      <SpriteEditor value={background} background choices={images} request={request} onChange={onChange} />
      {!background && <p role="status">选择图片后即可预览；运行前必须设置 CG 主画面。</p>}
      <StagePlayer
        title={title}
        background={background}
        visualMode="cg"
        people={[]}
        request={request}
        line={{
          id: 'cg-preview',
          speaker: '对白预览',
          text: '在下方“场内演出与预览”编辑固定对白或 AI 互动。',
        }}
      />
      <p>切回人物舞台会恢复原有站位和画面物品设置。</p>
    </section>
  )
}
