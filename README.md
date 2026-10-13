# Sceneweaver

Sceneweaver 是面向 **AI 驱动文字冒险游戏**的低代码开发工具，当前优先支持 Windows。创作者可以通过 Studio 可视化编辑器编排故事节点、场景画面、人物、物品与演出，并构建和分发具有独立玩家界面的游戏。玩家的对话与行动可以影响角色互动和剧情进展。

玩家需要配置自己的 LLM API Key 才能开始游戏。语音可选，需要另行配置 TTS API Key。

**版本：`v0.1.0-alpha.3` 源码测试版候选。** Windows 原生运行库和安装包不包含在此仓库；此版尚未经过陌生创作者从空工程独立完成作品的验收。复杂多 NPC 场景可能出现重复表达、审核重试和较长等待，语音供应商失败时会退回文字。已验证的范围和限制见[验收说明](docs/acceptance.md)，版本变更见[更新记录](CHANGELOG.md)。欢迎通过 GitHub Issues 提交问题和建议。

## 启动 Studio

需要 Windows、Docker Desktop（含 Compose）。在仓库根目录：

```powershell
if (!(Test-Path -LiteralPath .env)) { Copy-Item .env.example .env }
# 在本机 .env 填写 CHAT_API_KEY；不要提交该文件。
docker compose up -d --build
```

打开 <http://127.0.0.1:8766/>。可从空工程开始，或导入 `examples/stories/` 下的两部合成故事。停止服务：`docker compose down`。不要加 `-v`，除非你确实要删除本地作品和数据库卷。

首次可以不填密钥，先编辑和保存故事；生成试玩需要有效的 LLM Key。对话供应商、地址和模型分别由 `.env` 中的 `CHAT_MODEL_PROVIDER`、`CHAT_API_BASE`、`CHAT_DIALOGUE_MODEL` 配置；个人 API Key 填 `CHAT_API_KEY`。语音需要单独的 TTS 配置及 Key，地域和音色须与账户可用服务对应。修改配置后重新创建服务，勿把真实配置写入 `.env.example` 或故事包。连接检查只证明服务当时可连接，不保证所有模型行为正确。

如启动失败，先运行 `docker compose ps -a` 和 `docker compose logs --tail 100 init studio worker` 查看具体服务；分享日志前移除密钥和私人内容。默认数据库占用本机55432端口；若已有其它数据库占用，可在本机 `.env` 指定 `SCENEWEAVER_PG_PORT=55433`。升级已有作品时应先按[升级说明](docs/upgrade.md)备份并保留原 Compose 项目名和卷。

若要从 Studio 构建 Windows 独立游戏，另需自行准备 `Game/Game.exe`、`GameBackend/GameBackend.exe` 与 `pgsql/bin/postgres.exe` 所在的本机运行库，并设置 `SCENEWEAVER_WINDOWS_RUNTIME_HOST` 后使用 `compose.windows-runtime.yaml` 覆盖文件；当前源码预览不附带这些第三方／未签名二进制。没有运行库时，仍可编辑故事并在本机测试 Studio。

## 开发与验证

Windows 原生构建入口（实验性）需要 Windows x64、uv 和 Node.js ≥22.12：

```powershell
uv run --no-project --no-config --no-env-file --python 3.13.9 python prototypes/windows_p0/native_build.py all
```

入口自动创建 `.runtime/windows-p0/env`，安装经过哈希校验的应用与工具联合锁，运行 `npm ci` 后构建 Game、GameBackend、Sceneweaver、SceneweaverBackend。产物在 `.runtime/windows-p0/dist`，每次调用的步骤日志、依赖身份与失败收据在 `receipts`。不加载 `.env`，不使用开发环境的 API 配置。构建前无需手动创建 Python 环境或安装前端依赖；首次可能下载 Python 和依赖。单组件仍可用 `build-backend.ps1 -Game/-Studio` 或 `build-desktop.ps1 -Game/-Studio`，通过 `-OutputRoot` 选择本项目 `.runtime` 下另一目录。

组装工具使用该环境的 `.runtime/windows-p0/env/Scripts/python.exe`。另需自行准备具有合法分发权限的 PostgreSQL/pgvector Windows 目录；以 `assemble_runtime.py <dist目录> <pgsql目录> <新运行库目录>` 组装 Game 运行库，再以 `assemble_studio.py <dist目录> <运行库目录> <新Studio目录>` 组装开发工具。Python 环境与第三方数据库不随源码包提供；一次构建成功不等于干净机器或完整游玩验收。

维护者更新应用/工具依赖后，应显式运行同入口的 `lock` 模式并审查 `native-build.lock` 与 `native-lock-inputs.json`，随后重新验证。普通构建不重新解析依赖；锁输入不匹配会停止，避免静默升级。

需要 Python 3.12 或 3.13、`uv`、Node.js ≥22.12 和 Docker。执行 `uv sync --locked --extra dev`、`npm --prefix apps/web ci`、`docker compose up -d postgres`，然后运行 `.venv/Scripts/python.exe scripts/check.py`。自动检查使用专用 `sceneweaver_test` 数据库，不调用付费模型。公开源码 CI 从全新环境安装锁定依赖，检查源码边界，构建 Studio 与独立玩家 Web，并运行离线后端回归；原生 Windows 和真实模型验收单独执行。发布范围和已知问题见 [验收说明](docs/acceptance.md)。

程序代码按 [MIT](LICENSE) 授权；两部合成故事及内嵌美术按 [示例内容许可](CONTENT_LICENSE.md) 仅供测试，允许完整仓库的技术性 fork，不允许独立复用或另行发行。创作者自己的作品不因使用 Sceneweaver 而套用示例条款。不要上传 `.env`、Key 文件、玩家存档或个人素材。

升级前备份工程、媒体和数据库存档，并先在副本上检查；具体范围与回退方式见[升级说明](docs/upgrade.md)。
