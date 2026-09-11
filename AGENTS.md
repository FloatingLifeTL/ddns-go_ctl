# AGENTS.md

本文件适用于 `ddns-go_ctl` 公开仓库。公开用户说明以 `README.md` 为准，详细行为和兼容性说明见 `docs/project.md`。

## 仓库角色

- 公开仓库是公开代码、用户文档、Release、标签和版本说明的唯一事实源。
- 公开仓库不保存内部开发源镜像、本地私有归档或开发环境元数据。
- `README.md` 面向使用者；`.github/CONTRIBUTING.md` 面向贡献者；本文件面向仓库维护和 Agent 操作。

## 项目边界

- 本项目是依赖 `jeessy2/ddns-go` 官方可执行文件的外部 Windows 管理工具。
- 不复制、修改或打包 DDNS-GO 上游源码，也不打包 `ddns-go.exe`。
- 不要将本项目描述为 DDNS-GO 的源码 Fork、源码衍生项目或官方项目。
- 运行时需要用户从 DDNS-GO 官方 Releases 获取 `ddns-go.exe`，公开仓库不得提供或代为分发该文件。
- DDNS-GO 管理页面默认监听全部接口；应保持 DDNS-GO 设置中的“禁止从公网访问”开启。

## 内容边界

公开仓库允许提交以下内容：

- `src/ddns-go_ctl.py`、`src/ddns-go_ctl_py-run.bat`
- `src/ddns-go_ctl_py-run_abspath.bat_example.bak`，作为使用绝对路径占位符的示例保留
- `tests/` 下的标准库测试
- `scripts/release-notes.ps1`、`scripts/checksums.ps1`
- `docs/` 下的项目说明、版本信息和发布说明
- `.github/` 下的社区规范与工作流
- `README.md`、`AGENTS.md`、`LICENSE`、公开的 `.gitignore` 和 `.gitattributes`

不得提交以下内容：

- `ddns-go.exe`、真实用户配置、日志、运行状态、token、API key 或真实凭据
- `ctl-data/`、`__pycache__/`、`*.py[cod]`
- `build/`、`dist/`、`*.spec`、临时导出文件
- `_Private/`、`_Private_Archive/`、0_Archive 备份、开发环境元数据
- `_Release-Assets-Backups/` 及其中的发布产物与校验清单
- 未经明确要求新增的本机绝对路径；`ddns-go_ctl_py-run_abspath.bat_example.bak` 应使用通用占位路径，不得提交实际环境路径

如果本机存在 `_Private/`，它只能作为本地归档，不得使用 `git add -f`、提交或推送。

## 运行模型

- 运行时目录由实际入口文件决定：
  - `ddns-go_ctl.exe`：使用该 EXE 所在目录。
  - `ddns-go_ctl.py`：使用该 Python 脚本所在目录。
- `ddns-go.exe` 必须与控制入口位于同一运行目录。
- 源码方式部署时，将 `src/ddns-go_ctl.py` 和 `src/ddns-go_ctl_py-run.bat` 移动到 `ddns-go.exe` 所在目录，不要反向把 `ddns-go.exe` 复制进仓库 `src/`。
- `ddns-go_ctl_py-run.bat` 使用 `%~dp0`，必须和 `ddns-go_ctl.py` 同目录。
- `ddns-go_ctl_py-run_abspath.bat_example.bak` 是绝对路径版示例，可放在任意位置，但文件中必须指向正确的 `ddns-go_ctl.py` 绝对路径；移动脚本后必须同步修改该路径。
- 不改用运行环境变量、仓库源码路径或启动时工作目录来定位运行时资源，保持 `Path(__file__).resolve().parent` 和冻结模式的 `sys.executable` 语义。

## 版本与发布

- `docs/VERSION`、`src/ddns-go_ctl.py` 中的 `SCRIPT_VERSION`、README 当前版本和当前发布说明必须一致。
- 当前版本说明固定为 `docs/releases/v<version>.md`。
- 历史版本说明放在 `docs/releases/history/`。
- `docs/releases/` 中的版本 Markdown 文件必须是可直接上传到 GitHub Releases 的正式正文，同时作为仓库内备份；不得将其作为草稿或仅供内部使用的文件。
- `docs/releases/README.md` 是目录规范，不作为 Release 正文。
- Release 正文使用 `scripts/release-notes.ps1 -Body` 提取。
- 发布前必须执行 `./scripts/release-notes.ps1 -Check`。
- 发布说明只声明随 Release 上传的资产名和校验清单；资产大小、构建信息与 SHA-256 以 `SHA256SUMS.txt` 和发布页为准，不再写入发布说明，避免与清单重复。
- 上一条自 v2.7.1 起适用；更早版本的历史说明保留原有的内联 SHA-256，与已发布的 Release 正文保持一致，不做回溯修改。
- `main`、Tag 和 Release 由主发布者统一维护，未经明确授权不得修改或推送。

## 发布产物与校验清单

- 本地发布产物与清单存放在 `_Release-Assets-Backups/v<version>_/`，该目录已忽略，只作本机暂存与备份，不得提交或推送。
- 每个版本目录内只放该版本的 EXE 与它的 `SHA256SUMS.txt`，清单文件名固定。
- EXE 资产名统一为 `ddns-go_ctl_v<version>_win-amd64-portable.exe`，重命名必须在生成清单之前完成：清单记录文件的当前名称。
- 清单格式为 GNU coreutils 兼容文本：小写十六进制、两个空格分隔、LF 换行、无 BOM、行内只有裸文件名，且清单不列自身。
- 生成与校验：

```powershell
./scripts/checksums.ps1 _Release-Assets-Backups/v<version>_
./scripts/checksums.ps1 -Verify _Release-Assets-Backups/v<version>_
./scripts/checksums.ps1 -CheckFormat _Release-Assets-Backups/v<version>_
```

- 校验清单只提供下载完整性校验，不构成来源签名；未代码签名的版本不声称具备防篡改能力。
- 上传 Release 时 EXE 与清单一起上传，资产文件名与清单内记录逐字符一致。

## 开发检查

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
./scripts/release-notes.ps1 -Check
```

发布 EXE 时追加校验清单检查：

```powershell
./scripts/checksums.ps1 -Verify _Release-Assets-Backups/v<version>_
```

Python 脚本只使用标准库，支持基线为 CPython 3.11 或更高版本。测试不启动真实 DDNS-GO，不触发动态 DNS 更新。

PowerShell 脚本需要兼容 Windows PowerShell 5.1 与 PowerShell 7，并保持 UTF-8 BOM，避免中文解析错误。

## 修改边界

- 未获明确授权，不修改 `main`、Tag、Release 或发布历史。
- 未获明确授权，不 amend、不强推、不推送公开仓库。
- 不提交本地私有归档、内部开发源目录、会话数据或本机路径。
- 提交前运行公开仓库开发检查。
- 贡献者流程以 `.github/CONTRIBUTING.md` 为准。
- 保持 `Settings` 为可调参数的唯一来源，`DdnsController` 的可调参数属性维持只读委托；组件边界继续用 `typing.Protocol` 声明，默认实现只做转发、实际逻辑留在模块级函数。依赖注入位属内部约定，不作为对外稳定 API。
- 不覆盖或还原用户未要求的工作区改动，改动范围应保持聚焦。

## 兼容性

- 当前公开支持范围：Windows x86_64。
- 源码运行基线：CPython 3.11 或更高版本。
- 发布版 EXE：无需安装 Python。
- DDNS-GO 版本：不绑定具体版本，按官方启动参数透传；原则上可尝试其他版本，但不保证全部兼容。
