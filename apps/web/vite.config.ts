import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, '.', 'SCENEWEAVER_')
  return {
    plugins: [
      react(),
      ...(mode === 'game'
        ? [
            {
              name: 'game-no-studio',
              generateBundle(
                _options: unknown,
                bundle: Record<string, { type: string; modules?: Record<string, unknown> }>,
              ) {
                for (const chunk of Object.values(bundle)) {
                  for (const module of Object.keys(chunk.modules || {})) {
                    if (
                      /\/src\/(main|CreatorShell|StoryGraphWorkspace|DirectionEditor|NarrativeEditor|TimingTrace|StoryTiming|PerformanceLanes|RequestAudit)\.tsx$|@xyflow/.test(
                        module.replaceAll('\\', '/'),
                      )
                    ) {
                      throw new Error(`Game bundle contains creator module: ${module}`)
                    }
                  }
                  if (
                    chunk.type === 'chunk' &&
                    'code' in chunk &&
                    /打开作者诊断|作者推进入口|返回编辑|返回作品库与创作|这一段暂由作者推进/.test(
                      String(chunk.code),
                    )
                  ) {
                    throw new Error('Game bundle contains creator-facing text')
                  }
                }
              },
            },
          ]
        : []),
    ],
    ...(mode === 'game' ? { build: { outDir: 'dist-game', rolldownOptions: { input: 'game.html' } } } : {}),
    cacheDir: '.runtime/vite-cache',
    server: { proxy: { '/api': env.SCENEWEAVER_DEV_API_URL || 'http://127.0.0.1:8766' } },
  }
})
