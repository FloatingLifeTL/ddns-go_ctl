"""ddns-go_ctl.py 的标准库测试。

运行：python test_ddns_go_ctl.py
验证解析、运行状态、日志转发与进程核验逻辑：不启动 DDNS-GO 本体，不触发动态
DNS 更新，不读写脚本目录中的真实状态、日志或配置文件（一律使用临时目录）。
最后编辑：2026-08-11-Tue。
"""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "ddns-go_ctl.py"


def load_controller_module():
    """按路径加载带连字符的脚本文件。"""

    spec = importlib.util.spec_from_file_location("ddns_go_ctl", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    # 注册到 sys.modules，否则 dataclass 按模块名回查注解时会失败。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ctl = load_controller_module()


def mapping_hint(mappings, action):
    """按动作名从按键映射生成 `[键] 说明` 文案，测试随配置区同步。"""

    for row in mappings:
        if row[0] == action:
            return f"[{row[1]}] {row[4]}"
    raise AssertionError(f"未在按键映射中找到动作：{action}")


class TtyStream:
    """模拟 TTY stdout；记录每次 write 调用以便断言整帧单次写入。"""

    def __init__(self):
        self.chunks = []

    def write(self, text):
        self.chunks.append(text)

    def flush(self):
        pass

    def isatty(self):
        return True

    def getvalue(self):
        return "".join(self.chunks)


class TemporaryControllerMixin:
    """所有用例都在临时目录上操作，避免碰到现役的运行状态与日志。"""

    def make_controller(self, **kwargs):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return ctl.DdnsController(Path(temporary.name), **kwargs)


class ParseListenerTests(unittest.TestCase):
    """netstat 输出解析：认监听、认双栈、不认 UDP 与其他状态。"""

    def test_ipv4_listener(self):
        lines = ["  TCP    0.0.0.0:9876    0.0.0.0:0    LISTENING    1234"]
        self.assertEqual(ctl.parse_listener_process_ids(lines, 9876), [1234])

    def test_ipv6_listener_is_not_missed(self):
        lines = ["  TCP    [::]:9876    [::]:0    LISTENING    1234"]
        self.assertEqual(ctl.parse_listener_process_ids(lines, 9876), [1234])

    def test_dual_stack_rows_are_deduplicated(self):
        lines = [
            "  TCP    0.0.0.0:9876    0.0.0.0:0    LISTENING    1234",
            "  TCP    [::]:9876       [::]:0       LISTENING    1234",
        ]
        self.assertEqual(ctl.parse_listener_process_ids(lines, 9876), [1234])

    def test_multiple_listeners_are_sorted(self):
        lines = [
            "  TCP    0.0.0.0:9876    0.0.0.0:0    LISTENING    900",
            "  TCP    [::]:9876       [::]:0       LISTENING    100",
        ]
        self.assertEqual(ctl.parse_listener_process_ids(lines, 9876), [100, 900])

    def test_other_ports_are_ignored(self):
        lines = ["  TCP    0.0.0.0:9877    0.0.0.0:0    LISTENING    1234"]
        self.assertEqual(ctl.parse_listener_process_ids(lines, 9876), [])

    def test_port_is_matched_whole_not_by_substring(self):
        # IPv6 地址里可能出现与端口同形的片段，不得据此误判。
        lines = [
            "  TCP    [fdfe:dcba:9876::1]:1377    [fdfe:dcba:9876::91]:443    ESTABLISHED    14052",
        ]
        self.assertEqual(ctl.parse_listener_process_ids(lines, 9876), [])

    def test_non_listening_states_are_ignored(self):
        lines = [
            "  TCP    127.0.0.1:9876    127.0.0.1:1000    ESTABLISHED    1234",
            "  TCP    127.0.0.1:9876    127.0.0.1:1000    TIME_WAIT      0",
        ]
        self.assertEqual(ctl.parse_listener_process_ids(lines, 9876), [])

    def test_udp_and_headers_are_ignored(self):
        lines = [
            "活动连接",
            "  协议  本地地址          外部地址        状态           PID",
            "  UDP    0.0.0.0:9876    *:*                            1234",
            "",
        ]
        self.assertEqual(ctl.parse_listener_process_ids(lines, 9876), [])


class NormalizedPathTests(unittest.TestCase):
    def test_case_and_separators_are_normalized(self):
        left = ctl.normalized_path(Path(r"C:\Temp\Sub\..\ddns-go.exe"))
        right = ctl.normalized_path(Path(r"c:\temp\DDNS-GO.EXE"))
        self.assertEqual(left, right)


class ControllerValidationTests(TemporaryControllerMixin, unittest.TestCase):
    def test_port_range_is_enforced(self):
        for bad_port in (0, 65536, -1):
            with self.subTest(port=bad_port):
                with self.assertRaises(ValueError):
                    self.make_controller(port=bad_port)

    def test_timeouts_must_be_positive(self):
        with self.assertRaises(ValueError):
            self.make_controller(start_timeout_seconds=0)
        with self.assertRaises(ValueError):
            self.make_controller(stop_timeout_seconds=0)

    def test_sync_options_must_be_positive_or_none(self):
        with self.assertRaises(ValueError):
            self.make_controller(sync_interval_seconds=0)
        with self.assertRaises(ValueError):
            self.make_controller(cache_times=0)
        self.make_controller(sync_interval_seconds=None, cache_times=None)

    def test_dns_servers_must_be_nonempty_string_or_none(self):
        with self.assertRaises(ValueError):
            self.make_controller(dns_servers="")
        with self.assertRaises(ValueError):
            self.make_controller(dns_servers="   ")
        self.make_controller(dns_servers="223.5.5.5")
        self.make_controller(dns_servers=None)


class StartArgumentTests(TemporaryControllerMixin, unittest.TestCase):
    """启动参数组装：None 不传，配置项按 DDNS-GO 的参数名传递。"""

    def test_defaults_only_pass_listen_and_config(self):
        controller = self.make_controller(port=9876)
        arguments = controller._build_start_arguments()
        self.assertEqual(arguments[1:], ["-l", ":9876", "-c", str(controller.config_path)])
        self.assertEqual(
            controller.config_path,
            controller.script_directory / "ctl-data" / ".ddns_go_config.yaml",
        )

    def test_sync_options_are_appended_when_set(self):
        controller = self.make_controller(sync_interval_seconds=10, cache_times=180)
        arguments = controller._build_start_arguments()
        self.assertIn("-f", arguments)
        self.assertEqual(arguments[arguments.index("-f") + 1], "10")
        self.assertIn("-cacheTimes", arguments)
        self.assertEqual(arguments[arguments.index("-cacheTimes") + 1], "180")

    def test_dns_servers_are_passed_when_set(self):
        controller = self.make_controller(dns_servers="223.5.5.5,1.1.1.1")
        arguments = controller._build_start_arguments()
        self.assertIn("-dns", arguments)
        self.assertEqual(
            arguments[arguments.index("-dns") + 1], "223.5.5.5,1.1.1.1"
        )

    def test_dns_servers_not_passed_by_default(self):
        controller = self.make_controller()
        arguments = controller._build_start_arguments()
        self.assertNotIn("-dns", arguments)

    def test_daemon_flag_is_never_passed(self):
        # DDNS-GO 的 -d 会 fork 后让父进程退出，PID 追踪将失准，禁止出现。
        controller = self.make_controller(sync_interval_seconds=10, cache_times=180)
        self.assertNotIn("-d", controller._build_start_arguments())

    def test_official_params_are_all_listed(self):
        flags = [
            flag for flag, _value, _supported, _note in ctl.DDNS_GO_PARAMS
        ]
        self.assertEqual(
            flags,
            [
                "-l",
                "-c",
                "-f",
                "-cacheTimes",
                "-dns",
                "-noweb",
                "-skipVerify",
                "-resetPassword",
                "-s",
            ],
        )

    def test_config_path_override_is_used(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        controller = ctl.DdnsController(
            Path(temporary.name), config_path_override="custom.yaml"
        )
        self.assertEqual(
            controller.config_path,
            (controller.script_directory / "custom.yaml").resolve(),
        )
        self.assertIn(
            str(controller.config_path),
            controller._build_start_arguments(),
        )

    def test_extra_args_are_appended(self):
        controller = self.make_controller()
        with mock.patch.object(
            ctl, "DDNS_GO_EXTRA_ARGS", (("-noweb",), ("-skipVerify",))
        ):
            arguments = controller._build_start_arguments()
        self.assertIn("-noweb", arguments)
        self.assertIn("-skipVerify", arguments)

    def test_relay_arguments_select_mode_and_preserve_ddns_command(self):
        controller = self.make_controller()
        started_at = datetime(2026, 8, 3, 7, 10, 1, tzinfo=timezone.utc)
        launch_id = "11111111-2222-4333-8444-555555555555"
        log_path = controller.log_directory / "ddns-go_20260803-071001.log"

        background = controller._build_relay_arguments(
            True, launch_id, log_path, started_at
        )
        foreground = controller._build_relay_arguments(
            False, launch_id, log_path, started_at
        )

        self.assertEqual(background[background.index("--mode") + 1], "background")
        self.assertEqual(foreground[foreground.index("--mode") + 1], "foreground")
        separator = background.index("--")
        self.assertEqual(background[separator + 1], str(controller.executable_path))
        self.assertNotIn("-d", background[separator + 1 :])


class ControllerInvocationTests(unittest.TestCase):
    """冻结版转发器必须能在菜单退出后继续独立运行。"""

    def test_controller_self_path_points_to_source_script(self):
        self.assertEqual(ctl.controller_self_path().name, "ddns-go_ctl.py")

    def test_controller_self_path_points_to_executable_when_frozen(self):
        with mock.patch.object(ctl, "IS_FROZEN", True):
            self.assertEqual(
                ctl.controller_self_path(),
                Path(ctl.sys.executable).resolve(),
            )

    def test_source_mode_inherits_current_environment(self):
        with mock.patch.object(ctl, "IS_FROZEN", False):
            self.assertIsNone(ctl.independent_controller_environment())

    def test_source_mode_reports_own_pid(self):
        with (
            mock.patch.object(ctl.os, "getpid", return_value=111),
            mock.patch.object(ctl.os, "getppid", return_value=222),
            mock.patch.object(ctl, "IS_FROZEN", False),
        ):
            self.assertEqual(ctl.relay_process_identity(), 111)

    def test_frozen_mode_reports_bootloader_parent_pid(self):
        with (
            mock.patch.object(ctl.os, "getpid", return_value=111),
            mock.patch.object(ctl.os, "getppid", return_value=222),
            mock.patch.object(ctl, "IS_FROZEN", True),
        ):
            self.assertEqual(ctl.relay_process_identity(), 222)

    def test_frozen_mode_resets_pyinstaller_environment(self):
        variable_name = ctl.PYINSTALLER_RESET_ENVIRONMENT_NAME
        with (
            mock.patch.object(ctl, "IS_FROZEN", True),
            mock.patch.dict(
                ctl.os.environ,
                {"DDNS_CTL_TEST_VALUE": "preserved", variable_name: "0"},
                clear=True,
            ),
        ):
            environment = ctl.independent_controller_environment()

        self.assertEqual(environment[variable_name], "1")
        self.assertEqual(environment["DDNS_CTL_TEST_VALUE"], "preserved")
        self.assertIsNot(environment, ctl.os.environ)


class RuntimeStateTests(TemporaryControllerMixin, unittest.TestCase):
    """运行状态严格校验并通过临时文件原子替换。"""

    @staticmethod
    def make_state(**changes):
        values = {
            "schema_version": ctl.RUNTIME_SCHEMA_VERSION,
            "launch_id": "11111111-2222-4333-8444-555555555555",
            "status": "running",
            "mode": "background",
            "ddns_pid": 4321,
            "relay_pid": 5432,
            "started_at": "2026-08-03T07:10:01+08:00",
            "log_file": "logs/ddns-go_20260803-071001.log",
            "exit_code": None,
            "error": None,
        }
        values.update(changes)
        return ctl.RuntimeState(**values)

    def test_missing_file(self):
        controller = self.make_controller()
        self.assertEqual(controller.read_runtime_state(), (False, None))

    def test_round_trip_preserves_all_fields(self):
        controller = self.make_controller()
        state = self.make_state()
        ctl.write_runtime_state(controller.runtime_state_path, state)

        exists, loaded = controller.read_runtime_state()

        self.assertTrue(exists)
        self.assertEqual(loaded, state)
        leftover_temporary = list(
            controller.runtime_state_path.parent.glob("*.tmp")
        )
        self.assertEqual(leftover_temporary, [])

    def test_runtime_state_lives_under_run_directory(self):
        controller = self.make_controller()
        self.assertEqual(
            controller.runtime_state_path,
            controller.script_directory
            / "ctl-data"
            / "run"
            / ctl.RUNTIME_STATE_NAME,
        )

    def test_invalid_json_is_reported(self):
        controller = self.make_controller()
        controller.runtime_state_path.parent.mkdir(parents=True, exist_ok=True)
        controller.runtime_state_path.write_text("not-json", encoding="utf-8")
        with self.assertRaises(ctl.DdnsError):
            controller.read_runtime_state()

    def test_non_positive_pid_is_rejected(self):
        raw_state = self.make_state().to_dict()
        raw_state["ddns_pid"] = 0
        with self.assertRaises(ValueError):
            ctl.RuntimeState.from_dict(raw_state)

    def test_log_path_must_stay_directly_under_logs(self):
        raw_state = self.make_state().to_dict()
        raw_state["log_file"] = "logs/../stolen.log"
        with self.assertRaises(ValueError):
            ctl.RuntimeState.from_dict(raw_state)

    def test_stopped_state_keeps_only_field_names(self):
        stopped = ctl.mark_runtime_stopped(self.make_state())

        self.assertEqual(stopped.status, "stopped")
        for field_name in (
            "launch_id",
            "mode",
            "ddns_pid",
            "relay_pid",
            "started_at",
            "log_file",
            "exit_code",
            "error",
        ):
            self.assertIsNone(getattr(stopped, field_name), field_name)
        self.assertEqual(
            ctl.RuntimeState.from_dict(stopped.to_dict()), stopped
        )

    def test_stopped_state_rejects_non_null_information(self):
        raw_state = {
            "schema_version": ctl.RUNTIME_SCHEMA_VERSION,
            "launch_id": None,
            "status": "stopped",
            "mode": None,
            "ddns_pid": None,
            "relay_pid": None,
            "started_at": None,
            "log_file": None,
            "exit_code": 0,
            "error": None,
        }
        with self.assertRaises(ValueError):
            ctl.RuntimeState.from_dict(raw_state)


class LogFileTests(unittest.TestCase):
    def test_default_keep_count_is_15(self):
        self.assertEqual(ctl.LOG_KEEP_COUNT, 15)

    def test_each_launch_reserves_a_distinct_log_name(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        log_directory = Path(temporary.name) / "logs"
        started_at = datetime(2026, 8, 3, 7, 10, 1, tzinfo=timezone.utc)

        first = ctl.reserve_log_file(log_directory, started_at)
        second = ctl.reserve_log_file(log_directory, started_at)

        self.assertEqual(first.name, "ddns-go_20260803-071001.log")
        self.assertEqual(second.name, "ddns-go_20260803-071001-02.log")
        self.assertTrue(first.is_file())
        self.assertTrue(second.is_file())

    def test_log_pattern_aligns_with_reserved_names(self):
        pattern = ctl.LOG_FILE_PATTERN
        self.assertIsNotNone(pattern.fullmatch("ddns-go_20260803-071001.log"))
        self.assertIsNotNone(pattern.fullmatch("ddns-go_20260803-071001-02.log"))
        self.assertIsNone(pattern.fullmatch("ddns-go_20260803-071001-01.log"))
        self.assertIsNone(pattern.fullmatch("ddns-go_20260803-071001-00.log"))

    def test_prune_keeps_newest_logs_and_skips_current(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        log_directory = Path(temporary.name) / "logs"
        log_directory.mkdir()
        for index in range(1, 21):
            path = log_directory / f"ddns-go_20260803-{index:06d}.log"
            path.write_bytes(b"")
        current = log_directory / "ddns-go_20260803-999999.log"
        current.write_bytes(b"")

        failures = ctl.prune_log_files(log_directory, current_path=current)

        remaining = [
            path.name for path in log_directory.glob("ddns-go_*.log")
        ]
        self.assertEqual(failures, [])
        self.assertEqual(len(remaining), 15)
        self.assertIn(current.name, remaining)
        self.assertNotIn("ddns-go_20260803-000001.log", remaining)

    def test_prune_ignores_non_log_files(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        log_directory = Path(temporary.name) / "logs"
        log_directory.mkdir()
        old_log = log_directory / "ddns-go_20260803-000001.log"
        old_log.write_bytes(b"")
        newer_log = log_directory / "ddns-go_20260803-000002.log"
        newer_log.write_bytes(b"")
        other = log_directory / "README.txt"
        other.write_bytes(b"keep")

        ctl.prune_log_files(log_directory, keep_count=1)

        self.assertFalse(old_log.exists())
        self.assertTrue(newer_log.exists())
        self.assertTrue(other.exists())

    def test_clean_old_logs_result_deletes_old_logs(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        controller = ctl.DdnsController(Path(temporary.name))
        controller.log_directory.mkdir(parents=True)
        for index in range(1, 6):
            path = (
                controller.log_directory
                / f"ddns-go_20260803-{index:06d}.log"
            )
            path.write_bytes(b"")

        result = controller.clean_old_logs_result(2)

        remaining = [
            path.name
            for path in controller.log_directory.glob("ddns-go_*.log")
        ]
        self.assertEqual(len(remaining), 2)
        self.assertEqual(result.text, "已按要求清理，保留近 2 份，删除旧 3 份。")
        self.assertEqual(result.color, "green")

    def test_clean_old_logs_result_allows_zero(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        controller = ctl.DdnsController(Path(temporary.name))
        controller.log_directory.mkdir(parents=True)
        for index in range(1, 4):
            path = (
                controller.log_directory
                / f"ddns-go_20260803-{index:06d}.log"
            )
            path.write_bytes(b"")

        result = controller.clean_old_logs_result(0)

        remaining = [
            path.name
            for path in controller.log_directory.glob("ddns-go_*.log")
        ]
        self.assertEqual(remaining, [])
        self.assertIn("删除旧 3 份", result.text)

    def test_clean_old_logs_result_protects_runtime_log(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        controller = ctl.DdnsController(Path(temporary.name))
        controller.log_directory.mkdir(parents=True)
        for index in range(1, 7):
            path = (
                controller.log_directory
                / f"ddns-go_20260803-{index:06d}.log"
            )
            path.write_bytes(b"")

        runtime_log = controller.log_directory / "ddns-go_20260803-000001.log"
        runtime_state = ctl.RuntimeState(
            schema_version=ctl.RUNTIME_SCHEMA_VERSION,
            launch_id=str(uuid.uuid4()),
            status="running",
            mode="background",
            ddns_pid=123,
            relay_pid=456,
            started_at=datetime.now().astimezone().isoformat(),
            log_file="logs/ddns-go_20260803-000001.log",
        )
        ctl.write_runtime_state(controller.runtime_state_path, runtime_state)

        result = controller.clean_old_logs_result(2)

        remaining = [
            path.name
            for path in controller.log_directory.glob("ddns-go_*.log")
        ]
        self.assertTrue(runtime_log.exists())
        self.assertEqual(len(remaining), 2)
        self.assertIn("删除旧 4 份", result.text)
        self.assertIn("运行时日志受保护不删除", result.text)

    def test_clean_old_logs_result_no_protection_hint_when_keep_sufficient(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        controller = ctl.DdnsController(Path(temporary.name))
        controller.log_directory.mkdir(parents=True)
        for index in range(1, 4):
            path = (
                controller.log_directory
                / f"ddns-go_20260803-{index:06d}.log"
            )
            path.write_bytes(b"")

        runtime_log = controller.log_directory / "ddns-go_20260803-000001.log"
        runtime_state = ctl.RuntimeState(
            schema_version=ctl.RUNTIME_SCHEMA_VERSION,
            launch_id=str(uuid.uuid4()),
            status="running",
            mode="background",
            ddns_pid=123,
            relay_pid=456,
            started_at=datetime.now().astimezone().isoformat(),
            log_file="logs/ddns-go_20260803-000001.log",
        )
        ctl.write_runtime_state(controller.runtime_state_path, runtime_state)

        result = controller.clean_old_logs_result(10)

        self.assertNotIn("运行时日志受保护", result.text)
        self.assertIn("删除旧 0 份", result.text)

    def test_prune_keeps_same_second_suffix_as_newest(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        log_directory = Path(temporary.name) / "logs"
        log_directory.mkdir()
        first = log_directory / "ddns-go_20260803-071001.log"
        first.write_bytes(b"")
        second = log_directory / "ddns-go_20260803-071001-02.log"
        second.write_bytes(b"")

        ctl.prune_log_files(log_directory, keep_count=1)

        self.assertFalse(first.exists())
        self.assertTrue(second.exists())

    def test_latest_log_file_prefers_same_second_suffix(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        log_directory = Path(temporary.name) / "logs"
        log_directory.mkdir()
        first = log_directory / "ddns-go_20260803-071001.log"
        first.write_bytes(b"")
        second = log_directory / "ddns-go_20260803-071001-02.log"
        second.write_bytes(b"")

        self.assertEqual(
            ctl.latest_log_file(log_directory).name,
            "ddns-go_20260803-071001-02.log",
        )

    def test_relay_output_is_byte_exact_for_log_and_console(self):
        payload = "stdout\n中文 stderr\n末尾无换行".encode("utf-8")
        log_stream = io.BytesIO()
        console_stream = io.BytesIO()

        ctl.relay_output(io.BytesIO(payload), log_stream, console_stream, chunk_size=3)

        self.assertEqual(log_stream.getvalue(), payload)
        self.assertEqual(console_stream.getvalue(), payload)

    def test_log_write_failure_is_not_swallowed(self):
        class FailingStream(io.BytesIO):
            def write(self, value):
                raise OSError("disk full")

        with self.assertRaisesRegex(OSError, "disk full"):
            ctl.relay_output(io.BytesIO(b"must-not-be-lost"), FailingStream())


class RelayLifecycleTests(TemporaryControllerMixin, unittest.TestCase):
    @staticmethod
    def make_fake_ddns(directory):
        source_cmd = (
            Path(os.environ.get("SystemRoot", r"C:\Windows"))
            / "System32"
            / "cmd.exe"
        )
        fake_ddns = directory / ctl.EXECUTABLE_NAME
        shutil.copy2(source_cmd, fake_ddns)
        return fake_ddns.resolve()

    def test_named_stop_event_can_be_tracked_and_signaled(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        launch_id = str(ctl.uuid.uuid4())
        event = ctl.RelayStopEvent(ctl.relay_stop_event_name(directory, launch_id))
        self.addCleanup(event.close)

        self.assertTrue(ctl.relay_stop_event_exists(directory, launch_id))
        self.assertTrue(ctl.signal_relay_stop(directory, launch_id))
        self.assertEqual(event.wait(), ctl.WAIT_OBJECT_0)

    def test_background_and_foreground_use_expected_console_flags(self):
        for hidden, expected_flag in (
            (True, ctl.CREATE_NO_WINDOW),
            (False, ctl.CREATE_NEW_CONSOLE),
        ):
            with self.subTest(hidden=hidden):
                controller = self.make_controller()
                controller.executable_path.touch()
                runtime_state = RuntimeStateTests.make_state(
                    ddns_pid=4321,
                    relay_pid=5432,
                    mode="background" if hidden else "foreground",
                )
                stopped_state = controller.new_state(
                    "Stopped", "未运行", "test", "bright_black"
                )
                fake_relay = mock.Mock(pid=5432)
                fake_relay.poll.return_value = None
                relay_environment = {
                    ctl.PYINSTALLER_RESET_ENVIRONMENT_NAME: "1"
                }

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
                    mock.patch.object(
                        ctl,
                        "independent_controller_environment",
                        return_value=relay_environment,
                    ),
                    mock.patch.object(ctl.subprocess, "Popen", return_value=fake_relay) as popen,
                ):
                    result = controller.start(hidden=hidden)

                options = popen.call_args.kwargs
                self.assertEqual(options["creationflags"], expected_flag)
                self.assertIs(options["env"], relay_environment)
                self.assertEqual(
                    result.text,
                    f"DDNS-GO 已以{'后台模式' if hidden else '前台模式'}启动。",
                )
                self.assertEqual(result.detail_lines, ())
                if hidden:
                    self.assertIs(options["stdout"], subprocess.DEVNULL)
                    self.assertIs(options["stderr"], subprocess.DEVNULL)
                else:
                    self.assertNotIn("stdout", options)
                    self.assertNotIn("stderr", options)

    def test_already_running_result_reuses_state(self):
        controller = self.make_controller()
        controller.executable_path.touch()
        running_state = controller.new_state(
            "Running", "运行中", "正在监听端口。", "green", process_id=4321
        )
        with mock.patch.object(controller, "get_state", return_value=running_state):
            result = controller.start()

        self.assertEqual(result.text, "DDNS-GO 已在运行。")
        self.assertEqual(result.detail_lines, ())

    def test_start_reports_prune_warning(self):
        controller = self.make_controller()
        controller.executable_path.touch()
        runtime_state = RuntimeStateTests.make_state(
            ddns_pid=4321,
            relay_pid=5432,
            mode="background",
        )
        stopped_state = controller.new_state(
            "Stopped", "未运行", "test", "bright_black"
        )
        fake_relay = mock.Mock(pid=5432)
        fake_relay.poll.return_value = None
        failed_path = controller.log_directory / "ddns-go_20260803-000001.log"

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
            mock.patch.object(ctl.subprocess, "Popen", return_value=fake_relay),
            mock.patch.object(
                ctl, "prune_log_files", return_value=[failed_path]
            ),
        ):
            result = controller.start(hidden=True)

        self.assertEqual(len(result.detail_lines), 1)
        self.assertTrue(
            any("未能清理" in value for _, value in result.detail_lines)
        )
        self.assertEqual(result.color, "yellow")

    def test_relay_integration_captures_stdout_and_stderr_without_ddns(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name).resolve()
        fake_ddns = self.make_fake_ddns(directory)
        started_at = datetime.now().astimezone()
        log_path = ctl.reserve_log_file(
            directory / "ctl-data" / "logs", started_at
        )
        launch_id = str(ctl.uuid.uuid4())

        exit_code = ctl.run_log_relay(
            [
                "--application-directory",
                str(directory),
                "--launch-id",
                launch_id,
                "--mode",
                "background",
                "--log-name",
                log_path.name,
                "--started-at",
                started_at.isoformat(timespec="seconds"),
                "--",
                str(fake_ddns),
                "/d",
                "/s",
                "/c",
                "echo stdout & echo stderr 1>&2",
            ]
        )

        output = log_path.read_bytes()
        self.assertEqual(exit_code, 0)
        self.assertIn(b"stdout", output)
        self.assertIn(b"stderr", output)
        runtime_path = (
            directory
            / "ctl-data"
            / ctl.RUNTIME_STATE_DIRECTORY_NAME
            / ctl.RUNTIME_STATE_NAME
        )
        self.assertTrue(runtime_path.exists())
        _, stopped_state = ctl.read_runtime_state(runtime_path)
        self.assertEqual(stopped_state.status, "stopped")
        self.assertIsNone(stopped_state.launch_id)
        self.assertIsNone(stopped_state.relay_pid)
        self.assertIsNone(stopped_state.log_file)

    def test_job_close_terminates_assigned_process(self):
        sleeper = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=ctl.CREATE_NO_WINDOW,
        )
        job = None
        try:
            job = ctl.KillOnCloseJob()
            job.assign(sleeper.pid)
            job.close()
            job = None
            sleeper.wait(timeout=3)
            self.assertIsNotNone(sleeper.returncode)
        finally:
            if job is not None:
                job.close()
            if sleeper.poll() is None:
                sleeper.kill()
                sleeper.wait()

    def test_log_failure_kills_child_and_records_failed_state(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name).resolve()
        fake_ddns = self.make_fake_ddns(directory)
        started_at = datetime.now().astimezone()
        log_path = ctl.reserve_log_file(
            directory / "ctl-data" / "logs", started_at
        )
        launch_id = str(ctl.uuid.uuid4())
        arguments = [
            "--application-directory",
            str(directory),
            "--launch-id",
            launch_id,
            "--mode",
            "background",
            "--log-name",
            log_path.name,
            "--started-at",
            started_at.isoformat(timespec="seconds"),
            "--",
            str(fake_ddns),
            "/d",
            "/s",
            "/c",
            "echo started & ping -n 30 127.0.0.1 >nul",
        ]

        with (
            mock.patch.object(ctl, "relay_output", side_effect=OSError("disk full")),
            mock.patch.object(sys, "stderr", io.StringIO()),
        ):
            exit_code = ctl.run_log_relay(arguments)

        state_exists, runtime_state = ctl.read_runtime_state(
            directory
            / "ctl-data"
            / ctl.RUNTIME_STATE_DIRECTORY_NAME
            / ctl.RUNTIME_STATE_NAME
        )
        controller = ctl.DdnsController(directory)
        self.assertEqual(exit_code, 1)
        self.assertTrue(state_exists)
        self.assertEqual(runtime_state.status, "failed")
        self.assertIn("disk full", runtime_state.error)
        self.assertTrue(controller.wait_for_process_exit(runtime_state.ddns_pid))

    def test_stop_event_ends_relay_and_retains_stopped_state(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name).resolve()
        fake_ddns = self.make_fake_ddns(directory)
        started_at = datetime.now().astimezone()
        log_path = ctl.reserve_log_file(
            directory / "ctl-data" / "logs", started_at
        )
        launch_id = str(ctl.uuid.uuid4())
        relay = subprocess.Popen(
            [
                sys.executable,
                str(MODULE_PATH),
                ctl.RELAY_SWITCH,
                "--application-directory",
                str(directory),
                "--launch-id",
                launch_id,
                "--mode",
                "background",
                "--log-name",
                log_path.name,
                "--started-at",
                started_at.isoformat(timespec="seconds"),
                "--",
                str(fake_ddns),
                "/d",
                "/s",
                "/c",
                "echo relay-running & ping -n 30 127.0.0.1 >nul",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=ctl.CREATE_NO_WINDOW,
        )

        def cleanup_relay():
            if relay.poll() is None:
                relay.kill()
                relay.wait(timeout=5)

        self.addCleanup(cleanup_relay)
        runtime_path = (
            directory
            / "ctl-data"
            / ctl.RUNTIME_STATE_DIRECTORY_NAME
            / ctl.RUNTIME_STATE_NAME
        )
        deadline = time.monotonic() + ctl.START_TIMEOUT_SECONDS
        while True:
            state_exists, runtime_state = ctl.read_runtime_state(runtime_path)
            if (
                state_exists
                and runtime_state is not None
                and runtime_state.launch_id == launch_id
                and runtime_state.status == "running"
            ):
                break
            if relay.poll() is not None:
                self.fail("日志转发器在发布运行状态前退出")
            self.assertLess(time.monotonic(), deadline, "等待运行状态超时")
            time.sleep(0.05)

        deadline = time.monotonic() + ctl.START_TIMEOUT_SECONDS
        while b"relay-running" not in log_path.read_bytes():
            if relay.poll() is not None:
                self.fail("日志转发器在输出到达日志前退出")
            self.assertLess(time.monotonic(), deadline, "等待日志输出超时")
            time.sleep(0.05)

        self.assertTrue(ctl.signal_relay_stop(directory, launch_id))
        relay.wait(timeout=ctl.STOP_TIMEOUT_SECONDS + 2)

        self.assertTrue(runtime_path.exists())
        _, stopped_state = ctl.read_runtime_state(runtime_path)
        self.assertEqual(stopped_state.status, "stopped")
        self.assertIsNone(stopped_state.launch_id)
        self.assertIsNone(stopped_state.relay_pid)
        self.assertIsNone(stopped_state.log_file)
        self.assertTrue(log_path.is_file())
        self.assertIn(b"relay-running", log_path.read_bytes())


class ProcessIdentityTests(TemporaryControllerMixin, unittest.TestCase):
    """进程身份核验：这是"绝不误杀"的地基。"""

    def test_current_process_path_matches_interpreter(self):
        controller = self.make_controller()
        process_path = controller.get_process_path(os.getpid())
        self.assertIsNotNone(process_path)
        self.assertTrue(process_path.is_file())

        # 虚拟环境中 sys.executable 指向 venv 里的 python.exe，而进程映像可能是
        # 基础解释器，两者都算正确；软链接需先解析再比较。
        candidates = set()
        for candidate in (sys.executable, getattr(sys, "_base_executable", None)):
            if candidate:
                candidates.add(ctl.normalized_path(Path(candidate).resolve()))
        self.assertIn(ctl.normalized_path(process_path.resolve()), candidates)

    def test_absent_process_returns_none(self):
        controller = self.make_controller()
        self.assertIsNone(controller.get_process_path(999999))

    def test_foreign_process_does_not_match_target_executable(self):
        controller = self.make_controller()
        self.assertFalse(controller.process_matches_executable(os.getpid()))

    def test_refuses_to_terminate_foreign_process(self):
        controller = self.make_controller()
        sleeper = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=ctl.CREATE_NO_WINDOW,
        )
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)

        with self.assertRaises(ctl.DdnsError):
            controller.terminate_expected_process(sleeper.pid)
        # 关键断言：被拒绝的进程必须仍然活着。
        self.assertIsNone(sleeper.poll(), "非目标进程被误杀了")


class StateTests(TemporaryControllerMixin, unittest.TestCase):
    def test_display_path_uses_relative_form(self):
        controller = self.make_controller()
        path = (
            controller.script_directory
            / "ctl-data"
            / "logs"
            / "ddns-go_20260803-071001.log"
        )
        self.assertEqual(
            controller.display_path(path),
            "ctl-data/logs/ddns-go_20260803-071001.log",
        )

    def test_display_path_falls_back_when_relative_fails(self):
        controller = self.make_controller()
        path = Path("C:/somewhere/else.exe")
        with mock.patch.object(ctl.os.path, "relpath", side_effect=ValueError):
            self.assertEqual(controller.display_path(path), str(path))

    def test_missing_executable_is_reported_first(self):
        controller = self.make_controller()
        state = controller.get_state()
        self.assertEqual(state.code, "MissingExecutable")
        self.assertEqual(state.target_process_ids, ())

    def test_start_and_stop_refuse_without_executable(self):
        controller = self.make_controller()
        with self.assertRaises(ctl.DdnsError):
            controller.start()
        with self.assertRaises(ctl.DdnsError):
            controller.stop()

    def test_listener_query_returns_sorted_integers(self):
        # 轻量冒烟：只读地跑一次真实 netstat，确认命令与解析仍能配合。
        controller = self.make_controller()
        listener_ids = controller.get_listener_process_ids()
        self.assertIsInstance(listener_ids, list)
        self.assertTrue(all(isinstance(value, int) for value in listener_ids))
        self.assertEqual(listener_ids, sorted(listener_ids))

    def test_listener_query_timeout_is_reported(self):
        controller = self.make_controller()
        timeout = subprocess.TimeoutExpired("netstat.exe", 2)
        with (
            mock.patch.object(ctl.subprocess, "run", side_effect=timeout) as run,
            self.assertRaisesRegex(ctl.DdnsError, "查询超过"),
        ):
            controller.get_listener_process_ids()

        self.assertEqual(
            run.call_args.kwargs["timeout"], ctl.LISTENER_QUERY_TIMEOUT_SECONDS
        )

    def test_stopped_runtime_state_is_reported_as_not_running(self):
        controller = self.make_controller(port=54321)
        controller.executable_path.touch()
        ctl.write_runtime_state(
            controller.runtime_state_path,
            ctl.mark_runtime_stopped(RuntimeStateTests.make_state()),
        )

        state = controller.get_state()

        self.assertEqual(state.code, "Stopped")
        self.assertEqual(state.title, "未运行")
        self.assertIsNone(state.relay_process_id)
        self.assertIsNone(state.log_path)

    def test_stopped_state_with_listener_does_not_crash_log_resolution(self):
        controller = self.make_controller(port=54321)
        controller.executable_path.touch()
        stopped = ctl.mark_runtime_stopped(RuntimeStateTests.make_state())
        with (
            mock.patch.object(
                controller, "get_listener_process_ids", return_value=[4321]
            ),
            mock.patch.object(
                controller, "process_matches_executable", return_value=True
            ),
        ):
            state = controller.get_state(
                runtime_state=stopped,
                runtime_probe=(False, False, False),
            )

        self.assertEqual(state.code, "LoggingFailed")
        self.assertIsNone(state.log_path)

    def test_refresh_corrects_stale_running_state_to_stopped(self):
        controller = self.make_controller(port=54321)
        controller.executable_path.touch()
        ctl.write_runtime_state(
            controller.runtime_state_path,
            RuntimeStateTests.make_state(ddns_pid=999999, relay_pid=999998),
        )

        state = controller.refresh_state()
        _, corrected = controller.read_runtime_state()

        self.assertEqual(state.code, "Stopped")
        self.assertEqual(corrected.status, "stopped")
        self.assertIsNone(corrected.ddns_pid)
        self.assertIsNone(corrected.relay_pid)
        self.assertIsNone(corrected.log_file)

    def test_refresh_keeps_healthy_stopped_state_unchanged(self):
        controller = self.make_controller(port=54321)
        controller.executable_path.touch()
        stopped = ctl.mark_runtime_stopped(RuntimeStateTests.make_state())
        ctl.write_runtime_state(controller.runtime_state_path, stopped)

        state = controller.refresh_state()
        _, corrected = controller.read_runtime_state()

        self.assertEqual(state.code, "Stopped")
        self.assertEqual(corrected, stopped)

    def test_refresh_with_invalid_json_returns_invalid_state(self):
        controller = self.make_controller(port=54321)
        controller.executable_path.touch()
        controller.runtime_state_path.parent.mkdir(parents=True, exist_ok=True)
        controller.runtime_state_path.write_text("not-json", encoding="utf-8")

        state = controller.refresh_state()

        self.assertEqual(state.code, "InvalidRuntime")
        self.assertEqual(state.title, "状态核对失败")

    def test_refresh_marks_running_state_failed_when_relay_missing(self):
        controller = self.make_controller(port=54321)
        controller.executable_path.touch()
        ctl.write_runtime_state(
            controller.runtime_state_path,
            RuntimeStateTests.make_state(
                ddns_pid=4321,
                relay_pid=999998,
            ),
        )

        with mock.patch.object(
            controller, "process_matches_executable", return_value=True
        ):
            state = controller.refresh_state()

        _, corrected = controller.read_runtime_state()
        self.assertEqual(corrected.status, "failed")
        self.assertIn("日志转发器", corrected.error)
        self.assertEqual(state.code, "LoggingFailed")
        # 状态已从 running 修正为 failed，标题不得再声称“运行中”；
        # 该场景 DDNS-GO 进程仍存活，标题如实描述“进程存在，但日志状态异常”。
        self.assertEqual(state.title, "进程存在，但日志状态异常")

    def test_refresh_passes_reconciled_state_to_get_state(self):
        controller = self.make_controller()
        runtime_state = RuntimeStateTests.make_state()
        runtime_probe = (True, True, True)
        stopped_state = controller.new_state(
            "Stopped", "未运行", "test", "bright_black"
        )

        with (
            mock.patch.object(
                controller,
                "reconcile_runtime_state",
                return_value=(runtime_state, runtime_probe),
            ),
            mock.patch.object(
                controller, "get_state", return_value=stopped_state
            ) as get_state,
        ):
            controller.refresh_state()

        get_state.assert_called_once_with(
            runtime_state=runtime_state,
            runtime_probe=runtime_probe,
        )

    def test_logging_failed_title_reflects_non_running_status(self):
        controller = self.make_controller(port=54321)
        controller.executable_path.touch()
        failed_state = RuntimeStateTests.make_state(status="failed")

        with (
            mock.patch.object(
                controller, "get_listener_process_ids", return_value=[4321]
            ),
        ):
            state = controller.get_state(
                runtime_state=failed_state,
                runtime_probe=(True, False, False),
            )

        self.assertEqual(state.code, "LoggingFailed")
        # status 为 failed，标题不得声称“运行中”。
        self.assertEqual(state.title, "状态异常，日志链路已失效")

    def test_get_state_reuses_precomputed_runtime_probe(self):
        controller = self.make_controller()
        controller.executable_path.touch()
        runtime_state = RuntimeStateTests.make_state()

        with (
            mock.patch.object(controller, "_probe_runtime_state") as probe,
            mock.patch.object(
                controller, "get_listener_process_ids", return_value=[]
            ),
        ):
            state = controller.get_state(
                runtime_state=runtime_state,
                runtime_probe=(True, True, True),
            )

        probe.assert_not_called()
        self.assertEqual(state.code, "ProcessNotListening")

    def test_stop_writes_stopped_state_after_graceful_stop(self):
        controller = self.make_controller()
        running_state = RuntimeStateTests.make_state()
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
            mock.patch.object(
                controller,
                "read_runtime_state",
                return_value=(True, running_state),
            ),
            mock.patch.object(ctl, "write_runtime_state") as write_state,
        ):
            controller.stop()

        stopped_state = write_state.call_args.args[1]
        self.assertEqual(stopped_state.status, "stopped")
        self.assertIsNone(stopped_state.ddns_pid)
        self.assertIsNone(stopped_state.relay_pid)

    def test_stop_stale_runtime_without_state_file_reports_accurately(self):
        controller = self.make_controller()
        stale_state = controller.new_state(
            "StaleRuntime", "未运行", "test", "yellow"
        )
        with (
            mock.patch.object(controller, "get_state", return_value=stale_state),
            mock.patch.object(
                controller, "read_runtime_state", return_value=(False, None)
            ),
            mock.patch.object(ctl, "write_runtime_state") as write_state,
        ):
            result = controller.stop()

        write_state.assert_not_called()
        self.assertIn("已不存在", result.text)

    def test_stop_stale_runtime_with_state_file_marks_stopped(self):
        controller = self.make_controller()
        stale_state = controller.new_state(
            "StaleRuntime", "未运行", "test", "yellow"
        )
        running_state = RuntimeStateTests.make_state()
        with (
            mock.patch.object(controller, "get_state", return_value=stale_state),
            mock.patch.object(
                controller, "read_runtime_state", return_value=(True, running_state)
            ),
            mock.patch.object(ctl, "write_runtime_state") as write_state,
        ):
            result = controller.stop()

        write_state.assert_called_once()
        self.assertIn("已标记为 stopped", result.text)


class MenuTests(unittest.TestCase):
    """菜单循环：未预期异常兜底后继续运行，不退出。"""

    def test_run_menu_survives_unexpected_refresh_error(self):
        controller = ctl.DdnsController(Path(tempfile.gettempdir()))
        ui = ctl.ConsoleUI()
        ui.use_color = False
        calls = {"count": 0}

        def flaky_refresh():
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("boom")
            return controller.new_state(
                "Stopped", "未运行", "test", "bright_black"
            )

        with (
            mock.patch.object(controller, "refresh_state", side_effect=flaky_refresh),
            mock.patch.object(ctl, "read_immediate_key", return_value="q"),
            mock.patch.object(sys, "stdout", io.StringIO()),
        ):
            exit_code = ctl.run_menu(controller, ui)

        self.assertEqual(exit_code, 0)
        self.assertGreaterEqual(calls["count"], 2)

    def test_menu_shortcuts_route_to_actions(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        controller = ctl.DdnsController(Path(temporary.name))
        controller.executable_path.touch()
        state = controller.new_state("Stopped", "未运行", "test", "bright_black")
        ui = ctl.ConsoleUI()
        ui.use_color = False

        cases = (
            ("1", "start", {"hidden": True}),
            ("b", "start", {"hidden": True}),
            ("2", "start", {"hidden": False}),
            ("f", "start", {"hidden": False}),
            ("3", "stop", {}),
            ("s", "stop", {}),
            ("4", "open_management_page", {}),
            ("w", "open_management_page", {}),
            ("5", "open_log_directory", {}),
            ("l", "open_log_directory", {}),
        )
        for key, method_name, expected_kwargs in cases:
            with self.subTest(key=key):
                method = mock.Mock(return_value=ctl.ActionResult("ok"))
                with (
                    mock.patch.object(controller, method_name, method),
                    mock.patch.object(
                        controller, "refresh_state", return_value=state
                    ),
                    mock.patch.object(
                        ctl, "read_immediate_key", side_effect=[key, "q"]
                    ),
                    mock.patch.object(sys, "stdout", io.StringIO()),
                ):
                    exit_code = ctl.run_menu(controller, ui)

                self.assertEqual(exit_code, 0)
                method.assert_called_once_with(**expected_kwargs)

    def test_view_shortcuts_enter_log_viewer(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        controller = ctl.DdnsController(Path(temporary.name))
        controller.executable_path.touch()
        state = controller.new_state("Stopped", "未运行", "test", "bright_black")
        ui = ctl.ConsoleUI()
        ui.use_color = False

        for key in ("6", "v"):
            with self.subTest(key=key):
                view = mock.Mock(return_value=ctl.ActionResult("ok"))
                with (
                    mock.patch.object(ctl, "view_runtime_log", view),
                    mock.patch.object(
                        controller, "refresh_state", return_value=state
                    ),
                    mock.patch.object(
                        ctl, "read_immediate_key", side_effect=[key, "q"]
                    ),
                    mock.patch.object(sys, "stdout", io.StringIO()),
                ):
                    exit_code = ctl.run_menu(controller, ui)

                self.assertEqual(exit_code, 0)
                view.assert_called_once()

    def test_refresh_key_reports_rechecked_state(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        controller = ctl.DdnsController(Path(temporary.name))
        controller.executable_path.touch()
        state = controller.new_state("Stopped", "未运行", "test", "bright_black")
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = io.StringIO()

        with (
            mock.patch.object(controller, "refresh_state", return_value=state),
            mock.patch.object(
                ctl, "read_immediate_key", side_effect=["r", "q"]
            ),
            mock.patch.object(sys, "stdout", stream),
        ):
            exit_code = ctl.run_menu(controller, ui)

        self.assertEqual(exit_code, 0)
        self.assertIn("状态已核对并刷新", stream.getvalue())

    def test_invalid_key_appends_after_operation_result(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        controller = ctl.DdnsController(Path(temporary.name))
        controller.executable_path.touch()
        state = controller.new_state("Stopped", "未运行", "test", "bright_black")
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = io.StringIO()

        with (
            mock.patch.object(controller, "refresh_state", return_value=state),
            mock.patch.object(
                ctl, "read_immediate_key", side_effect=["r", "z", "z", "q"]
            ),
            mock.patch.object(sys, "stdout", stream),
        ):
            exit_code = ctl.run_menu(controller, ui)

        self.assertEqual(exit_code, 0)
        self.assertIn(
            "操作结果：提示：无效按键",
            stream.getvalue(),
        )
        self.assertIn(
            "操作结果：状态已核对并刷新：未运行",
            stream.getvalue(),
        )
        self.assertTrue(
            all(
                line.count("提示：无效按键") <= 1
                for line in stream.getvalue().splitlines()
            )
        )

    def test_d_clears_operation_result(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        controller = ctl.DdnsController(Path(temporary.name))
        controller.executable_path.touch()
        state = controller.new_state("Stopped", "未运行", "test", "bright_black")
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = io.StringIO()

        with (
            mock.patch.object(controller, "refresh_state", return_value=state),
            mock.patch.object(
                ctl, "read_immediate_key", side_effect=["z", "d", "q"]
            ),
            mock.patch.object(sys, "stdout", stream),
        ):
            exit_code = ctl.run_menu(controller, ui)

        self.assertEqual(exit_code, 0)
        self.assertEqual(stream.getvalue().count("提示：无效按键"), 1)
        self.assertGreaterEqual(stream.getvalue().count("操作结果：-"), 2)


class LogViewerTests(unittest.TestCase):
    """日志查看：回退、分页、跟随、配色与菜单入口。"""

    def make_controller(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return ctl.DdnsController(Path(temporary.name))

    def make_log(self, controller, name, content):
        log_path = controller.log_directory / name
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(content, encoding="utf-8")
        return log_path

    def test_latest_log_file_picks_newest_named_log(self):
        controller = self.make_controller()
        self.make_log(controller, "ddns-go_20260803-071001.log", "旧")
        self.make_log(controller, "ddns-go_20260803-071001-02.log", "同秒新")
        self.make_log(controller, "ddns-go_20260803-071002.log", "最新")
        (controller.log_directory / "README.txt").write_text("忽略", encoding="utf-8")

        self.assertEqual(
            ctl.latest_log_file(controller.log_directory).name,
            "ddns-go_20260803-071002.log",
        )

    def test_latest_log_file_returns_none_when_empty(self):
        controller = self.make_controller()
        controller.log_directory.mkdir(parents=True)

        self.assertIsNone(ctl.latest_log_file(controller.log_directory))

    def test_view_without_log_opens_empty_viewer(self):
        controller = self.make_controller()
        state = controller.new_state("Stopped", "未运行", "test", "bright_black")
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = io.StringIO()

        with (
            mock.patch.object(sys, "stdout", stream),
            mock.patch.object(ctl, "read_immediate_key", return_value="x"),
        ):
            result = ctl.view_runtime_log(controller, state, ui)

        self.assertEqual(result.color, "cyan")
        self.assertEqual(result.text, "已退出日志查看。")
        self.assertIn("暂无日志内容", stream.getvalue())
        self.assertIn('文件: "ctl-data/logs/"', stream.getvalue())
        self.assertIn("（日志文件夹为空）", stream.getvalue())
        self.assertIn(
            f"（日志文件夹为空） | 日志文件保留上限: {ctl.LOG_KEEP_COUNT} 份"
            " | 当前日志份数: 0 份",
            stream.getvalue(),
        )

    def test_view_falls_back_to_latest_log(self):
        controller = self.make_controller()
        self.make_log(controller, "ddns-go_20260803-071001.log", "旧内容")
        self.make_log(controller, "ddns-go_20260803-071002.log", "新内容")
        state = controller.new_state("Stopped", "未运行", "test", "bright_black")
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = io.StringIO()

        with (
            mock.patch.object(sys, "stdout", stream),
            mock.patch.object(ctl, "read_immediate_key", return_value="x"),
        ):
            result = ctl.view_runtime_log(controller, state, ui)

        output = stream.getvalue()
        self.assertEqual(result.color, "cyan")
        self.assertIn("最新历史日志", output)
        self.assertIn("新内容", output)
        self.assertNotIn("旧内容", output)
        self.assertIn(
            f"查看日志中 | 每页 {ctl.LOG_VIEW_PAGE_LINES} 行 | "
            + mapping_hint(ctl.LOG_VIEW_KEY_MAPPINGS, "return"),
            output,
        )

    def test_view_marks_current_runtime_log(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller, "ddns-go_20260803-071001.log", "运行内容"
        )
        state = controller.new_state(
            "Running", "运行中", "test", "green", log_path=log_path
        )
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = io.StringIO()

        with (
            mock.patch.object(sys, "stdout", stream),
            mock.patch.object(ctl, "read_immediate_key", return_value="x"),
        ):
            ctl.view_runtime_log(controller, state, ui)

        self.assertIn("当前运行时日志", stream.getvalue())

    def test_x_and_escape_return_to_menu(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller, "ddns-go_20260803-071001.log", "内容"
        )
        state = controller.new_state(
            "Running", "运行中", "test", "green", log_path=log_path
        )
        ui = ctl.ConsoleUI()
        ui.use_color = False

        for key in ("x", "Escape"):
            with self.subTest(key=key):
                stream = io.StringIO()
                with (
                    mock.patch.object(sys, "stdout", stream),
                    mock.patch.object(
                        ctl, "read_immediate_key", return_value=key
                    ),
                ):
                    result = ctl.view_runtime_log(controller, state, ui)
                self.assertEqual(result.color, "cyan")
                self.assertEqual(result.text, "已退出日志查看。")

    def test_redirected_eof_returns_to_menu(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller, "ddns-go_20260803-071001.log", "内容"
        )
        state = controller.new_state(
            "Running", "运行中", "test", "green", log_path=log_path
        )
        ui = ctl.ConsoleUI()
        ui.use_color = False

        with (
            mock.patch.object(sys, "stdout", io.StringIO()),
            mock.patch.object(ctl, "read_immediate_key", return_value="Q"),
        ):
            result = ctl.view_runtime_log(controller, state, ui)

        self.assertEqual(result.color, "cyan")
        self.assertEqual(result.text, "已退出日志查看。")

    def test_viewer_colors_distinguish_body(self):
        controller = self.make_controller()
        log_path = controller.log_directory / "ddns-go_20260803-071001.log"
        ui = ctl.ConsoleUI()
        ui.use_color = True
        stream = io.StringIO()

        with mock.patch.object(sys, "stdout", stream):
            ctl.render_log_viewer(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                ["正文第一行", "正文第二行"],
                0,
                True,
            )

        output = stream.getvalue()
        for text, color in ctl.log_view_banner_parts():
            self.assertIn(ui.colorize(text, color), output)
        self.assertIn(
            '文件: "ctl-data/logs/ddns-go_20260803-071001.log"', output
        )
        self.assertIn(
            ui.colorize(" |", "bright_black")
            + ui.colorize(
                f" 日志文件保留上限: {ctl.LOG_KEEP_COUNT} 份"
                f" | 当前日志份数: 0 份",
                "dim_bright_black",
            ),
            output,
        )
        self.assertIn("\033[36m连续模式\033[0m", output)
        self.assertIn("\033[90m", output)
        self.assertIn("\033[33m", output)
        self.assertIn("\033[90m,\033[0m", output)
        for line in ("正文第一行", "正文第二行"):
            self.assertIn(line, output)
            self.assertNotIn("\033", line)

    def test_no_log_separator_uses_yellow(self):
        controller = self.make_controller()
        ui = ctl.ConsoleUI()
        ui.use_color = True
        stream = io.StringIO()

        with mock.patch.object(sys, "stdout", stream):
            ctl.render_log_viewer(
                controller,
                ui,
                controller.log_directory,
                "无日志",
                [],
                0,
                True,
            )

        output = stream.getvalue()
        self.assertIn(
            ui.colorize("（日志文件夹为空）", "yellow")
            + ui.colorize(" |", "yellow"),
            output,
        )

    def test_viewer_without_color_has_no_ansi(self):
        controller = self.make_controller()
        log_path = controller.log_directory / "ddns-go_20260803-071001.log"
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = io.StringIO()

        with mock.patch.object(sys, "stdout", stream):
            ctl.render_log_viewer(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                ["正文"],
                0,
                True,
            )

        self.assertNotIn("\033", stream.getvalue())

    def test_viewer_writes_frame_once_and_always_clears_scrollback(self):
        controller = self.make_controller()
        log_path = controller.log_directory / "ddns-go_20260803-071001.log"
        ui = ctl.ConsoleUI()
        ui.use_color = True
        stream = TtyStream()
        lines = [f"第{i}行" for i in range(269)]

        with mock.patch.object(sys, "stdout", stream):
            ctl.render_log_viewer(
                controller,
                ui,
                log_path,
                "最新历史日志",
                lines,
                260,
                True,
                ctl.LOG_VIEW_MODE_PAGING,
            )
            ctl.render_log_viewer(
                controller,
                ui,
                log_path,
                "最新历史日志",
                lines,
                260,
                True,
                ctl.LOG_VIEW_MODE_PAGING,
            )

        first, second = stream.chunks
        self.assertEqual(len(stream.chunks), 2)
        self.assertIn("\033[3J", first)
        self.assertIn("\033[3J", second)
        self.assertIn("\033[2J", second)

    def test_viewer_paging_mode_shows_aligned_page_label(self):
        controller = self.make_controller()
        log_path = controller.log_directory / "ddns-go_20260803-071001.log"
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = io.StringIO()
        lines = [f"第{i}行" for i in range(269)]

        with mock.patch.object(sys, "stdout", stream):
            ctl.render_log_viewer(
                controller,
                ui,
                log_path,
                "最新历史日志",
                lines,
                260,
                True,
                ctl.LOG_VIEW_MODE_PAGING,
            )

        self.assertIn("页 14/14", stream.getvalue())
        self.assertIn("当页行区间: 261-269", stream.getvalue())
        self.assertIn(f"每页 {ctl.LOG_VIEW_PAGE_LINES} 行", stream.getvalue())

    def test_viewer_follow_mode_shows_line_range(self):
        controller = self.make_controller()
        log_path = controller.log_directory / "ddns-go_20260803-071001.log"
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = io.StringIO()
        lines = [f"第{i}行" for i in range(269)]

        with mock.patch.object(sys, "stdout", stream):
            ctl.render_log_viewer(
                controller,
                ui,
                log_path,
                "最新历史日志",
                lines,
                249,
                True,
                ctl.LOG_VIEW_MODE_FOLLOW,
            )

        self.assertIn("当页行区间: 250-269", stream.getvalue())
        self.assertIn("连续模式 / 翻页模式", stream.getvalue())
        self.assertIn(f"每页 {ctl.LOG_VIEW_PAGE_LINES} 行", stream.getvalue())
        self.assertIn(
            mapping_hint(ctl.LOG_VIEW_KEY_MAPPINGS, "toggle_mode"),
            stream.getvalue(),
        )
        self.assertIn(
            mapping_hint(ctl.LOG_VIEW_KEY_MAPPINGS, "jump_page"),
            stream.getvalue(),
        )
        self.assertNotIn("| 页 ", stream.getvalue())

    def test_viewer_shows_current_log_count(self):
        controller = self.make_controller()
        for index in range(1, 4):
            self.make_log(
                controller,
                f"ddns-go_20260803-07100{index}.log",
                "内容",
            )
        log_path = controller.log_directory / "ddns-go_20260803-071001.log"
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = io.StringIO()

        with mock.patch.object(sys, "stdout", stream):
            ctl.render_log_viewer(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                [],
                0,
                True,
            )

        self.assertIn(
            f"日志文件保留上限: {ctl.LOG_KEEP_COUNT} 份 | 当前日志份数: 3 份",
            stream.getvalue(),
        )

    def test_viewer_pads_last_page_to_fixed_height(self):
        controller = self.make_controller()
        log_path = controller.log_directory / "ddns-go_20260803-071001.log"
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = io.StringIO()
        lines = [f"第{i}行" for i in range(269)]

        with mock.patch.object(sys, "stdout", stream):
            ctl.render_log_viewer(
                controller,
                ui,
                log_path,
                "最新历史日志",
                lines,
                260,
                True,
                ctl.LOG_VIEW_MODE_PAGING,
            )

        rendered_lines = stream.getvalue().splitlines()
        first_divider = rendered_lines.index("-" * 60)
        second_divider = rendered_lines.index("-" * 60, first_divider + 1)
        self.assertEqual(second_divider - first_divider - 1, 20)

    def test_viewer_empty_file_placeholder_in_both_modes(self):
        controller = self.make_controller()
        log_path = controller.log_directory / "ddns-go_20260803-071001.log"
        ui = ctl.ConsoleUI()
        ui.use_color = False

        for mode in (ctl.LOG_VIEW_MODE_FOLLOW, ctl.LOG_VIEW_MODE_PAGING):
            with self.subTest(mode=mode):
                stream = io.StringIO()
                with mock.patch.object(sys, "stdout", stream):
                    ctl.render_log_viewer(
                        controller,
                        ui,
                        log_path,
                        "当前运行时日志",
                        [],
                        0,
                        True,
                        mode,
                    )
                output = stream.getvalue()
                self.assertIn("总行数: 0", output)
                self.assertNotIn("（暂无日志内容）", output)
                rendered_lines = output.splitlines()
                first_divider = rendered_lines.index("-" * 60)
                second_divider = rendered_lines.index(
                    "-" * 60, first_divider + 1
                )
                self.assertEqual(
                    second_divider - first_divider - 1, 20
                )

    def test_viewer_small_log_shows_short_line_range(self):
        controller = self.make_controller()
        log_path = controller.log_directory / "ddns-go_20260803-071001.log"
        ui = ctl.ConsoleUI()
        ui.use_color = False
        lines = [f"第{i}行" for i in range(9)]

        for mode in (ctl.LOG_VIEW_MODE_FOLLOW, ctl.LOG_VIEW_MODE_PAGING):
            with self.subTest(mode=mode):
                stream = io.StringIO()
                with mock.patch.object(sys, "stdout", stream):
                    ctl.render_log_viewer(
                        controller,
                        ui,
                        log_path,
                        "当前运行时日志",
                        lines,
                        0,
                        True,
                        mode,
                    )
                output = stream.getvalue()
                self.assertIn("当页行区间: 1-9", output)
                if mode == ctl.LOG_VIEW_MODE_PAGING:
                    self.assertIn("页 1/1", output)

    def test_viewer_shows_status_message(self):
        controller = self.make_controller()
        log_path = controller.log_directory / "ddns-go_20260803-071001.log"
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = io.StringIO()
        lines = [f"第{i}行" for i in range(45)]

        with mock.patch.object(sys, "stdout", stream):
            ctl.render_log_viewer(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                lines,
                0,
                False,
                ctl.LOG_VIEW_MODE_FOLLOW,
                "行号 999 不存在，有效范围 1-45",
            )

        self.assertIn("操作结果：行号 999 不存在，有效范围 1-45", stream.getvalue())

    def test_viewer_shows_operation_result_placeholder(self):
        controller = self.make_controller()
        log_path = controller.log_directory / "ddns-go_20260803-071001.log"
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = io.StringIO()

        with mock.patch.object(sys, "stdout", stream):
            ctl.render_log_viewer(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                [],
                0,
                True,
            )

        self.assertIn("操作结果：-", stream.getvalue())

    def test_clear_screen_prefix_obeys_tty_state(self):
        stream = TtyStream()
        with mock.patch.object(sys, "stdout", stream):
            self.assertEqual(ctl.clear_screen_prefix(), "\033[2J\033[3J\033[H")
        with mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(ctl.clear_screen_prefix(), "")

    def test_read_line_number_reads_digits_interactively(self):
        ui = ctl.ConsoleUI()
        ui.use_color = False

        class FakeStdin:
            def isatty(self):
                return True

            def readline(self):
                raise AssertionError("interactive path must not use readline")

        cases = (
            (["1", "2", "\r"], 12),
            (["1", "2", "\x08", "3", "\r"], 13),
            (["Escape"], None),
            (["1", "2", "x"], None),
            (["X"], None),
            (["\r"], None),
        )
        for keys, expected in cases:
            with self.subTest(keys=keys):
                out = TtyStream()
                with (
                    mock.patch.object(sys, "stdout", out),
                    mock.patch.object(sys, "stdin", FakeStdin()),
                    mock.patch.object(
                        ctl, "_read_raw_console_key", side_effect=keys
                    ),
                ):
                    result = ctl.read_line_number(ui)
                self.assertEqual(result, expected)
                if expected is not None:
                    body = out.getvalue().rstrip("\n")
                    self.assertTrue(body.startswith("跳转到行号: "))
                    self.assertNotIn("\n", body[len("跳转到行号: ") :])

    def test_read_line_number_fallback_handles_line_input(self):
        ui = ctl.ConsoleUI()
        ui.use_color = False

        cases = (
            ("123\n", 123),
            (" 45 \n", 45),
            ("abc\n", None),
            ("\n", None),
            ("", None),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                with (
                    mock.patch.object(sys, "stdin", io.StringIO(value)),
                    mock.patch.object(sys, "stdout", io.StringIO()),
                ):
                    result = ctl.read_line_number(ui)
                self.assertEqual(result, expected)

    def test_read_line_number_rejects_other_keys_with_hint(self):
        ui = ctl.ConsoleUI()
        ui.use_color = False
        out = TtyStream()
        invalid_calls = []

        class FakeStdin:
            def isatty(self):
                return True

            def readline(self):
                raise AssertionError("interactive path must not use readline")

        with (
            mock.patch.object(sys, "stdout", out),
            mock.patch.object(sys, "stdin", FakeStdin()),
            mock.patch.object(
                ctl,
                "_read_raw_console_key",
                side_effect=["1", "a", "2", "\r"],
            ),
        ):
            result = ctl.read_line_number(
                ui, on_invalid=lambda: invalid_calls.append(True)
            )

        self.assertEqual(result, 12)
        self.assertEqual(invalid_calls, [True])
        self.assertNotIn("（无效按键）", out.getvalue())

    def test_loop_skips_redraw_when_nothing_changes(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller,
            "ddns-go_20260803-071001.log",
            "\n".join(f"第{i}行" for i in range(45)) + "\n",
        )
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = TtyStream()
        keys = iter([None, "x"])

        def next_key():
            return next(keys)

        def refresh():
            return False, None

        with mock.patch.object(sys, "stdout", stream):
            result = ctl._run_log_view_loop(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                reader,
                25,
                True,
                ctl.LOG_VIEW_MODE_FOLLOW,
                next_key,
                refresh,
                0.0,
            )

        self.assertEqual(result.text, "已退出日志查看。")
        self.assertEqual(len(stream.chunks), 1)

    def test_prev_next_home_show_result_feedback(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller,
            "ddns-go_20260803-071001.log",
            "\n".join(f"第{i}行" for i in range(45)) + "\n",
        )
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = TtyStream()
        keys = iter(["w", "s", "h", "x"])

        def next_key():
            return next(keys)

        def refresh():
            return False, None

        with mock.patch.object(sys, "stdout", stream):
            result = ctl._run_log_view_loop(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                reader,
                25,
                True,
                ctl.LOG_VIEW_MODE_FOLLOW,
                next_key,
                refresh,
                0.0,
            )

        self.assertEqual(result.text, "已退出日志查看。")
        self.assertEqual(len(stream.chunks), 4)
        self.assertIn("操作结果：已上页", stream.chunks[1])
        self.assertIn("操作结果：已下页", stream.chunks[2])
        self.assertIn("操作结果：已回开头", stream.chunks[3])

    def test_valid_key_clears_invalid_hint(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller,
            "ddns-go_20260803-071001.log",
            "\n".join(f"第{i}行" for i in range(45)) + "\n",
        )
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = TtyStream()
        keys = iter(["z", "w", "x"])

        def next_key():
            return next(keys)

        def refresh():
            return False, None

        with mock.patch.object(sys, "stdout", stream):
            result = ctl._run_log_view_loop(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                reader,
                25,
                True,
                ctl.LOG_VIEW_MODE_FOLLOW,
                next_key,
                refresh,
                0.0,
            )

        self.assertEqual(result.text, "已退出日志查看。")
        self.assertEqual(len(stream.chunks), 3)
        self.assertIn("操作结果：提示：无效按键", stream.chunks[1])
        self.assertNotIn("提示：无效按键", stream.chunks[2])
        self.assertIn("操作结果：已上页", stream.chunks[2])

    def test_d_clears_operation_result(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller,
            "ddns-go_20260803-071001.log",
            "\n".join(f"第{i}行" for i in range(45)) + "\n",
        )
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = TtyStream()
        keys = iter(["z", "d", "x"])

        def next_key():
            return next(keys)

        def refresh():
            return False, None

        with mock.patch.object(sys, "stdout", stream):
            result = ctl._run_log_view_loop(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                reader,
                25,
                True,
                ctl.LOG_VIEW_MODE_FOLLOW,
                next_key,
                refresh,
                0.0,
            )

        self.assertEqual(result.text, "已退出日志查看。")
        self.assertEqual(len(stream.chunks), 3)
        self.assertIn("操作结果：提示：无效按键", stream.chunks[1])
        self.assertIn("操作结果：-", stream.chunks[2])
        self.assertNotIn("提示：无效按键", stream.chunks[2])

    def test_manual_refresh_key_rechecks_log_content(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                controller = self.make_controller()
                log_path = self.make_log(
                    controller,
                    "ddns-go_20260803-071001.log",
                    "\n".join(f"第{i}行" for i in range(45)) + "\n",
                )
                reader = ctl.LogViewReader(log_path)
                self.addCleanup(reader.close)
                ui = ctl.ConsoleUI()
                ui.use_color = False
                stream = TtyStream()
                keys = iter(["r", "x"])

                def next_key():
                    return next(keys)

                def refresh():
                    if changed:
                        with log_path.open(
                            "a", encoding="utf-8"
                        ) as output:
                            output.write(
                                "\n".join(f"新{i}行" for i in range(10))
                                + "\n"
                            )
                    return reader.update(), None

                with mock.patch.object(sys, "stdout", stream):
                    result = ctl._run_log_view_loop(
                        controller,
                        ui,
                        log_path,
                        "当前运行时日志",
                        reader,
                        40,
                        False,
                        ctl.LOG_VIEW_MODE_PAGING,
                        next_key,
                        refresh,
                        0.0,
                    )

                self.assertEqual(result.text, "已退出日志查看。")
                self.assertEqual(len(stream.chunks), 2)
                if changed:
                    self.assertIn("操作结果：已刷新日志内容", stream.getvalue())
                    self.assertIn("总行数: 55", stream.getvalue())
                else:
                    self.assertIn("操作结果：日志内容无变化", stream.getvalue())
                    self.assertIn("总行数: 45", stream.getvalue())

    def test_l_opens_log_directory(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller,
            "ddns-go_20260803-071001.log",
            "\n".join(f"第{i}行" for i in range(45)) + "\n",
        )
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = TtyStream()
        keys = iter(["l", "x"])

        def next_key():
            return next(keys)

        def refresh():
            return False, None

        with (
            mock.patch.object(
                controller,
                "open_log_directory",
                return_value=ctl.ActionResult("已打开日志文件夹。", "cyan"),
            ),
            mock.patch.object(sys, "stdout", stream),
        ):
            result = ctl._run_log_view_loop(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                reader,
                25,
                True,
                ctl.LOG_VIEW_MODE_FOLLOW,
                next_key,
                refresh,
                0.0,
            )

        self.assertEqual(result.text, "已退出日志查看。")
        self.assertEqual(len(stream.chunks), 2)
        self.assertIn("操作结果：已打开日志文件夹。", stream.getvalue())

    def test_c_cleans_logs_by_count(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller,
            "ddns-go_20260803-071001.log",
            "\n".join(f"第{i}行" for i in range(45)) + "\n",
        )
        state = controller.new_state(
            "Running", "运行中", "test", "green", log_path=log_path
        )
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = io.StringIO()

        clean_result = ctl.ActionResult(
            "已按要求清理，保留近 2 份，删除旧 3 份。", "green"
        )
        clean_method = mock.Mock(return_value=clean_result)
        with (
            mock.patch.object(ctl, "read_line_number", return_value=2),
            mock.patch.object(controller, "clean_old_logs_result", clean_method),
            mock.patch.object(
                ctl, "read_immediate_key", side_effect=["c", "x"]
            ),
            mock.patch.object(sys, "stdout", stream),
        ):
            result = ctl.view_runtime_log(controller, state, ui)

        self.assertEqual(result.text, "已退出日志查看。")
        self.assertIn("操作结果：已按要求清理，保留近 2 份，删除旧 3 份。", stream.getvalue())
        clean_method.assert_called_once_with(2)

    def test_c_cleans_zero_deletes_viewed_history_log(self):
        controller = self.make_controller()
        old_log = self.make_log(
            controller, "ddns-go_20260803-071001.log", "旧日志"
        )
        new_log = self.make_log(
            controller, "ddns-go_20260803-071002.log", "新日志"
        )
        reader = ctl.LogViewReader(old_log)
        self.addCleanup(reader.close)
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = io.StringIO()
        keys = iter(["c", "x"])

        def next_key():
            return next(keys)

        def refresh():
            return False, None

        with (
            mock.patch.object(ctl, "read_line_number", return_value=0),
            mock.patch.object(sys, "stdout", stream),
        ):
            result = ctl._run_log_view_loop(
                controller,
                ui,
                old_log,
                "最新历史日志",
                reader,
                0,
                True,
                ctl.LOG_VIEW_MODE_FOLLOW,
                next_key,
                refresh,
                0.0,
            )

        self.assertEqual(result.text, "已退出日志查看。")
        self.assertFalse(old_log.exists())
        self.assertFalse(new_log.exists())
        self.assertIn("删除旧 2 份", stream.getvalue())

    def test_c_cleans_logs_in_paging_mode_returns_to_last_page(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller,
            "ddns-go_20260803-071001.log",
            "\n".join(f"第{i}行" for i in range(45)) + "\n",
        )
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = TtyStream()
        keys = iter(["c", "x"])

        def next_key():
            return next(keys)

        def refresh():
            return False, None

        reopened = {}

        def replace_reader(new_reader):
            reopened["reader"] = new_reader

        with (
            mock.patch.object(ctl, "read_line_number", return_value=0),
            mock.patch.object(
                controller,
                "clean_old_logs_result",
                return_value=ctl.ActionResult(
                    "已按要求清理，保留近 0 份，删除旧 0 份。", "green"
                ),
            ),
            mock.patch.object(sys, "stdout", stream),
        ):
            result = ctl._run_log_view_loop(
                controller,
                ui,
                log_path,
                "最新历史日志",
                reader,
                40,
                True,
                ctl.LOG_VIEW_MODE_PAGING,
                next_key,
                refresh,
                0.0,
                replace_reader,
            )
        if "reader" in reopened:
            reopened["reader"].close()

        self.assertEqual(result.text, "已退出日志查看。")
        self.assertEqual(len(stream.chunks), 2)
        self.assertIn("页 3/3", stream.chunks[1])
        self.assertIn("末尾跟随中", stream.chunks[1])
        self.assertIn(
            "操作结果：已按要求清理，保留近 0 份，删除旧 0 份。",
            stream.chunks[1],
        )

    def test_paging_and_follow_state_transitions(self):
        self.assertEqual(ctl.latest_page_start(45), 25)
        self.assertEqual(ctl.last_page_start(45), 40)
        self.assertEqual(ctl.log_page_count(45), 3)

        follow_cases = (
            (("prev", 45, 25, True), (5, False)),
            (("home", 45, 5, False), (0, False)),
            (("next", 45, 0, False), (20, False)),
            (("next", 45, 20, False), (25, False)),
            (("end", 45, 0, False), (25, True)),
        )
        for args, expected in follow_cases:
            with self.subTest(mode="follow", args=args):
                action, total, start, follow = args
                self.assertEqual(
                    ctl.apply_log_view_action(
                        action,
                        total,
                        start,
                        follow,
                        mode=ctl.LOG_VIEW_MODE_FOLLOW,
                    ),
                    expected,
                )

        paging_cases = (
            (("prev", 45, 40, True), (20, False)),
            (("home", 45, 20, False), (0, False)),
            (("next", 45, 0, False), (20, False)),
            (("next", 45, 20, False), (40, False)),
            (("end", 45, 0, False), (40, True)),
        )
        for args, expected in paging_cases:
            with self.subTest(mode="paging", args=args):
                action, total, start, follow = args
                self.assertEqual(
                    ctl.apply_log_view_action(
                        action,
                        total,
                        start,
                        follow,
                        mode=ctl.LOG_VIEW_MODE_PAGING,
                    ),
                    expected,
                )

    def test_page_start_helpers_handle_boundaries(self):
        self.assertEqual(ctl.latest_page_start(0), 0)
        self.assertEqual(ctl.last_page_start(0), 0)
        self.assertEqual(ctl.latest_page_start(20), 0)
        self.assertEqual(ctl.last_page_start(20), 0)
        self.assertEqual(ctl.latest_page_start(21), 1)
        self.assertEqual(ctl.last_page_start(21), 20)
        self.assertEqual(ctl.view_last_start(ctl.LOG_VIEW_MODE_FOLLOW, 45), 25)
        self.assertEqual(ctl.view_last_start(ctl.LOG_VIEW_MODE_PAGING, 45), 40)

    def test_page_navigation_never_resumes_follow(self):
        cases = (
            (("prev", 10, 0, False), (0, False)),
            (("home", 10, 0, False), (0, False)),
            (("prev", 10, 0, True), (0, True)),
            (("home", 10, 0, True), (0, True)),
            (("prev", 45, 25, False), (5, False)),
            (("home", 45, 25, False), (0, False)),
            (("prev", 45, 25, True), (5, False)),
            (("home", 45, 25, True), (0, False)),
        )
        for args, expected in cases:
            with self.subTest(args=args):
                action, total, start, follow = args
                self.assertEqual(
                    ctl.apply_log_view_action(
                        action,
                        total,
                        start,
                        follow,
                        mode=ctl.LOG_VIEW_MODE_FOLLOW,
                    ),
                    expected,
                )

    def test_log_view_ad_shortcuts(self):
        self.assertEqual(ctl.log_view_action("s"), "next")
        self.assertEqual(ctl.log_view_action("w"), "prev")
        self.assertEqual(ctl.log_view_action("r"), "refresh_log")
        self.assertEqual(ctl.log_view_action("l"), "log_directory")
        self.assertEqual(ctl.log_view_action("c"), "clean_logs")
        self.assertEqual(ctl.log_view_action("d"), "clear_result")
        self.assertIsNone(ctl.log_view_action("a"))
        self.assertIsNone(ctl.log_view_action("]"))
        self.assertIsNone(ctl.log_view_action("["))
        self.assertEqual(ctl.log_view_action(" "), "next")
        self.assertEqual(ctl.log_view_action("pagedown"), "next")
        self.assertEqual(ctl.log_view_action("pageup"), "prev")
        self.assertEqual(ctl.log_view_action("h"), "home")
        self.assertEqual(ctl.log_view_action("home"), "home")
        self.assertIsNone(ctl.log_view_action("g"))
        self.assertIsNone(ctl.log_view_action("1"))
        self.assertIsNone(ctl.log_view_action("2"))
        self.assertIsNone(ctl.log_view_action("3"))
        self.assertIsNone(ctl.log_view_action("0"))

    def test_read_raw_console_key_maps_home(self):
        with mock.patch.object(
            ctl.msvcrt, "getwch", side_effect=["\xe0", "G"]
        ):
            self.assertEqual(ctl._read_raw_console_key(), "Home")

    def test_log_view_visible_key_mappings_have_at_most_two_keys(self):
        for mappings, key_label_index in (
            (ctl.MENU_KEY_MAPPINGS, 1),
            (ctl.LOG_VIEW_KEY_MAPPINGS, 1),
        ):
            for row in mappings:
                key_label = row[key_label_index]
                self.assertLessEqual(
                    len(key_label.split("/")), 2, key_label
                )

    def test_log_view_footer_uses_key_mapping_config(self):
        footer = ctl.log_view_footer()
        self.assertIn(
            mapping_hint(ctl.LOG_VIEW_KEY_MAPPINGS, "next"), footer
        )
        self.assertIn(
            mapping_hint(ctl.LOG_VIEW_KEY_MAPPINGS, "prev"), footer
        )
        self.assertIn(
            mapping_hint(ctl.LOG_VIEW_KEY_MAPPINGS, "home"), footer
        )
        self.assertIn(
            mapping_hint(ctl.LOG_VIEW_KEY_MAPPINGS, "refresh_log"), footer
        )
        self.assertIn(
            mapping_hint(ctl.LOG_VIEW_KEY_MAPPINGS, "toggle_mode"), footer
        )
        self.assertNotIn(
            mapping_hint(ctl.LOG_VIEW_KEY_MAPPINGS, "clean_logs"), footer
        )
        self.assertNotIn(
            mapping_hint(ctl.LOG_VIEW_KEY_MAPPINGS, "clear_result"), footer
        )
        self.assertLess(
            footer.index(mapping_hint(ctl.LOG_VIEW_KEY_MAPPINGS, "prev")),
            footer.index(mapping_hint(ctl.LOG_VIEW_KEY_MAPPINGS, "next")),
        )
        self.assertNotIn(
            mapping_hint(ctl.LOG_VIEW_KEY_MAPPINGS, "return"), footer
        )
        self.assertNotIn(
            mapping_hint(ctl.LOG_VIEW_KEY_MAPPINGS, "log_directory"), footer
        )
        self.assertNotIn("\n", footer)
        for _action, _visible, hidden, _word, _desc in ctl.LOG_VIEW_KEY_MAPPINGS:
            if hidden:
                self.assertNotIn(hidden, footer)

    def test_clear_result_banner_uses_dim_color_after_clean(self):
        parts = ctl.log_view_banner_parts()
        hint = mapping_hint(ctl.LOG_VIEW_KEY_MAPPINGS, "clear_result")
        clean_hint = mapping_hint(ctl.LOG_VIEW_KEY_MAPPINGS, "clean_logs")
        hint_part = (f" {hint}", "dim_bright_black")
        clean_part = (f" {clean_hint}", "bright_black")
        separator = (" |", "bright_black")

        self.assertIn(hint_part, parts)
        self.assertGreater(parts.index(hint_part), parts.index(clean_part))
        self.assertEqual(parts[parts.index(hint_part) - 1], separator)

    def test_banner_actions_follow_config_order(self):
        self.assertEqual(
            [action for action, _keys, _label in ctl.log_view_banner_items()],
            list(ctl.LOG_VIEW_BANNER_ACTIONS),
        )

    def test_f_toggles_between_follow_and_paging_modes(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller,
            "ddns-go_20260803-071001.log",
            "\n".join(f"第{i}行" for i in range(45)) + "\n",
        )
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = TtyStream()
        keys = iter(["f", "f", "x"])

        def next_key():
            return next(keys)

        def refresh():
            return False, None

        with mock.patch.object(sys, "stdout", stream):
            result = ctl._run_log_view_loop(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                reader,
                25,
                True,
                ctl.LOG_VIEW_MODE_FOLLOW,
                next_key,
                refresh,
                0.0,
            )

        self.assertEqual(result.text, "已退出日志查看。")
        self.assertEqual(len(stream.chunks), 3)
        self.assertIn("当页行区间: 26-45", stream.chunks[0])
        self.assertIn("连续模式 / 翻页模式", stream.chunks[0])
        self.assertIn(
            mapping_hint(ctl.LOG_VIEW_KEY_MAPPINGS, "toggle_mode"),
            stream.chunks[0],
        )
        self.assertIn("页 3/3", stream.chunks[1])
        self.assertIn("当页行区间: 41-45", stream.chunks[1])
        self.assertIn("第40行", stream.chunks[1])
        self.assertIn("连续模式 / 翻页模式", stream.chunks[1])
        self.assertIn("操作结果：已切换为翻页模式", stream.chunks[1])
        self.assertIn("当页行区间: 26-45", stream.chunks[2])
        self.assertIn("末尾跟随中", stream.chunks[2])
        self.assertIn("操作结果：已切换为连续模式", stream.chunks[2])

    def test_f_toggles_to_paging_while_paused(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller,
            "ddns-go_20260803-071001.log",
            "\n".join(f"第{i}行" for i in range(45)),
        )
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = TtyStream()
        keys = iter(["h", "f", "x"])

        def next_key():
            return next(keys)

        def refresh():
            return False, None

        with mock.patch.object(sys, "stdout", stream):
            result = ctl._run_log_view_loop(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                reader,
                25,
                True,
                ctl.LOG_VIEW_MODE_FOLLOW,
                next_key,
                refresh,
                0.0,
            )

        self.assertEqual(result.text, "已退出日志查看。")
        self.assertEqual(len(stream.chunks), 3)
        self.assertIn("当页行区间: 1-20", stream.chunks[1])
        self.assertIn("暂停末尾跟随", stream.chunks[1])
        self.assertIn("页 1/3", stream.chunks[2])
        self.assertIn("暂停末尾跟随", stream.chunks[2])
        self.assertIn("操作结果：已切换为翻页模式", stream.chunks[2])

    def test_e_toggles_follow_state_in_place(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller,
            "ddns-go_20260803-071001.log",
            "\n".join(f"第{i}行" for i in range(45)) + "\n",
        )
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = TtyStream()
        keys = iter(["e", "e", "x"])

        def next_key():
            return next(keys)

        def refresh():
            return False, None

        with mock.patch.object(sys, "stdout", stream):
            result = ctl._run_log_view_loop(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                reader,
                25,
                True,
                ctl.LOG_VIEW_MODE_FOLLOW,
                next_key,
                refresh,
                0.0,
            )

        self.assertEqual(result.text, "已退出日志查看。")
        self.assertEqual(len(stream.chunks), 3)
        self.assertIn("当页行区间: 26-45", stream.chunks[1])
        self.assertIn("暂停末尾跟随", stream.chunks[1])
        self.assertIn("操作结果：已暂停末尾跟随", stream.chunks[1])
        self.assertIn("当页行区间: 26-45", stream.chunks[2])
        self.assertIn("末尾跟随中", stream.chunks[2])
        self.assertIn("操作结果：已开启末尾跟随", stream.chunks[2])

    def test_paused_at_end_stays_paused_when_log_grows(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller,
            "ddns-go_20260803-071001.log",
            "\n".join(f"第{i}行" for i in range(45)) + "\n",
        )
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = TtyStream()
        keys = iter([None, "x"])

        def next_key():
            return next(keys)

        def refresh():
            with log_path.open("a", encoding="utf-8") as output:
                output.write("\n".join(f"新{i}行" for i in range(10)) + "\n")
            return reader.update(), None

        with mock.patch.object(sys, "stdout", stream):
            result = ctl._run_log_view_loop(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                reader,
                25,
                False,
                ctl.LOG_VIEW_MODE_FOLLOW,
                next_key,
                refresh,
                0.0,
            )

        self.assertEqual(result.text, "已退出日志查看。")
        self.assertEqual(len(stream.chunks), 2)
        self.assertIn("总行数: 55", stream.chunks[1])
        self.assertIn("当页行区间: 26-45", stream.chunks[1])
        self.assertIn("暂停末尾跟随", stream.chunks[1])

    def test_follow_mode_paused_keeps_page_when_log_grows(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller,
            "ddns-go_20260803-071001.log",
            "\n".join(f"第{i}行" for i in range(45)) + "\n",
        )
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = TtyStream()
        keys = iter([None, "x"])

        def next_key():
            return next(keys)

        def refresh():
            with log_path.open("a", encoding="utf-8") as output:
                output.write("\n".join(f"新{i}行" for i in range(10)) + "\n")
            return reader.update(), None

        with mock.patch.object(sys, "stdout", stream):
            result = ctl._run_log_view_loop(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                reader,
                20,
                False,
                ctl.LOG_VIEW_MODE_FOLLOW,
                next_key,
                refresh,
                0.0,
            )

        self.assertEqual(result.text, "已退出日志查看。")
        self.assertEqual(len(stream.chunks), 2)
        self.assertIn("当页行区间: 21-40", stream.chunks[1])
        self.assertIn("暂停末尾跟随", stream.chunks[1])

    def test_paging_mode_follow_advances_on_new_page(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller,
            "ddns-go_20260803-071001.log",
            "\n".join(f"第{i}行" for i in range(45)) + "\n",
        )
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = TtyStream()
        keys = iter([None, "x"])

        def next_key():
            return next(keys)

        def refresh():
            with log_path.open("a", encoding="utf-8") as output:
                output.write("\n".join(f"新{i}行" for i in range(16)) + "\n")
            return reader.update(), None

        with mock.patch.object(sys, "stdout", stream):
            result = ctl._run_log_view_loop(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                reader,
                40,
                True,
                ctl.LOG_VIEW_MODE_PAGING,
                next_key,
                refresh,
                0.0,
            )

        self.assertEqual(result.text, "已退出日志查看。")
        self.assertEqual(len(stream.chunks), 2)
        self.assertIn("页 4/4", stream.chunks[1])
        self.assertIn("跟随中", stream.chunks[1])

    def test_jump_goes_to_line_in_follow_mode_and_pauses(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller,
            "ddns-go_20260803-071001.log",
            "\n".join(f"第{i}行" for i in range(45)) + "\n",
        )
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = TtyStream()
        keys = iter(["j", "x"])

        def next_key():
            return next(keys)

        def refresh():
            return False, None

        with (
            mock.patch.object(ctl, "read_line_number", return_value=21),
            mock.patch.object(sys, "stdout", stream),
        ):
            result = ctl._run_log_view_loop(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                reader,
                25,
                True,
                ctl.LOG_VIEW_MODE_FOLLOW,
                next_key,
                refresh,
                0.0,
            )

        self.assertEqual(result.text, "已退出日志查看。")
        self.assertEqual(len(stream.chunks), 3)
        self.assertIn("操作结果：提示：跳行输入中，仅数字键有效", stream.chunks[1])
        self.assertIn("当页行区间: 21-40", stream.chunks[2])
        self.assertIn("暂停末尾跟随", stream.chunks[2])
        self.assertIn("操作结果：已跳转到第 21 行", stream.chunks[2])

    def test_invalid_key_appends_after_operation_result(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller,
            "ddns-go_20260803-071001.log",
            "\n".join(f"第{i}行" for i in range(45)) + "\n",
        )
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = TtyStream()
        keys = iter(["j", "z", "z", "x"])

        def next_key():
            return next(keys)

        def refresh():
            return False, None

        with (
            mock.patch.object(ctl, "read_line_number", return_value=21),
            mock.patch.object(sys, "stdout", stream),
        ):
            result = ctl._run_log_view_loop(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                reader,
                25,
                True,
                ctl.LOG_VIEW_MODE_FOLLOW,
                next_key,
                refresh,
                0.0,
            )

        self.assertEqual(result.text, "已退出日志查看。")
        self.assertEqual(len(stream.chunks), 5)
        self.assertIn(
            "操作结果：提示：无效按键",
            stream.chunks[4],
        )
        self.assertEqual(
            stream.chunks[4].count("提示：无效按键"),
            1,
        )

    def test_jump_goes_to_page_containing_line_in_paging_mode(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller,
            "ddns-go_20260803-071001.log",
            "\n".join(f"第{i}行" for i in range(45)) + "\n",
        )
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = TtyStream()
        keys = iter(["j", "j", "x"])

        def next_key():
            return next(keys)

        def refresh():
            return False, None

        with (
            mock.patch.object(ctl, "read_line_number", side_effect=[21, 45]),
            mock.patch.object(sys, "stdout", stream),
        ):
            result = ctl._run_log_view_loop(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                reader,
                40,
                True,
                ctl.LOG_VIEW_MODE_PAGING,
                next_key,
                refresh,
                0.0,
            )

        self.assertEqual(result.text, "已退出日志查看。")
        self.assertEqual(len(stream.chunks), 5)
        self.assertIn("操作结果：提示：跳行输入中，仅数字键有效", stream.chunks[1])
        self.assertIn("页 2/3", stream.chunks[2])
        self.assertIn("暂停末尾跟随", stream.chunks[2])
        self.assertIn("操作结果：已跳转到第 21 行", stream.chunks[2])
        self.assertIn("操作结果：提示：跳行输入中，仅数字键有效", stream.chunks[3])
        self.assertIn("页 3/3", stream.chunks[4])
        self.assertIn("跟随中", stream.chunks[4])
        self.assertIn("操作结果：已跳转到第 45 行", stream.chunks[4])

    def test_jump_invalid_input_just_redraws(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller,
            "ddns-go_20260803-071001.log",
            "\n".join(f"第{i}行" for i in range(45)) + "\n",
        )
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = TtyStream()
        keys = iter(["j", "x"])

        def next_key():
            return next(keys)

        def refresh():
            return False, None

        with (
            mock.patch.object(ctl, "read_line_number", return_value=None),
            mock.patch.object(sys, "stdout", stream),
        ):
            result = ctl._run_log_view_loop(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                reader,
                25,
                True,
                ctl.LOG_VIEW_MODE_FOLLOW,
                next_key,
                refresh,
                0.0,
            )

        self.assertEqual(result.text, "已退出日志查看。")
        self.assertEqual(len(stream.chunks), 3)
        self.assertIn("操作结果：提示：跳行输入中，仅数字键有效", stream.chunks[1])
        self.assertIn("当页行区间: 26-45", stream.chunks[2])
        self.assertIn("跟随中", stream.chunks[2])
        self.assertIn("操作结果：-", stream.chunks[2])

    def test_jump_ignores_non_digit_during_input(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller,
            "ddns-go_20260803-071001.log",
            "\n".join(f"第{i}行" for i in range(45)) + "\n",
        )
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = TtyStream()
        keys = iter(["j", "x"])

        def next_key():
            return next(keys)

        def refresh():
            return False, None

        with (
            mock.patch.object(
                ctl,
                "_read_raw_console_key",
                side_effect=["1", "z", "2", "\r"],
            ),
            mock.patch.object(sys, "stdout", stream),
        ):
            result = ctl._run_log_view_loop(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                reader,
                25,
                True,
                ctl.LOG_VIEW_MODE_FOLLOW,
                next_key,
                refresh,
                0.0,
            )

        self.assertEqual(result.text, "已退出日志查看。")
        self.assertGreaterEqual(len(stream.chunks), 3)
        self.assertIn(
            "操作结果：提示：跳行输入中，仅数字键有效",
            stream.getvalue(),
        )
        self.assertIn("操作结果：已跳转到第 12 行", stream.getvalue())
        self.assertNotIn("操作结果：提示：仅数字键有效", stream.getvalue())

    def test_unknown_key_shows_invalid_hint(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller,
            "ddns-go_20260803-071001.log",
            "\n".join(f"第{i}行" for i in range(45)) + "\n",
        )
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = TtyStream()
        keys = iter(["z", "x"])

        def next_key():
            return next(keys)

        def refresh():
            return False, None

        with mock.patch.object(sys, "stdout", stream):
            result = ctl._run_log_view_loop(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                reader,
                25,
                True,
                ctl.LOG_VIEW_MODE_FOLLOW,
                next_key,
                refresh,
                0.0,
            )

        self.assertEqual(result.text, "已退出日志查看。")
        self.assertEqual(len(stream.chunks), 2)
        self.assertIn("操作结果：提示：无效按键", stream.chunks[1])
        self.assertIn("当页行区间: 26-45", stream.chunks[1])
        self.assertIn("末尾跟随中", stream.chunks[1])

    def test_jump_out_of_range_shows_message_and_keeps_position(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller,
            "ddns-go_20260803-071001.log",
            "\n".join(f"第{i}行" for i in range(45)) + "\n",
        )
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = TtyStream()
        keys = iter(["j", "x"])

        def next_key():
            return next(keys)

        def refresh():
            return False, None

        with (
            mock.patch.object(ctl, "read_line_number", return_value=999),
            mock.patch.object(sys, "stdout", stream),
        ):
            result = ctl._run_log_view_loop(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                reader,
                25,
                True,
                ctl.LOG_VIEW_MODE_FOLLOW,
                next_key,
                refresh,
                0.0,
            )

        self.assertEqual(result.text, "已退出日志查看。")
        self.assertEqual(len(stream.chunks), 3)
        self.assertIn("操作结果：提示：跳行输入中，仅数字键有效", stream.chunks[1])
        self.assertIn("当页行区间: 26-45", stream.chunks[2])
        self.assertIn("跟随中", stream.chunks[2])
        self.assertIn("操作结果：行号 999 不存在，有效范围 1-45", stream.chunks[2])

    def test_jump_empty_log_shows_message(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller, "ddns-go_20260803-071001.log", ""
        )
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = TtyStream()
        keys = iter(["j", "x"])

        def next_key():
            return next(keys)

        def refresh():
            return False, None

        with (
            mock.patch.object(ctl, "read_line_number", return_value=1),
            mock.patch.object(sys, "stdout", stream),
        ):
            result = ctl._run_log_view_loop(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                reader,
                0,
                True,
                ctl.LOG_VIEW_MODE_FOLLOW,
                next_key,
                refresh,
                0.0,
            )

        self.assertEqual(result.text, "已退出日志查看。")
        self.assertEqual(len(stream.chunks), 3)
        self.assertIn("操作结果：提示：跳行输入中，仅数字键有效", stream.chunks[1])
        self.assertIn("操作结果：日志暂无内容", stream.chunks[2])

    def test_jump_page_in_paging_mode(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller,
            "ddns-go_20260803-071001.log",
            "\n".join(f"第{i}行" for i in range(45)) + "\n",
        )
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = TtyStream()
        keys = iter(["k", "k", "x"])

        def next_key():
            return next(keys)

        def refresh():
            return False, None

        with (
            mock.patch.object(ctl, "read_line_number", side_effect=[2, 3]),
            mock.patch.object(sys, "stdout", stream),
        ):
            result = ctl._run_log_view_loop(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                reader,
                40,
                True,
                ctl.LOG_VIEW_MODE_PAGING,
                next_key,
                refresh,
                0.0,
            )

        self.assertEqual(result.text, "已退出日志查看。")
        self.assertEqual(len(stream.chunks), 5)
        self.assertIn("操作结果：提示：跳页输入中，仅数字键有效", stream.chunks[1])
        self.assertIn("页 2/3", stream.chunks[2])
        self.assertIn("当页行区间: 21-40", stream.chunks[2])
        self.assertIn("暂停末尾跟随", stream.chunks[2])
        self.assertIn("操作结果：已跳转到第 2 页", stream.chunks[2])
        self.assertIn("操作结果：提示：跳页输入中，仅数字键有效", stream.chunks[3])
        self.assertIn("页 3/3", stream.chunks[4])
        self.assertIn("末尾跟随中", stream.chunks[4])
        self.assertIn("操作结果：已跳转到第 3 页", stream.chunks[4])

    def test_jump_page_out_of_range_shows_message(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller,
            "ddns-go_20260803-071001.log",
            "\n".join(f"第{i}行" for i in range(45)) + "\n",
        )
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = TtyStream()
        keys = iter(["k", "x"])

        def next_key():
            return next(keys)

        def refresh():
            return False, None

        with (
            mock.patch.object(ctl, "read_line_number", return_value=99),
            mock.patch.object(sys, "stdout", stream),
        ):
            result = ctl._run_log_view_loop(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                reader,
                40,
                True,
                ctl.LOG_VIEW_MODE_PAGING,
                next_key,
                refresh,
                0.0,
            )

        self.assertEqual(result.text, "已退出日志查看。")
        self.assertEqual(len(stream.chunks), 3)
        self.assertIn("操作结果：提示：跳页输入中，仅数字键有效", stream.chunks[1])
        self.assertIn("页 3/3", stream.chunks[2])
        self.assertIn("末尾跟随中", stream.chunks[2])
        self.assertIn("操作结果：页码 99 不存在，有效范围 1-3", stream.chunks[2])

    def test_jump_page_only_in_paging_mode(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller,
            "ddns-go_20260803-071001.log",
            "\n".join(f"第{i}行" for i in range(45)) + "\n",
        )
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = TtyStream()
        keys = iter(["k", "x"])

        def next_key():
            return next(keys)

        def refresh():
            return False, None

        with (
            mock.patch.object(
                ctl,
                "read_line_number",
                side_effect=AssertionError("跳页不应读取行号"),
            ),
            mock.patch.object(sys, "stdout", stream),
        ):
            result = ctl._run_log_view_loop(
                controller,
                ui,
                log_path,
                "当前运行时日志",
                reader,
                25,
                True,
                ctl.LOG_VIEW_MODE_FOLLOW,
                next_key,
                refresh,
                0.0,
            )

        self.assertEqual(result.text, "已退出日志查看。")
        self.assertEqual(len(stream.chunks), 2)
        self.assertIn("当页行区间: 26-45", stream.chunks[1])
        self.assertIn("末尾跟随中", stream.chunks[1])
        self.assertIn("操作结果：提示：跳页仅翻页模式可用", stream.chunks[1])

    def test_log_view_reader_increments_and_keeps_partial_line(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        log_path = Path(temporary.name) / "ddns-go.log"
        log_path.write_text("第一行\n第二行\n", encoding="utf-8")
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)

        self.assertEqual(reader.lines(), ["第一行", "第二行"])
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write("第三行\n第四行")

        self.assertTrue(reader.update())
        self.assertEqual(
            reader.lines(), ["第一行", "第二行", "第三行", "第四行"]
        )
        self.assertFalse(reader.update())

    def test_log_view_reader_resets_on_truncation(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        log_path = Path(temporary.name) / "ddns-go.log"
        log_path.write_text("旧内容\n", encoding="utf-8")
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)

        self.assertEqual(reader.lines(), ["旧内容"])
        log_path.write_text("新行\n", encoding="utf-8")

        self.assertTrue(reader.update())
        self.assertEqual(reader.lines(), ["新行"])

    def test_log_view_reader_lines_share_internal_list_without_partial_line(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        log_path = Path(temporary.name) / "ddns-go.log"
        log_path.write_text("第一行\n第二行\n", encoding="utf-8")
        reader = ctl.LogViewReader(log_path)
        self.addCleanup(reader.close)

        first = reader.lines()
        self.assertIs(first, reader.lines())
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write("第三行")
        self.assertTrue(reader.update())
        partial = reader.lines()
        self.assertIsNot(partial, first)
        self.assertEqual(partial, ["第一行", "第二行", "第三行"])

    def test_run_menu_enters_log_viewer_and_returns(self):
        controller = self.make_controller()
        log_path = self.make_log(
            controller, "ddns-go_20260803-071001.log", "运行日志内容"
        )
        state = controller.new_state(
            "Running", "运行中", "test", "green", log_path=log_path
        )
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = io.StringIO()

        with (
            mock.patch.object(controller, "refresh_state", return_value=state),
            mock.patch.object(
                ctl, "read_immediate_key", side_effect=["v", "x", "q"]
            ),
            mock.patch.object(sys, "stdout", stream),
        ):
            exit_code = ctl.run_menu(controller, ui)

        self.assertEqual(exit_code, 0)
        self.assertIn(
            f"查看日志中 | 每页 {ctl.LOG_VIEW_PAGE_LINES} 行 | "
            + mapping_hint(ctl.LOG_VIEW_KEY_MAPPINGS, "return"),
            stream.getvalue(),
        )
        self.assertIn("运行日志内容", stream.getvalue())


class PollUntilTests(unittest.TestCase):
    """通用轮询辅助：成功与超时两条路径。"""

    def test_poll_until_returns_true_when_predicate_succeeds(self):
        calls = {"count": 0}

        def succeed_on_second_call() -> bool:
            calls["count"] += 1
            return calls["count"] >= 2

        deadline = time.monotonic() + 1
        self.assertTrue(ctl.poll_until(deadline, 0.01, succeed_on_second_call))
        self.assertGreaterEqual(calls["count"], 2)

    def test_poll_until_returns_false_on_timeout(self):
        deadline = time.monotonic() + 0.02
        self.assertFalse(ctl.poll_until(deadline, 0.005, lambda: False))


class ActionResultTests(unittest.TestCase):
    """界面颜色由动作显式给出，不再靠匹配提示文案推断。"""

    def test_default_color_is_green(self):
        self.assertEqual(ctl.ActionResult("ok").color, "green")

    def test_invoke_menu_action_passes_through_color(self):
        result = ctl.invoke_menu_action(
            lambda: ctl.ActionResult("完成", "yellow")
        )
        self.assertEqual(
            (result.text, result.color, result.detail_lines),
            ("完成", "yellow", ()),
        )

    def test_invoke_menu_action_passes_through_detail_lines(self):
        detail_lines = (("PID", "1234"), ("日志", "ddns-go.log"))
        result = ctl.invoke_menu_action(
            lambda: ctl.ActionResult("完成", "green", detail_lines)
        )
        self.assertEqual((result.text, result.color), ("完成", "green"))
        self.assertEqual(result.detail_lines, detail_lines)

    def test_invoke_menu_action_renders_errors_in_red(self):
        def failing_action():
            raise ctl.DdnsError("出错了")

        result = ctl.invoke_menu_action(failing_action)
        self.assertEqual(result.color, "red")
        self.assertIn("出错了", result.text)
        self.assertEqual(result.detail_lines, ())


class ConsoleUITests(unittest.TestCase):
    def test_render_shows_absolute_directory_and_relative_paths(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        controller = ctl.DdnsController(Path(temporary.name))
        controller.executable_path.touch()
        state = controller.new_state("Stopped", "未运行", "test", "bright_black")
        ui = ctl.ConsoleUI()
        ui.use_color = False
        stream = io.StringIO()
        with mock.patch.object(sys, "stdout", stream):
            ui.render(state, controller, None)
        text = stream.getvalue()
        self.assertIn(str(controller.script_directory) + os.sep, text)
        self.assertIn("ddns-go_ctl.py", text)
        self.assertIn("ddns-go.exe", text)
        self.assertIn("ctl-data/.ddns_go_config.yaml", text)
        self.assertNotIn(
            str(controller.script_directory) + os.sep + "ddns-go.exe", text
        )
        self.assertIn("操作结果：-", text)

    def test_operation_result_label_uses_default_color(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        controller = ctl.DdnsController(Path(temporary.name))
        controller.executable_path.touch()
        state = controller.new_state("Stopped", "未运行", "test", "bright_black")
        ui = ctl.ConsoleUI()
        ui.use_color = True
        stream = io.StringIO()
        result = ctl.ActionResult("提示：无效按键", "yellow")

        with mock.patch.object(sys, "stdout", stream):
            ui.render(state, controller, result)

        output = stream.getvalue()
        self.assertIn(" 操作结果：\033[33m提示：无效按键\033[0m", output)
        self.assertNotIn("\033[90m 操作结果：", output)

    def test_render_dims_secondary_letter_keys(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        controller = ctl.DdnsController(Path(temporary.name))
        controller.executable_path.touch()
        state = controller.new_state("Stopped", "未运行", "test", "bright_black")
        ui = ctl.ConsoleUI()
        ui.use_color = True
        stream = io.StringIO()

        with mock.patch.object(sys, "stdout", stream):
            ui.render(state, controller, None)

        output = stream.getvalue()
        for _action, visible, _hidden, _word, _desc in ctl.MENU_KEY_MAPPINGS:
            if visible[0].isdigit() and "/" in visible:
                primary, secondary = visible.split("/", 1)
                self.assertIn(
                    f"[{primary}{ui.colorize(f'/{secondary}', 'bright_black')}]",
                    output,
                )
        self.assertIn(
            mapping_hint(ctl.MENU_KEY_MAPPINGS, "refresh"), output
        )
        self.assertIn(
            mapping_hint(ctl.MENU_KEY_MAPPINGS, "quit"), output
        )

    def test_clear_result_hint_uses_gray_color_and_is_last(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        controller = ctl.DdnsController(Path(temporary.name))
        controller.executable_path.touch()
        state = controller.new_state("Stopped", "未运行", "test", "bright_black")
        ui = ctl.ConsoleUI()
        ui.use_color = True
        stream = io.StringIO()

        with mock.patch.object(sys, "stdout", stream):
            ui.render(state, controller, None)

        output = stream.getvalue()
        hint = mapping_hint(ctl.MENU_KEY_MAPPINGS, "clear_result")
        quit_hint = mapping_hint(ctl.MENU_KEY_MAPPINGS, "quit")
        self.assertIn(ui.colorize(f" {hint}", "bright_black"), output)
        self.assertGreater(output.index(hint), output.index(quit_hint))

    def test_detail_label_uses_cyan_and_value_keeps_action_color(self):
        ui = ctl.ConsoleUI()
        ui.use_color = True

        line = ui.detail_line("PID", "1234", "green")

        self.assertIn("\033[36mPID\033[0m", line)
        self.assertIn(":", line)
        self.assertIn("\033[32m1234\033[0m", line)

    def test_render_clears_screen_and_scrollback_when_tty(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        controller = ctl.DdnsController(Path(temporary.name))
        controller.executable_path.touch()
        state = controller.new_state("Stopped", "未运行", "test", "bright_black")
        ui = ctl.ConsoleUI()
        ui.use_color = False

        class TtyStream(io.StringIO):
            def isatty(self) -> bool:
                return True

        stream = TtyStream()
        with mock.patch.object(sys, "stdout", stream):
            ui.render(state, controller, None)

        self.assertTrue(stream.getvalue().startswith("\033[2J\033[3J\033[H"))


class SingleInstanceMutexTests(unittest.TestCase):
    def test_second_instance_is_rejected(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)

        with ctl.SingleInstanceMutex(directory):
            with self.assertRaises(ctl.DdnsError):
                with ctl.SingleInstanceMutex(directory):
                    pass

        # 前一个实例退出后应当可以再次获取。
        with ctl.SingleInstanceMutex(directory):
            pass

    def test_different_directories_do_not_collide(self):
        first = tempfile.TemporaryDirectory()
        second = tempfile.TemporaryDirectory()
        self.addCleanup(first.cleanup)
        self.addCleanup(second.cleanup)

        with ctl.SingleInstanceMutex(Path(first.name)):
            with ctl.SingleInstanceMutex(Path(second.name)):
                pass


if __name__ == "__main__":
    if os.name != "nt":
        print("该测试仅支持 Windows。", file=sys.stderr)
        raise SystemExit(1)
    unittest.main(verbosity=2)
