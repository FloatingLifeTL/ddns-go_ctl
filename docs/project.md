# ddns-go_ctl 项目说明

本文说明 `ddns-go_ctl` 的运行方式、脚本功能、配置、日志和进程管理边界。README 只保留快速使用入口，详细行为以本文为准。

当前版本记录在 `docs/VERSION`。

## 1. 运行模型

`ddns-go_ctl` 是一个外部控制程序，不修改 DDNS-GO 源码，也不与 DDNS-GO 官方进程共用运行身份。

运行时目录由实际入口文件决定：

- `ddns-go_ctl.exe`：使用该 EXE 所在目录。
- `ddns-go_ctl.py`：使用该 Python 脚本所在目录。

DDNS-GO 可执行文件必须位于同一个运行目录中。源码方式部署时，应将 `ddns-go_ctl.py` 和配套批处理移动到 DDNS-GO 所在目录，而不是把 `ddns-go.exe` 放回仓库 `src/`。

运行目录中的文件关系如下：

```text
运行目录/
├── ddns-go.exe                 DDNS-GO 官方可执行文件
├── ddns-go_ctl.exe             发布版控制程序，或省略
├── ddns-go_ctl.py              源码方式控制脚本，或省略
├── ddns-go_ctl_py-run.bat      源码方式启动批处理，或省略
└── ctl-data/
    ├── .ddns_go_config.yaml    DDNS-GO 配置文件
    ├── run/
    │   └── ddns-go_runtime.json 管理状态文件
    └── logs/
        └── ddns-go_*.log       每次启动生成的日志
```

脚本使用绝对路径定位同目录资源，不依赖用户启动菜单时的当前工作目录。因此，不要在仓库 `src/` 中运行脚本后把运行时数据误认为仓库的一部分。

## 2. 安装与启动

### 2.1 发布版 EXE

1. 从官方 Releases 获取 `ddns-go.exe`。
2. 将 `ddns-go.exe` 和 `ddns-go_ctl.exe` 放入同一个目录。
3. 在命令提示符中进入该目录。

```bat
ddns-go_ctl.exe
```

发布版无需安装 Python；正常启动会创建 `_MEI...` 临时目录，正常退出后清理，未代码签名。发布资产和校验清单以对应 GitHub Releases 页面为准，SHA-256 只记录在随附的 `SHA256SUMS.txt` 中，供下载后核对完整性；清单只提供完整性校验，不构成来源签名。

### 2.2 源码方式

1. 获取官方 `ddns-go.exe`。
2. 从源码副本的 `src/` 中取出 `ddns-go_ctl.py`。
3. 若使用批处理，同时取出 `src/ddns-go_ctl_py-run.bat`，并保持两个文件同目录。
4. 将这两个文件移动到 `ddns-go.exe` 所在目录。
5. 在该目录运行：

```bat
python ddns-go_ctl.py
```

或双击：

```bat
ddns-go_ctl_py-run.bat
```

`ddns-go_ctl_py-run.bat` 内容为：

```bat
@python "%~dp0ddns-go_ctl.py" %*
```

其中 `%~dp0` 指向批处理所在目录，因此批处理与 Python 脚本必须同时移动。`ddns-go_ctl_py-run_abspath.bat_example.bak` 是使用绝对路径占位符的示例备份，不参与正式启动；使用时需改成实际脚本路径，并且不要将真实本机路径留存到公开发布文件。

### 2.3 单实例限制

脚本以运行目录生成 Windows 命名互斥量。同一运行目录内只允许一个 `ddns-go_ctl` 交互实例存在。

不同运行目录可以同时使用不同的 DDNS-GO 副本和配置，但相同的 `ddns-go.exe` 副本不应被两个控制脚本同时管理。

## 3. 主菜单功能

启动后显示交互式控制台菜单。每次进入菜单前都会按真实进程、端口和日志链路刷新状态。

| 操作 | 按键 | 说明 |
|---|---|---|
| 后台启动 | `1` / `B` | 隐藏 DDNS-GO 和日志转发器的控制台窗口 |
| 前台启动 | `2` / `F` | 创建独立控制台并保留 DDNS-GO 输出 |
| 停止 | `3` / `S` | 只停止已核验为本目录 `ddns-go.exe` 的受管进程 |
| 打开管理页面 | `4` / `W` | 使用默认浏览器打开当前端口 |
| 打开日志文件夹 | `5` / `L` | 在资源管理器中打开 `ctl-data/logs` |
| 查看日志 | `6` / `V` | 进入分页日志查看器 |
| 刷新状态 | `R` | 重新核验状态并修正无效状态文件 |
| 退出 | `Q` / `Esc` | 退出交互菜单 |
| 清除结果 | `D` | 清除上一条操作结果 |

菜单显示的按键由脚本顶部的 `MENU_KEY_MAPPINGS` 生成。修改该项后必须重新运行测试，并确认日志查看页的按键仍不冲突。

## 4. 启停与状态管理

### 4.1 启动

启动时执行以下流程：

1. 检查当前运行目录中是否存在 `ddns-go.exe`。
2. 检查是否已有受管进程、端口空闲状态或异常状态。
3. 独占创建本次日志文件。
4. 启动日志转发器。
5. 由转发器创建 DDNS-GO，并将其加入 Windows Job Object。
6. 等待转发器发布真实 DDNS-GO PID。
7. 在启动超时时间内确认目标 PID 正在监听配置端口。
8. 写入最新运行状态。

后台模式使用 `CREATE_NO_WINDOW`，不直接使用 DDNS-GO 的 `-d` 参数。原因如下：

- DDNS-GO 在 `-d` 模式下会先退出父进程，真实监听端口的是另一个 PID。
- 控制脚本通过 `Popen` 获得的 PID 会立即失效，启动确认可能误报失败。
- 未托管的新进程可能成为无法通过 Job Object 回收的孤儿进程。

前台模式使用 `CREATE_NEW_CONSOLE`，由日志转发器创建控制台。转发器同时读取 DDNS-GO 的标准输出和标准错误，并将输出原样写入控制台和日志。

### 4.2 停止

停止前会根据当前状态检查得到候选 PID，再执行以下安全措施：

1. 优先向该次启动独有的命名停止事件发送停止请求。
2. 等待日志转发器关闭 Job Object，通知整棵 DDNS-GO 进程树退出。
3. 若转发器未响应，则对已核验路径的 PID 逐个执行 `TerminateProcess`。
4. 等待进程退出，并将状态文件写为 `stopped`。

任何 PID 在终止前都会重新查询完整映像路径。路径不再匹配当前目录 `ddns-go.exe` 时拒绝停止，避免 PID 被复用后误杀其他程序。

### 4.3 状态判定

状态文件仅作为线索，不能单独作为健康依据。实际状态按以下信息合并：

- `ddns-go.exe` 是否存在。
- 状态文件中的 DDNS-GO PID 是否仍指向当前目录的 `ddns-go.exe`。
- 日志转发器 PID 是否仍指向当前控制执行器。
- 该次启动的命名停止事件是否存在。
- 当前日志文件是否存在。
- 目标端口是否由受管 PID 监听。
- 目标端口是否被其他程序占用。
- `netstat.exe` 查询是否成功。

常见状态包括：

| 状态 | 含义 |
|---|---|
| 未运行 | 无受管进程，且端口空闲，或状态文件已明确标记停止 |
| 运行中 | DDNS-GO PID、转发器、停止事件和日志文件全部匹配 |
| 未监听 | 受管 DDNS-GO 进程存在，但尚未或已不再监听目标端口 |
| 端口被占用 | 目标端口由无法匹配当前 `ddns-go.exe` 的进程监听 |
| 状态查询失败 | `netstat.exe` 不可用、超时或返回异常 |
| 状态文件无效 | 状态文件缺失必要字段、字段类型错误或 schema 不兼容 |
| 日志链路失效 | DDNS-GO 存在，但转发器、状态记录或日志文件不匹配 |
| 陈旧状态 | 状态文件中的运行信息已失效 |

状态刷新发现进程已经退出时，会把合法状态文件更新为 `stopped`；遇到损坏或字段不适配的状态文件时不会启动新进程，而是显示错误。

## 5. 运行时目录与文件

所有数据都相对于运行目录生成：

```text
ctl-data/
├── .ddns_go_config.yaml
├── run/
│   └── ddns-go_runtime.json
└── logs/
    └── ddns-go_YYYYMMDD-HHmmss[-NN].log
```

### 5.1 配置文件

默认配置路径为：

```text
ctl-data/.ddns_go_config.yaml
```

脚本将 `-c` 参数指向该文件，不负责生成或改写 DDNS-GO 配置内容。设置自定义配置路径后，相对路径也以运行目录为基准解析。

### 5.2 运行状态文件

`ddns-go_runtime.json` 使用固定 schema 版本保存：

- `schema_version`
- `launch_id`
- `status`
- `mode`
- `ddns_pid`
- `relay_pid`
- `started_at`
- `log_file`
- `exit_code`
- `error`

状态文件通过临时文件加 `os.replace` 原子替换。解析时严格检查字段集合、状态值、UUID、PID、ISO 8601 时间和日志文件名，避免把被篡改的路径或 PID 用于进程管理。

正常停止后状态文件仍保留，`status` 为 `stopped`，运行信息字段清空为 `null`。

### 5.3 日志文件

每次启动独占创建一份日志，文件名格式为：

```text
ddns-go_YYYYMMDD-HHmmss.log
```

同一秒再次启动时追加两位序号：

```text
ddns-go_YYYYMMDD-HHmmss-02.log
```

默认总保留数量为 15 份，当前正在写入的日志受保护并计入名额。即使手动指定保留 0 份，当前日志也不会被删除。清理失败会写回菜单作为警告，不会伪装成完全成功。

## 6. 日志查看器

在交互式控制台中，日志查看页使用标准库实现，每页默认显示 20 行，并每 0.5 秒轮询一次新增内容。

进入查看页后：

- 优先打开当前运行时日志。
- 当前日志不存在时回退到命名最新的历史日志。
- 日志目录为空时显示空占位，不退出菜单。

### 6.1 查看页按键

| 操作 | 按键 | 说明 |
|---|---|---|
| 返回 | `X` / `Esc` | 返回主菜单 |
| 打开日志文件夹 | `L` | 在资源管理器中打开日志目录 |
| 清理日志 | `C` | 输入保留份数并清理旧日志 |
| 清除结果 | `D` | 清除当前操作结果 |
| 上页 | `W` / `U` | 向前翻一页 |
| 下页 | `S` / `Space` | 向后翻一页 |
| 开头 | `H` / `Home` | 跳到日志开头 |
| 切换末尾跟随 | `E` / `End` | 开启或暂停自动跟随末尾 |
| 跳行 | `J` | 按行号跳转 |
| 跳页 | `K` | 在翻页模式中按页码跳转 |
| 切换模式 | `F` | 切换连续模式与翻页模式 |
| 刷新日志 | `R` | 手动读取新增内容 |

兼容键包括 `PageUp`、`PageDown`、`Home`、`End`。实际显示以脚本次运行时菜单为准，因为这些按键均可由 `LOG_VIEW_KEY_MAPPINGS` 配置。

### 6.2 连续模式与翻页模式

连续模式使用末尾一页窗口，跟随新日志时始终显示最后 20 行。用户向前翻页后会暂停跟随，只有重新开启末尾跟随时才恢复。

翻页模式按固定 20 行块分页，显示当前页号、总页数和页码跳转。切换到翻页模式时，如果原先处于末尾跟随，会自动对齐到目标模式的末页；否则保留当前阅读位置和暂停状态。

## 7. 配置项

脚本顶部使用 `DDNS_GO_PARAMS` 统一维护 DDNS-GO 启动参数。当前配置如下：

| 官方参数 | 当前值 | 自动透传 | 说明 |
|---|---|---|---|
| `-l` | `9876` | 是 | 管理页监听端口，实际传递为 `-l :9876` |
| `-c` | `None` | 是 | 自定义配置路径；默认使用 `ctl-data/.ddns_go_config.yaml` |
| `-f` | `None` | 是 | DNS 同步间隔秒数；不传时使用 DDNS-GO 默认值 |
| `-cacheTimes` | `None` | 是 | 间隔若干次后与服务商比对；不传时使用 DDNS-GO 默认值 |
| `-dns` | `None` | 是 | 自定义 DNS 服务器；不传时使用系统 DNS |
| `-noweb` | `False` | 否 | 不启动 Web 页面，与本工具的管理页功能冲突 |
| `-skipVerify` | `False` | 否 | 跳过证书验证，降低传输安全 |
| `-resetPassword` | `None` | 否 | 一次性密码重置命令，与托管启动冲突 |
| `-s` | `None` | 否 | DDNS-GO 服务安装或卸载，与本工具的进程托管冲突 |

修改参数时只修改 `DDNS_GO_PARAMS` 中对应行的“当前值”：

```python
("端口", 9876, True, "监听端口；启动时生成 -l :<端口>")
```

- 值填 `None` 表示不传递该参数，由 DDNS-GO 使用自身默认值。
- `PORT`、`CONFIG_PATH_OVERRIDE`、`SYNC_INTERVAL_SECONDS`、`CACHE_TIMES` 和 `DNS_SERVERS` 均从 `DDNS_GO_PARAMS` 自动派生，不要单独修改这些常量。
- 需要临时传递默认禁用的参数时，在 `DDNS_GO_EXTRA_ARGS` 中显式追加。
- `-l` 使用 `:<端口>` 形式，使 DDNS-GO 同时监听 IPv4 和 IPv6。

修改配置后必须运行完整测试，并核对管理页地址、状态判定和日志文件行为。

## 8. 源码结构与接口边界

源码保持单文件形态，但内部按职责分层，目的是让底层能力可替换、可测试，而不是提供对外扩展 API。

### 8.1 配置单一来源

可调参数集中存放在不可变的 `Settings` 数据类中，字段默认值来自 `DDNS_GO_PARAMS` 派生常量与脚本配置常量。DDNS-GO 启动命令由 `Settings.build_start_arguments()` 组装。

`DdnsController` 的 `port`、`start_timeout_seconds`、`stop_timeout_seconds`、`sync_interval_seconds`、`cache_times` 和 `dns_servers` 均为只读属性，一律委托给 `settings`，因此不存在第二份可被改写的状态。日常改配置仍然只改 `DDNS_GO_PARAMS` 与 `DDNS_GO_EXTRA_ARGS`，不要在调用处硬编码，也不要直接给这些属性赋值。

### 8.2 组件边界

进程操作、端口查询、运行状态存储、日志管理和子进程创建分别由 `IProcessOperations`、`IPortQuery`、`IRuntimeStateStore`、`ILogManager` 和 `IProcessSpawner` 声明边界；界面侧由 `IRenderContext`、`IConsoleOutput` 和 `IKeyReader` 声明。边界使用 `typing.Protocol` 表达，实现类只需结构匹配，不要求显式继承。

默认实现分别是 `Win32ProcessOperations`、`NetstatPortQuery`、`FileRuntimeStateStore`、`FileLogManager` 和 `SubprocessSpawner`。它们都只做转发，实际逻辑仍由模块级函数承担，因此模块级函数保持为唯一的默认实现入口。

### 8.3 依赖注入点

`DdnsController` 在原有位置参数之后提供 keyword-only 注入参数：`process_ops`、`port_query`、`state_store`、`log_manager` 和 `spawner`。省略时装配默认实现，行为与注入前完全一致；测试可传入替代实现来隔离 Win32 API、`netstat.exe` 和真实文件系统。

这些注入点属于内部结构约定，不作为对外稳定 API 承诺，后续版本可以调整。

### 8.4 行为不变量

接口隔离没有改变用户可见行为：菜单按键、启动参数、日志命名与保留规则、运行目录布局、`ctl-data/` 数据格式和安全边界均保持原样。任何触及上述行为的改动都应当视为功能变更，需要按版本规则升版本号。

## 9. 进程与安全边界

### 9.1 Windows Job Object

每次启动都会创建带有 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的 Job Object，并将 DDNS-GO 加入其中。转发器负责持续搬空 DDNS-GO 输出管道，避免子进程输出写满后阻塞。

日志转发器异常退出、停止事件关闭或 Job Object 句柄关闭时，DDNS-GO 进程树会被同步终止。

### 9.2 路径核验

脚本使用 Win32 API 查询进程完整映像路径，并在同一进程句柄上完成路径复核和终止操作。查询失败时不会仅凭 PID 执行停止。

### 9.3 端口占用

端口查询使用系统 `netstat.exe`，不简单假定 PID 可用。查询失败和“无监听进程”是两种不同状态，前者不会伪装为端口空闲。

目标端口被其他程序占用时，脚本拒绝启动，也不会停止占用者。

### 9.4 配置和密钥

配置文件、运行状态和日志只保存在所选运行目录的 `ctl-data/` 中。公开仓库不包含真实配置、DNS 密钥、API 令牌或实际运行时数据。

仓库中的 `ddns-go_ctl_py-run_abspath.bat_example.bak` 是带绝对路径的示例备份，不是推荐的运行配置。示例文件若保留在公开副本中，发布前应确认其中不包含实际环境路径。

DDNS-GO 本身的管理页面默认监听所有接口。即使使用本工具管理，也必须保持 DDNS-GO 设置中的“禁止从公网访问”开启。不要把整个运行目录放在公网可访问的共享位置。

### 9.5 不支持的运行方式

- 不支持非 Windows 系统。
- 不支持在本机使用 `-d` 参数由 DDNS-GO 自行脱离控制。
- 不支持在脚本未移动时通过仓库 `src/` 间接管理其他目录中的 `ddns-go.exe`。
- 不支持直接停止端口占用者或未通过路径核验的进程。
- 不保证兼容所有 DDNS-GO 未来版本。

## 10. 测试

源码仓库根目录运行：

```bat
python -m unittest discover -s tests -p "test_*.py" -v
```

测试使用标准库 `unittest`，不启动真实 DDNS-GO，也不触发动态 DNS 更新。`tests/` 下按 `test_*.py` 命名的是独立测试模块，统一由上述发现命令一次跑完：`test_ddns_go_ctl.py` 覆盖参数组装、路径规范化、运行状态读写、日志命名与清理、端口解析、状态核对、启停边界、菜单分发和日志查看状态；`test_interface_isolation.py` 覆盖接口协议一致性、`Settings` 单一来源和依赖注入装配。

发布说明辅助脚本的布局检查：

```powershell
./scripts/release-notes.ps1 -Check
```

发布 EXE 时追加校验清单检查：

```powershell
./scripts/checksums.ps1 -Verify _Release-Assets-Backups/v<version>_
```

## 11. 版本与兼容范围

- 支持系统：Windows x86_64。
- 源码方式：CPython 3.11 或更高版本。
- 源码方式仅使用 Python 标准库，不需要安装第三方 Python 包。
- 当前验证基线：Windows 11 内核 `10.0.26200`、AMD64；CPython `3.11.9` 与 `3.13.13` 均实测通过。
- DDNS-GO 版本：不绑定具体版本，按官方启动参数透传；原则上可尝试其他版本，但不保证全部兼容。
- 定版实测：`v6.17.7`（2026-09-08）后台启动、端口监听、状态落盘、停止归零与日志转发正常。
- 发布版 `ddns-go_ctl.exe` 不包含 DDNS-GO，也不代替用户下载和更新官方程序。
