"""在 Windows 上以交互菜单管理同目录中的便携版 DDNS-GO。

脚本完全使用 Python 标准库，不导入或探测任何第三方 Python 包。

版本与更新时间：
- 脚本版本：v2.7.1
  更新时间：2026-08-24-Sun
- 脚本定版实测：
  DDNS-GO 版本：v6.17.5
  兼容说明：脚本不绑定 DDNS-GO 具体版本，原则上可用于其他版本，但不保证全部兼容。

功能：
- 支持前后台启动、停止、打开管理页面、打开日志文件夹；启停前均核验 ddns-go.exe
  完整路径。
- 支持 V 进入分页日志查看：分页浏览全文并实时跟随，X 或 Esc 返回菜单；无当前
  日志时自动回退到最新一份历史日志。
- 前后台输出均由独立转发器完整写入日志，前台保留原控制台显示。
- 每次启动生成一份日志，默认保留最近 15 份，更旧的非当前日志自动清理。
- 使用 Windows Job Object 托管 DDNS-GO 进程树；停止、日志失效或转发器异常时同步结束 DDNS-GO。

安全原则：
- 运行状态文件只作为线索，启停前均核对进程的 EXE 完整路径。
- 管理页绑定全接口，安全依赖 DDNS-GO 网页设置中默认勾选的“禁止从公网访问”，
  请勿在公网可达的环境取消该选项。
- 端口被其他程序占用时拒绝启动，也不会停止占用者。
- 端口查询失败与“没有监听进程”严格区分。
- 运行状态文件使用临时文件原子替换。
- 刷新状态时会按实际进程核对并修正运行状态 JSON。
- 正常停止后保留运行状态 JSON，status 为 stopped，仅保留字段名、信息值清空。
- 后台启动由本脚本自行隐藏窗口，不使用 DDNS-GO 的 -d 参数（原因见 start()）。

版本界定：
- 受支持基线：Windows x86_64、CPython 3.11.x。
- 现行验证（2026-07-22）：Windows 内核 10.0.26200 AMD64、CPython 3.11.15。
- CPython 3.8 仅通过语法兼容检查，不属于已验证运行时支持范围。
- 脚本不绑定 DDNS-GO 具体版本，按官方启动参数透传，原则上可用于其他版本，
  但不保证全部兼容。
- 验证覆盖参数和进程管理边界，不在自动测试中触发动态 DNS 更新。
- 补充验证（2026-07-26）：DDNS-GO 以 -l :PORT 启动会同时监听 0.0.0.0 与 [::]，
  两条记录同属一个 PID；其 -d 参数为 fork 后父进程立即退出，不可使用。

功能边界：
- 官方 README 全部启动参数统一在 DDNS_GO_PARAMS 中列出；-l、-c、-f、-cacheTimes、
  -dns 自动透传，-noweb、-skipVerify、-resetPassword、-s install/uninstall 默认
  不自动透传，确需透传可在 DDNS_GO_EXTRA_ARGS 中显式追加。

目录布局：
- ctl-data/.ddns_go_config.yaml：配置文件。
- ctl-data/run/ddns-go_runtime.json：运行状态文件。
- ctl-data/logs/ddns-go_YYYYMMDD-HHmmss.log：按启动划分的日志。
配套测试：test_ddns_go_ctl.py（标准库 unittest，不启动 DDNS-GO 本体）。
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, fields, replace
from datetime import datetime
import hashlib
import json
import locale
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import threading
import time
from typing import (
    BinaryIO,
    Callable,
    Iterable,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    runtime_checkable,
)
import unicodedata
import uuid
import webbrowser

try:
    import msvcrt
except ImportError:  # 仅 Windows 提供；非 Windows 由 main() 提前拦截。
    msvcrt = None


# ===== 启动参数配置 =====
# 统一列出 DDNS-GO 官方 README 的全部启动参数，每行格式：
#   (官方参数, 当前值, 是否支持, 中文说明)
# 一行里的三个逗号把位置分成 4 段：
#   ("-l", 9876, True, "监听端口；启动时生成 -l :<端口>")
#     ^     ^     ^     ^
#     1     2     3     4
#   1 = 官方参数名
#   2 = 当前值
#   3 = 是否支持（True = 自动透传，False = 不自动透传）
#   4 = 中文说明
# 支持项：脚本会自动组装进 DDNS-GO 启动命令。
# 不支持项：仅作保留说明，不自动透传；如确需透传，请在 DDNS_GO_EXTRA_ARGS 追加。
# 值填 None 表示不传该参数，沿用 DDNS-GO 默认。
# 手改步骤：直接改对应参数行的第 2 个值，其余字段保留。
#   ("-l", 9876, True, ...)             -> ("-l", 9877, True, ...)             端口改成 9877
#   ("-f", None, True, ...)             -> ("-f", 10, True, ...)               每 10 秒同步
#   ("-c", None, True, ...)             -> ("-c", "custom.yaml", True, ...)    指定配置路径
#   ("-dns", None, True, ...)           -> ("-dns", "223.5.5.5", True, ...)    指定 DNS
DDNS_GO_PARAMS = (
    ("-l", 9876, True, "监听端口；启动时生成 -l :<端口>"),
    ("-c", None, True, "配置文件路径；None 表示固定 ctl-data/.ddns_go_config.yaml"),
    ("-f", None, True, "同步间隔（秒）；None 表示不传，沿用默认 300"),
    ("-cacheTimes", None, True, "间隔 N 次与服务商比对；None 表示不传，沿用默认 5"),
    ("-dns", None, True, "自定义 DNS；None 表示不传，沿用系统 DNS"),
    ("-noweb", False, False, "不启动 Web；与网页管理功能冲突"),
    ("-skipVerify", False, False, "跳过证书验证；降低传输安全"),
    ("-resetPassword", None, False, "重置密码；一次性命令，与托管启动冲突"),
    ("-s", None, False, "服务安装/卸载；与脚本自带进程托管冲突"),
)

# 需要透传“不支持”参数时，在这里按 ("-参数名", "值") 或 ("-参数名",) 追加。
# 例：DDNS_GO_EXTRA_ARGS = (("-noweb",), ("-skipVerify",))
DDNS_GO_EXTRA_ARGS = ()

# 由启动参数配置派生，代码内部统一读取；日常手改请修改 DDNS_GO_PARAMS。
DDNS_GO_PARAM_VALUES = {
    flag: value for flag, value, _supported, _note in DDNS_GO_PARAMS
}
# 对应官方参数 -l：DDNS-GO 管理页监听端口。
PORT = DDNS_GO_PARAM_VALUES["-l"]
# 对应官方参数 -c：自定义配置文件路径；None 表示默认 ctl-data 路径。
CONFIG_PATH_OVERRIDE = DDNS_GO_PARAM_VALUES["-c"]
# 对应官方参数 -f：同步间隔（秒）。
SYNC_INTERVAL_SECONDS = DDNS_GO_PARAM_VALUES["-f"]
# 对应官方参数 -cacheTimes：间隔 N 次与服务商比对。
CACHE_TIMES = DDNS_GO_PARAM_VALUES["-cacheTimes"]
# 对应官方参数 -dns：自定义 DNS 服务器。
DNS_SERVERS = DDNS_GO_PARAM_VALUES["-dns"]

# ===== 脚本自身与日志查看配置 =====
# 启动端口确认超时（秒）。
START_TIMEOUT_SECONDS = 10
# 停止等待超时（秒）。
STOP_TIMEOUT_SECONDS = 5
# netstat 端口监听查询超时（秒）。
LISTENER_QUERY_TIMEOUT_SECONDS = 2
# 每次启动保留最近 N 份日志，更旧的非当前日志自动删除。
LOG_KEEP_COUNT = 15
# 日志查看页固定每页显示的行数；连续模式显示末尾行区间，翻页模式按固定页分页。
LOG_VIEW_PAGE_LINES = 20
# 日志查看页轮询新日志的间隔（秒）。
LOG_VIEW_POLL_SECONDS = 0.5
# 查看页模式名：连续模式显示末尾行区间，翻页模式按固定页块分页。
LOG_VIEW_MODE_FOLLOW = "follow"
LOG_VIEW_MODE_PAGING = "paging"
# 查看页统一无效按键提示；跳行/跳页输入与主循环共用同一文案与渲染路径。
LOG_VIEW_INVALID_KEY_MESSAGE = "提示：无效按键"
# 跳行/跳页输入只接受数字；无效按键时用整帧状态提示代替内联打印。
LOG_VIEW_DIGITS_ONLY_MESSAGE = "提示：仅数字键有效"
# 跳行/跳页输入进行中的常驻操作结果，直接包含“仅数字键有效”说明。
LOG_VIEW_JUMPING_MESSAGE = "提示：跳行输入中，仅数字键有效"
LOG_VIEW_JUMP_PAGE_MESSAGE = "提示：跳页输入中，仅数字键有效"
# 脚本自身版本，与 README.md 保持一致。
SCRIPT_VERSION = "2.7.1"

# ===== 按键配置 =====
# 一级菜单与二级菜单统一格式：
#   (动作名, 可见按键, 隐藏兼容键, 含义单词, 中文说明)
# 含义单词为“无”的按键属于本地约定键，没有具体英文单词含义。
# 每个动作最多配置 2 个按键，用 / 分隔；删改键位只需改这里，菜单自动同步。
MENU_KEY_MAPPINGS = (
    ("background", "1/B", "", "Background", "后台启动（隐藏 EXE 窗口）"),
    ("foreground", "2/F", "", "Foreground", "前台启动（保留 EXE 窗口）"),
    ("stop", "3/S", "", "Stop", "停止"),
    ("management_page", "4/W", "", "Web", "打开管理页面"),
    ("log_directory", "5/L", "", "Log", "打开日志文件夹"),
    ("view_log", "6/V", "", "View", "查看运行时日志"),
    ("refresh", "R", "", "Refresh", "刷新状态"),
    ("quit", "Q/Esc", "", "Quit / Escape", "退出"),
    ("clear_result", "D", "", "Dismiss", "清除操作结果"),
)

# 二级菜单：(动作名, 可见按键（页脚主键）, 隐藏兼容键, 含义单词, 中文说明)
# 页脚只显示“可见按键”，每个动作最多 2 个；“隐藏兼容键”仍可输入但不展示。
# 键名用 / 分隔；Space 代表空格键；隐藏兼容键可为空 ""。
# 一行里的四个逗号把位置分成 5 段：
#   ("next", "S/Space", "D/PageDown", "无 / Next", "下页")
#     ^       ^          ^            ^            ^
#     1       2          3            4            5
#   1 = 动作名
#   2 = 页脚主键
#   3 = 隐藏兼容键
#   4 = 含义单词
#   5 = 中文说明
# 手改步骤：
#   改“页脚主键”：改第 2 个值，用 / 分隔，最多 2 个。
#   改“隐藏兼容键”：改第 3 个值，仍可输入但不展示；不需要就留 ""。
#   例：下页可见主键改成 S/Space，原 D/PageDown 继续作为隐藏兼容键：
#     ("next", "S/Space", "D/PageDown", ...)
LOG_VIEW_KEY_MAPPINGS = (
    ("return", "X/Esc", "", "Exit / Escape", "返回"),
    ("log_directory", "L", "", "Log", "打开日志文件夹"),
    ("clean_logs", "C", "", "Clean", "清理日志"),
    ("clear_result", "D", "", "Dismiss", "清除操作结果"),
    ("prev", "W/U", "PageUp", "无 / Up", "上页"),
    ("next", "S/Space", "PageDown", "无 / Next", "下页"),
    ("home", "H/Home", "", "Home / Home", "开头"),
    ("toggle_follow", "E/End", "", "End / End", "切换末尾跟随"),
    ("jump", "J", "", "Jump", "跳行"),
    ("jump_page", "K", "", "无", "跳页"),
    ("toggle_mode", "F", "", "无", "切换模式"),
    ("refresh_log", "R", "", "Refresh", "刷新日志内容"),
)

# 查看页横幅固定显示的动作；顺序即横幅显示顺序，页脚自动排除这些动作。
LOG_VIEW_BANNER_ACTIONS = (
    "return",
    "log_directory",
    "clean_logs",
    "clear_result",
)
# 横幅弱化显示的动作；只保留最暗灰色，并排在普通暗色提示之后。
LOG_VIEW_DIM_BANNER_ACTIONS = ("clear_result",)


def _normalized_action_key(key: str) -> str:
    """归一化动作表按键；Esc 统一为 escape。"""

    normalized = key.casefold()
    return "escape" if normalized == "esc" else normalized


# 一级菜单按键到动作名的映射，由 MENU_KEY_MAPPINGS 自动派生。
MENU_ACTIONS = {
    _normalized_action_key(key): action
    for action, visible_keys, hidden_keys, _word, _desc in MENU_KEY_MAPPINGS
    for key in f"{visible_keys}/{hidden_keys}".split("/")
    if key
}
# 一级菜单渲染项，由 MENU_KEY_MAPPINGS 自动派生。
MENU_ITEMS = tuple(
    (action, visible_keys, desc)
    for action, visible_keys, _hidden, _word, desc in MENU_KEY_MAPPINGS
)
# 一级菜单灰色显示的动作；排在普通按键提示之后。
MENU_GRAY_ACTIONS = ("clear_result",)
# 一级菜单统一无效按键提示；按下有效按键后由对应操作结果覆盖。
MENU_INVALID_KEY_MESSAGE = "提示：无效按键"
# 二级菜单按键到动作名的映射，由 LOG_VIEW_KEY_MAPPINGS 自动派生。
LOG_VIEW_KEY_ACTIONS = {
    _normalized_action_key(key): action
    for action, visible_keys, hidden_keys, _word, _desc in LOG_VIEW_KEY_MAPPINGS
    for key in f"{visible_keys}/{hidden_keys}".split("/")
    if key
}
# 空格键实际输入为 " "，与配置里的 Space 显示名对齐。
LOG_VIEW_KEY_ACTIONS[" "] = LOG_VIEW_KEY_ACTIONS.get("space")

# DDNS-GO 可执行文件名。
EXECUTABLE_NAME = "ddns-go.exe"
# DDNS-GO 配置文件名（对应官方参数 -c 的默认目标）。
CONFIG_NAME = ".ddns_go_config.yaml"
# 运行时数据根目录名。
DATA_DIRECTORY_NAME = "ctl-data"
# 运行状态子目录名。
RUNTIME_STATE_DIRECTORY_NAME = "run"
# 日志子目录名。
LOG_DIRECTORY_NAME = "logs"
# 运行状态文件名。
RUNTIME_STATE_NAME = "ddns-go_runtime.json"
# 运行状态文件结构版本。
RUNTIME_SCHEMA_VERSION = 1
# 转发器模式私有开关参数，避免与 DDNS-GO 参数冲突。
RELAY_SWITCH = "--ddns-go-relay"
# PyInstaller onefile 模式下需要重置的环境变量名。
PYINSTALLER_RESET_ENVIRONMENT_NAME = "PYINSTALLER_RESET_ENVIRONMENT"

# 冻结 EXE 与源码模式的关键差异只探测一次，避免各处重复判断。
IS_FROZEN = bool(getattr(sys, "frozen", False))

# DdnsState.code 的稳定取值；状态查询与启停判断共用，避免字符串拼写漂移。
STATE_CODE_MISSING_EXECUTABLE = "MissingExecutable"  # ddns-go.exe 不存在
STATE_CODE_RUNNING = "Running"  # 进程存在且端口监听正常
STATE_CODE_PROCESS_NOT_LISTENING = "ProcessNotListening"  # 进程在但未监听 PORT
STATE_CODE_LOGGING_FAILED = "LoggingFailed"  # 日志文件或转发器写入失败
STATE_CODE_RELAY_ONLY = "RelayOnly"  # 只有转发器，DDNS-GO 子进程未确认
STATE_CODE_STALE_RUNTIME = "StaleRuntime"  # 状态文件记录的进程已退出
STATE_CODE_PORT_OCCUPIED = "PortOccupied"  # PORT 被其他进程占用
STATE_CODE_QUERY_FAILED = "QueryFailed"  # 端口监听查询失败
STATE_CODE_INVALID_RUNTIME = "InvalidRuntime"  # 运行状态文件无效
STATE_CODE_STOPPED = "Stopped"  # 已停止

# Win32 进程访问权限与同步标志。
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
INFINITE = 0xFFFFFFFF
# Win32 错误码、事件权限与终端控制标志。
ERROR_INVALID_PARAMETER = 87
ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
STD_OUTPUT_HANDLE = -11
# Job Object 限制信息类及随作业结束终止子进程的标志。
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

# subprocess 控制台创建标志；低版本 Python 缺失时回退到固定常量。
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

# netstat 监听行解析正则：仅认 TCP LISTENING 且端口、PID 完整匹配。
LISTENER_PATTERN = re.compile(
    r"^\s*TCP\s+\S+:(?P<port>\d+)\s+\S+\s+LISTENING\s+(?P<pid>\d+)\s*$",
    re.IGNORECASE,
)
# 同秒重复启动时 reserve_log_file 从 -02 起追加序号，-00/-01 永不产生；
# 正则与之对齐，不允许 -00/-01 形态，避免出现“规范允许但实际不存在”的偏差。
LOG_FILE_PATTERN = re.compile(
    r"^ddns-go_\d{8}-\d{6}(?:-(?!0[01])\d{2})?\.log$",
    re.IGNORECASE,
)


# ===== 接口定义（typing.Protocol + Settings）=====
# 组件边界用 Protocol 声明，具体实现只需结构匹配（鸭子类型）。
# 实现类一律继续调用本模块的模块级函数/全局，使测试的 mock.patch 拦截路径不变。

@dataclass(frozen=True)
class Settings:
    """控制器配置聚合：DDNS-GO 启动参数组装与可调参数的唯一来源。

    字段默认来自模块顶部的 DDNS_GO_PARAMS 派生常量与脚本配置常量；
    手动改配置请改 DDNS_GO_PARAMS / 对应常量，不要在调用处硬编码。
    """

    port: int = PORT
    start_timeout_seconds: int = START_TIMEOUT_SECONDS
    stop_timeout_seconds: int = STOP_TIMEOUT_SECONDS
    sync_interval_seconds: Optional[int] = SYNC_INTERVAL_SECONDS
    cache_times: Optional[int] = CACHE_TIMES
    dns_servers: Optional[str] = DNS_SERVERS
    config_path_override: Optional[str] = CONFIG_PATH_OVERRIDE

    def build_start_arguments(
        self, executable_path: Path, config_path: Path
    ) -> List[str]:
        """组装 DDNS-GO 启动参数；配置为 None 的项一律不传，沿用其自身默认值。"""

        arguments = [
            str(executable_path),
            "-l",
            f":{self.port}",
            "-c",
            str(config_path),
        ]
        if self.sync_interval_seconds is not None:
            arguments += ["-f", str(self.sync_interval_seconds)]
        if self.cache_times is not None:
            arguments += ["-cacheTimes", str(self.cache_times)]
        if self.dns_servers is not None:
            arguments += ["-dns", self.dns_servers]
        for extra in DDNS_GO_EXTRA_ARGS:
            if isinstance(extra, str):
                arguments.append(extra)
            else:
                flag, *values = extra
                arguments.append(flag)
                arguments.extend(str(value) for value in values)
        return arguments


@runtime_checkable
class IProcessOperations(Protocol):
    """Win32 原始进程操作；句柄由调用方负责关闭。"""

    def open_process(self, access: int, process_id: int): ...
    def query_process_path(self, handle) -> Path: ...
    def terminate_process(self, handle, exit_code: int = 1) -> bool: ...
    def wait_for_single_object(self, handle, timeout_ms: int) -> int: ...
    def close_handle(self, handle) -> None: ...


@runtime_checkable
class IPortQuery(Protocol):
    """端口监听查询；返回监听指定端口的 PID 列表。"""

    def query(self, port: int) -> List[int]: ...


@runtime_checkable
class IRuntimeStateStore(Protocol):
    """运行状态文件的读写。"""

    def read(self) -> Tuple[bool, Optional[RuntimeState]]: ...
    def write(self, state: RuntimeState) -> None: ...


@runtime_checkable
class ILogManager(Protocol):
    """日志文件预留、清理、列出与最新定位。"""

    def reserve(self, started_at: Optional[datetime] = None) -> Path: ...
    def prune(
        self,
        keep_count: int,
        current_path: Optional[Path] = None,
        protected_paths: Iterable[Path] = (),
    ) -> List[Path]: ...
    def list_log_files(self) -> List[Path]: ...
    def latest(self) -> Optional[Path]: ...


@runtime_checkable
class IProcessSpawner(Protocol):
    """创建子进程（日志转发器）；返回 Popen 句柄。"""

    def spawn(self, command: Sequence[str], **options) -> subprocess.Popen: ...


@runtime_checkable
class IRenderContext(Protocol):
    """主菜单整帧渲染所需的最小控制器视图；实现只需结构满足。

    仅声明 render 实际用到的字段与格式化方法，避免 UI 接口反向依赖
    具体 DdnsController，渲染层与业务实现解耦。
    """

    port: int
    script_directory: Path
    log_directory: Path
    config_path: Path

    def display_path(self, path: Path) -> str: ...


@runtime_checkable
class IConsoleOutput(Protocol):
    """控制台输出能力（着色、整帧渲染、提示、回显）。"""

    def colorize(self, text: str, color: str) -> str: ...
    def render(
        self,
        state: DdnsState,
        controller: IRenderContext,
        last_result: Optional[ActionResult],
    ) -> None: ...
    def prompt(self, text: str) -> None: ...
    def echo(self, text: str) -> None: ...


@runtime_checkable
class IKeyReader(Protocol):
    """单键与数字输入能力。"""

    def read_key(self, prompt_text: str = "请选择：") -> str: ...
    def read_line_number(
        self,
        prompt_text: str = "跳转到行号: ",
        on_invalid: Optional[Callable[[], None]] = None,
    ) -> Optional[int]: ...


class IoCounters(ctypes.Structure):
    """JOB_OBJECT_EXTENDED_LIMIT_INFORMATION 所需的 Win32 I/O 计数结构。"""

    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JobObjectBasicLimitInformation(ctypes.Structure):
    """Job Object 基本限制信息的 ctypes 布局。"""

    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JobObjectExtendedLimitInformation(ctypes.Structure):
    """Job Object 扩展限制信息；LimitFlags 位于内部 BasicLimitInformation。"""

    _fields_ = [
        ("BasicLimitInformation", JobObjectBasicLimitInformation),
        ("IoInfo", IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


# 标准库没有直接查询任意 Windows 进程完整路径的接口，因此在此声明所需的
# 最小 Win32 API 集合。停止操作会在同一个句柄上完成身份复核与终止，避免
# “查询路径后 PID 被复用”造成误杀。
if os.name == "nt":
    KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)

    KERNEL32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    KERNEL32.OpenProcess.restype = wintypes.HANDLE

    KERNEL32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    KERNEL32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    KERNEL32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    KERNEL32.TerminateProcess.restype = wintypes.BOOL

    KERNEL32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    KERNEL32.WaitForSingleObject.restype = wintypes.DWORD

    KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    KERNEL32.CloseHandle.restype = wintypes.BOOL

    KERNEL32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    KERNEL32.CreateMutexW.restype = wintypes.HANDLE
    KERNEL32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    KERNEL32.ReleaseMutex.restype = wintypes.BOOL

    KERNEL32.CreateEventW.argtypes = [
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    KERNEL32.CreateEventW.restype = wintypes.HANDLE
    KERNEL32.OpenEventW.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    KERNEL32.OpenEventW.restype = wintypes.HANDLE
    KERNEL32.SetEvent.argtypes = [wintypes.HANDLE]
    KERNEL32.SetEvent.restype = wintypes.BOOL

    KERNEL32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    KERNEL32.CreateJobObjectW.restype = wintypes.HANDLE
    KERNEL32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    KERNEL32.SetInformationJobObject.restype = wintypes.BOOL
    KERNEL32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    KERNEL32.AssignProcessToJobObject.restype = wintypes.BOOL

    KERNEL32.GetStdHandle.argtypes = [wintypes.DWORD]
    KERNEL32.GetStdHandle.restype = wintypes.HANDLE
    KERNEL32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    KERNEL32.GetConsoleMode.restype = wintypes.BOOL
    KERNEL32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    KERNEL32.SetConsoleMode.restype = wintypes.BOOL
else:
    KERNEL32 = None


# 进程、状态与启停核心

class DdnsError(RuntimeError):
    """可直接展示给用户的控制器错误。"""


def _require_kernel32() -> None:
    """Win32 能力仅 Windows 提供；非 Windows 由 main() 提前拦截。"""

    if KERNEL32 is None:
        raise DdnsError("该脚本仅支持 Windows。")


def _win32_error(action: str, error_code: Optional[int] = None) -> DdnsError:
    """构造与现有界面文案一致的可展示 Win32 错误。"""

    error_code = ctypes.get_last_error() if error_code is None else error_code
    return DdnsError(f"{action}：{ctypes.FormatError(error_code)}")


def _win32_os_error() -> OSError:
    """构造保留错误码与系统描述的 OSError，供进程查询边界抛出。"""

    error_code = ctypes.get_last_error()
    return OSError(error_code, ctypes.FormatError(error_code))


# ===== 基础设施实现（结构满足上方 Protocol，mock 仍可拦截模块级函数）=====

class Win32ProcessOperations:
    """kernel32 原始进程操作的直接封装。

    行为与原 DdnsController 内联实现一致；open_process 对已退出 PID 返回 None，
    terminate_process 失败返回 False（由调用方按 PID 构造错误文案）。
    """

    def open_process(self, access: int, process_id: int):
        """按权限打开进程句柄；进程不存在时返回 None。"""

        _require_kernel32()

        handle = KERNEL32.OpenProcess(access, False, process_id)
        if handle:
            return handle

        # OpenProcess 对已经退出或不存在的 PID 返回 ERROR_INVALID_PARAMETER；
        # 这是正常竞态，不应当显示成查询错误。
        if ctypes.get_last_error() == ERROR_INVALID_PARAMETER:
            return None
        raise _win32_os_error()

    def query_process_path(self, handle) -> Path:
        """通过进程句柄查询完整映像路径。"""

        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not KERNEL32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        ):
            raise _win32_os_error()
        return Path(buffer.value)

    def terminate_process(self, handle, exit_code: int = 1) -> bool:
        """终止进程；返回 Win32 调用结果，失败文案由调用方按 PID 构造。"""

        return bool(KERNEL32.TerminateProcess(handle, exit_code))

    def wait_for_single_object(self, handle, timeout_ms: int) -> int:
        """等待句柄信号或超时，返回 Win32 等待结果。"""

        return int(KERNEL32.WaitForSingleObject(handle, timeout_ms))

    def close_handle(self, handle) -> None:
        """关闭句柄。"""

        KERNEL32.CloseHandle(handle)


class NetstatPortQuery:
    """netstat.exe TCP 监听查询；查询失败与无监听严格区分。"""

    def query(self, port: int) -> List[int]:
        """查询监听 PID；命令失败时抛错，绝不伪造为空监听结果。"""

        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        netstat_path = system_root / "System32" / "netstat.exe"
        if not netstat_path.is_file():
            raise DdnsError("系统组件 netstat.exe 不可用。")

        # 不加 -p tcp：该过滤器只返回 IPv4 表，纯 IPv6 的监听者会整个消失，
        # 使端口占用检查失真。全表中的 UDP 与非监听行由 LISTENER_PATTERN 排除，
        # 同一进程的双栈两行由下方的集合去重。
        try:
            completed = subprocess.run(
                [str(netstat_path), "-ano"],
                check=False,
                capture_output=True,
                text=True,
                encoding=locale.getpreferredencoding(False),
                errors="replace",
                creationflags=CREATE_NO_WINDOW,
                timeout=LISTENER_QUERY_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise DdnsError(
                f"netstat.exe 查询超过 {LISTENER_QUERY_TIMEOUT_SECONDS} 秒。"
            ) from exc
        except OSError as exc:
            raise DdnsError(f"netstat.exe 查询失败：{exc}") from exc

        if completed.returncode != 0:
            raise DdnsError(f"netstat.exe 查询失败，退出码：{completed.returncode}。")

        return parse_listener_process_ids(completed.stdout.splitlines(), port)


class FileRuntimeStateStore:
    """绑定路径的运行状态文件读写；委托模块级原子写函数。"""

    def __init__(self, runtime_state_path: Path) -> None:
        """绑定运行状态文件路径。"""

        self.runtime_state_path = runtime_state_path

    def read(self) -> Tuple[bool, Optional[RuntimeState]]:
        """读取并校验运行状态；不存在返回 (False, None)，损坏则明确报错。"""

        return read_runtime_state(self.runtime_state_path)

    def write(self, state: RuntimeState) -> None:
        """原子替换写入运行状态。"""

        write_runtime_state(self.runtime_state_path, state)


class FileLogManager:
    """绑定日志目录的日志管理；委托模块级日志函数。"""

    def __init__(self, log_directory: Path) -> None:
        """绑定日志目录。"""

        self.log_directory = log_directory

    def reserve(self, started_at: Optional[datetime] = None) -> Path:
        """独占预留本次日志名。"""

        return reserve_log_file(self.log_directory, started_at)

    def prune(
        self,
        keep_count: int,
        current_path: Optional[Path] = None,
        protected_paths: Iterable[Path] = (),
    ) -> List[Path]:
        """按时间顺序清理旧日志，返回清理失败路径。"""

        return prune_log_files(
            self.log_directory, keep_count, current_path, protected_paths
        )

    def list_log_files(self) -> List[Path]:
        """列出规范命名的日志文件。"""

        return _list_log_files(self.log_directory)

    def latest(self) -> Optional[Path]:
        """返回命名最新的一份日志。"""

        return latest_log_file(self.log_directory)


class SubprocessSpawner:
    """subprocess.Popen 封装；透传调用方全部关键字参数。"""

    def spawn(self, command: Sequence[str], **options) -> subprocess.Popen:
        """按给定命令与选项创建子进程。"""

        return subprocess.Popen(command, **options)


class SingleInstanceMutex:
    """用按脚本目录区分的 Windows 命名互斥量串行化管理操作。"""

    def __init__(self, script_directory: Path) -> None:
        """按脚本目录派生互斥量名并初始化句柄槽位。"""

        path_digest = _script_directory_digest(script_directory)
        self.name = f"Local\\ddns-go_ctl_{path_digest}"
        self.handle = None

    def __enter__(self) -> "SingleInstanceMutex":
        """创建互斥量；若已有同目录实例则抛错。"""

        _require_kernel32()

        handle = KERNEL32.CreateMutexW(None, True, self.name)
        if not handle:
            raise _win32_error("无法创建单实例互斥量")

        # CreateMutexW 对已存在的同名互斥量返回 ERROR_ALREADY_EXISTS，
        # 用它识别同目录的另一个管理器实例。
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            KERNEL32.CloseHandle(handle)
            raise DdnsError("同目录的另一个 DDNS-GO 管理器已经在运行。")

        self.handle = handle
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        """释放并关闭互斥量句柄。"""

        if self.handle is not None:
            KERNEL32.ReleaseMutex(self.handle)
            KERNEL32.CloseHandle(self.handle)
            self.handle = None


@dataclass(frozen=True)
class DdnsState:
    """统一状态；target_process_ids 仅包含已核验路径、允许停止的 PID。"""

    code: str
    title: str
    detail: str
    color: str
    executable_path: Path
    process_id: Optional[int] = None
    relay_process_id: Optional[int] = None
    log_path: Optional[Path] = None
    launch_id: Optional[str] = None
    target_process_ids: Tuple[int, ...] = ()


@dataclass(frozen=True)
class ActionResult:
    """菜单动作的结果。

    颜色由动作本身给出，界面不再靠匹配提示文案来推断成败，改文案不会影响显示。
    detail_lines 提供结构化分项；为空时仍按单行文本显示。
    """

    text: str
    color: str = "green"
    detail_lines: Tuple[Tuple[str, str], ...] = ()


def _optional_positive_int(name: str, value: object) -> None:
    """校验运行状态字段必须为 null 或正整数；不合法时抛 ValueError。"""

    if value is not None and (type(value) is not int or value <= 0):
        raise ValueError(f"{name} 必须是正整数或 null")


@dataclass(frozen=True)
class RuntimeState:
    """`ctl-data/run/ddns-go_runtime.json` 的固定版本 1 数据模型。

    `stopped` 状态只保留字段名，信息值全部为 `null`。
    """

    schema_version: int
    launch_id: Optional[str]
    status: str
    mode: Optional[str]
    ddns_pid: Optional[int]
    relay_pid: Optional[int]
    started_at: Optional[str]
    log_file: Optional[str]
    exit_code: Optional[int] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        """按 schema 字段声明顺序导出可序列化字典。"""

        # dataclass 声明顺序即 schema 字段顺序，字段派生避免手写清单漂移。
        return {name: getattr(self, name) for name in RUNTIME_STATE_FIELDS}

    @classmethod
    def from_dict(cls, value: object) -> "RuntimeState":
        """严格校验状态文件，避免把被篡改的路径或 PID 用于进程管理。"""

        if not isinstance(value, dict):
            raise ValueError("顶层必须是 JSON 对象")

        # 字段集合必须完全一致：旧版本缺字段与未知字段混入都视为无效。
        required_names = set(RUNTIME_STATE_FIELDS)
        if set(value) != required_names:
            raise ValueError("字段集合与运行状态版本不匹配")
        if value["schema_version"] != RUNTIME_SCHEMA_VERSION:
            raise ValueError(f"不支持的 schema_version：{value['schema_version']!r}")

        status = value["status"]
        if status not in {"starting", "running", "failed", "stopped"}:
            raise ValueError("status 必须是 starting、running、failed 或 stopped")

        launch_id = value["launch_id"]
        mode = value["mode"]
        if status == "stopped":
            # stopped 只保留元字段，运行信息全部清空，因此直接构造而非逐个校验。
            if any(
                value[name] is not None
                for name in RUNTIME_STATE_VALUE_FIELDS
            ):
                raise ValueError("stopped 状态只保留字段名，信息值必须为 null")
            return cls(
                schema_version=RUNTIME_SCHEMA_VERSION,
                status=status,
                **{name: None for name in RUNTIME_STATE_VALUE_FIELDS},
            )

        if not isinstance(launch_id, str):
            raise ValueError("launch_id 必须是字符串")
        try:
            uuid.UUID(launch_id)
        except (ValueError, AttributeError) as exc:
            raise ValueError("launch_id 不是合法 UUID") from exc

        if mode not in {"foreground", "background"}:
            raise ValueError("mode 必须是 foreground 或 background")

        ddns_pid = value["ddns_pid"]
        _optional_positive_int("ddns_pid", ddns_pid)
        relay_pid = value["relay_pid"]
        _optional_positive_int("relay_pid", relay_pid)
        # 非 stopped 必须有转发器 PID，否则无法核验日志链路真实存在。
        if relay_pid is None:
            raise ValueError("非 stopped 状态必须包含 relay_pid")

        started_at = value["started_at"]
        if not isinstance(started_at, str):
            raise ValueError("started_at 必须是字符串")
        try:
            parsed_started_at = datetime.fromisoformat(started_at)
        except ValueError as exc:
            raise ValueError("started_at 不是合法 ISO 8601 时间") from exc
        # 要求带时区偏移，避免无偏移时间在本地/UTC 语义间被误读。
        if parsed_started_at.tzinfo is None:
            raise ValueError("started_at 必须包含时区偏移")

        log_file = value["log_file"]
        if not isinstance(log_file, str):
            raise ValueError("log_file 必须是字符串")
        log_parts = PurePosixPath(log_file).parts
        # 路径字段会用于拼接真实文件系统路径，只接受 logs/ 下的规范文件名。
        if (
            len(log_parts) != 2
            or log_parts[0] != LOG_DIRECTORY_NAME
            or not LOG_FILE_PATTERN.fullmatch(log_parts[1])
        ):
            raise ValueError("log_file 必须是 logs/ 下的规范日志文件名")

        exit_code = value["exit_code"]
        if exit_code is not None and type(exit_code) is not int:
            raise ValueError("exit_code 必须是整数或 null")
        error = value["error"]
        if error is not None and not isinstance(error, str):
            raise ValueError("error 必须是字符串或 null")

        return cls(
            schema_version=RUNTIME_SCHEMA_VERSION,
            launch_id=launch_id,
            status=status,
            mode=mode,
            ddns_pid=ddns_pid,
            relay_pid=relay_pid,
            started_at=started_at,
            log_file=log_file,
            exit_code=exit_code,
            error=error,
        )


# dataclass 声明顺序即状态 schema 字段顺序；派生一次，避免 to_dict、严格校验与
# stopped 清理多处手写同一字段表。schema_version/status 是“停止”状态仍需保留的
# 元信息，因此单独列出需要清空的值字段。
RUNTIME_STATE_FIELDS = tuple(field.name for field in fields(RuntimeState))
RUNTIME_STATE_VALUE_FIELDS = tuple(
    name
    for name in RUNTIME_STATE_FIELDS
    if name not in ("schema_version", "status")
)


def mark_runtime_stopped(state: RuntimeState) -> RuntimeState:
    """把运行状态改为 stopped；只保留字段名，清空全部运行信息值。"""

    return replace(
        state, status="stopped", **{name: None for name in RUNTIME_STATE_VALUE_FIELDS}
    )


def normalized_path(path: Path) -> str:
    """按 Windows 不区分大小写的路径语义生成比较值。"""

    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def _script_directory_digest(script_directory: Path) -> str:
    """按脚本目录生成互斥量/命名事件共用的短摘要。"""

    return hashlib.sha256(
        normalized_path(script_directory).encode("utf-8")
    ).hexdigest()[:20]


def application_directory() -> Path:
    """返回源码脚本或冻结 EXE 所在目录，供同目录资源定位使用。"""

    if IS_FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def controller_self_path() -> Path:
    """返回 ctl 自身文件（源码脚本或冻结 EXE）的完整路径。"""

    if IS_FROZEN:
        return Path(sys.executable).resolve()
    return Path(__file__).resolve()


def controller_invocation() -> List[str]:
    """返回再次启动当前 ctl 的命令前缀，兼容源码与 PyInstaller 冻结模式。"""

    if IS_FROZEN:
        return [str(Path(sys.executable).resolve())]
    return [str(Path(sys.executable).resolve()), str(Path(__file__).resolve())]


def relay_process_identity() -> int:
    """返回转发器应向菜单上报的本进程 PID。

    源码模式即 os.getpid()；冻结 onefile 中 Popen 返回的是 bootloader 父进程，
    Python 子进程需上报父进程 PID 才能与菜单核验一致。
    """

    if IS_FROZEN:
        return os.getppid()
    return os.getpid()


def independent_controller_environment() -> Optional[dict]:
    """让需长期存活的冻结版子进程使用独立的 PyInstaller 运行环境。"""

    if not IS_FROZEN:
        return None
    environment = os.environ.copy()
    environment[PYINSTALLER_RESET_ENVIRONMENT_NAME] = "1"
    return environment


def read_runtime_state(path: Path) -> Tuple[bool, Optional[RuntimeState]]:
    """读取并校验运行状态；不存在返回 `(False, None)`，损坏则明确报错。"""

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False, None
    except OSError as exc:
        raise DdnsError(f"无法读取运行状态文件：{exc}") from exc

    try:
        raw_value = json.loads(text)
        return True, RuntimeState.from_dict(raw_value)
    except (json.JSONDecodeError, ValueError) as exc:
        raise DdnsError(f"运行状态文件无效：{exc}") from exc


def write_runtime_state(path: Path, state: RuntimeState) -> None:
    """先完整落盘临时 JSON，再原子替换正式状态文件。"""

    temporary_path = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary_path.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(state.to_dict(), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary_path), str(path))
    except OSError as exc:
        raise DdnsError(f"无法保存运行状态文件：{exc}") from exc


def reserve_log_file(
    log_directory: Path, started_at: Optional[datetime] = None
) -> Path:
    """以独占创建方式预留本次日志名，同秒重复启动时追加两位序号。"""

    timestamp = started_at or datetime.now().astimezone()
    base_name = f"ddns-go_{timestamp.strftime('%Y%m%d-%H%M%S')}"
    try:
        log_directory.mkdir(parents=True, exist_ok=True)
        # 同秒最多提供 1 个无序号名加 -02 到 -99 共 98 个候选，再满视为用尽。
        for index in range(1, 100):
            suffix = "" if index == 1 else f"-{index:02d}"
            candidate = log_directory / f"{base_name}{suffix}.log"
            try:
                with candidate.open("xb"):
                    pass
                return candidate
            except FileExistsError:
                continue
    except OSError as exc:
        raise DdnsError(f"无法创建日志文件：{exc}") from exc
    raise DdnsError("同一秒内的日志文件名已用尽，请稍后再启动。")


def _list_log_files(log_directory: Path) -> List[Path]:
    """返回目录下所有规范命名的日志文件；目录缺失时视为空列表。"""

    try:
        return [
            path
            for path in log_directory.glob("ddns-go_*.log")
            if path.is_file() and LOG_FILE_PATTERN.fullmatch(path.name)
        ]
    except OSError as exc:
        raise DdnsError(f"无法读取日志目录：{exc}") from exc


def _log_sort_key(path: Path) -> Tuple[str, int]:
    """按名称中的时间戳和同秒序号排序；无序号视为 0。"""

    # 规范名固定为“日期-时间”或“日期-时间-序号”两/三段，序号缺省按 0 排序。
    name = path.name[len("ddns-go_") : -len(".log")]
    parts = name.split("-")
    timestamp = "-".join(parts[:2])
    suffix = int(parts[2]) if len(parts) == 3 else 0
    return (timestamp, suffix)


def prune_log_files(
    log_directory: Path,
    keep_count: int = LOG_KEEP_COUNT,
    current_path: Optional[Path] = None,
    protected_paths: Iterable[Path] = (),
) -> List[Path]:
    """按日志时间顺序保留最近 keep_count 份日志，返回清理失败的路径。

    current_path 与 protected_paths 都视为受保护日志：即使按名称排序较早
    也不会被删除，并且会占用 keep_count 的保留名额。非日志文件一律不处理。
    """

    if keep_count < 0:
        raise ValueError("LOG_KEEP_COUNT 必须是非负整数。")

    candidates = _list_log_files(log_directory)

    # 统一收集受保护日志的规范化路径；current_path 只是其中最常见的一种。
    protected_keys = (
        {normalized_path(current_path)} if current_path is not None else set()
    )
    protected_keys.update(normalized_path(path) for path in protected_paths)
    # 受保护日志不能进入删除候选，但计算“应删除多少份”时仍计入总数，
    # 否则 keep_count 会把受保护日志误当成可删除的余额。
    protected_inside_count = sum(
        normalized_path(path) in protected_keys for path in candidates
    )
    if protected_inside_count:
        candidates = [
            path
            for path in candidates
            if normalized_path(path) not in protected_keys
        ]
    total_count = len(candidates) + protected_inside_count

    remove_count = max(0, total_count - keep_count)
    candidates.sort(key=_log_sort_key)
    # 按最旧优先截取应删数量；受保护日志已从候选剔除，不会出现在删除列表。
    obsolete_paths = candidates[:remove_count]

    failures = []
    for path in obsolete_paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            failures.append(path)
    return failures


def latest_log_file(log_directory: Path) -> Optional[Path]:
    """返回日志目录中命名最新的一份日志；目录为空或不存在时返回 None。"""

    candidates = _list_log_files(log_directory)
    if not candidates:
        return None

    candidates.sort(key=_log_sort_key)
    return candidates[-1]


def _prune_failure_warning(failures: Sequence[Path]) -> Tuple[str, str]:
    """构造日志清理失败的统一警告分项。"""

    return ("警告", f"{len(failures)} 个旧日志未能清理")


def poll_until(
    deadline: float,
    interval: float,
    predicate: Callable[[], bool],
) -> bool:
    """按固定步长轮询直到条件成立；超时返回 False。"""

    while True:
        if predicate():
            return True
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            return False
        time.sleep(min(interval, remaining_seconds))


def relay_stop_event_name(script_directory: Path, launch_id: str) -> str:
    """生成只属于一次启动的命名事件，用于安全请求转发器退出。"""

    path_digest = _script_directory_digest(script_directory)
    launch_digest = uuid.UUID(launch_id).hex
    return f"Local\\ddns-go_ctl_relay_stop_{path_digest}_{launch_digest}"


class RelayStopEvent:
    """转发器持有的 Windows 命名停止事件。"""

    def __init__(self, name: str) -> None:
        """创建命名的停止事件。"""

        _require_kernel32()
        handle = KERNEL32.CreateEventW(None, True, False, name)
        if not handle:
            raise _win32_error("无法创建转发器停止事件")
        self.name = name
        self.handle = handle

    def wait(self) -> int:
        """阻塞等待事件被设置，返回 Win32 等待结果。"""

        return KERNEL32.WaitForSingleObject(self.handle, INFINITE)

    def signal(self) -> None:
        """设置事件，通知转发器开始安全停止。"""

        if not KERNEL32.SetEvent(self.handle):
            raise _win32_error("无法设置转发器停止事件")

    def close(self) -> None:
        """关闭事件句柄。"""

        if self.handle is not None:
            KERNEL32.CloseHandle(self.handle)
            self.handle = None


def signal_relay_stop(script_directory: Path, launch_id: str) -> bool:
    """向指定启动实例发送停止请求；事件不存在时返回 False。"""

    _require_kernel32()
    name = relay_stop_event_name(script_directory, launch_id)
    handle = KERNEL32.OpenEventW(EVENT_MODIFY_STATE, False, name)
    if not handle:
        return False
    try:
        if not KERNEL32.SetEvent(handle):
            raise _win32_error("无法通知日志转发器停止")
        return True
    finally:
        KERNEL32.CloseHandle(handle)


def relay_stop_event_exists(script_directory: Path, launch_id: str) -> bool:
    """用命名事件复核状态文件中的转发器确实属于该次启动。"""

    if KERNEL32 is None:
        return False
    name = relay_stop_event_name(script_directory, launch_id)
    handle = KERNEL32.OpenEventW(SYNCHRONIZE, False, name)
    if not handle:
        return False
    KERNEL32.CloseHandle(handle)
    return True


class KillOnCloseJob:
    """持有启用 KILL_ON_JOB_CLOSE 的 Job Object，确保转发器与 DDNS-GO 同生共死。"""

    def __init__(self) -> None:
        """创建并配置 KILL_ON_JOB_CLOSE 的 Job Object。"""

        _require_kernel32()
        handle = KERNEL32.CreateJobObjectW(None, None)
        if not handle:
            raise _win32_error("无法创建 Job Object")

        information = JobObjectExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not KERNEL32.SetInformationJobObject(
            handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error_code = ctypes.get_last_error()
            KERNEL32.CloseHandle(handle)
            raise _win32_error("无法配置 Job Object", error_code)
        self.handle = handle

    def assign(self, process_id: int) -> None:
        """把刚创建的 DDNS-GO 进程加入 Job；失败时调用方必须立即回收该进程。"""

        access = PROCESS_SET_QUOTA | PROCESS_TERMINATE
        process_handle = KERNEL32.OpenProcess(access, False, process_id)
        if not process_handle:
            raise _win32_error(f"无法打开待托管进程 {process_id}")
        try:
            if not KERNEL32.AssignProcessToJobObject(self.handle, process_handle):
                raise _win32_error(f"无法把进程 {process_id} 加入 Job Object")
        finally:
            KERNEL32.CloseHandle(process_handle)

    def close(self) -> None:
        """关闭 Job 句柄；已托管进程会随之被终止。"""

        if self.handle is not None:
            KERNEL32.CloseHandle(self.handle)
            self.handle = None


def relay_output(
    source: BinaryIO,
    log_stream: BinaryIO,
    console_stream: Optional[BinaryIO] = None,
    chunk_size: int = 65536,
) -> None:
    """逐块原样复制合并输出；任何写入失败都向上传递并触发 fail-closed。"""

    while True:
        chunk = source.read(chunk_size)
        if not chunk:
            return
        log_stream.write(chunk)
        log_stream.flush()
        if console_stream is not None:
            console_stream.write(chunk)
            console_stream.flush()


def parse_listener_process_ids(lines: Iterable[str], port: int) -> List[int]:
    """从 netstat TCP 输出中提取指定监听端口的 PID。"""

    listener_ids = set()
    for line in lines:
        match = LISTENER_PATTERN.match(line)
        if match and int(match.group("port")) == port:
            listener_ids.add(int(match.group("pid")))
    return sorted(listener_ids)


def parse_relay_arguments(arguments: Sequence[str]) -> argparse.Namespace:
    """解析 ctl 私有转发模式参数；该接口不提供给用户菜单直接调用。"""

    parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    parser.add_argument("--application-directory", required=True)
    parser.add_argument("--launch-id", required=True)
    parser.add_argument("--mode", choices=("foreground", "background"), required=True)
    parser.add_argument("--log-name", required=True)
    parser.add_argument("--started-at", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    try:
        namespace = parser.parse_args(list(arguments))
    except argparse.ArgumentError as exc:
        raise DdnsError(f"日志转发器参数无效：{exc}") from exc

    # REMAINDER 会原样保留 `--`，剥离后必须保证后面确实带着 DDNS-GO 启动命令。
    if namespace.command[:1] == ["--"]:
        namespace.command = namespace.command[1:]
    if not namespace.command:
        raise DdnsError("日志转发器缺少 DDNS-GO 启动命令。")
    return namespace


def run_log_relay(arguments: Sequence[str]) -> int:
    """托管一个 DDNS-GO 进程，并将其全部输出透明转发到控制台与日志。"""

    try:
        options = parse_relay_arguments(arguments)
        script_directory = Path(options.application_directory).resolve()
        launch_id = str(uuid.UUID(options.launch_id))
        started_at = datetime.fromisoformat(options.started_at)
        if started_at.tzinfo is None:
            raise DdnsError("日志转发器 started_at 缺少时区偏移。")
        if not LOG_FILE_PATTERN.fullmatch(options.log_name):
            raise DdnsError("日志转发器收到的日志文件名无效。")

        # 转发器是受信子进程，仍拒绝执行任何非本目录 ddns-go.exe 的命令。
        expected_executable = (script_directory / EXECUTABLE_NAME).resolve()
        if normalized_path(Path(options.command[0])) != normalized_path(
            expected_executable
        ):
            raise DdnsError("日志转发器拒绝启动非本目录 ddns-go.exe。")
    except (DdnsError, ValueError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    data_directory = script_directory / DATA_DIRECTORY_NAME
    log_path = data_directory / LOG_DIRECTORY_NAME / options.log_name
    runtime_path = (
        data_directory / RUNTIME_STATE_DIRECTORY_NAME / RUNTIME_STATE_NAME
    )
    runtime_log_file = PurePosixPath(LOG_DIRECTORY_NAME, options.log_name).as_posix()
    state = RuntimeState(
        schema_version=RUNTIME_SCHEMA_VERSION,
        launch_id=launch_id,
        status="starting",
        mode=options.mode,
        ddns_pid=None,
        relay_pid=relay_process_identity(),
        started_at=started_at.isoformat(timespec="seconds"),
        log_file=runtime_log_file,
        exit_code=None,
        error=None,
    )

    child = None
    job = None
    stop_event = None
    stop_thread = None
    stop_requested = threading.Event()
    exit_code = None

    try:
        # 前台模式把子进程输出原样回显到自己的控制台；后台只写日志。
        console_stream = None
        if options.mode == "foreground":
            console_stream = getattr(sys.stdout, "buffer", None)
            if console_stream is None:
                raise DdnsError("前台日志转发器没有可用的二进制控制台输出流。")

        # 先创建停止事件并发布 starting 状态，菜单才能等待并核验本次启动。
        stop_event = RelayStopEvent(relay_stop_event_name(script_directory, launch_id))
        write_runtime_state(runtime_path, state)

        # 日志先于 DDNS-GO 创建并成功打开，确保不会出现进程已运行但开头输出丢失。
        with log_path.open("ab", buffering=0) as log_stream:
            job = KillOnCloseJob()
            child = subprocess.Popen(
                options.command,
                cwd=str(script_directory),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                close_fds=True,
            )
            try:
                job.assign(child.pid)
            except Exception:
                if child.poll() is None:
                    child.kill()
                    child.wait()
                raise

            state = replace(state, status="running", ddns_pid=child.pid)
            write_runtime_state(runtime_path, state)

            def wait_for_stop_request() -> None:
                """等待停止事件后关闭 Job，触发整棵进程树退出。

                停止事件由独立线程等待，主线程才能持续排空管道，避免子进程写满后阻塞；
                关闭 Job 会因 KILL_ON_JOB_CLOSE 终止整棵进程树，随后管道 EOF 让主线程退出。
                """

                if stop_event.wait() == WAIT_OBJECT_0:
                    stop_requested.set()
                    job.close()

            stop_thread = threading.Thread(
                target=wait_for_stop_request,
                name="ddns-go-relay-stop",
                daemon=True,
            )
            stop_thread.start()

            if child.stdout is None:
                raise DdnsError("日志转发器未获得 DDNS-GO 输出管道。")
            relay_output(child.stdout, log_stream, console_stream)
            exit_code = child.wait()

        if stop_requested.is_set() or exit_code == 0:
            write_runtime_state(runtime_path, mark_runtime_stopped(state))
            return 0

        state = replace(
            state,
            status="failed",
            exit_code=exit_code,
            error=f"DDNS-GO 异常退出，退出码：{exit_code}。",
        )
        write_runtime_state(runtime_path, state)
        return 1
    except Exception as exc:
        if child is not None and child.poll() is None:
            try:
                child.kill()
                exit_code = child.wait()
            except OSError:
                pass

        # 把异常原因写入状态文件，菜单可展示失败原因而不是仅看到转发器退出。
        failure_state = replace(
            state,
            status="failed",
            ddns_pid=child.pid if child is not None else state.ddns_pid,
            exit_code=exit_code,
            error=str(exc),
        )
        try:
            write_runtime_state(runtime_path, failure_state)
        except DdnsError as state_exc:
            print(f"[错误] {exc}；且无法保存故障状态：{state_exc}", file=sys.stderr)
        else:
            print(f"[错误] 日志转发器失败：{exc}", file=sys.stderr)
        return 1
    finally:
        # 正常退出时主动唤醒等待线程；异常或控制台关闭时，Job 句柄关闭会兜底终止 DDNS-GO。
        if stop_event is not None:
            try:
                stop_event.signal()
            except DdnsError:
                pass
        if stop_thread is not None:
            stop_thread.join(timeout=1)
        if child is not None and child.stdout is not None:
            child.stdout.close()
        if job is not None:
            job.close()
        if stop_event is not None:
            stop_event.close()


class DdnsController:
    """封装状态查询和启停操作，不依赖具体的菜单渲染方式。"""

    _UNSET = object()

    def __init__(
        self,
        script_directory: Path,
        port: int = PORT,
        start_timeout_seconds: int = START_TIMEOUT_SECONDS,
        stop_timeout_seconds: int = STOP_TIMEOUT_SECONDS,
        sync_interval_seconds: Optional[int] = SYNC_INTERVAL_SECONDS,
        cache_times: Optional[int] = CACHE_TIMES,
        dns_servers: Optional[str] = DNS_SERVERS,
        config_path_override: Optional[str] = CONFIG_PATH_OVERRIDE,
        *,
        process_ops: Optional[IProcessOperations] = None,
        port_query: Optional[IPortQuery] = None,
        state_store: Optional[IRuntimeStateStore] = None,
        log_manager: Optional[ILogManager] = None,
        spawner: Optional[IProcessSpawner] = None,
    ) -> None:
        """校验可调参数并预解析脚本目录下的固定路径。

        末尾的 keyword-only 参数是接口隔离的注入点：默认为真实实现，调用方可
        传入替代实现（mock 或换源）；注入的实现类仍委托模块级函数，保持原测试
        的 mock.patch 拦截路径不变。
        """

        if not 1 <= port <= 65535:
            raise ValueError("Port 必须是 1 到 65535 之间的整数。")
        if start_timeout_seconds < 1 or stop_timeout_seconds < 1:
            raise ValueError("启动和停止超时时间必须是正整数。")
        if sync_interval_seconds is not None and sync_interval_seconds < 1:
            raise ValueError("SYNC_INTERVAL_SECONDS 必须是正整数或 None。")
        if cache_times is not None and cache_times < 1:
            raise ValueError("CACHE_TIMES 必须是正整数或 None。")
        if dns_servers is not None and not dns_servers.strip():
            raise ValueError("DNS_SERVERS 必须是非空字符串或 None。")
        if config_path_override is not None and not str(
            config_path_override
        ).strip():
            raise ValueError("CONFIG_PATH_OVERRIDE 必须是非空路径或 None。")

        self.script_directory = script_directory.resolve()
        self.executable_path = (self.script_directory / EXECUTABLE_NAME).resolve()
        # 目标路径在控制器生命周期内不变，预先规范化可避免每次核验重复处理。
        self._normalized_executable_path = normalized_path(self.executable_path)
        self.relay_executable_path = Path(sys.executable).resolve()
        self._normalized_relay_executable_path = normalized_path(
            self.relay_executable_path
        )
        self.data_directory = self.script_directory / DATA_DIRECTORY_NAME
        self.log_directory = self.data_directory / LOG_DIRECTORY_NAME
        if config_path_override is None:
            self.config_path = self.data_directory / CONFIG_NAME
        else:
            # 自定义配置路径按脚本目录解析相对路径，避免受启动时工作目录影响。
            override_path = Path(config_path_override)
            if not override_path.is_absolute():
                override_path = self.script_directory / override_path
            self.config_path = override_path.resolve()
        self.runtime_state_path = (
            self.data_directory / RUNTIME_STATE_DIRECTORY_NAME / RUNTIME_STATE_NAME
        )

        # 接口隔离装配：未注入时使用真实实现；注入项由调用方保证满足协议。
        self.process_ops = (
            process_ops if process_ops is not None else Win32ProcessOperations()
        )
        self.port_query = (
            port_query if port_query is not None else NetstatPortQuery()
        )
        self.state_store = (
            state_store
            if state_store is not None
            else FileRuntimeStateStore(self.runtime_state_path)
        )
        self.log_manager = (
            log_manager if log_manager is not None else FileLogManager(self.log_directory)
        )
        self.spawner = spawner if spawner is not None else SubprocessSpawner()
        # Settings 是可调参数的唯一来源；dns_servers 在此统一规范化一次，
        # 实例的 port / 超时等只读属性均委托给 settings，避免两套状态漂移。
        self.settings = Settings(
            port=port,
            start_timeout_seconds=start_timeout_seconds,
            stop_timeout_seconds=stop_timeout_seconds,
            sync_interval_seconds=sync_interval_seconds,
            cache_times=cache_times,
            dns_servers=dns_servers.strip() if dns_servers is not None else None,
            config_path_override=config_path_override,
        )

    @property
    def port(self) -> int:
        """监听端口（只读，委托 Settings）。"""

        return self.settings.port

    @property
    def start_timeout_seconds(self) -> int:
        """启动确认超时（秒，只读，委托 Settings）。"""

        return self.settings.start_timeout_seconds

    @property
    def stop_timeout_seconds(self) -> int:
        """停止等待超时（秒，只读，委托 Settings）。"""

        return self.settings.stop_timeout_seconds

    @property
    def sync_interval_seconds(self) -> Optional[int]:
        """DDNS-GO 同步间隔（秒，只读，委托 Settings）。"""

        return self.settings.sync_interval_seconds

    @property
    def cache_times(self) -> Optional[int]:
        """解析缓存次数（只读，委托 Settings）。"""

        return self.settings.cache_times

    @property
    def dns_servers(self) -> Optional[str]:
        """自定义 DNS（只读，委托 Settings）。"""

        return self.settings.dns_servers

    def _open_process(self, access: int, process_id: int):
        """按权限打开进程句柄；进程不存在时返回 None。"""

        return self.process_ops.open_process(access, process_id)

    def _query_process_path_from_handle(self, handle) -> Path:
        """通过进程句柄查询完整映像路径。"""

        return self.process_ops.query_process_path(handle)

    def get_process_path(self, process_id: int) -> Optional[Path]:
        """返回进程的完整路径；进程不存在返回 None，查询失败则抛错。"""

        try:
            handle = self._open_process(PROCESS_QUERY_LIMITED_INFORMATION, process_id)
        except OSError as exc:
            raise DdnsError(f"无法查询进程 {process_id}：{exc}") from exc

        if handle is None:
            return None

        try:
            return self._query_process_path_from_handle(handle)
        except OSError as exc:
            raise DdnsError(f"无法查询进程 {process_id} 的程序路径：{exc}") from exc
        finally:
            self.process_ops.close_handle(handle)

    def process_matches_executable(self, process_id: int) -> bool:
        """核对 PID 是否属于脚本目录中的 ddns-go.exe。"""

        process_path = self.get_process_path(process_id)
        if process_path is None:
            return False
        return normalized_path(process_path) == self._normalized_executable_path

    def process_matches_relay(self, process_id: int) -> bool:
        """核对转发器进程映像；命名停止事件再负责区分同路径的菜单实例。"""

        process_path = self.get_process_path(process_id)
        if process_path is None:
            return False
        return normalized_path(process_path) == self._normalized_relay_executable_path

    def get_listener_process_ids(self) -> List[int]:
        """查询监听 PID；命令失败时抛错，绝不伪造为空监听结果。"""

        return self.port_query.query(self.port)

    def read_runtime_state(self) -> Tuple[bool, Optional[RuntimeState]]:
        """委托状态存储组件读取运行状态文件（默认绑定控制器自身路径）。"""

        return self.state_store.read()

    def _probe_runtime_state(
        self, runtime_state: RuntimeState
    ) -> Tuple[bool, bool, bool]:
        """返回 (DDNS-GO 路径匹配, 转发器路径匹配, 停止事件存在)。"""

        ddns_matches = bool(
            runtime_state.ddns_pid is not None
            and self.process_matches_executable(runtime_state.ddns_pid)
        )
        relay_matches = False
        relay_event_matches = False
        if runtime_state.relay_pid is not None:
            relay_matches = self.process_matches_relay(runtime_state.relay_pid)
            relay_event_matches = relay_stop_event_exists(
                self.script_directory, runtime_state.launch_id
            )
        return ddns_matches, relay_matches, relay_event_matches

    def reconcile_runtime_state(
        self,
    ) -> Tuple[Optional[RuntimeState], Optional[Tuple[bool, bool, bool]]]:
        """按实际进程核对 JSON，并把陈旧状态改写为 stopped。

        刷新状态时调用；只在状态文件可解析且与真实进程不一致时写盘。running 状态
        要求 DDNS-GO 与转发器同时存活，否则视为日志链路已失效。返回 (核对后的状态,
        进程探测结果)，便于 refresh_state 复用，避免同一轮刷新重复查进程。
        """

        state_exists, runtime_state = self.read_runtime_state()
        if not state_exists or runtime_state is None:
            return None, None
        if runtime_state.status == "stopped":
            return runtime_state, (False, False, False)

        ddns_alive, relay_matches, relay_event_matches = self._probe_runtime_state(
            runtime_state
        )
        relay_alive = relay_matches and relay_event_matches

        if runtime_state.status == "starting":
            # starting 只需转发器存活；转发器已消失说明状态交接中断，归为 stopped。
            corrected = None if relay_alive else mark_runtime_stopped(runtime_state)
        elif runtime_state.status == "running":
            if not ddns_alive:
                corrected = mark_runtime_stopped(runtime_state)
            elif not relay_alive:
                # DDNS-GO 仍在但转发器/事件不匹配：保留 failed 状态，提示日志链路失效。
                corrected = replace(
                    runtime_state,
                    status="failed",
                    error="日志转发器不存在或身份不匹配，日志链路已失效。",
                )
            else:
                corrected = None
        else:  # failed
            # failed 且 DDNS-GO 仍存活时保留原状，避免把异常进程误当成已停止。
            corrected = None if ddns_alive else mark_runtime_stopped(runtime_state)

        if corrected is not None:
            self.state_store.write(corrected)
            return corrected, (ddns_alive, relay_matches, relay_event_matches)
        return runtime_state, (ddns_alive, relay_matches, relay_event_matches)

    def refresh_state(self) -> DdnsState:
        """先核对修正运行状态 JSON，再返回当前完整状态。"""

        try:
            runtime_state, runtime_probe = self.reconcile_runtime_state()
        except DdnsError as exc:
            return self.new_state(
                STATE_CODE_INVALID_RUNTIME,
                "状态核对失败",
                str(exc),
                "red",
            )
        return self.get_state(
            runtime_state=runtime_state,
            runtime_probe=runtime_probe,
        )

    def runtime_log_path(self, runtime_state: RuntimeState) -> Optional[Path]:
        """把已通过模型校验的 POSIX 相对日志路径解析到 ctl-data 目录；无日志字段时返回 None。"""

        if runtime_state.log_file is None:
            return None
        return self.data_directory.joinpath(*PurePosixPath(runtime_state.log_file).parts)

    def display_path(self, path: Path) -> str:
        """把路径转换为相对 ctl 目录的显示形式（统一正斜杠）。

        仅用于界面展示；进程核验与文件读写仍使用绝对路径。路径位于其他驱动器
        无法求相对路径时回退为绝对路径。
        """

        try:
            relative = os.path.relpath(str(path), str(self.script_directory))
        except ValueError:  # Windows 下不同驱动器无法计算相对路径
            return str(path)
        return relative.replace(os.sep, "/")

    def new_state(
        self,
        code: str,
        title: str,
        detail: str,
        color: str,
        process_id: Optional[int] = None,
        relay_process_id: Optional[int] = None,
        log_path: Optional[Path] = None,
        launch_id: Optional[str] = None,
        target_process_ids: Sequence[int] = (),
    ) -> DdnsState:
        """用控制器固定 EXE 路径构造一条界面可展示的状态。"""

        return DdnsState(
            code=code,
            title=title,
            detail=detail,
            color=color,
            executable_path=self.executable_path,
            process_id=process_id,
            relay_process_id=relay_process_id,
            log_path=log_path,
            launch_id=launch_id,
            target_process_ids=tuple(target_process_ids),
        )

    def _expected_listener_ids(
        self,
        listener_ids: Sequence[int],
        runtime_state: Optional[RuntimeState],
        runtime_ddns_matches: bool,
    ) -> List[int]:
        """只保留已核验路径的监听 PID；状态文件 PID 直接复用本轮 probe 结果。"""

        expected_listener_ids = []
        for process_id in listener_ids:
            if runtime_state is not None and runtime_state.ddns_pid == process_id:
                # probe 已核验过该 PID 的路径，直接复用结果，避免对同一 PID
                # 二次查询进程路径；不匹配时也不回退到重新核验。
                if runtime_ddns_matches:
                    expected_listener_ids.append(process_id)
            elif self.process_matches_executable(process_id):
                expected_listener_ids.append(process_id)
        return expected_listener_ids

    def _logging_failed_state(
        self,
        runtime_error: Optional[str],
        runtime_state: Optional[RuntimeState],
        active_id: int,
        log_path: Optional[Path],
        relay_matches: bool,
        relay_event_matches: bool,
        target_process_ids: Sequence[int],
    ) -> DdnsState:
        """构造日志链路异常状态；标题区分 running 与其他运行状态。"""

        reasons = []
        if runtime_error:
            reasons.append(runtime_error)
        elif runtime_state is None:
            reasons.append("运行状态文件缺失")
        else:
            if runtime_state.status != "running":
                reasons.append(f"运行状态为 {runtime_state.status}")
            if runtime_state.ddns_pid != active_id:
                reasons.append("状态文件中的 DDNS-GO PID 不一致")
            if not relay_matches or not relay_event_matches:
                reasons.append("日志转发器不存在或身份不匹配")
            if log_path is None or not log_path.is_file():
                reasons.append("当前日志文件缺失")
        # 标题随实际状态区分：running 时是“运行中但日志异常”，其余状态
        # （failed/starting 等）不宜声称“运行中”，避免误导。
        title = (
            "运行中，但日志状态异常"
            if runtime_state is not None
            and runtime_state.status == "running"
            else "状态异常，日志链路已失效"
        )
        return self.new_state(
            STATE_CODE_LOGGING_FAILED,
            title,
            "；".join(reasons) + "。",
            "red",
            process_id=active_id,
            relay_process_id=(
                runtime_state.relay_pid if runtime_state is not None else None
            ),
            log_path=log_path,
            launch_id=(
                runtime_state.launch_id if runtime_state is not None else None
            ),
            target_process_ids=target_process_ids,
        )

    def get_state(
        self,
        runtime_state: object = _UNSET,
        runtime_probe: Optional[Tuple[bool, bool, bool]] = None,
    ) -> DdnsState:
        """只读合并 EXE、运行状态、转发器、实际进程和监听端口。

        运行状态文件从不单独作为“健康”依据；DDNS-GO 路径、转发器路径、该次启动的
        命名事件和日志文件必须同时存在。target_process_ids 仍是唯一允许停止的集合。
        传入已核对的 runtime_state 时可避免重复读取状态文件。
        """

        # 程序文件缺失是最基础的状态，直接返回，不继续做进程与端口查询。
        if not self.executable_path.is_file():
            return self.new_state(
                STATE_CODE_MISSING_EXECUTABLE,
                "程序文件缺失",
                "脚本目录中未找到 ddns-go.exe。",
                "red",
            )

        if runtime_state is self._UNSET:
            runtime_exists = False
            runtime_error = None
            try:
                runtime_exists, runtime_state = self.read_runtime_state()
            except DdnsError as exc:
                runtime_error = str(exc)
                # 读取失败时哨兵值不能泄漏到后续逻辑，否则 _UNSET.ddns_pid
                # 会抛出难以理解的 AttributeError 而非 DdnsError。
                runtime_state = None
        else:
            runtime_exists = runtime_state is not None
            runtime_error = None

        runtime_ddns_matches = False
        relay_matches = False
        relay_event_matches = False
        try:
            if runtime_state is not None:
                if runtime_probe is not None:
                    (
                        runtime_ddns_matches,
                        relay_matches,
                        relay_event_matches,
                    ) = runtime_probe
                else:
                    (
                        runtime_ddns_matches,
                        relay_matches,
                        relay_event_matches,
                    ) = self._probe_runtime_state(runtime_state)
            # 一次查询内同时完成身份核验与端口监听，任何一步失败都归为查询失败。
            listener_ids = self.get_listener_process_ids()
            expected_listener_ids = self._expected_listener_ids(
                listener_ids, runtime_state, runtime_ddns_matches
            )
        except DdnsError as exc:
            known_process_id = (
                runtime_state.ddns_pid
                if runtime_state is not None and runtime_ddns_matches
                else None
            )
            return self.new_state(
                STATE_CODE_QUERY_FAILED,
                "状态查询失败",
                str(exc),
                "red",
                process_id=known_process_id,
            )

        # 有已核验的监听 PID 时，进一步要求状态文件、转发器、事件和日志全部匹配才算健康。
        if expected_listener_ids:
            active_id = (
                runtime_state.ddns_pid
                if runtime_state is not None
                and runtime_state.ddns_pid in expected_listener_ids
                else expected_listener_ids[0]
            )
            log_path = (
                self.runtime_log_path(runtime_state)
                if runtime_state is not None
                else None
            )
            logging_healthy = bool(
                runtime_state is not None
                and runtime_state.status == "running"
                and runtime_state.ddns_pid == active_id
                and relay_matches
                and relay_event_matches
                and log_path is not None
                and log_path.is_file()
            )
            if not logging_healthy:
                return self._logging_failed_state(
                    runtime_error,
                    runtime_state,
                    active_id,
                    log_path,
                    relay_matches,
                    relay_event_matches,
                    expected_listener_ids,
                )
            return self.new_state(
                STATE_CODE_RUNNING,
                "运行中",
                f"正在监听端口 {self.port}，日志转发器工作正常。",
                "green",
                process_id=active_id,
                relay_process_id=runtime_state.relay_pid,
                log_path=log_path,
                launch_id=runtime_state.launch_id,
                target_process_ids=expected_listener_ids,
            )

        # 受管 DDNS-GO 仍在但未监听端口：转发器健康时提示黄色，链路异常时提示红色。
        if runtime_state is not None and runtime_ddns_matches:
            log_path = self.runtime_log_path(runtime_state)
            relay_healthy = relay_matches and relay_event_matches
            return self.new_state(
                (
                    STATE_CODE_PROCESS_NOT_LISTENING
                    if relay_healthy
                    else STATE_CODE_LOGGING_FAILED
                ),
                (
                    "进程存在，但未监听"
                    if relay_healthy
                    else "进程存在，但日志状态异常"
                ),
                f"PID {runtime_state.ddns_pid} 存在，但未监听端口 {self.port}。",
                "yellow" if relay_healthy else "red",
                process_id=runtime_state.ddns_pid,
                relay_process_id=runtime_state.relay_pid,
                log_path=log_path,
                launch_id=runtime_state.launch_id,
                target_process_ids=(runtime_state.ddns_pid,),
            )

        # 没有受管进程却有人监听端口，拒绝操作并展示占用者 PID。
        if listener_ids:
            ids_text = ", ".join(str(process_id) for process_id in listener_ids)
            return self.new_state(
                STATE_CODE_PORT_OCCUPIED,
                "端口被其他程序占用",
                f"端口 {self.port} 由其他进程监听，PID：{ids_text}。",
                "red",
            )

        # 状态文件明确 stopped 时优先展示其说明，而不是笼统报“端口空闲”。
        if runtime_state is not None and runtime_state.status == "stopped":
            return self.new_state(
                STATE_CODE_STOPPED,
                "未运行",
                "运行状态文件已保留，当前无运行信息。",
                "bright_black",
            )

        # 状态文件非 stopped 但进程/转发器不匹配：区分“只剩转发器”与“陈旧状态”。
        if runtime_state is not None:
            log_path = self.runtime_log_path(runtime_state)
            if relay_matches and relay_event_matches:
                return self.new_state(
                    STATE_CODE_RELAY_ONLY,
                    "日志转发器仍在运行",
                    "未找到受管 DDNS-GO 进程；停止操作将关闭该转发器。",
                    "yellow",
                    relay_process_id=runtime_state.relay_pid,
                    log_path=log_path,
                    launch_id=runtime_state.launch_id,
                )

            detail = "检测到已失效的运行状态，启动时将自动清理。"
            if runtime_state.error:
                detail += f" 上次错误：{runtime_state.error}"
            return self.new_state(
                STATE_CODE_STALE_RUNTIME,
                "未运行",
                detail,
                "yellow",
                relay_process_id=runtime_state.relay_pid,
                log_path=log_path,
                launch_id=runtime_state.launch_id,
            )

        # 状态文件存在但无法解析或读取失败：归为无效状态，不当作正常未运行。
        if runtime_exists or runtime_error is not None:
            return self.new_state(
                STATE_CODE_INVALID_RUNTIME,
                "运行状态文件无效",
                runtime_error or "运行状态文件无法解析。",
                "red",
            )

        # 无状态文件且无监听进程：最干净的未运行状态。
        return self.new_state(
            STATE_CODE_STOPPED,
            "未运行",
            f"端口 {self.port} 当前没有监听进程。",
            "bright_black",
        )

    def terminate_expected_process(self, process_id: int) -> bool:
        """在同一进程句柄上完成身份复核、终止和退出确认。"""

        access = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE | SYNCHRONIZE
        try:
            handle = self._open_process(access, process_id)
        except OSError as exc:
            raise DdnsError(f"无法打开进程 {process_id}：{exc}") from exc

        if handle is None:
            return False

        try:
            try:
                process_path = self._query_process_path_from_handle(handle)
            except OSError as exc:
                raise DdnsError(
                    f"无法核对 PID {process_id} 的程序路径，未停止该进程。"
                ) from exc

            if normalized_path(process_path) != self._normalized_executable_path:
                raise DdnsError(
                    f"PID {process_id} 已不再属于当前目录的 ddns-go.exe，未停止该进程。"
                )

            if not self.process_ops.terminate_process(handle, 1):
                raise _win32_error(f"停止进程 {process_id} 失败")

            timeout_ms = int(self.stop_timeout_seconds * 1000)
            wait_result = self.process_ops.wait_for_single_object(handle, timeout_ms)
            if wait_result == WAIT_TIMEOUT:
                raise DdnsError(
                    f"进程 {process_id} 未能在 {self.stop_timeout_seconds} 秒内退出。"
                )
            if wait_result != WAIT_OBJECT_0:
                raise _win32_error(f"等待进程 {process_id} 退出失败")
            return True
        finally:
            self.process_ops.close_handle(handle)

    def wait_for_process_exit(self, process_id: int) -> bool:
        """等待已知 PID 退出；进程本已不存在返回 True，超时返回 False。"""

        try:
            handle = self._open_process(SYNCHRONIZE, process_id)
        except OSError as exc:
            raise DdnsError(f"无法等待进程 {process_id}：{exc}") from exc
        if handle is None:
            return True
        try:
            wait_result = self.process_ops.wait_for_single_object(
                handle, int(self.stop_timeout_seconds * 1000)
            )
            if wait_result == WAIT_OBJECT_0:
                return True
            if wait_result == WAIT_TIMEOUT:
                return False
            raise _win32_error(f"等待进程 {process_id} 退出失败")
        finally:
            self.process_ops.close_handle(handle)

    def _build_start_arguments(self) -> List[str]:
        """组装 DDNS-GO 的启动参数；配置为 None 的项一律不传，沿用其自身默认值。"""

        return self.settings.build_start_arguments(
            self.executable_path, self.config_path
        )

    def _build_relay_arguments(
        self,
        hidden: bool,
        launch_id: str,
        log_path: Path,
        started_at: datetime,
    ) -> List[str]:
        """组装 ctl 私有转发模式命令，`--` 后仅允许本控制器生成的 DDNS-GO 参数。"""

        mode = "background" if hidden else "foreground"
        return [
            *controller_invocation(),
            RELAY_SWITCH,
            "--application-directory",
            str(self.script_directory),
            "--launch-id",
            launch_id,
            "--mode",
            mode,
            "--log-name",
            log_path.name,
            "--started-at",
            started_at.isoformat(timespec="seconds"),
            "--",
            *self._build_start_arguments(),
        ]

    def _wait_for_relay_state(
        self,
        relay_process: subprocess.Popen,
        launch_id: str,
    ) -> RuntimeState:
        """等待转发器原子发布真实 DDNS-GO PID，并拒绝串入其他启动实例的状态。"""

        deadline = time.monotonic() + self.start_timeout_seconds
        runtime_state: Optional[RuntimeState] = None

        def relay_state_ready() -> bool:
            """返回 True 表示状态已核验；明确失败直接抛出，等待由 poll_until 负责。"""

            nonlocal runtime_state
            state_exists, runtime_state = self.read_runtime_state()
            if (
                state_exists
                and runtime_state is not None
                and runtime_state.launch_id == launch_id
            ):
                if runtime_state.status == "failed":
                    raise DdnsError(runtime_state.error or "日志转发器启动失败。")
                if runtime_state.status == "running":
                    if runtime_state.ddns_pid is None:
                        raise DdnsError("转发器运行状态缺少 DDNS-GO PID。")
                    if runtime_state.relay_pid != relay_process.pid:
                        raise DdnsError("转发器 PID 与启动进程不一致。")
                    if not self.process_matches_relay(runtime_state.relay_pid):
                        raise DdnsError("无法核验日志转发器进程身份。")
                    if not self.process_matches_executable(runtime_state.ddns_pid):
                        raise DdnsError("无法核验转发器创建的 DDNS-GO 进程身份。")
                    if not relay_stop_event_exists(
                        self.script_directory, runtime_state.launch_id
                    ):
                        raise DdnsError("日志转发器停止事件不存在。")
                    return True

            relay_exit_code = relay_process.poll()
            if relay_exit_code is not None:
                raise DdnsError(
                    f"日志转发器在状态交接前退出，退出码：{relay_exit_code}。"
                )
            return False

        if not poll_until(deadline, 0.05, relay_state_ready):
            raise DdnsError("等待日志转发器发布运行状态超时。")
        return runtime_state

    def _cleanup_after_failed_start(
        self,
        relay_process: Optional[subprocess.Popen],
        ddns_process_id: Optional[int],
        runtime_state: Optional[RuntimeState],
    ) -> Optional[DdnsError]:
        """回收启动失败时创建的转发器与 DDNS-GO，并保留 stopped 状态文件。

        relay_process 是本次 Popen 返回的精确句柄，可以直接终止；DDNS-GO PID 仍须重新
        核验完整路径。任何一步失败都会合并返回，避免清理错误掩盖原始启动故障。
        """

        failures = []
        if relay_process is not None and relay_process.poll() is None:
            try:
                relay_process.terminate()
                relay_process.wait(timeout=self.stop_timeout_seconds)
            except (OSError, subprocess.TimeoutExpired) as exc:
                failures.append(f"无法回收日志转发器：{exc}")

        if ddns_process_id is not None:
            try:
                if self.process_matches_executable(ddns_process_id):
                    self.terminate_expected_process(ddns_process_id)
            except DdnsError as exc:
                failures.append(str(exc))

        saved_state = runtime_state
        if saved_state is None:
            try:
                _, saved_state = self.read_runtime_state()
            except DdnsError as exc:
                failures.append(str(exc))
        if saved_state is not None:
            try:
                self.state_store.write(mark_runtime_stopped(saved_state))
            except DdnsError as exc:
                failures.append(str(exc))
        if failures:
            return DdnsError("；".join(failures))
        return None

    def start(self, hidden: bool = True) -> ActionResult:
        """启动 DDNS-GO 并等待它监听目标端口。

        启动超时但进程仍存活时保留转发器、DDNS-GO 与状态文件，并返回警告；只有
        明确失败才会回收本次启动创建的两个进程。
        """

        state = self.get_state()
        window_mode_label = "后台模式" if hidden else "前台模式"

        # 仅放行可安全启动的状态；进程仍在、端口被占或状态异常一律拒绝。
        if state.code == STATE_CODE_MISSING_EXECUTABLE:
            raise DdnsError("脚本目录中未找到 ddns-go.exe。")
        if state.code == STATE_CODE_RUNNING:
            if state.process_id is None:
                raise DdnsError("运行状态缺少有效 PID。")
            return ActionResult("DDNS-GO 已在运行。")
        if state.code in (
            STATE_CODE_PROCESS_NOT_LISTENING,
            STATE_CODE_LOGGING_FAILED,
            STATE_CODE_RELAY_ONLY,
        ):
            raise DdnsError(
                f"{state.title}：{state.detail} 请先停止后再启动。"
            )
        if state.code in (
            STATE_CODE_PORT_OCCUPIED,
            STATE_CODE_QUERY_FAILED,
            STATE_CODE_INVALID_RUNTIME,
        ):
            raise DdnsError(state.detail)

        # 先独占创建本次日志名，再清理日志；新日志天然不会进入删除候选。
        started_at = datetime.now().astimezone()
        log_path = self.log_manager.reserve(started_at)
        prune_failures = self.log_manager.prune(
            LOG_KEEP_COUNT, current_path=log_path
        )
        launch_id = str(uuid.uuid4())
        relay_arguments = self._build_relay_arguments(
            hidden, launch_id, log_path, started_at
        )

        # 前台新控制台属于透明转发器：DDNS-GO 的字节先进入管道，再原样写回该控制台
        # 与日志。后台使用同一条数据路径，只是隐藏转发器窗口。
        #
        # 不要改用 DDNS-GO 自带的 -d 参数来实现后台运行：实测它 fork 之后父进程
        # 立即以退出码 0 结束，真正监听端口的是另一个 PID。那样 Popen 拿到的
        # PID 会瞬间失效，启动确认会误报失败，同时留下不受本脚本管理的孤儿进程。
        creation_flags = CREATE_NO_WINDOW if hidden else CREATE_NEW_CONSOLE
        popen_options = {
            "cwd": str(self.script_directory),
            "creationflags": creation_flags,
            "close_fds": True,
        }
        relay_environment = independent_controller_environment()
        if relay_environment is not None:
            popen_options["env"] = relay_environment
        if hidden:
            popen_options.update(
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        relay_process = None
        runtime_state = None
        listener_verified = False
        try:
            relay_process = self.spawner.spawn(relay_arguments, **popen_options)
            runtime_state = self._wait_for_relay_state(relay_process, launch_id)
            ddns_process_id = runtime_state.ddns_pid
            if ddns_process_id is None:
                raise DdnsError("日志转发器未提供 DDNS-GO PID。")
            deadline = time.monotonic() + self.start_timeout_seconds

            def listener_confirmed() -> bool:
                """返回 True 表示目标 PID 已监听；转发器退出则视为明确失败。"""

                relay_exit_code = relay_process.poll()
                if relay_exit_code is not None:
                    _, latest_state = self.read_runtime_state()
                    error = (
                        latest_state.error
                        if latest_state is not None
                        and latest_state.launch_id == launch_id
                        else None
                    )
                    raise DdnsError(
                        error
                        or f"日志转发器在端口确认前退出，退出码：{relay_exit_code}。"
                    )

                return ddns_process_id in self.get_listener_process_ids()

            listener_verified = poll_until(deadline, 0.25, listener_confirmed)
        except Exception as exc:
            ddns_process_id = (
                runtime_state.ddns_pid if runtime_state is not None else None
            )
            cleanup_error = self._cleanup_after_failed_start(
                relay_process, ddns_process_id, runtime_state
            )
            if cleanup_error is not None:
                if isinstance(exc, DdnsError):
                    raise DdnsError(
                        f"{exc}；启动失败后的安全清理未完成：{cleanup_error}"
                    ) from cleanup_error
                if isinstance(exc, OSError):
                    raise DdnsError(
                        f"启动 DDNS-GO 失败：{exc}；安全清理未完成：{cleanup_error}"
                    ) from cleanup_error
                raise RuntimeError(
                    f"启动期间发生未预期错误，且安全清理未完成：{cleanup_error}"
                ) from exc

            if isinstance(exc, OSError):
                raise DdnsError(f"启动 DDNS-GO 失败：{exc}") from exc
            raise

        if listener_verified:
            text = f"DDNS-GO 已以{window_mode_label}启动。"
            color = "green"
        else:
            text = (
                f"DDNS-GO 已以{window_mode_label}启动；但在 "
                f"{self.start_timeout_seconds} 秒内未确认端口监听。"
            )
            color = "yellow"
        if prune_failures:
            return ActionResult(
                text,
                "yellow",
                (_prune_failure_warning(prune_failures),),
            )
        return ActionResult(text, color)

    def stop(self) -> ActionResult:
        """仅停止状态检查已确认属于当前目录 EXE 的候选进程。"""

        state = self.get_state()

        # 只允许停止当前目录的受管进程；占用者或无法核验的状态直接拒绝。
        if state.code == STATE_CODE_MISSING_EXECUTABLE:
            raise DdnsError("脚本目录中未找到 ddns-go.exe，无法安全核对待停止进程。")
        if state.code == STATE_CODE_STOPPED:
            return ActionResult("DDNS-GO 当前未运行。", "yellow")
        if state.code == STATE_CODE_STALE_RUNTIME:
            _, saved_state = self.read_runtime_state()
            if saved_state is not None:
                self.state_store.write(mark_runtime_stopped(saved_state))
                return ActionResult(
                    "DDNS-GO 当前未运行；运行状态已标记为 stopped。", "yellow"
                )
            return ActionResult(
                "DDNS-GO 当前未运行；运行状态文件已不存在，无需清理。", "yellow"
            )
        if state.code == STATE_CODE_PORT_OCCUPIED:
            raise DdnsError("目标端口由其他程序占用，未停止任何进程。")
        if state.code in (STATE_CODE_QUERY_FAILED, STATE_CODE_INVALID_RUNTIME):
            raise DdnsError(state.detail)

        # 优先请求转发器安全收尾并等待受管 DDNS-GO 自行退出；请求失败时逐个强停。
        relay_signaled = False
        if state.launch_id is not None:
            relay_signaled = signal_relay_stop(
                self.script_directory, state.launch_id
            )

        stopped_ids = []
        for process_id in state.target_process_ids:
            if relay_signaled and self.wait_for_process_exit(process_id):
                stopped_ids.append(process_id)
                continue
            if self.terminate_expected_process(process_id):
                stopped_ids.append(process_id)

        if state.code == STATE_CODE_RELAY_ONLY and not relay_signaled:
            raise DdnsError("无法向仍在运行的日志转发器发送安全停止请求。")
        if state.relay_process_id is not None and relay_signaled:
            if not self.wait_for_process_exit(state.relay_process_id):
                raise DdnsError(
                    f"日志转发器 {state.relay_process_id} 未能在 "
                    f"{self.stop_timeout_seconds} 秒内退出。"
                )

        # 无论是否实际终止过进程，都按最新状态文件落盘 stopped。
        _, saved_state = self.read_runtime_state()
        if saved_state is not None:
            self.state_store.write(mark_runtime_stopped(saved_state))
        if not stopped_ids:
            return ActionResult("DDNS-GO 与日志转发器已停止。")
        ids_text = ", ".join(str(process_id) for process_id in stopped_ids)
        return ActionResult(f"DDNS-GO 与日志转发器已停止，DDNS-GO PID：{ids_text}。")

    def open_management_page(self) -> ActionResult:
        """在默认浏览器打开 DDNS-GO 管理页面。"""

        management_url = f"http://localhost:{self.port}"
        try:
            opened = webbrowser.open(management_url, new=2)
        except (OSError, webbrowser.Error) as exc:
            raise DdnsError(f"调用默认浏览器失败：{exc}") from exc
        if not opened:
            raise DdnsError(f"系统未能调用默认浏览器打开 {management_url}")
        return ActionResult(f"已打开 {management_url}", "cyan")

    def open_log_directory(self) -> ActionResult:
        """在资源管理器中打开日志文件夹；目录不存在时先创建。"""

        try:
            self.log_directory.mkdir(parents=True, exist_ok=True)
            # os.startfile 交给 Windows 默认应用打开目录（资源管理器），
            # 且立即返回，不会阻塞交互菜单。
            os.startfile(str(self.log_directory))
        except OSError as exc:
            raise DdnsError(f"打开日志文件夹失败：{exc}") from exc
        return ActionResult(
            f"已在资源管理器中打开 {self.display_path(self.log_directory)}",
            "cyan",
        )

    def clean_old_logs_result(
        self,
        keep_count: int,
    ) -> ActionResult:
        """按指定保留份数清理日志，并构造可直接展示的结果。"""

        if keep_count < 0:
            return ActionResult("日志保留数量不能为负数。", "yellow")
        # 先统计清理前份数，用于计算实际删除数量。
        before = len(self.log_manager.list_log_files())
        protected_paths = []
        try:
            state_exists, runtime_state = self.read_runtime_state()
        except DdnsError:
            state_exists = False
            runtime_state = None
        # 运行中的日志是当前写入目标，即使保留 0 份也必须保护，避免删除正在使用的文件。
        if (
            state_exists
            and runtime_state is not None
            and runtime_state.status != "stopped"
        ):
            runtime_log = self.runtime_log_path(runtime_state)
            if runtime_log is not None and runtime_log.is_file():
                protected_paths.append(runtime_log)
        # 清理统一复用 prune_log_files，与启动时保持同一保护/失败语义。
        failures = self.log_manager.prune(
            keep_count, protected_paths=protected_paths
        )
        deleted = before - len(self.log_manager.list_log_files())
        if before == 0:
            return ActionResult("日志目录为空，无需清理。", "yellow")

        text = f"已按要求清理，保留近 {keep_count} 份，删除旧 {deleted} 份。"
        if protected_paths and before > keep_count:
            text += " 运行时日志受保护不删除"
        color = "green"
        detail_lines: Tuple[Tuple[str, str], ...] = ()
        if failures:
            text += f" 有 {len(failures)} 份日志清理失败。"
            detail_lines = (_prune_failure_warning(failures),)
            color = "yellow"
        return ActionResult(text, color, detail_lines)


# 控制台显示

# 2J 清可视区，3J 清滚动历史，H 回顶部；菜单与日志查看页共用同一全屏重绘序列。
CLEAR_SCREEN = "\033[2J\033[3J\033[H"


def clear_screen_prefix() -> str:
    """返回 TTY 下全屏清屏的控制序列；输出重定向时返回空串。"""

    return CLEAR_SCREEN if sys.stdout.isatty() else ""


def display_width(text: str) -> int:
    """按终端显示宽度计算文本长度，CJK 全角字符按 2 计。"""

    return sum(
        2 if unicodedata.east_asian_width(char) in ("F", "W") else 1
        for char in text
    )


class ConsoleUI(IConsoleOutput):
    """标准库控制台界面；终端支持时启用 ANSI 颜色。"""

    ANSI_COLORS = {
        "red": "31",
        "green": "32",
        "yellow": "33",
        "cyan": "36",
        "bright_black": "90",
        # 2;90 = 亮黑再叠加弱化，用于日志保留上限等最弱的辅助信息。
        "dim_bright_black": "2;90",
    }
    # 主菜单信息行标签；缩进按最长标签自动对齐，避免硬编码空格。
    INFO_LABELS = (
        "状态",
        "PID",
        "转发器",
        "端口",
        "目录",
        "程序",
        "ctl",
        "配置",
        "日志",
        "说明",
        "操作结果",
    )
    INFO_LABEL_WIDTH = max(display_width(label) for label in INFO_LABELS)
    # 结果分项标签；子缩进并单独对齐，不参与主信息行列宽。
    DETAIL_LABELS = ("模式", "PID", "转发器", "端口", "日志")
    DETAIL_LABEL_WIDTH = max(
        display_width(label) for label in DETAIL_LABELS
    )

    def __init__(self) -> None:
        """探测终端是否支持 ANSI 颜色。"""

        self.use_color = self._enable_ansi_colors()

    @staticmethod
    def _enable_ansi_colors() -> bool:
        """按标准输出与 Windows 控制台能力启用 ANSI 颜色。"""

        if not sys.stdout.isatty():
            return False
        if os.name != "nt" or KERNEL32 is None:
            return True

        handle = KERNEL32.GetStdHandle(STD_OUTPUT_HANDLE)
        if not handle:
            return False
        mode = wintypes.DWORD()
        if not KERNEL32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(
            KERNEL32.SetConsoleMode(
                handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            )
        )

    def colorize(self, text: str, color: str) -> str:
        """终端不支持或颜色未知时原样返回文本，否则包裹 ANSI 颜色。"""

        color_code = self.ANSI_COLORS.get(color)
        if not self.use_color or color_code is None:
            return text
        return f"\033[{color_code}m{text}\033[0m"

    def detail_line(self, label: str, value: str, color: str) -> str:
        """构造一条结构化分项；标签用青色，与值颜色区分。"""

        padding = " " * (self.DETAIL_LABEL_WIDTH - display_width(label))
        return (
            " " * (self.INFO_LABEL_WIDTH + 3)
            + self.colorize(label, "cyan")
            + padding
            + ": "
            + self.colorize(value, color)
        )

    def render(
        self,
        state: DdnsState,
        controller: IRenderContext,
        last_result: Optional[ActionResult],
    ) -> None:
        """全屏绘制主菜单整帧。"""

        # 每次渲染都整屏清屏并清滚动历史，避免旧菜单帧残留成“日志”。
        print(clear_screen_prefix(), end="")

        process_id = state.process_id if state.process_id is not None else "-"
        print("=" * 60)
        print(f" DDNS-GO 管理 v{SCRIPT_VERSION}")
        print("=" * 60)

        def field(label: str, value: str) -> None:
            """按最长标签宽度输出一条自适应缩进的信息行。"""

            padding = " " * (self.INFO_LABEL_WIDTH - display_width(label))
            print(f" {label}{padding}: {value}")

        field("状态", self.colorize(state.title, state.color))
        field("PID", str(process_id))
        relay_process_id = (
            state.relay_process_id if state.relay_process_id is not None else "-"
        )
        if state.log_path is not None:
            log_path = controller.display_path(state.log_path)
            log_hint = ""
        else:
            # 未运行/暂无运行时日志时，仍显示日志文件夹的相对路径，便于定位；
            # 说明用灰色与路径本身区分。
            log_path = f"{controller.display_path(controller.log_directory)}/"
            log_hint = self.colorize("（暂无运行时日志）", "bright_black")
        field("转发器", str(relay_process_id))
        field("端口", str(controller.port))
        directory_text = str(controller.script_directory)
        if not directory_text.endswith(os.sep):
            directory_text += os.sep
        field("目录", f"\"{directory_text}\"")
        field("程序", f"\"{controller.display_path(state.executable_path)}\"")
        field("ctl", f"\"{controller.display_path(controller_self_path())}\"")
        field("配置", f"\"{controller.display_path(controller.config_path)}\"")
        field("日志", f"\"{log_path}\"{log_hint}")
        field("说明", state.detail)
        if last_result is not None:
            print(
                " 操作结果："
                + self.colorize(last_result.text, last_result.color)
            )
            for label, value in last_result.detail_lines:
                print(self.detail_line(label, value, last_result.color))
        else:
            print(
                " 操作结果："
                + self.colorize("-", "bright_black")
            )
        print("=" * 60)
        print()
        for action, key, label in MENU_ITEMS:
            if action in MENU_GRAY_ACTIONS:
                print(self.colorize(f" [{key}] {label}", "bright_black"))
                continue
            if key[0].isdigit() and "/" in key:
                # 数字主键保持醒目，副键用灰色弱化，避免整行按键同色。
                primary, secondary = key.split("/", 1)
                key_text = primary + self.colorize(
                    f"/{secondary}", "bright_black"
                )
            else:
                key_text = key
            print(f" [{key_text}] {label}")
        print()

    @staticmethod
    def prompt(text: str) -> None:
        """输出无换行提示。"""

        print(text, end="", flush=True)

    @staticmethod
    def echo(text: str) -> None:
        """输出单行回显。"""

        print(text)


class TuiInput(IKeyReader):
    """统一封装控制台输入，供菜单和日志查看会话复用。"""

    def __init__(self, ui: IConsoleOutput) -> None:
        """绑定当前界面对象。"""

        self.ui = ui

    def read_key(self, prompt_text: str = "请选择：") -> str:
        """读取单个控制台键；兼容现有 `read_immediate_key` 的注入点。"""

        return read_immediate_key(self.ui, prompt_text)

    def read_line_number(
        self,
        prompt_text: str = "跳转到行号: ",
        on_invalid: Optional[Callable[[], None]] = None,
    ) -> Optional[int]:
        """读取数字输入；兼容现有 `read_line_number` 的测试注入点。"""

        return read_line_number(self.ui, prompt_text, on_invalid)


# 日志查看页

class LogViewReader:
    """增量读取追加型日志，实时跟随时不重复读取已显示内容。"""

    def __init__(self, log_path: Path) -> None:
        """打开追加型日志文件，并预读现有全部内容。"""

        self._log_path = log_path
        self._handle = log_path.open("rb")
        self._lines: List[str] = []
        self._buffer = ""
        self._offset = 0
        self._reset()

    def _reset(self) -> None:
        """文件被截断时从头全量重读，替换已有行列表与半行缓冲。"""

        self._handle.seek(0)
        raw = self._handle.read()
        self._offset = len(raw)
        self._lines = []
        self._buffer = ""
        self._append_text(raw.decode("utf-8", errors="replace"))

    def _append_text(self, text: str) -> None:
        """把新增文本切分为完整行与末尾半行；半行等待下一次补齐。"""

        combined = self._buffer + text
        parts = combined.split("\n")
        self._lines.extend(part.rstrip("\r") for part in parts[:-1])
        self._buffer = parts[-1]

    def lines(self) -> List[str]:
        """返回全部已读行；末尾未换行的半行也参与显示。

        无半行缓冲时直接共享内部列表，避免跟随刷新时整表拷贝；
        调用方只能读取，不能修改返回的列表。
        """

        if self._buffer:
            return self._lines + [self._buffer]
        return self._lines

    def update(self) -> bool:
        """读取新增字节并更新行列表；文件被截断时全量重置。"""

        file_size = os.fstat(self._handle.fileno()).st_size
        if file_size < self._offset:
            # 日志被外部截断时整文件重读，避免偏移越过文件尾。
            self._reset()
            return True
        if file_size == self._offset:
            return False
        self._handle.seek(self._offset)
        raw = self._handle.read(file_size - self._offset)
        self._offset = file_size
        self._append_text(raw.decode("utf-8", errors="replace"))
        return True

    def close(self) -> None:
        """释放日志文件句柄。"""

        self._handle.close()


class EmptyLogReader:
    """没有任何日志时使用的空阅读器，仍可进入查看页显示占位提示。"""

    def lines(self) -> List[str]:
        """返回空行列表。"""

        return []

    def update(self) -> bool:
        """空阅读器没有可读取内容。"""

        return False

    def close(self) -> None:
        """空阅读器无需释放资源。"""

        pass


def open_log_reader(
    controller: DdnsController,
    log_path: Path,
    current_kind: str,
) -> Tuple[object, Path, str]:
    """按当前日志路径打开阅读器；文件不存在时回退最新日志，仍无则空阅读器。"""

    if log_path.is_file():
        return LogViewReader(log_path), log_path, current_kind
    fallback = latest_log_file(controller.log_directory)
    if fallback is None:
        return EmptyLogReader(), controller.log_directory, "无日志"
    return LogViewReader(fallback), fallback, "最新历史日志"


def _last_page_start_for(
    total_lines: int, page_lines: int, aligned: bool
) -> int:
    """按是否固定页对齐计算末页起点；空文件与非法页行数回退为 0。"""

    if total_lines <= 0 or page_lines <= 0:
        return 0
    if aligned:
        return ((total_lines - 1) // page_lines) * page_lines
    return max(0, total_lines - page_lines)


def last_page_start(
    total_lines: int, page_lines: int = LOG_VIEW_PAGE_LINES
) -> int:
    """返回翻页模式最后一页（固定 20 行块）的起点行号。"""

    return _last_page_start_for(total_lines, page_lines, aligned=True)


def latest_page_start(
    total_lines: int, page_lines: int = LOG_VIEW_PAGE_LINES
) -> int:
    """返回连续模式末尾一页窗口的起点行号。"""

    return _last_page_start_for(total_lines, page_lines, aligned=False)


def view_last_start(
    mode: str,
    total_lines: int,
    page_lines: int = LOG_VIEW_PAGE_LINES,
) -> int:
    """按查看模式返回末页起点：翻页模式用固定页，连续模式用尾窗。"""

    if mode == LOG_VIEW_MODE_PAGING:
        return last_page_start(total_lines, page_lines)
    return latest_page_start(total_lines, page_lines)


def log_page_count(
    total_lines: int, page_lines: int = LOG_VIEW_PAGE_LINES
) -> int:
    """按固定页行数计算总页数；空文件也视为 1 页。"""

    if total_lines <= 0 or page_lines <= 0:
        return 1
    return (total_lines + page_lines - 1) // page_lines


def log_view_action(key: str) -> Optional[str]:
    """把查看页按键归一化为动作；未知键返回 None。"""

    return LOG_VIEW_KEY_ACTIONS.get(key.casefold())


def apply_log_view_action(
    action: str,
    total_lines: int,
    page_start: int,
    follow: bool,
    page_lines: int = LOG_VIEW_PAGE_LINES,
    mode: str = LOG_VIEW_MODE_FOLLOW,
) -> Tuple[int, bool]:
    """按键后返回新的 (page_start, follow)；next 不改变跟随，end 恢复末尾跟随。

    mode 决定末页语义：连续模式按末尾一页窗口，翻页模式按固定页块。
    """

    last_start = view_last_start(mode, total_lines, page_lines)
    if action == "next":
        # 下页不改变跟随状态，即使到达末页也由用户决定是否跟随。
        page_start = min(page_start + page_lines, last_start)
        return page_start, follow
    elif action == "prev":
        page_start = max(0, page_start - page_lines)
        if page_start < last_start:
            follow = False
    elif action == "home":
        page_start = 0
        if page_start < last_start:
            follow = False
    elif action == "end":
        # 只有 end/切换末尾跟随能恢复跟随；翻页只可能暂停，不会自动恢复。
        page_start = last_start
        follow = True
    else:
        return page_start, follow
    return page_start, follow


def _read_raw_console_key() -> str:
    """读取单个控制台键并归一化；Ctrl+C 抛 KeyboardInterrupt。"""

    key = msvcrt.getwch()
    if key in ("\x00", "\xe0"):
        # 功能键首字节后还有扫描码第二字节；按 Windows 键盘映射统一为页面键名。
        second = msvcrt.getwch()
        return {
            "I": "PageUp",
            "Q": "PageDown",
            "G": "Home",
            "O": "End",
        }.get(second, "")
    if key == "\x1b":
        return "Escape"
    if key == "\x03":
        raise KeyboardInterrupt
    return key


def log_view_banner_items() -> Tuple[Tuple[str, str, str], ...]:
    """返回查看页第一行横幅的按键项，严格按 LOG_VIEW_BANNER_ACTIONS 顺序。"""

    by_action = {
        action: (keys, label)
        for action, keys, _hidden, _word, label in LOG_VIEW_KEY_MAPPINGS
    }
    return tuple(
        (action, by_action[action][0], by_action[action][1])
        for action in LOG_VIEW_BANNER_ACTIONS
    )


def log_view_banner_parts() -> Tuple[Tuple[str, str], ...]:
    """返回横幅彩色分段；首项青色，其余按动作弱化级别着色。"""

    items = log_view_banner_items()
    prefix = f"查看日志中 | 每页 {LOG_VIEW_PAGE_LINES} 行 | "
    if not items:
        return ((prefix.rstrip(" | "), "bright_black"),)
    parts = [(prefix + f"[{items[0][1]}] {items[0][2]} |", "cyan")]
    previous_color = "cyan"
    for index, (action, keys, label) in enumerate(items[1:]):
        color = (
            "dim_bright_black"
            if action in LOG_VIEW_DIM_BANNER_ACTIONS
            else "bright_black"
        )
        if index > 0:
            parts.append((" |", previous_color))
        parts.append((f" [{keys}] {label}", color))
        previous_color = color
    return tuple(parts)


def log_view_footer() -> str:
    """按按键映射配置生成查看页页脚纯文本；返回提示由第一行横幅承担。"""

    return ", ".join(
        f"[{keys}] {label}" for keys, label in log_view_footer_items()
    )


def log_view_footer_items() -> Tuple[Tuple[str, str], ...]:
    """返回页脚按键项；横幅动作由 LOG_VIEW_BANNER_ACTIONS 排除。"""

    return tuple(
        (keys, label)
        for _action, keys, _hidden, _word, label in LOG_VIEW_KEY_MAPPINGS
        if _action not in LOG_VIEW_BANNER_ACTIONS
    )


def render_log_view_footer(ui: IConsoleOutput) -> str:
    """按配置渲染页脚；逗号用灰色弱化，与黄色按键提示区分。"""

    comma = ui.colorize(",", "bright_black")
    return (comma + " ").join(
        ui.colorize(f"[{keys}] {label}", "yellow")
        for keys, label in log_view_footer_items()
    )


def render_log_viewer(
    controller: DdnsController,
    ui: IConsoleOutput,
    log_path: Path,
    log_kind: str,
    lines: Sequence[str],
    page_start: int,
    follow: bool,
    mode: str = LOG_VIEW_MODE_FOLLOW,
    operation_result: Optional[str] = None,
) -> None:
    """绘制查看页：顶部横幅、灰色元信息、默认色正文、黄色按键提示。

    页头以“连续模式 / 翻页模式”标明当前模式；两种模式都标注行区间，
    翻页模式额外显示固定页号，并固定提示每页显示行数；正文补齐到整页行数。
    operation_result 为常驻操作结果行内容，没有操作时显示占位符。
    整帧一次写入降低重绘成本；每次绘制与菜单一致，全屏清屏并清滚动历史。
    """

    total_lines = len(lines)
    page_total = log_page_count(total_lines, LOG_VIEW_PAGE_LINES)
    follow_label = "末尾跟随中" if follow else "暂停末尾跟随"
    follow_color = "cyan" if follow else "yellow"

    paging_active = mode == LOG_VIEW_MODE_PAGING
    mode_text = (
        ui.colorize(
            "连续模式",
            "cyan" if not paging_active else "bright_black",
        )
        + ui.colorize(" / ", "bright_black")
        + ui.colorize(
            "翻页模式",
            "cyan" if paging_active else "bright_black",
        )
    )

    # 两种模式统一显示总行数与当页行区间，翻页模式再附加固定页号。
    meta_text = f" 总行数: {total_lines} | "
    if total_lines > 0:
        first_line = page_start + 1
        last_line = min(page_start + LOG_VIEW_PAGE_LINES, total_lines)
        meta_text += f"当页行区间: {first_line}-{last_line} | "
    else:
        meta_text += "暂无日志内容 | "
    if paging_active:
        current_page = page_start // LOG_VIEW_PAGE_LINES + 1
        meta_text += f"页 {current_page}/{page_total} | "
    info_line = ui.colorize(meta_text, "bright_black") + ui.colorize(
        follow_label, follow_color
    )

    # 末页不足整页时补空行；空日志也固定输出整页行数，保持正文区与页脚位置稳定。
    visible_lines = list(lines[page_start : page_start + LOG_VIEW_PAGE_LINES])
    if visible_lines:
        visible_lines.extend([""] * (LOG_VIEW_PAGE_LINES - len(visible_lines)))
        body = "\n".join(visible_lines)
    else:
        body = "\n".join([""] * LOG_VIEW_PAGE_LINES)

    footer = render_log_view_footer(ui)
    file_path_text = controller.display_path(log_path)
    if log_kind == "无日志":
        file_path_text += "/"
    log_file_line = ui.colorize(
        f" {log_kind} | 文件: \"{file_path_text}\"",
        "bright_black",
    )
    if log_kind == "无日志":
        log_file_line += ui.colorize("（日志文件夹为空）", "yellow")
    separator_color = "yellow" if log_kind == "无日志" else "bright_black"
    log_file_line += ui.colorize(" |", separator_color)
    log_file_line += ui.colorize(
        f" 日志文件保留上限: {LOG_KEEP_COUNT} 份"
        f" | 当前日志份数: {len(_list_log_files(controller.log_directory))} 份",
        "dim_bright_black",
    )
    banner_text = "".join(
        ui.colorize(text, color)
        for text, color in log_view_banner_parts()
    )
    frame_parts = [
        banner_text,
        log_file_line,
        info_line,
        " " + mode_text,
    ]
    if operation_result:
        frame_parts.append(
            ui.colorize(" 操作结果：", "bright_black")
            + ui.colorize(operation_result, "yellow")
        )
    else:
        frame_parts.append(
            ui.colorize(" 操作结果：", "bright_black")
            + ui.colorize("-", "bright_black")
        )
    frame_parts.extend(
        [
            ui.colorize("-" * 60, "bright_black"),
            body,
            ui.colorize("-" * 60, "bright_black"),
            footer,
        ]
    )
    frame = "\n".join(frame_parts) + "\n"

    sys.stdout.write(clear_screen_prefix() + frame)
    sys.stdout.flush()


class LogViewerState:
    """日志查看页的状态对象，集中管理阅读器、页面位置和操作结果。"""

    def __init__(
        self,
        reader: object,
        log_path: Path,
        log_kind: str,
        mode: str,
        page_start: int,
        follow: bool,
        on_reader_replaced: Optional[Callable[..., None]] = None,
    ) -> None:
        """保存查看页初始状态并读取当前正文行。"""

        self.reader = reader
        self.log_path = log_path
        self.log_kind = log_kind
        self.mode = mode
        self.page_start = page_start
        self.follow = follow
        self.operation_result: Optional[str] = None
        self.lines: List[str] = reader.lines()
        self.on_reader_replaced = on_reader_replaced

    def replace_reader(self, reader: object) -> None:
        """清理后替换阅读器，并同步当前行列表与外部 reader holder。"""

        self.reader = reader
        self.lines = reader.lines()
        if self.on_reader_replaced is not None:
            self.on_reader_replaced(reader)


class LogViewerSession:
    """日志查看页的标准库 TUI 会话，负责渲染、按键分发和轮询刷新。"""

    def __init__(
        self,
        controller: DdnsController,
        ui: IConsoleOutput,
        state: LogViewerState,
        next_key: Callable[[], Optional[str]],
        refresh: Callable[[], Tuple[bool, Optional[ActionResult]]],
        poll_seconds: float,
    ) -> None:
        """绑定控制器、界面、状态和调用方注入的输入/刷新器。"""

        self.controller = controller
        self.ui = ui
        self.state = state
        self.next_key = next_key
        self.refresh = refresh
        self.poll_seconds = poll_seconds
        self.input = TuiInput(ui)

    def _render(self, message: Optional[str] = None) -> None:
        """按当前状态重绘查看页整帧；message 会更新常驻操作结果。"""

        if message is not None:
            self.state.operation_result = message
        render_log_viewer(
            self.controller,
            self.ui,
            self.state.log_path,
            self.state.log_kind,
            self.state.lines,
            self.state.page_start,
            self.state.follow,
            self.state.mode,
            self.state.operation_result,
        )

    def _apply_reader_refresh(self, changed: bool) -> None:
        """按最新日志内容校正 page_start 与 follow；无变化时不改动。"""

        if not changed:
            return
        self.state.lines = self.state.reader.lines()
        if self.state.follow:
            self.state.page_start, self.state.follow = apply_log_view_action(
                "end",
                len(self.state.lines),
                self.state.page_start,
                self.state.follow,
                LOG_VIEW_PAGE_LINES,
                self.state.mode,
            )
        else:
            last_start = view_last_start(
                self.state.mode, len(self.state.lines), LOG_VIEW_PAGE_LINES
            )
            self.state.page_start = min(self.state.page_start, last_start)
            # 暂停状态由用户显式切换；刷新只夹紧页面，不自动恢复跟随。

    def _render_digits_invalid(self) -> None:
        """跳行/跳页输入遇到非数字键时显示统一整帧提示。"""

        self.state.operation_result = LOG_VIEW_DIGITS_ONLY_MESSAGE
        self._render()

    def _handle_action(self, action: Optional[str]) -> Optional[ActionResult]:
        """处理一条查看页按键动作；返回错误结果时结束当前会话。"""

        state = self.state
        if action == "return":
            return ActionResult("已退出日志查看。", "cyan")
        if action == "clear_result":
            # 手动清除常驻操作结果；没有结果时不额外重绘。
            if state.operation_result is not None:
                state.operation_result = None
                self._render()
            return None
        if action == "toggle_mode":
            was_following = state.follow
            # 切到翻页模式先对齐到所在页块；跟随中则落回目标模式末页并继续跟随，
            # 暂停中则保留当前页面并保持暂停。
            if state.mode == LOG_VIEW_MODE_PAGING:
                state.mode = LOG_VIEW_MODE_FOLLOW
                mode_result = "已切换为连续模式"
            else:
                state.mode = LOG_VIEW_MODE_PAGING
                mode_result = "已切换为翻页模式"
                state.page_start = (
                    state.page_start // LOG_VIEW_PAGE_LINES
                ) * LOG_VIEW_PAGE_LINES
            if was_following:
                state.page_start = view_last_start(
                    state.mode, len(state.lines), LOG_VIEW_PAGE_LINES
                )
                state.follow = True
            else:
                state.follow = False
            self._render(mode_result)
            return None
        if action == "jump":
            state.operation_result = LOG_VIEW_JUMPING_MESSAGE
            self._render()
            target = self.input.read_line_number()
            if target is None:
                state.operation_result = None
                self._render()
                return None
            message = None
            if len(state.lines) == 0:
                message = "日志暂无内容"
            elif 1 <= target <= len(state.lines):
                target_index = target - 1
                # 翻页模式跳到目标行所在页块；连续模式把目标行纳入末尾窗口内。
                if state.mode == LOG_VIEW_MODE_PAGING:
                    state.page_start = (
                        target_index // LOG_VIEW_PAGE_LINES
                    ) * LOG_VIEW_PAGE_LINES
                else:
                    state.page_start = min(
                        target_index,
                        latest_page_start(
                            len(state.lines), LOG_VIEW_PAGE_LINES
                        ),
                    )
                state.follow = (
                    state.page_start
                    >= view_last_start(
                        state.mode, len(state.lines), LOG_VIEW_PAGE_LINES
                    )
                )
                message = f"已跳转到第 {target} 行"
            else:
                message = (
                    f"行号 {target} 不存在，有效范围 1-{len(state.lines)}"
                )
            self._render(message)
            return None
        if action == "jump_page":
            if state.mode != LOG_VIEW_MODE_PAGING:
                self._render("提示：跳页仅翻页模式可用")
                return None
            state.operation_result = LOG_VIEW_JUMP_PAGE_MESSAGE
            self._render()
            # 跳页仅翻页模式生效；跳到末页时恢复跟随，否则保持暂停。
            page = self.input.read_line_number("跳转到页码: ")
            if page is None:
                state.operation_result = None
                self._render()
                return None
            message = None
            if len(state.lines) == 0:
                message = "日志暂无内容"
            else:
                page_total = log_page_count(
                    len(state.lines), LOG_VIEW_PAGE_LINES
                )
                if 1 <= page <= page_total:
                    state.page_start = (page - 1) * LOG_VIEW_PAGE_LINES
                    state.follow = (
                        state.page_start
                        >= view_last_start(
                            state.mode, len(state.lines), LOG_VIEW_PAGE_LINES
                        )
                    )
                    message = f"已跳转到第 {page} 页"
                else:
                    message = (
                        f"页码 {page} 不存在，有效范围 1-{page_total}"
                    )
            self._render(message)
            return None
        if action == "toggle_follow":
            # 开启末尾跟随会回到当前模式末页；关闭只暂停跟随，页面保持不动。
            if state.follow:
                state.follow = False
                follow_result = "已暂停末尾跟随"
            else:
                state.page_start, state.follow = apply_log_view_action(
                    "end",
                    len(state.lines),
                    state.page_start,
                    state.follow,
                    LOG_VIEW_PAGE_LINES,
                    state.mode,
                )
                follow_result = "已开启末尾跟随"
            self._render(follow_result)
            return None
        if action == "refresh_log":
            # 手动刷新与自动轮询共用同一套校正逻辑，只显示一次状态提示。
            changed, error = self.refresh()
            if error is not None:
                return error
            self._apply_reader_refresh(changed)
            self._render("已刷新日志内容" if changed else "日志内容无变化")
            return None
        if action == "log_directory":
            result = invoke_menu_action(self.controller.open_log_directory)
            self._render(result.text)
            return None
        if action == "clean_logs":
            keep_count = self.input.read_line_number(
                "保留日志份数: ",
                on_invalid=self._render_digits_invalid,
            )
            if keep_count is None:
                self._render("已取消日志清理")
                return None
            # 先释放当前文件句柄，否则 Windows 无法删除正在查看的日志。
            state.reader.close()
            result = invoke_menu_action(
                lambda: self.controller.clean_old_logs_result(keep_count)
            )
            try:
                # 清理后按原路径重新打开；文件已被删则回退最新日志或空占位。
                reopened_reader, state.log_path, state.log_kind = (
                    open_log_reader(
                        self.controller, state.log_path, state.log_kind
                    )
                )
            except (OSError, DdnsError):
                reopened_reader = EmptyLogReader()
                state.log_path = self.controller.log_directory
                state.log_kind = "无日志"
            state.replace_reader(reopened_reader)
            # 清理后文件集合变化，回到当前模式末页并恢复跟随，避免停留在失效页面。
            state.page_start = view_last_start(
                state.mode, len(state.lines), LOG_VIEW_PAGE_LINES
            )
            state.follow = True
            self._render(result.text)
            return None
        if action is None:
            # 未知按键直接显示统一无效提示，不保留上一次操作结果。
            state.operation_result = LOG_VIEW_INVALID_KEY_MESSAGE
            self._render()
            return None
        next_start, next_follow = apply_log_view_action(
            action,
            len(state.lines),
            state.page_start,
            state.follow,
            LOG_VIEW_PAGE_LINES,
            state.mode,
        )
        if (next_start, next_follow) != (state.page_start, state.follow):
            # 翻页动作统一走同一套夹紧/跟随规则；反馈始终显示，页面无变化也重绘。
            state.page_start, state.follow = next_start, next_follow
        self._render(
            {
                "prev": "已上页",
                "next": "已下页",
                "home": "已回开头",
            }[action]
        )
        return None

    def run(self) -> ActionResult:
        """运行日志查看页事件循环，直到用户返回或刷新失败。"""

        self._render()
        while True:
            key = self.next_key()
            if key is not None:
                action = log_view_action(key)
                result = self._handle_action(action)
                if result is not None:
                    return result
                continue

            # 无按键时按轮询间隔刷新日志；新增内容会按当前模式夹紧或跟随。
            changed, error = self.refresh()
            if error is not None:
                return error
            if changed:
                self._apply_reader_refresh(True)
                self._render()
            if self.poll_seconds > 0:
                time.sleep(self.poll_seconds)


def _run_log_view_loop(
    controller: DdnsController,
    ui: IConsoleOutput,
    log_path: Path,
    log_kind: str,
    reader: LogViewReader,
    page_start: int,
    follow: bool,
    mode: str,
    next_key: Callable[[], Optional[str]],
    refresh: Callable[[], Tuple[bool, Optional[ActionResult]]],
    poll_seconds: float,
    on_reader_replaced: Optional[Callable[..., None]] = None,
) -> ActionResult:
    """统一查看页循环；按键提供器与刷新器由调用方按输入模式注入。"""

    state = LogViewerState(
        reader,
        log_path,
        log_kind,
        mode,
        page_start,
        follow,
        on_reader_replaced,
    )
    session = LogViewerSession(
        controller,
        ui,
        state,
        next_key,
        refresh,
        poll_seconds,
    )
    return session.run()





def view_runtime_log(
    controller: DdnsController,
    state: DdnsState,
    ui: IConsoleOutput,
) -> ActionResult:
    """进入运行时日志查看页；无当前日志时回退到最新历史日志，无日志时显示空占位。"""

    try:
        reader, log_path, log_kind = open_log_reader(
            controller,
            (
                state.log_path
                if state.log_path is not None
                else controller.log_directory
            ),
            "当前运行时日志",
        )
    except OSError as exc:
        return ActionResult(f"[错误] 无法读取日志：{exc}", "red")
    except DdnsError as exc:
        return ActionResult(f"[错误] {exc}", "red")

    page_start = latest_page_start(len(reader.lines()), LOG_VIEW_PAGE_LINES)
    follow = True
    reader_holder = {"reader": reader}

    def replace_reader(new_reader) -> None:
        """清理后替换当前日志阅读器，自动刷新也跟随新对象。"""

        reader_holder["reader"] = new_reader

    try:
        if sys.stdout.isatty() and msvcrt is not None:
            def next_key() -> Optional[str]:
                """返回有按键时的归一化键；无按键返回 None 供主循环轮询。"""

                if msvcrt.kbhit():
                    return _read_raw_console_key()
                return None

            def refresh() -> Tuple[bool, Optional[ActionResult]]:
                """增量读取当前日志；清理替换后也读最新阅读器，失败转为可展示错误。"""

                try:
                    return reader_holder["reader"].update(), None
                except OSError as exc:
                    return False, ActionResult(
                        f"[错误] 无法读取日志：{exc}", "red"
                    )
            poll_seconds = LOG_VIEW_POLL_SECONDS
        else:
            def next_key() -> Optional[str]:
                """非 TTY 下逐个读取按键；输入流关闭时按退出处理。"""

                key = read_immediate_key(ui, "按键：")
                # 输入流关闭时 read_immediate_key 回退为 Q，这里同样视为返回，避免空转。
                return "x" if key.casefold() == "q" else key

            def refresh() -> Tuple[bool, Optional[ActionResult]]:
                """非 TTY 模式不轮询日志，始终返回无变化。"""

                return False, None

            poll_seconds = 0.0

        return _run_log_view_loop(
            controller,
            ui,
            log_path,
            log_kind,
            reader,
            page_start,
            follow,
            LOG_VIEW_MODE_FOLLOW,
            next_key,
            refresh,
            poll_seconds,
            replace_reader,
        )
    except KeyboardInterrupt:
        return ActionResult("已退出日志查看。", "cyan")
    finally:
        # 关闭当前阅读器；清理后替换过的新对象也由 reader_holder 统一管理。
        reader_holder["reader"].close()


# 按键处理与交互入口

def read_line_number(
    ui: IConsoleOutput,
    prompt_text: str = "跳转到行号: ",
    on_invalid: Optional[Callable[[], None]] = None,
) -> Optional[int]:
    """交互读取目标行号；Esc、X 或空输入返回 None。

    on_invalid 用于触发与查看页主循环一致的全屏无效按键提示。
    """

    ui.prompt(prompt_text)

    # 与查看页主循环一致：只要标准输出是控制台就用 msvcrt 直接读键，
    # 避免 stdin 被包装后误落到 readline 分支。
    if sys.stdout.isatty() and msvcrt is not None:
        try:
            # 数字逐键输入：Esc/X 取消，回车提交，退格删除；其他键触发统一无效提示。
            digits = []
            while True:
                key = _read_raw_console_key()
                if key == "Escape" or key.casefold() == "x":
                    ui.echo("")
                    return None
                if key in ("\r", "\n"):
                    break
                if key == "\x08":
                    if digits:
                        digits.pop()
                        ui.prompt("\b \b")
                elif key.isdigit():
                    digits.append(key)
                    ui.prompt(key)
                elif on_invalid is not None:
                    on_invalid()
                    ui.prompt(prompt_text + "".join(digits))
            ui.echo("")
            if digits:
                return int("".join(digits))
            return None
        except OSError:
            pass

    fallback_value = sys.stdin.readline()
    # 非控制台回退到逐行读取；空输入与非法数字都按取消处理。
    ui.echo("")
    if fallback_value == "":
        return None
    fallback_value = fallback_value.strip()
    if not fallback_value:
        return None
    try:
        return int(fallback_value)
    except ValueError:
        return None


def read_immediate_key(
    ui: IConsoleOutput, prompt_text: str = "请选择："
) -> str:
    """在真实控制台读取单键；输入重定向时回退到逐行读取。"""

    if sys.stdin.isatty() and msvcrt is not None:
        try:
            key = _read_raw_console_key()
        except OSError:
            pass
        else:
            if key == "":
                return ""
            if key == "Escape":
                return "Escape"
            return key

    ui.prompt(prompt_text)
    fallback_value = sys.stdin.readline()
    if fallback_value == "":
        # 输入流关闭时回退为 Q，调用方约定将其视为退出，避免无限空转。
        ui.echo("")
        return "Q"
    fallback_value = fallback_value.strip()
    if fallback_value.casefold() == "esc":
        return "Escape"
    return fallback_value


def invoke_menu_action(
    action: Callable[[], ActionResult]
) -> ActionResult:
    """统一将菜单动作的结果或异常转换为可展示的文本与颜色。"""

    try:
        return action()
    except DdnsError as exc:
        return ActionResult(f"[错误] {exc}", "red")


class MenuActionDispatcher:
    """主菜单动作分发器；把按键对应关系和实际控制操作解耦。"""

    def __init__(self, controller: DdnsController, ui: IConsoleOutput) -> None:
        """绑定控制器与界面，并建立动作名到处理器的映射。"""

        self.controller = controller
        self.ui = ui
        self._handlers = {
            "background": lambda state, last_result: invoke_menu_action(
                lambda: self.controller.start(hidden=True)
            ),
            "foreground": lambda state, last_result: invoke_menu_action(
                lambda: self.controller.start(hidden=False)
            ),
            "stop": lambda state, last_result: invoke_menu_action(
                self.controller.stop
            ),
            "management_page": lambda state, last_result: invoke_menu_action(
                self.controller.open_management_page
            ),
            "log_directory": lambda state, last_result: invoke_menu_action(
                self.controller.open_log_directory
            ),
            "view_log": lambda state, last_result: invoke_menu_action(
                lambda: view_runtime_log(self.controller, state, self.ui)
            ),
            "refresh": lambda state, last_result: ActionResult(
                f"状态已核对并刷新：{state.title}", "cyan"
            ),
        }

    def dispatch(
        self,
        action_name: str,
        state: DdnsState,
        last_result: Optional[ActionResult],
    ) -> ActionResult:
        """执行指定菜单动作并返回可展示结果。"""

        return self._handlers[action_name](state, last_result)


class MainMenuSession:
    """主菜单标准库 TUI 会话，统一管理渲染、输入和动作分发。"""

    def __init__(self, controller: DdnsController, ui: IConsoleOutput) -> None:
        """创建菜单会话及其共享输入、动作分发器。"""

        self.controller = controller
        self.ui = ui
        self.input = TuiInput(ui)
        self.dispatcher = MenuActionDispatcher(controller, ui)

    def run(self) -> int:
        """运行主菜单事件循环；调用方负责持有单实例互斥量。"""

        last_result: Optional[ActionResult] = None
        while True:
            try:
                current_state = self.controller.refresh_state()
                self.ui.render(current_state, self.controller, last_result)
                choice = self.input.read_key()
                action_name = MENU_ACTIONS.get(choice.casefold())

                # 外部操作统一由 invoke_menu_action 执行，异常转为界面错误，不让菜单崩溃。
                if action_name is None:
                    last_result = ActionResult(
                        MENU_INVALID_KEY_MESSAGE, "yellow"
                    )
                elif action_name == "quit":
                    return 0
                elif action_name == "clear_result":
                    last_result = None
                else:
                    last_result = self.dispatcher.dispatch(
                        action_name, current_state, last_result
                    )
            except KeyboardInterrupt:
                return 0
            except Exception as exc:
                # 未预期异常兑底：显示错误但保持菜单可用，不让脚本直接退出。
                # 错误同时打到 stderr（render 可能在异常前未执行），并短暂休眠
                # 避免持续失败时形成无输出热循环。
                print(f"[未预期错误] {exc}", file=sys.stderr)
                time.sleep(0.5)
                last_result = ActionResult(f"[未预期错误] {exc}", "red")


def run_menu(controller: DdnsController, ui: IConsoleOutput) -> int:
    """运行标准库 TUI 主菜单；调用方负责持有单实例互斥量。"""

    return MainMenuSession(controller, ui).run()


def main(arguments: Optional[Sequence[str]] = None) -> int:
    """分派私有转发模式或交互菜单；转发器不获取菜单单实例互斥量。"""

    if os.name != "nt":
        print("[错误] 该脚本仅支持 Windows。", file=sys.stderr)
        return 1

    command_line = list(sys.argv[1:] if arguments is None else arguments)
    if command_line[:1] == [RELAY_SWITCH]:
        return run_log_relay(command_line[1:])

    script_directory = application_directory()
    controller = DdnsController(script_directory)
    ui = ConsoleUI()

    try:
        with SingleInstanceMutex(script_directory):
            return run_menu(controller, ui)
    except DdnsError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
