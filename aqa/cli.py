"""命令行入口。

    python -m aqa run --mock
    python -m aqa run --model-mode api --repo owner/firmware --commit abc1234
    python -m aqa serve --port 8080
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Settings
from .orchestrator import Orchestrator


def _cmd_run(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    if args.mock:
        settings.model_mode = "mock"
        settings.device_kind = "mock"
        settings.build_kind = "mock"
        settings.bugtracker_kind = "local"
    else:
        if args.model_mode:
            settings.model_mode = args.model_mode
        if args.device:
            settings.device_kind = args.device
        if args.bugtracker:
            settings.bugtracker_kind = args.bugtracker
    if args.no_confirm:
        settings.require_human_confirm = False

    event = None
    if args.event:
        event = json.loads(Path(args.event).read_text(encoding="utf-8"))

    repo_dir = Path(args.repo_dir or ".").resolve()
    orch = Orchestrator(settings)
    ctx, outcomes = orch.run(
        event=event, repo=args.repo or "", commit=args.commit or "", repo_dir=repo_dir
    )

    report = ctx.artifacts / "report.md"
    if report.exists():
        print(report.read_text(encoding="utf-8"))
    return 0 if all(o.ok for o in outcomes) else 1


def _cmd_demo(args: argparse.Namespace) -> int:
    """一键演示：AI 生成产品代码 → 打包 → 部署到模拟设备 → 设计测试 → 执行 → 报告 → 修 bug。

    与 run 的区别：run 测的是「已存在的代码」，demo 从「一句需求」开始，
    连被测产品本身都是 AI 写出来的。
    """
    settings = Settings.from_env()
    settings.generate_product = True
    settings.device_kind = "sim"          # 真实执行生成的产品
    settings.build_kind = "package"       # 打包成固件
    settings.bugtracker_kind = "local"
    if not args.model_mode or args.model_mode == "mock":
        settings.model_mode = "mock"
    else:
        settings.model_mode = args.model_mode
    if args.requirement:
        settings.product_requirement = args.requirement
    if args.buggy:
        # 演示「被测版本确实有缺陷」时的完整闭环：让 AI 把缺陷写进产品，
        # 再看平台能不能靠日志与设备状态把它们找出来。
        settings.product_requirement += (
            "\n\n【重要】这个版本是待测版本，请保留以下两处真实缺陷，不要修复、"
            "也不要在日志里声明这是缺陷：\n"
            "1) 配网：不区分正确/错误凭据，等待云端 ACK 10 秒后直接 system_reset 重启；\n"
            "2) OTA：打印 ota_done，但从不真正切换固件版本，version 始终为初始版本。"
        )

    orch = Orchestrator(settings)
    ctx, outcomes = orch.run(repo=args.repo or "demo/ai-generated-firmware", commit="auto", quiet=False)

    report = ctx.artifacts / "report.md"
    if report.exists():
        print(report.read_text(encoding="utf-8"))
    return 0 if all(o.ok for o in outcomes) else 1


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn  # noqa: PLC0415 - 可选依赖
    except ImportError:
        print("需要先安装：pip install fastapi uvicorn", file=sys.stderr)
        return 2
    uvicorn.run("aqa.api:app", host=args.host, port=args.port, reload=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aqa", description="AI 原生自动化测试平台")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="执行一次完整测试流水线")
    run.add_argument("--mock", action="store_true", help="离线演示：mock 模型 + mock 设备，无需任何密钥")
    run.add_argument("--model-mode", choices=["mock", "api"], default=None)
    run.add_argument("--device", choices=["mock", "adb", "serial"], default=None)
    run.add_argument("--bugtracker", choices=["local", "github"], default=None)
    run.add_argument("--repo", default="", help="owner/name")
    run.add_argument("--commit", default="", help="提交 SHA（可截断）")
    run.add_argument("--event", default="", help="GitHub webhook payload JSON 文件")
    run.add_argument("--repo-dir", default="", help="本地源码目录（构建用）")
    run.add_argument("--no-confirm", action="store_true", help="跳过人工确认，自动确认全部缺陷")
    run.set_defaults(func=_cmd_run)

    serve = sub.add_parser("serve", help="启动 webhook 服务")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)
    serve.set_defaults(func=_cmd_serve)

    demo = sub.add_parser("demo", help="一键演示：AI 写代码 → 打包 → 测 → 报告 → 提 bug")
    demo.add_argument("--model-mode", choices=["mock", "api"], default="mock")
    demo.add_argument("--requirement", default="", help="自定义产品需求描述")
    demo.add_argument("--repo", default="", help="报告里显示的仓库名")
    demo.add_argument(
        "--buggy",
        action="store_true",
        help="让 AI 在生成的产品里保留两处真实缺陷，用于演示缺陷发现闭环",
    )
    demo.set_defaults(func=_cmd_demo)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
