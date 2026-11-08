"""智能中控屏固件模拟器。

用 Python 模拟嵌入式固件的运行时行为，供自动化测试平台驱动：
真实项目中这一层是 C 固件 + 串口/ADB 通讯，对外接口保持一致（串口日志 + 屏幕状态）。
"""

from __future__ import annotations

VERSION = "1.4.0"


class Panel:
    """设备状态机：boot / provision / ota / reconnect。"""

    def __init__(self) -> None:
        self.screen = "off"
        self.version = VERSION
        self.online = False
        self.logs: list[str] = []

    def _log(self, line: str) -> None:
        self.logs.append(f"[uart] {line}")

    # ------------------------------------------------------------ 上电启动

    def boot(self) -> bool:
        self._log("bootloader ok")
        self._log("kernel ok")
        self._log("ui_service started")
        self.screen = "home"
        self._log("screen=home")
        return True

    # ------------------------------------------------------------ Wi-Fi 配网

    def provision(self, ssid: str = "TestAP", ack_timeout_s: int = 10) -> bool:
        self._log(f"provision start ssid={ssid}")
        elapsed = 0
        for attempt in range(1, 6):
            elapsed += 2
            self._log(f"prov_retry={attempt} elapsed={elapsed}s")
            if elapsed >= ack_timeout_s:
                self._log("ERROR: ack_timeout, cloud ack not received")
                self._log("system_reset triggered by prov_watchdog")
                self.screen = "boot_loop"
                self.online = False
                return False
        self.online = True
        self.screen = "provisioned"
        self._log("mqtt connected")
        return True

    # ------------------------------------------------------------ OTA 升级

    def ota(self, target: str = "1.4.2") -> bool:
        self._log(f"ota_start target=v{target}")
        self._log("ota_write_ok")
        self._log("ota_done")
        # 升级完成后直接重启，未切换启动分区
        if self.version != target:
            self._log(f"version=v{self.version}")
            self._log("ERROR: version_mismatch after ota")
            self.screen = "ota_failed"
            return False
        return True

    # ------------------------------------------------------------ 断网重连

    def reconnect(self, offline_s: int = 60) -> bool:
        self._log(f"link_down {offline_s}s")
        self._log("link_up")
        self._log("mqtt reconnected")
        self.online = True
        self.screen = "home"
        return True
