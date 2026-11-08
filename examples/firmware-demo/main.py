"""设备入口：python main.py <scenario>"""

from __future__ import annotations

import argparse
import json

from panel.device import Panel

SCENARIOS = {
    "boot": lambda p: p.boot(),
    "provision": lambda p: p.provision(),
    "ota": lambda p: p.ota(),
    "reconnect": lambda p: p.reconnect(),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="smart panel simulator")
    parser.add_argument("scenario", choices=sorted(SCENARIOS))
    args = parser.parse_args()

    panel = Panel()
    panel.boot()  # 任何场景都从上电开始
    ok = SCENARIOS[args.scenario](panel)

    for line in panel.logs:
        print(line)
    print("STATE " + json.dumps({
        "screen": panel.screen,
        "online": panel.online,
        "version": panel.version,
        "ok": ok,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
