# 将角色扮演 Alpha 源码发布到 GitHub

目标仓库：<https://github.com/THY495/SceneWeaver>。请确认它仍是空仓库；若 GitHub 已自动创建 README、LICENSE 或其他初始提交，先停下核对历史，不要使用强制推送。

**只在本发布目录执行以下命令。** 原开发工作区含办公助手历史代码、未提交改动及本地数据，不适合作为首次公开提交。此目录为独立、无原项目 Git 历史的源码快照，包含 MIT 代码许可证和单独的示例内容条款。先阅读 [`README.md`](README.md)、[`LICENSE`](LICENSE)、[`CONTENT_LICENSE.md`](CONTENT_LICENSE.md) 与 [`docs/acceptance.md`](docs/acceptance.md)。

在本目录打开 PowerShell：

```powershell
python scripts/verify_release.py
if ($LASTEXITCODE -ne 0) { throw "发布包校验失败，请停止" }
git init -b main
if ($LASTEXITCODE -ne 0) { throw "Git 初始化失败，请停止" }
git add .
if ($LASTEXITCODE -ne 0) { throw "暂存失败，请停止" }
git status --short
git diff --cached --check
if ($LASTEXITCODE -ne 0) { throw "暂存文件存在空白错误，请停止" }
python scripts/verify_release.py --staged
if ($LASTEXITCODE -ne 0) { throw "暂存清单校验失败，请停止" }

git config user.name
git config user.email
# 若上述任一项为空，先设置为你的 GitHub 显示名和已验证邮箱（或 GitHub noreply 邮箱）：
# git config user.name "THY495"
# git config user.email "你的 GitHub 邮箱"

git commit -m "Release Sceneweaver v0.1.0-alpha.1 source preview"
if ($LASTEXITCODE -ne 0) { throw "提交失败，请停止" }
git tag v0.1.0-alpha.1
if ($LASTEXITCODE -ne 0) { throw "标签创建失败，请停止" }
git remote add origin https://github.com/THY495/SceneWeaver.git
if ($LASTEXITCODE -ne 0) { throw "远端配置失败，请停止" }
git remote -v
git push -u origin main
if ($LASTEXITCODE -ne 0) { throw "main 推送失败，请停止" }
git push origin v0.1.0-alpha.1
if ($LASTEXITCODE -ne 0) { throw "标签推送失败，请检查网络或权限" }
git status --short
```

`git status --short` 的暂存文件应只包括源码、示例、测试和文档；不能出现 `.env`、Key、存档、`.runtime`、`node_modules` 或 Windows 二进制。`verify_release.py --staged` 会按发布清单检查全部暂存文件与内容，发现增删或改动时会失败。不要用 `git add -f` 加入被忽略的凭据，也不要对远端使用 `--force`。

首次 `git push` 可能要求你通过浏览器或 Git Credential Manager 登录 GitHub；若在 HTTPS 密码栏输入凭据，应使用 GitHub 个人访问令牌，不是账户密码。不要把令牌写进命令、仓库或聊天。推送后打开仓库页面确认 README 和 MIT 许可证已显示，并在 Tags 中确认 `v0.1.0-alpha.1`；如创建 GitHub Release，标记为 **pre-release**，不要附上本机未审查的 Windows 二进制。
