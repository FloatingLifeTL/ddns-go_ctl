"""控制器接口边界、配置单一来源与依赖注入的专项测试。

运行（仓库根目录）：python -m unittest discover -s tests -p "test_*.py" -v
单文件运行：python tests/test_interface_isolation.py
测试基线：Windows x86_64、CPython 3.11+，仅使用标准库；实测版本见源码顶部。

验证三件事：
1. 以 isinstance 检查所列实现的 Protocol 成员存在性，不代替签名与行为验证。
2. Settings 仍引用模块级 DDNS_GO_EXTRA_ARGS，mock 拦截路径不变。
3. DdnsController 的 keyword-only 依赖注入点可换源（mock 实现生效），
   且不注入时默认装配真实实现、行为不变。

与 test_ddns_go_ctl.py 分别按路径加载同一份源码，不执行其交互入口。
依赖注入用例以 mock 隔离进程操作；默认实现冒烟用例会查询当前解释器的真实
进程路径。文件写入限于临时目录，不启动 DDNS-GO，也不触发动态 DNS 更新。
最后编辑：2026-09-11-Fri。
"""

from __future__ import annotations

from datetime import datetime
import importlib.util
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "ddns-go_ctl.py"


def load_controller_module():
    """按路径加载带连字符的脚本文件（与主测试套件一致）。"""

    spec = importlib.util.spec_from_file_location("ddns_go_ctl", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    # 与主测试一致：先注册模块，dataclass 才能按模块名解析注解。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ctl = load_controller_module()


def make_running_state(**overrides):
    """构造默认合法的 running 状态；可覆盖字段，虚拟 PID 不对应实际启动进程。"""

    fields = dict(
        schema_version=ctl.RUNTIME_SCHEMA_VERSION,
        launch_id=str(uuid.uuid4()),
        status="running",
        mode="background",
        ddns_pid=4321,
        relay_pid=5432,
        started_at=datetime.now().astimezone().isoformat(),
        log_file="logs/ddns-go_20260803-000001.log",
    )
    fields.update(overrides)
    return ctl.RuntimeState(**fields)


class TemporaryDirectoryMixin:
    """为注入测试隔离文件写入；需与提供 addCleanup 的 unittest.TestCase 混用。"""

    def make_directory(self):
        """返回独立临时目录，并在用例结束（含断言失败）时清理。"""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)


class ProtocolConformanceTests(unittest.TestCase):
    """检查默认实现的协议成员、配置委托和注入参数位置；不在此执行真实启停。"""

    def test_infrastructure_implementations_satisfy_protocols(self):
        self.assertIsInstance(ctl.Win32ProcessOperations(), ctl.IProcessOperations)
        self.assertIsInstance(ctl.NetstatPortQuery(), ctl.IPortQuery)
        self.assertIsInstance(
            ctl.FileRuntimeStateStore(Path("x")), ctl.IRuntimeStateStore
        )
        self.assertIsInstance(ctl.FileLogManager(Path("x")), ctl.ILogManager)
        self.assertIsInstance(ctl.SubprocessSpawner(), ctl.IProcessSpawner)

    def test_ui_implementations_satisfy_protocols(self):
        self.assertIsInstance(ctl.ConsoleUI(), ctl.IConsoleOutput)
        self.assertIsInstance(ctl.TuiInput(ctl.ConsoleUI()), ctl.IKeyReader)

    def test_controller_is_a_render_context(self):
        # UI 的 render 依赖 IRenderContext 而非具体控制器：控制器应结构满足。
        controller = ctl.DdnsController(Path(tempfile.gettempdir()), port=9876)
        self.assertIsInstance(controller, ctl.IRenderContext)

    def test_tunable_attributes_are_readonly_and_delegate_to_settings(self):
        # 这里检查值同源与拒绝直接赋值；整体替换 Settings 的效果由 SettingsTests 验证。
        controller = ctl.DdnsController(
            Path(tempfile.gettempdir()), port=9876, dns_servers="1.1.1.1,8.8.8.8"
        )
        self.assertEqual(controller.port, 9876)
        self.assertEqual(controller.dns_servers, "1.1.1.1,8.8.8.8")
        self.assertEqual(controller.port, controller.settings.port)
        self.assertEqual(controller.dns_servers, controller.settings.dns_servers)
        with self.assertRaises(AttributeError):
            controller.port = 1

    def test_default_dependencies_are_real_implementations(self):
        controller = ctl.DdnsController(Path(tempfile.gettempdir()))
        self.assertIsInstance(controller.process_ops, ctl.Win32ProcessOperations)
        self.assertIsInstance(controller.port_query, ctl.NetstatPortQuery)
        self.assertIsInstance(controller.state_store, ctl.FileRuntimeStateStore)
        self.assertIsInstance(controller.log_manager, ctl.FileLogManager)
        self.assertIsInstance(controller.spawner, ctl.SubprocessSpawner)

    def test_dependency_arguments_are_keyword_only(self):
        # 8 个位置参数之后是 *；多传一个位置实参应被拒绝，防止调用方误排依赖。
        with self.assertRaises(TypeError):
            ctl.DdnsController(
                Path(tempfile.gettempdir()), None, None, None, None, None,
                None, None, object()
            )


class SettingsTests(unittest.TestCase):
    """Settings 组装启动参数：None 不传，EXTRA_ARGS 仍走模块级可 patch 路径。"""

    def test_none_config_items_are_omitted(self):
        settings = ctl.Settings(
            port=9876,
            sync_interval_seconds=None,
            cache_times=None,
            dns_servers=None,
        )
        arguments = settings.build_start_arguments(
            Path("C:/ddns-go.exe"), Path("C:/cfg.yaml")
        )
        self.assertEqual(arguments[0], str(Path("C:/ddns-go.exe")))
        self.assertEqual(arguments[1], "-l")
        self.assertEqual(arguments[2], ":9876")
        self.assertNotIn("-f", arguments)
        self.assertNotIn("-cacheTimes", arguments)
        self.assertNotIn("-dns", arguments)

    def test_extra_args_reference_module_global_at_call_time(self):
        # 先创建 Settings 再 patch 全局参数，才能排除构造时已缓存参数的实现。
        settings = ctl.Settings(port=9876)
        with mock.patch.object(
            ctl, "DDNS_GO_EXTRA_ARGS", (("-noweb",), ("-skipVerify",))
        ):
            arguments = settings.build_start_arguments(
                Path("C:/ddns-go.exe"), Path("C:/cfg.yaml")
            )
        self.assertIn("-noweb", arguments)
        self.assertIn("-skipVerify", arguments)

    def test_controller_delegates_start_arguments_to_settings(self):
        controller = ctl.DdnsController(Path(tempfile.gettempdir()), port=9876)
        arguments = controller._build_start_arguments()
        self.assertEqual(
            arguments,
            controller.settings.build_start_arguments(
                controller.executable_path, controller.config_path
            ),
        )

    def test_settings_is_the_single_source_of_tunables(self):
        # 单一来源：整体替换 settings 后，只读属性与启动参数立即跟随，无两套状态。
        controller = ctl.DdnsController(Path(tempfile.gettempdir()), port=9876)
        controller.settings = ctl.Settings(port=5432)
        self.assertEqual(controller.port, 5432)
        self.assertEqual(controller._build_start_arguments()[2], ":5432")


class DependencyInjectionTests(TemporaryDirectoryMixin, unittest.TestCase):
    """核对依赖调用参数、返回值和资源释放，防止控制器绕过注入对象。

    启停用例替换状态查询、等待与创建能力，不操作虚拟 PID；
    test_default_controller_uses_real_process_query 刻意保留默认实现，
    作为当前解释器路径查询的 Windows 冒烟测试。
    """

    def test_get_listener_process_ids_delegates_to_injected_port_query(self):
        port_query = mock.Mock()
        port_query.query.return_value = [111, 222]
        controller = ctl.DdnsController(
            self.make_directory(), port=9876, port_query=port_query
        )

        result = controller.get_listener_process_ids()

        port_query.query.assert_called_once_with(9876)
        self.assertEqual(result, [111, 222])

    def test_get_process_path_delegates_to_injected_process_ops(self):
        process_ops = mock.Mock()
        # 不透明哨兵没有系统句柄语义；查询与释放必须原样交回注入对象。
        handle = object()
        process_ops.open_process.return_value = handle
        process_ops.query_process_path.return_value = Path("C:/fake/ddns-go.exe")
        controller = ctl.DdnsController(
            self.make_directory(), process_ops=process_ops
        )

        result = controller.get_process_path(42)

        process_ops.open_process.assert_called_once_with(
            ctl.PROCESS_QUERY_LIMITED_INFORMATION, 42
        )
        process_ops.query_process_path.assert_called_once_with(handle)
        process_ops.close_handle.assert_called_once_with(handle)
        self.assertEqual(result, Path("C:/fake/ddns-go.exe"))

    def test_read_runtime_state_delegates_to_injected_state_store(self):
        state_store = mock.Mock()
        running_state = make_running_state()
        state_store.read.return_value = (True, running_state)
        controller = ctl.DdnsController(
            self.make_directory(), state_store=state_store
        )

        exists, loaded = controller.read_runtime_state()

        state_store.read.assert_called_once_with()
        self.assertTrue(exists)
        # 用身份断言确认控制器原样返回存储提供的对象，而非重新构造一个值相等的状态。
        self.assertIs(loaded, running_state)

    def test_stop_writes_through_injected_state_store(self):
        """模拟受管进程已安全退出，只验证 stopped 状态通过注入存储写回。"""

        running_state = make_running_state()
        state_store = mock.Mock()
        state_store.read.return_value = (True, running_state)
        controller = ctl.DdnsController(
            self.make_directory(), state_store=state_store
        )
        controller_state = controller.new_state(
            "Running",
            "运行中",
            "test",
            "green",
            process_id=4321,
            relay_process_id=5432,
            launch_id=running_state.launch_id,
            target_process_ids=(4321,),
        )

        with (
            mock.patch.object(controller, "get_state", return_value=controller_state),
            mock.patch.object(ctl, "signal_relay_stop", return_value=True),
            mock.patch.object(controller, "wait_for_process_exit", return_value=True),
        ):
            controller.stop()

        state_store.write.assert_called_once()
        written = state_store.write.call_args.args[0]
        self.assertEqual(written.status, "stopped")
        self.assertIsNone(written.ddns_pid)

    def test_clean_old_logs_delegates_to_injected_log_manager(self):
        """固定清理前后列表，仅验证委托参数和结果颜色，不验证实际删除数量。"""

        log_manager = mock.Mock()
        log_manager.list_log_files.return_value = [
            Path("a.log"),
            Path("b.log"),
        ]
        log_manager.prune.return_value = []
        controller = ctl.DdnsController(
            self.make_directory(), log_manager=log_manager
        )

        result = controller.clean_old_logs_result(1)

        log_manager.prune.assert_called_once_with(1, protected_paths=[])
        self.assertEqual(result.color, "green")

    def test_start_delegates_relay_spawn_to_injected_spawner(self):
        """注入假转发器及成功的启动确认，验证后台选项确实交给注入 spawner。"""

        spawner = mock.Mock()
        controller = ctl.DdnsController(self.make_directory(), spawner=spawner)
        # 空 EXE 仅作占位；本用例通过 mock 提供状态并拦截进程创建，不执行它。
        controller.executable_path.touch()
        runtime_state = make_running_state()
        stopped_state = controller.new_state(
            "Stopped", "未运行", "test", "bright_black"
        )
        # 转发器 PID 为 5432，状态中的 DDNS-GO PID 为 4321；监听确认必须使用后者。
        fake_relay = mock.Mock(pid=5432)
        fake_relay.poll.return_value = None
        spawner.spawn.return_value = fake_relay

        with (
            mock.patch.object(controller, "get_state", return_value=stopped_state),
            mock.patch.object(
                controller,
                "_wait_for_relay_state",
                return_value=runtime_state,
            ),
            mock.patch.object(
                controller, "get_listener_process_ids", return_value=[4321]
            ),
        ):
            result = controller.start(hidden=True)

        spawner.spawn.assert_called_once()
        spawned_args, spawn_kwargs = spawner.spawn.call_args
        self.assertEqual(spawn_kwargs["creationflags"], ctl.CREATE_NO_WINDOW)
        self.assertIs(spawn_kwargs["stdout"], ctl.subprocess.DEVNULL)
        self.assertIn(ctl.RELAY_SWITCH, spawned_args[0])
        self.assertEqual(result.text, "DDNS-GO 已以后台模式启动。")

    def test_default_controller_uses_real_process_query(self):
        # 未注入任何依赖时，真实实现仍可用：查询当前解释器进程路径。
        controller = ctl.DdnsController(self.make_directory())
        process_path = controller.get_process_path(os.getpid())
        self.assertIsInstance(process_path, Path)
        self.assertTrue(str(process_path).casefold().endswith("python.exe"))


# 直接运行时执行本文件用例；被 discover 导入时不额外启动一轮测试。
if __name__ == "__main__":
    unittest.main()
