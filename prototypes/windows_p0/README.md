# Windows P0 独立运行试验

最新候选为 `.runtime/windows-p0/桌面试玩 P0-r3`，修复任务完成时缓存会话序号的并发问题；冻结接受/拒绝交接与恢复通过。`verify_backend.py` 现在包含 `transfer_checks.py` 的正式 HTTP 物品验收，决策由测试通过创作者接口注入。详见 `docs/126-frozen-handoff-and-completion-race.md`。

桌面试验新增 `desktop_host.py` + `build-desktop.ps1`，使用 pywebview/WebView2 自动启动后台、交接令牌并关闭回收。`assemble_desktop.py` 组装全新文件夹，`verify_desktop.py` 只验证隐藏窗口加载/跳转和后台停止。当前候选为 `.runtime/windows-p0/桌面试玩 P0-r2`；窗口与玩家模式证据、先决条件和限制见 `docs/125-desktop-window-and-player-mode.md`。

正式后台接入：`backend_host.py` 使用正式 API/Worker/Agents 和专用离线假模型；`build-backend.ps1` 收集现有前端资源并冻结；`verify_backend.py --host-exe ...` 验证真实 HTTP 角色对话、检查点、重复请求和退出恢复。详见 `docs/124-frozen-backend-dialogue-and-recovery.md`。这是独立后台切片，尚无最终桌面窗口与自动令牌交接，不要当正式玩家版。

最新宿主：`managed_host.py` 组合实例锁、DPAPI、路径别名、端口选择、正常停库及 Job 清理。`build-host.ps1` 将其冻结成独立 exe；`verify_managed_host.py` 支持 `--host-exe` 验证冻结版本。复现和限制见 `docs/123-managed-host-and-frozen-lifecycle.md`。它仍是数据库宿主，不包含完整 AI 后台/前端，也不应作为玩家程序分发。

精简包与强杀清理的最新证据见 `docs/122-slim-runtime-and-host-lifecycle.md`。`build_pg.py` 用显式清单和 PE 导入收集本地候选包（输出必须是不存在的新目录）；`verify_host_exit.py <pgroot> <work> <report>` 创建可丢弃宿主并测试强杀清理。`windows_job.py` 不得用于现有应用主进程。构建示例：

```powershell
.runtime/windows-p0-build/Scripts/python.exe prototypes/windows_p0/build_pg.py .runtime/windows-pg/pgsql 'C:/Program Files/Microsoft Visual Studio/2022/Community/VC/Redist/MSVC/14.44.35112/x64/Microsoft.VC143.CRT' .runtime/windows-pg/postgresql-17.11-4.zip .runtime/windows-pg/pgvector-0.8.6/LICENSE .runtime/windows-pg/new-candidate
```

运行库版本路径按实际安装选择。中文发行路径目前需试验级 ASCII 别名，尚未形成玩家可用的自动处理机制，不能直接当正式发行包。

最新试验：`queue_probe.py` 为 SQLite 任务协调适配，`pg_probe.py` 为原生 PostgreSQL 生命周期及正式业务回归，`audit_native.py` 为原生依赖清单。三者都不包含在最初 Tk 冻结样机内。结果和后续门槛见 `docs/121-native-windows-storage-validation.md`。

原生试验命令（需先准备文档 121 对应的 PostgreSQL/pgvector；创建全新独立测试库）：

```powershell
.venv/Scripts/python.exe prototypes/windows_p0/pg_probe.py .runtime/windows-pg/pgsql .runtime/windows-pg/runs evals/windows-p0/native-pg.json
.runtime/windows-p0-build/Scripts/python.exe prototypes/windows_p0/audit_native.py .runtime/windows-pg/pgsql evals/windows-p0/native-dependencies.json
```

后一个命令依赖构建环境里的 `pefile`；审计结果不是最终发行许可或干净系统验收。

这是打包与存储试验，不是新的正式游戏 UI，也不替代现有 Sceneweaver 后端。只依赖标准库、Pydantic 和现有纯物品规则；不读取 `.env`，不调用模型，不连接 PostgreSQL，不监听端口。

上述依赖范围指 `probe.py` 冻结样机。后续 `sqlite_slice.py` 是独立的正式业务适配试验，使用现有开发环境的 SQLAlchemy/pgvector 等依赖，由 `tests/test_windows_sqlite_slice.py` 验证，不包含在冻结 exe 内。详情见 `docs/120-real-runtime-storage-slice.md`。

固定开场和假模型回复会明确标识。支持交谈、放下/拿起旧信、退出后恢复。默认存档在当前用户 `LOCALAPPDATA/SceneweaverP0/ferry-v1`，使用 `--data-dir` 可指定测试目录。必须复制完整发行文件夹，不能只复制 exe。

## 构建

在仓库根目录，用可用的 Windows Python 创建隔离环境（当前实测为 Python 3.13.9 AMD64）：

```powershell
uv venv .runtime/windows-p0-build --python .venv/Scripts/python.exe
uv pip install --python .runtime/windows-p0-build/Scripts/python.exe -r prototypes/windows_p0/requirements-build.txt
powershell -NoProfile -File prototypes/windows_p0/build.ps1
```

产物：`.runtime/windows-p0/dist/SceneweaverP0/SceneweaverP0.exe`。构建脚本只收集明确入口、其导入模块、物品预设与依赖，不复制整个工作目录。`notices` 是初步第三方说明，不是完成了公开发行审计。

## 自动验收

```powershell
.venv/Scripts/python.exe prototypes/windows_p0/verify.py .runtime/windows-p0/dist/SceneweaverP0 .runtime/windows-p0/isolated evals/windows-p0/host-isolation.json
```

验证器复制完整包到带中文和空格的新目录，为子进程移除开发 PATH、PYTHONPATH、模型配置等环境；所有样机操作由冻结 exe 执行，验证器自身仍运行于开发 Python。子进程隐藏启动，GUI smoke 只创建并销毁隐藏窗口。

故障测试使用 `--crash before/after` 在事务提交前/后直接结束该测试进程，检查重启回滚及相同请求重放。这不是模拟整机掉电，也不是当前 PostgreSQL 队列/多角色 agent 的回归替代品。

## 干净 Windows 人工验收

1. 复制完整发行文件夹到没有 Python、Node、Docker 和当前数据库的 Windows 机器；按普通用户权限操作。
2. 双击 exe，确认开场显示且明确标注假模型。
3. 输入一句话，放下旧信，关闭窗口后重新打开；确认对白和物品位置恢复。
4. 拿起旧信并退出，确认再次恢复正确。记录操作系统具体版本、架构与启动失败信息。
5. 不填写任何真实密钥；本试验没有 API 设置。不要删除用户已有存档来重复测试，可使用独立 `--data-dir`。

当前自动验收不能证明干净系统兼容性、视觉可用性、管理员权限边界、杀毒软件信任或正式引擎迁移完成。详细决策和剩余工作见 `docs/119-windows-p0-packaging-probe.md`。
