# Sceneweaver

Sceneweaver 是面向 **AI 驱动文字冒险游戏**的低代码开发工具。本仓库只发布角色扮演方向：创作者用 Studio 编辑故事图、场景、人物、物品和演出，再构建具有独立玩家入口的游戏。玩家配置自己的 LLM API Key 才能开始；TTS 可选。产品设置固定为 `roleplay`，没有办公助手界面或启动配置；少量共用底层模块仍保留早期兼容分支，后续会继续拆除。

**版本：`v0.1.0-alpha.1` 源码预览。** Windows 原生运行库和可信签名安装包不包含在此仓库。当前版本尚未经过最新版陌生创作者从空工程独立完成作品的验收；请将问题和建议发至 GitHub Issues。

## 启动 Studio

需要 Windows、Docker Desktop（含 Compose）。在仓库根目录：

```powershell
Copy-Item .env.example .env
# 在本机 .env 填写 CHAT_API_KEY；不要提交该文件。
docker compose up -d --build
```

打开 <http://127.0.0.1:8766/>。可从空工程开始，或导入 `examples/stories/` 下的两部合成故事。停止服务：`docker compose down`。不要加 `-v`，除非你确实要删除本地作品和数据库卷。

若要从 Studio 构建 Windows 独立游戏，另需自行准备 `Game/Game.exe`、`GameBackend/GameBackend.exe` 与 `pgsql/bin/postgres.exe` 所在的本机运行库，并设置 `SCENEWEAVER_WINDOWS_RUNTIME_HOST` 后使用 `compose.windows-runtime.yaml` 覆盖文件；当前源码预览不附带这些第三方／未签名二进制。没有运行库时，仍可编辑故事并在本机测试 Studio。

## 开发与验证

需要 Python 3.12 或 3.13、`uv`、Node.js ≥22.12 和 Docker。执行 `uv sync --frozen --extra dev`、`npm --prefix apps/web ci`、`docker compose up -d postgres`，然后运行 `.venv/Scripts/python.exe scripts/check.py`。自动检查使用专用 `sceneweaver_test` 数据库，不调用付费模型。发布范围和已知问题见 [验收说明](docs/acceptance.md)。

程序代码按 [MIT](LICENSE) 授权；两部合成故事及内嵌美术按 [示例内容许可](CONTENT_LICENSE.md) 仅供测试，允许完整仓库的技术性 fork，不允许独立复用或另行发行。创作者自己的作品不因使用 Sceneweaver 而套用示例条款。不要上传 `.env`、Key 文件、玩家存档或个人素材。
