# 渡船演示素材生成记录（2026-09-28）

使用内置 `image_gen` 模式，四张最终 PNG 已复制到本目录的 `assets/`，没有以默认生成目录作为工程素材位置。三张人物图请求透明背景，文件均为 RGBA，alpha 范围为 0–254；背景为 RGB。素材仅用于检验创作者导入、场景装配及独立游戏呈现，不新增人物外貌的剧情事实。

最终背景 `ferry-night-pavilion-v2.png`：

> Use case: illustration-story. Asset type: compact PNG background for a 16:9 visual-novel game; hard 2 MB file-size target. Stylized flat-color 2D illustration of a ferry waiting pavilion on a rainy river at night. Use broad smooth shapes, solid color regions, minimal texture and only a few rain streaks so the PNG compresses well. One amber lantern beneath a simple roof, river beyond, wet ground, no characters, no boat. Keep the central foreground empty for three standing character sprites and the bottom clear for dialogue. Restrained navy/teal/amber palette; clean modern narrative-game art. No legible text, no signage, no UI, no watermark.

人物 `alan-captain.png`：

> Use case: illustration-story. Asset type: transparent full-body visual-novel character sprite for a synthetic Sceneweaver demo. Character: 阿岚, an adult ferry captain in a practical dark navy raincoat, calm and direct, alert to storm safety, slight confident stance. Style: polished hand-painted 2D game illustration with realistic proportions and painterly edges, matching a moody rainy riverside pavilion at night; cool blue rim light and warm amber lantern highlights. Composition: one standing person, full body, front three-quarter view, centered, feet visible, enough transparent margin, no props beyond simple rainwear. Constraints: truly transparent background; no environment, no text, no name label, no UI, no watermark.

人物 `shenzhou-attendant.png`：

> Use case: illustration-story. Asset type: transparent full-body visual-novel character sprite for a synthetic Sceneweaver demo. Character: 沈舟, an adult dock attendant, patient and careful, wearing a weathered olive field jacket over work clothes, holding a small closed notebook, thoughtful expression. Style: polished hand-painted 2D game illustration with realistic proportions and painterly edges, matching a moody rainy riverside pavilion at night; cool blue rim light and warm amber lantern highlights. Composition: one standing person, full body, front three-quarter view, centered, feet visible, enough transparent margin. Constraints: truly transparent background; no environment, no text, no name label, no UI, no watermark.

人物 `linche-officer.png`：

> Use case: illustration-story. Asset type: transparent full-body visual-novel character sprite for a synthetic Sceneweaver demo. Character: 林澈, an adult ferry-terminal duty officer, cautious and conscientious, wearing a charcoal service jacket and simple badge without readable writing, holding a folded storm notice, reserved expression. Style: polished hand-painted 2D game illustration with realistic proportions and painterly edges, matching a moody rainy riverside pavilion at night; cool blue rim light and warm amber lantern highlights. Composition: one standing person, full body, front three-quarter view, centered, feet visible, enough transparent margin. Constraints: truly transparent background; no environment, no text, no name label, no UI, no watermark.

背景的前两次尝试分别为 2,602,108 与 2,166,178 字节，超过 r25 冻结 Studio 的 2 MiB 上传上限，因此未保留为工程素材；第三次最终图为 1,801,948 字节。当前**源码**将上限调整到 4 MiB，并通过真实 2,602,108 字节 PNG 的验证函数检查，冻结 r25 仍保持 2 MiB。人物图分别为 1,846,930、1,798,276、1,517,387 字节。
