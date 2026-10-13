# 升级与数据保留

升级前先在数据副本验证，再替换日常环境；具体可用性以相应版本的验收说明为准。

## 升级前

关闭 Studio、游戏和 Worker，避免仍在写入时备份。保留完整工程目录（含媒体、文档和项目清单），以及应用 data 目录和实际使用的 PostgreSQL 数据库备份。故事导出包是创作内容备份，不等于玩家存档备份；玩家进度保存在数据库中。

本机 API 配置与玩家凭据单独保留，不加入 Git、问题附件或分享的故事包。原生凭据受当前 Windows 用户保护，复制凭据文件不能保证在另一台机器或另一账户可用，必要时重新输入 Key。

Docker 数据卷和工程目录不能靠重新下载源码恢复。停止 Docker 服务可用 docker compose down，保留卷，不加 -v；删除卷会丢失数据库。

数据库采用受支持的一致性备份（逻辑备份，或停止数据库后的物理备份）；不要直接复制仍在运行的数据库数据目录。

### Docker 源码部署备份

在原版本目录操作，确认使用的是原 Compose 项目。可用 `docker compose ls` 核对项目名；切换源码目录时，应继续使用原项目名（例如通过本机 `.env` 的 `COMPOSE_PROJECT_NAME`），否则 Compose 会创建另一组空卷，看起来像作品丢失。

以下适用于示例 Compose 的默认数据库用户和库名；自定义部署需换成实际名称。先关闭游戏，停止写入服务，备份到新的目录：

```powershell
$backupDir = Join-Path '.runtime/backups' (Get-Date -Format 'yyyyMMdd-HHmmss')
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
docker compose stop studio worker
if ($LASTEXITCODE -ne 0) { throw '写入服务未成功停止，停止升级' }
docker compose exec -T postgres pg_dump -U sceneweaver -d sceneweaver -Fc -f /tmp/sceneweaver-before-update.dump
if ($LASTEXITCODE -ne 0) { throw '数据库备份失败，停止升级' }
docker compose cp postgres:/tmp/sceneweaver-before-update.dump (Join-Path $backupDir 'database.dump')
if ($LASTEXITCODE -ne 0) { throw '数据库备份复制失败，停止升级' }
docker compose cp studio:/app/data/. (Join-Path $backupDir 'app-data')
if ($LASTEXITCODE -ne 0) { throw '应用数据备份失败，停止升级' }
```

备份目录可能含作品、玩家数据或本机配置，应私人保存。另行备份存放在应用 data 目录之外的工程和媒体。保留原 `.env` 和原项目名，但不要把它们加入公开包。回退需在新的隔离数据库中用 `pg_restore` 验证备份，再配合旧版本恢复；不要在唯一原数据库上先试恢复或降级。

## 在副本上升级

从独立目录解压或检出正式源码，按 README 使用 uv sync --locked --extra dev、npm --prefix apps/web ci 安装锁定依赖，或按 Docker 启动说明构建新镜像。不要把旧 .venv 或 node_modules 复制到新目录。保留自己的配置，不用 .env.example 覆盖已填写的 .env。

源码数据库升级按迁移链执行到 head。新增020迁移记录角色记忆的完整上下文依赖；不应手工补造未知依赖。旧模型推导笔记如果缺少完整依赖，会保留记录但不再供角色直接使用，需要从仍可见的原始记录重新整理。升级可能暂时改变可用的角色记忆，这是保守隔离，不是凭空恢复或删除原始言论。

当前源码候选做过一次有限跨版本检查：由公开 alpha.2 的实际代码创建工程、玩家存档及阅读位置，迁移到当前源码后恢复同一存档；原对白、阅读位置和工程媒体保持，依赖未知的旧推导记忆仍保留但不可直接使用。使用人工声明和受控数据，不代表所有旧工程、正在执行的任务、原生程序或长期游玩都能无缝升级。

原生宿主也有幂等依赖升级逻辑，但当前源码候选的原生完整验收尚未完成。不要以源码测试通过代替原生更新验证。

## 检查与回退

核对工程可打开、人物/媒体/导演设定仍在；同一冻结故事的存档对白、位置与阅读进度可恢复。更新故事内容会影响存档所属作品的身份；相同标题不表示旧存档可用于新的故事发行包，不要改写存档身份来强行恢复。

密钥缺失时不能创建新游戏；TTS Key 缺失时使用文字路径。先做无付费检查，再按自己的预算测试真实模型。测试失败保留错误类型、版本与日志，去掉 Key、请求头和私人对白后再报告。

需要回退时使用升级前完整数据备份和与其匹配的旧版本，先验证恢复副本。不要把旧程序直接连接升级后的唯一数据库，也不要靠删除迁移字段或强行降级修复。保存原始备份直到新版本和存档验证完成。
