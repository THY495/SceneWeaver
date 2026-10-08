# 提交 v0.1.0-alpha.2 到 GitHub

目标仓库：https://github.com/THY495/SceneWeaver 。下面将源码更新提交到现有 main 历史；不会 git init、强推或改动 alpha.1 标签。维护者已准备本机 Git checkout 时，优先使用包旁的提交说明和该 checkout。

## 本机已准备好的提交目录

本次已准备 `D:\WorkSpace\Chatbot_thinking\.runtime\alpha-release\SceneWeaver-alpha2-push`，源码已暂存但尚未提交或推送。使用以下步骤即可，不要再向此目录 clone，也不要从开发项目根目录执行 git add。

```powershell
Set-Location -LiteralPath 'D:\WorkSpace\Chatbot_thinking\.runtime\alpha-release\SceneWeaver-alpha2-push'
git config --global --add safe.directory 'D:/WorkSpace/Chatbot_thinking/.runtime/alpha-release/SceneWeaver-alpha2-push'
git diff --cached --check
if ($LASTEXITCODE -ne 0) { throw '差异检查失败' }
python scripts/verify_release.py --staged
if ($LASTEXITCODE -ne 0) { throw '暂存文件校验失败' }
git diff --cached --stat
git config user.name 'Hanyu'
git config user.email 'hanyut046@gmail.com'
git commit -m 'Release Sceneweaver v0.1.0-alpha.2 source preview'
if ($LASTEXITCODE -ne 0) { throw '提交失败' }
git push origin main
if ($LASTEXITCODE -ne 0) { throw '推送失败；不要强推' }
```

safe.directory 只为这个目录处理沙箱账户与当前用户所有权不同的情况，不添加全盘例外。若 main 发生远程变更，普通 push 会拒绝，请先处理变更，不用 --force。下面的从快照开始步骤仅用于另建提交目录，两套步骤不要重复执行。

## 从此发布快照开始

在 PowerShell 中运行下列命令。snapshot 是本次验证的发布快照；checkout 必须是尚不存在的新目录，且不能放在 snapshot 内。每条 Git/Python 命令都应成功后再继续。

```powershell
$releaseSnapshot = 'D:\WorkSpace\Chatbot_thinking\.runtime\alpha-release\sceneweaver-v0.1.0-alpha.2-roleplay-release'
$releaseCheckout = 'D:\WorkSpace\Chatbot_thinking\.runtime\alpha-release\SceneWeaver-alpha2-fresh'
Set-Location -LiteralPath $releaseSnapshot
python scripts/verify_release.py
if ($LASTEXITCODE -ne 0) { throw '发布快照校验失败' }

if (Test-Path -LiteralPath $releaseCheckout) { throw '目标目录已存在，请使用包旁的已准备 checkout 指令，或选一个新目录' }
git clone --branch main https://github.com/THY495/SceneWeaver.git $releaseCheckout
if ($LASTEXITCODE -ne 0) { throw '克隆失败' }
python "$releaseSnapshot\scripts\update_release_checkout.py" --target $releaseCheckout --branch main
if ($LASTEXITCODE -ne 0) { throw '复制发布快照失败' }
Set-Location -LiteralPath $releaseCheckout

git add -A
if ($LASTEXITCODE -ne 0) { throw '暂存失败' }
git diff --cached --check
if ($LASTEXITCODE -ne 0) { throw '差异检查失败' }
python scripts/verify_release.py --staged
if ($LASTEXITCODE -ne 0) { throw '暂存文件不匹配清单' }
git diff --cached --stat
git status --short
```

检查列表应仅有源码、测试、示例、文档和构建配置，不应包含 .env、密钥、存档、.runtime 或 node_modules。然后提交并推送：

```powershell
git config user.name 'Hanyu'
git config user.email 'hanyut046@gmail.com'
git commit -m 'Release Sceneweaver v0.1.0-alpha.2 source preview'
if ($LASTEXITCODE -ne 0) { throw '提交失败' }
git push origin main
if ($LASTEXITCODE -ne 0) { throw '推送失败，请处理登录或远程变化；不要强推' }
```

若 Git 报 detected dubious ownership，仅为实际 checkout 加例外，再重试原失败命令：

```powershell
git config --global --add safe.directory $releaseCheckout
```

不要使用 safe.directory=*。若推送需要 GitHub 登录，使用正常浏览器登录或 Git Credential Manager，勿把令牌写入命令或仓库。

## 可选：给此提交建立版本标签

main 推送成功且确认该提交就是要发布的版本后：

```powershell
git tag -a v0.1.0-alpha.2 -m 'Sceneweaver v0.1.0-alpha.2 source preview'
if ($LASTEXITCODE -ne 0) { throw '标签创建失败；如已存在，不要移动或覆盖' }
git push origin v0.1.0-alpha.2
if ($LASTEXITCODE -ne 0) { throw '标签推送失败' }
```

GitHub Releases 页面可选择此标签并标为 pre-release。上传源码 ZIP 是可选步骤；它不包含 Windows 可执行发行包。此文档只是操作说明，准备过程中未替你 push 或发布。
