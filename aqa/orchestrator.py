"""流水线编排：负责顺序执行、失败重试、进度输出与产物落盘。"""

from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapters.bugtracker import build_tracker
from .adapters.build import build_builder
from .adapters.device import build_device
from .config import Settings
from .context import RunContext, new_run_id
from .llm import build_models
from .stages import ProductStage, Runtime, Stage, default_stages


@dataclass
class StageOutcome:
    name: str
    label: str
    ok: bool
    attempts: int
    elapsed_s: float
    error: str = ""


class Orchestrator:
    def __init__(self, settings: Settings, stages: list[Stage] | None = None) -> None:
        self.settings = settings
        # 开启产品生成时，把「AI 写代码」作为第 0 层插到最前面
        prefix = [ProductStage()] if settings.generate_product else []
        self.stages = stages or [*prefix, *default_stages()]

    # ------------------------------------------------------------- 依赖装配

    def _runtime(self, repo_dir: Path) -> Runtime:
        s = self.settings
        return Runtime(
            settings=s,
            models=build_models(s),
            device=build_device(s),
            builder=build_builder(s),
            tracker=build_tracker(s),
            repo_dir=repo_dir,
        )

    # ------------------------------------------------------------- 主流程

    def run(
        self,
        event: dict[str, Any] | None = None,
        repo: str = "",
        commit: str = "",
        repo_dir: Path | None = None,
        quiet: bool = False,
    ) -> tuple[RunContext, list[StageOutcome]]:
        self.settings.validate()

        # 先从事件里预取仓库与提交，保证启动横幅显示的信息是真实的
        event = event or {}
        if event:
            repo = repo or (event.get("repository") or {}).get("full_name", "")
            commit = commit or str(event.get("after") or event.get("commit") or "")[:12]

        run_id = new_run_id()
        artifacts = Path(self.settings.workspace) / "runs" / run_id
        ctx = RunContext(
            run_id=run_id,
            repo=repo or "local/repo",
            commit=commit or "HEAD",
            device_id=self.settings.device_id,
            artifacts=artifacts,
            data={"event": event or {}},
        )
        # 源码目录优先级：显式传入 → 配置的 checkout 目录 → 当前工作目录。
        # webhook 场景一定要配 AQA_SOURCE_DIR，否则会把平台自己的产物目录当源码。
        source = repo_dir or self.settings.source_dir or Path.cwd()
        rt = self._runtime(source)

        if not quiet:
            print(f"\n{'=' * 68}")
            print(f" AI-Native QA  |  run {run_id}")
            print(f" 仓库 {ctx.repo}  提交 {ctx.commit}  设备 {self.settings.device_kind}")
            print(f" 模型 {rt.models.mode}  产物 {artifacts}")
            print(f"{'=' * 68}")

        outcomes: list[StageOutcome] = []
        aborted = False

        for index, stage in enumerate(self.stages, 1):
            if aborted:
                outcomes.append(StageOutcome(stage.name, stage.label, False, 0, 0.0, "上游失败，跳过"))
                continue

            attempts = 0
            started = time.time()
            last_error = ""
            while attempts <= self.settings.max_retries:
                attempts += 1
                try:
                    result = stage.run(ctx, rt)
                    ctx.data[stage.name] = result
                    elapsed = time.time() - started
                    ctx.log(stage.name, "ok", f"第 {attempts} 次尝试成功", elapsed_s=round(elapsed, 2))
                    outcomes.append(StageOutcome(stage.name, stage.label, True, attempts, elapsed))
                    if not quiet:
                        print(f"[{index}/{len(self.stages)}] {stage.label:<8} ✅ "
                              f"{self._brief(result)}  ({elapsed:.2f}s)")
                    break
                except Exception as exc:  # noqa: BLE001 - 统一收集失败信息
                    last_error = f"{type(exc).__name__}: {exc}"
                    ctx.log(stage.name, "retry" if attempts <= self.settings.max_retries else "failed", last_error)
                    if attempts <= self.settings.max_retries:
                        if not quiet:
                            print(f"[{index}/{len(self.stages)}] {stage.label:<8} ↻ 重试 {attempts}/"
                                  f"{self.settings.max_retries}：{last_error[:90]}")
                        time.sleep(min(2 ** (attempts - 1), 5))
            else:
                elapsed = time.time() - started
                outcomes.append(StageOutcome(stage.name, stage.label, False, attempts, elapsed, last_error))
                if not quiet:
                    print(f"[{index}/{len(self.stages)}] {stage.label:<8} ❌ 失败：{last_error[:120]}")
                # 执行层失败仍要出报告；其余关键层失败则中止
                if stage.name in ("trigger", "build", "deploy", "plan"):
                    aborted = True

        try:
            rt.device.teardown()
        except Exception:  # noqa: BLE001
            pass

        summary = {
            "run_id": run_id,
            "ok": all(o.ok for o in outcomes),
            "stages": [o.__dict__ for o in outcomes],
            "artifacts": str(artifacts),
        }
        ctx.save("run.json", summary)
        ctx.save_text("stacktrace.log", traceback.format_exc() if aborted else "no fatal error\n")

        if not quiet:
            failed = [o for o in outcomes if not o.ok]
            print(f"{'-' * 68}")
            print(f" 结果：{'✅ 全链路通过' if not failed else '❌ 存在失败阶段'}")
            print(f" 报告：{artifacts / 'report.md'}")
            if (artifacts / "bugs.json").exists():
                bugs = json.loads((artifacts / "bugs.json").read_text(encoding="utf-8"))
                if bugs:
                    print(f" 缺陷：{len(bugs)} 条")
                    for bug in bugs:
                        print(f"   - {bug.get('id')} {bug.get('title')}  [{bug.get('status')}]")
            print(f"{'=' * 68}\n")

        return ctx, outcomes

    # ------------------------------------------------------------- 输出辅助

    @staticmethod
    def _brief(result: Any) -> str:
        if not isinstance(result, dict):
            return ""
        for key in ("files", "artifact", "path", "sha256", "total", "filed", "confirmed"):
            if key in result:
                return f"{key}={result[key]}"
        if "cases" in result:
            return f"{len(result['cases'])} 个用例"
        if "traces" in result:
            return f"{len(result['traces'])} 条轨迹"
        if "pass_rate" in result:
            return f"通过率 {result['pass_rate']}%"
        if "verdicts" in result:
            brief = f"PASS {result.get('passed')} / FAIL {result.get('failed')}"
            if result.get("inconclusive"):
                brief += f" / 无法判定 {result['inconclusive']}"
            if result.get("skipped"):
                brief += f" / 未执行 {result['skipped']}"
            return brief
        return ""
