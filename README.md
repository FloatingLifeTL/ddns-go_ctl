# ddns-go_ctl

`ddns-go_ctl` 是一个面向 Windows 的 DDNS-GO 外部管理工具，用于启动、停止、查看状态、打开管理页面和查看运行时日志。

当前版本：`2.7.1`

本项目不包含、不修改、不打包 `ddns-go.exe`。DDNS-GO 可执行文件需要从官方 Releases 单独获取。

完整功能说明见 [项目说明](./docs/project.md)，版本发布说明见 [Release Note 目录](./docs/releases/README.md)。

## 部署说明

`ddns-go_ctl` 使用“入口文件所在目录”作为运行目录：

- 发布版运行时，入口是 `ddns-go_ctl.exe`。
- 源码方式运行时，入口是移动后的 `ddns-go_ctl.py`。
- `ddns-go.exe` 必须与入口文件位于同一目录。
- 运行时数据会生成在入口文件目录下的 `ctl-data\`。

部署时是把控制脚本移动到 DDNS-GO 所在目录，不是把 `ddns-go.exe` 放回源码目录。

### 发布版 EXE

1. 从 [ddns-go_ctl Releases](https://github.com/FloatingLifeTL/ddns-go_ctl/releases) 下载 `ddns-go_ctl.exe`。
2. 从 [DDNS-GO Releases](https://github.com/jeessy2/ddns-go/releases) 获取官方 Windows 可执行文件。
3. 将 `ddns-go_ctl.exe` 放入 DDNS-GO 所在目录，两者保持同目录。
4. 在命令提示符中进入该目录并运行：

```bat
ddns-go_ctl.exe
```

也可以直接双击 `ddns-go_ctl.exe`。

当前 `v2.7.1` 发布资产的构建信息、大小和 SHA-256 见 [v2.7.1 发布说明](./docs/releases/v2.7.1.md)。

### 源码方式

1. 获取本仓库源码。
2. 从 [DDNS-GO Releases](https://github.com/jeessy2/ddns-go/releases) 获取官方 Windows 可执行文件。
3. 将以下源码入口从仓库的 `src\` 移动到 `ddns-go.exe` 所在目录：

```text
src\ddns-go_ctl.py
src\ddns-go_ctl_py-run.bat
```

`ddns-go_ctl.py` 是实际控制脚本；`ddns-go_ctl_py-run.bat` 是与它配套的 Windows 启动批处理，启动时通过 `%~dp0` 定位同一目录中的 Python 脚本，因此两个文件应一起移动。

4. 在 `ddns-go.exe` 所在目录打开命令提示符并运行：

```bat
python ddns-go_ctl.py
```

也可以双击同目录中的 `ddns-go_ctl_py-run.bat`。

另外，`ddns-go_ctl_py-run_abspath.bat_example.bak` 是绝对路径版启动示例；需要放到其他位置时，先改名为 `ddns-go_ctl_py-run_abspath.bat`，再把其中路径改成 `ddns-go_ctl.py` 的实际完整路径。该批处理可以放在任意位置，但脚本移动后也必须同步更新这个绝对路径。

不要从源码仓库中直接运行 `src\ddns-go_ctl.py`，也不要将 `ddns-go.exe` 复制进 `src\`。否则脚本会把源码的 `src\` 当成运行目录，后续配置、状态和日志都会生成在错误的位置。

### 源码环境

- 支持系统：Windows x86_64。
- 发布版 EXE：无需安装 Python。
- 源码方式：需要 CPython 3.11 或更高版本，且 `python` 已加入命令提示符的 `PATH`。

### 运行目录

```text
DDNS-GO 运行目录\
├── ddns-go.exe
├── ddns-go_ctl.exe               发布版入口，或省略
├── ddns-go_ctl.py                源码方式入口，或省略
├── ddns-go_ctl_py-run.bat        源码配套批处理，或省略
└── ctl-data\
    ├── .ddns_go_config.yaml
    ├── run\
    │   └── ddns-go_runtime.json
    └── logs\
        └── ddns-go_*.log
```

控制脚本会把该目录作为运行目录；DDNS-GO 使用该目录中的配置，管理状态和运行日志由控制脚本写入。源码仓库的 `src\` 不在这个运行目录中。

## 使用方式

启动后进入交互式控制台菜单。主要操作包括：

- 后台启动或前台启动 DDNS-GO。
- 安全停止由本工具管理的 DDNS-GO。
- 打开 DDNS-GO 管理页面。
- 打开日志目录。
- 分页查看并实时跟随日志。
- 刷新并核对当前运行状态。

当前按键、启动参数、日志规则、进程管理和安全边界等详细说明见 [docs/project.md](./docs/project.md)。

## 源码仓库目录

```text
.
├── README.md
├── AGENTS.md
├── LICENSE
├── src\
│   ├── ddns-go_ctl.py
│   ├── ddns-go_ctl_py-run.bat
│   └── ddns-go_ctl_py-run_abspath.bat_example.bak
├── scripts\
│   └── release-notes.ps1
├── docs\
│   ├── VERSION
│   ├── project.md
│   ├── THIRD_PARTY_NOTICES.md
│   └── releases\
│       ├── README.md
│       ├── v2.7.1.md
│       └── history\
│           ├── v2.7.0.md
│           └── v2.6.3.md
├── tests\
│   └── test_ddns_go_ctl.py
└── .github\
    ├── CONTRIBUTING.md
    ├── SECURITY.md
    └── workflows\
        └── tests.yml
```

说明：

- `src\` 保存源码入口，不是运行目录。
- `AGENTS.md` 保存公开仓库维护、目录边界、版本与开发检查规范。
- `scripts\` 保存发布说明辅助脚本，不是 DDNS-GO 运行时目录。
- `ddns-go_ctl_py-run_abspath.bat_example.bak` 是绝对路径版启动脚本示例；`.bak` 本身不参与启动，使用方式见上方“源码方式”。
- `dist\`、`build\`、`_Private\` 和运行时 `ctl-data\` 不纳入公开仓库。

## 测试

在源码仓库根目录运行：

```bat
python tests\test_ddns_go_ctl.py
```

测试不会启动真实 DDNS-GO，也不会触发动态 DNS 更新。

## 安全提醒

- 不要把控制脚本和 DDNS-GO 放在公网可访问的共享目录中运行。
- DDNS-GO 管理页面默认监听全部接口；请保持 DDNS-GO 设置中的“禁止从公网访问”开启。
- 本项目不会停止无法核验为当前运行目录 `ddns-go.exe` 的进程，也不会停止占用目标端口的其他程序。
- 公开仓库不包含实际运行时数据、`ddns-go.exe` 或 API 密钥。`src\ddns-go_ctl_py-run_abspath.bat_example.bak` 是人工维护的绝对路径示例，不应视为安全示例，正式发布前应确认其中没有实际环境路径。

## 许可证

本项目源码使用 [MIT License](./LICENSE)。DDNS-GO 的上游许可证和第三方声明见 [THIRD_PARTY_NOTICES.md](./docs/THIRD_PARTY_NOTICES.md)。

安全报告方式见 [SECURITY.md](./.github/SECURITY.md)，贡献方式见 [CONTRIBUTING.md](./.github/CONTRIBUTING.md)。
