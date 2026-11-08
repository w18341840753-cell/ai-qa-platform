"""九层流水线阶段实现。

每层只有一个职责，输入输出都落在 RunContext 里，方便单层替换、单层重跑。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapters.bugtracker import BugTracker
from .adapters.build import BuildAdapter
from .adapters.device import DeviceAdapter, Trace
from .context import RunContext
from .generator import MockProductGenerator, build_generator
from .llm import Models, extract_json


@dataclass
class Runtime:
    """运行期依赖集合（全部是适配器 / 模型，便于测试时整体替换）。"""

    settings: Any
    models: Models
    device: DeviceAdapter
    builder: BuildAdapter
    tracker: BugTracker
    repo_dir: Path


class Stage:
    name = "stage"
    label = "阶段"

    def run(self, ctx: RunContext, rt: Runtime) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------- 0. AI 生成产品代码


class ProductStage(Stage):
    """AI 原生的起点：让模型根据需求直接写出一套可运行的产品代码。

    api 模式下若模型产出不可用（缺 main.py / 非法 JSON），自动降级到内置模板，
    保证流水线不会因为「AI 写代码失败」而整轮白跑 —— 但降级事实会记录在产物里。
    """

    name = "product"
    label = "AI 生成产品代码"

    def run(self, ctx: RunContext, rt: Runtime) -> dict[str, Any]:
        out_dir = ctx.artifacts / "product"
        generator = build_generator(rt.models)
        try:
            product = generator.generate(rt.settings.product_requirement, out_dir)
        except Exception as exc:  # noqa: BLE001 - 生成失败必须可降级
            if rt.models.mode != "api":
                raise
            ctx.log(self.name, "warn", f"模型生成失败，降级到内置模板：{exc}")
            product = MockProductGenerator().generate(rt.settings.product_requirement, out_dir)
            product.generator = "mock(fallback)"

        payload = product.to_dict()
        payload["file_count"] = len(payload.get("files", []))
        ctx.save("product.json", payload)
        return payload


# ---------------------------------------------------------------- 1. 触发


class TriggerStage(Stage):
    name = "trigger"
    label = "触发"

    def run(self, ctx: RunContext, rt: Runtime) -> dict[str, Any]:
        event = ctx.data.get("event") or {}
        commit = (
            event.get("after")
            or event.get("commit")
            or (event.get("head_commit") or {}).get("id")
            or ctx.commit
        )
        branch = (event.get("ref") or "refs/heads/main").split("/")[-1]
        repo = (event.get("repository") or {}).get("full_name") or ctx.repo
        changed = event.get("changed_files") or ["src/net/provision.c", "src/ota/upgrade.c"]

        ctx.commit, ctx.branch, ctx.repo = str(commit)[:12], branch, repo
        normalized = {
            "repo": repo,
            "commit": ctx.commit,
            "branch": branch,
            "changed_files": changed,
            "author": (event.get("pusher") or {}).get("name", "unknown"),
        }
        ctx.save("event.json", normalized)
        return normalized


# ---------------------------------------------------------------- 2. 构建


class BuildStage(Stage):
    name = "build"
    label = "构建"

    def run(self, ctx: RunContext, rt: Runtime) -> dict[str, Any]:
        # 若上一步由 AI 生成了产品，就构建这份生成物；否则构建指定的源码目录
        product = ctx.data.get("product") or {}
        source = Path(product["path"]) if product.get("path") else rt.repo_dir
        artifact = rt.builder.build(source, ctx.artifacts)
        artifact["source"] = str(source)
        ctx.save("build.json", artifact)
        return artifact


# ---------------------------------------------------------------- 3. 部署到设备


class DeployStage(Stage):
    name = "deploy"
    label = "部署到设备"

    def run(self, ctx: RunContext, rt: Runtime) -> dict[str, Any]:
        prepared = rt.device.prepare()
        if not prepared.get("ready", False):
            raise RuntimeError(f"设备未就绪：{prepared}")
        artifact = ctx.data.get("build", {})
        deployed = rt.device.deploy(artifact)
        deployed["health_after_deploy"] = rt.device.health()
        ctx.save("deploy.json", deployed)
        return deployed


# ---------------------------------------------------------------- 4. AI 设计测试


PLAN_SYSTEM = (
    "你是资深嵌入式测试架构师。根据代码变更与设备能力设计回归测试用例。"
    "只输出 JSON，不要解释文字。格式："
    '{"rationale": "...", "cases": [{"id": "TC-01", "title": "...", '
    '"priority": "P0", "steps": ["..."], "expect": "..."}]}'
)


class PlanStage(Stage):
    name = "plan"
    label = "AI 设计测试"

    def run(self, ctx: RunContext, rt: Runtime) -> dict[str, Any]:
        trigger = ctx.data.get("trigger", {})
        prompt = (
            f"请为本次提交设计测试计划（test_plan）。\n"
            f"仓库：{trigger.get('repo')}\n提交：{trigger.get('commit')}\n"
            f"变更文件：{', '.join(trigger.get('changed_files', []))}\n"
            f"被测设备：{rt.settings.device_kind} / {rt.settings.device_id}\n"
            f"要求：覆盖变更直接影响的主路径，P0 用例聚焦启动、配网、升级。\n"
            f"每个用例必须带 scenario 字段，取值只能是 boot / provision / ota / reconnect 之一，"
            f"用于驱动设备执行对应场景。"
        )
        raw = rt.models.text.complete(PLAN_SYSTEM, prompt)
        plan = extract_json(raw)
        if not plan.get("cases"):
            raise RuntimeError("测试计划为空，模型输出不可用")
        for idx, case in enumerate(plan["cases"], 1):
            case.setdefault("id", f"TC-{idx:02d}")
            case.setdefault("priority", "P1")
        ctx.save("testplan.json", plan)
        return plan


# ---------------------------------------------------------------- 5. 执行


class ExecuteStage(Stage):
    name = "execute"
    label = "执行"

    def run(self, ctx: RunContext, rt: Runtime) -> dict[str, Any]:
        plan = ctx.data.get("plan", {})
        cases = plan.get("cases", [])
        shot_dir = ctx.artifacts / "traces"
        shot_dir.mkdir(exist_ok=True)

        traces: list[dict[str, Any]] = []
        for case in cases:
            try:
                trace = rt.device.run_case(case, shot_dir)
            except Exception as exc:  # noqa: BLE001 - 单个用例失败不应中断整轮
                trace = Trace(case_id=case["id"], ok_to_proceed=False, logs=f"执行异常：{exc}")
            traces.append(trace.to_dict())
        ctx.save("traces.json", traces)
        return {"total": len(traces), "traces": traces}


# ---------------------------------------------------------------- 6. AI 判定


JUDGE_SYSTEM = (
    "你是设备测试判定专家。根据用例预期、截图与串口日志判断该用例是否通过。"
    "只输出 JSON："
    '{"verdict": "PASS|FAIL", "confidence": 0.0, "reason": "...", "visual_evidence": "..."}'
)

RCA_SYSTEM = (
    "你是嵌入式固件缺陷分析专家。根据失败现象与日志做根因归因（root_cause）。"
    "只输出 JSON："
    '{"severity": "P0|P1|P2", "root_cause": "...", "evidence": "...", "suggestion": "..."}'
)


class JudgeStage(Stage):
    name = "judge"
    label = "AI 判定"

    def run(self, ctx: RunContext, rt: Runtime) -> dict[str, Any]:
        plan = {c["id"]: c for c in ctx.data.get("plan", {}).get("cases", [])}
        traces = ctx.data.get("execute", {}).get("traces", [])
        verdicts: list[dict[str, Any]] = []

        for trace in traces:
            case = plan.get(trace["case_id"], {})

            # 屏幕快照：真实设备是 PNG；模拟设备是文本渲染，读进来一起喂给模型
            shot = Path(trace["screenshot"]) if trace.get("screenshot") else None
            screen_text = ""
            if shot and shot.exists() and shot.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                screen_text = shot.read_text(encoding="utf-8", errors="replace")[:800]
                shot = None  # 非图片文件不能当图像输入

            prompt = (
                f"用例 {trace['case_id']}：{case.get('title', '')}\n"
                f"预期：{case.get('expect', '')}\n"
                f"SCREEN_HINT：{trace.get('hint', '')}\n"
                f"屏幕状态：\n{screen_text}\n"
                f"串口日志：\n{trace.get('logs', '')[:1200]}\n"
                f"请判断该用例是否通过。"
            )
            try:
                raw = rt.models.vision.analyze(JUDGE_SYSTEM, prompt, shot)
                result = extract_json(raw)
            except Exception as exc:  # noqa: BLE001 - 判定失败按不通过处理，避免漏报
                result = {
                    "verdict": "FAIL",
                    "confidence": 0.0,
                    "reason": f"判定过程异常：{exc}",
                    "visual_evidence": "",
                }

            record = {
                "case_id": trace["case_id"],
                "title": case.get("title", ""),
                "priority": case.get("priority", "P1"),
                "verdict": result.get("verdict", "FAIL"),
                "confidence": result.get("confidence", 0),
                "reason": result.get("reason", ""),
                "visual_evidence": result.get("visual_evidence", ""),
                "logs": trace.get("logs", ""),
            }

            # 失败才做归因，省 token 也省时间
            if record["verdict"] == "FAIL":
                rca_prompt = (
                    f"以下用例失败，请做根因归因（root_cause）。\n"
                    f"用例：{record['case_id']} {record['title']}\n"
                    f"现象：{record['reason']}\n串口日志：\n{record['logs'][:1500]}"
                )
                try:
                    record["rca"] = extract_json(rt.models.text.complete(RCA_SYSTEM, rca_prompt))
                except Exception as exc:  # noqa: BLE001
                    record["rca"] = {"severity": "P1", "root_cause": f"归因失败：{exc}"}
            verdicts.append(record)

        ctx.save("verdicts.json", verdicts)
        failed = [v for v in verdicts if v["verdict"] == "FAIL"]
        return {"total": len(verdicts), "passed": len(verdicts) - len(failed), "failed": len(failed), "verdicts": verdicts}


# ---------------------------------------------------------------- 7. 报告


class ReportStage(Stage):
    name = "report"
    label = "出报告"

    def run(self, ctx: RunContext, rt: Runtime) -> dict[str, Any]:
        judge = ctx.data.get("judge", {})
        verdicts = judge.get("verdicts", [])
        total = judge.get("total", 0)
        passed, failed = judge.get("passed", 0), judge.get("failed", 0)
        rate = (passed / total * 100) if total else 0.0

        rows = "\n".join(
            f"| {v['case_id']} | {v['title']} | {v['priority']} | "
            f"{'✅ PASS' if v['verdict'] == 'PASS' else '❌ FAIL'} | {v['confidence']:.2f} | "
            f"{(v.get('rca') or {}).get('root_cause', v['reason'])[:60]} |"
            for v in verdicts
        )
        product = ctx.data.get("product") or {}
        product_line = (
            f"**被测产品**：{product.get('name')}（由 {product.get('generator')} 生成，"
            f"{len(product.get('files', []))} 个文件）\n"
            if product.get("path")
            else ""
        )
        md = f"""# 自动化测试报告

**仓库**：{ctx.repo} · **提交**：`{ctx.commit}` · **分支**：{ctx.branch}
{product_line}**设备**：{rt.settings.device_kind} / {rt.settings.device_id}
**模型**：{rt.models.mode}（文本 {rt.settings.text_model} + 视觉 {rt.settings.vision_model}）
**Run ID**：`{ctx.run_id}`

## 结论

| 指标 | 数值 |
|---|---|
| 用例总数 | {total} |
| 通过 | {passed} |
| 失败 | {failed} |
| 通过率 | {rate:.1f}% |

## 用例明细

| 用例 | 标题 | 优先级 | 结果 | 置信度 | 根因摘要 |
|---|---|---|---|---|---|
{rows}

## 测试计划依据

{(ctx.data.get('plan') or {}).get('rationale', '—')}
"""
        ctx.save_text("report.md", md)
        summary = {
            "run_id": ctx.run_id,
            "repo": ctx.repo,
            "commit": ctx.commit,
            "total": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": round(rate, 1),
            "verdicts": verdicts,
        }
        ctx.save("report.json", summary)
        return summary


# ---------------------------------------------------------------- 8. 建 bug


BUG_SYSTEM = (
    "你是缺陷报告撰写专家。根据失败用例与根因，生成一条可直接提交的缺陷（bug）。"
    "只输出 JSON："
    '{"title": "...", "body_markdown": "...", "labels": ["bug", "auto-filed"]}'
)


class BugFileStage(Stage):
    name = "bugfile"
    label = "自动建 bug"

    def run(self, ctx: RunContext, rt: Runtime) -> dict[str, Any]:
        verdicts = ctx.data.get("judge", {}).get("verdicts", [])
        failures = [v for v in verdicts if v["verdict"] == "FAIL"]
        filed: list[dict[str, Any]] = []

        for item in failures:
            rca = item.get("rca") or {}
            prompt = (
                f"请为以下失败生成缺陷（bug）：\n"
                f"用例：{item['case_id']} {item['title']}\n"
                f"现象：{item['reason']}\n"
                f"根因：{rca.get('root_cause', '未知')}\n"
                f"建议修复：{rca.get('suggestion', '—')}\n"
                f"提交：{ctx.commit}"
            )
            try:
                draft = extract_json(rt.models.text.complete(BUG_SYSTEM, prompt))
            except Exception as exc:  # noqa: BLE001 - 模型不可用时退化为模板
                draft = {
                    "title": f"【自动发现】{item['case_id']} {item['title']}",
                    "body_markdown": f"现象：{item['reason']}\n\n根因：{rca.get('root_cause', '')}",
                    "labels": ["bug", "auto-filed"],
                }
                draft["fallback_reason"] = str(exc)

            root_cause = rca.get("root_cause", item["reason"])
            fp = rt.tracker.fingerprint(root_cause)
            existing = rt.tracker.find_existing(fp)
            if existing:
                # 同一根因：不新建 issue，只登记受影响的用例，便于统计影响面
                affected = list(existing.get("affected_cases", []))
                if item["case_id"] not in affected:
                    affected.append(item["case_id"])
                merged = {**existing, "affected_cases": affected}
                rt.tracker.record(fp, merged)
                filed.append({**merged, "deduped": True})
                continue

            created = rt.tracker.create(
                {
                    **draft,
                    "fingerprint": fp,
                    "run_id": ctx.run_id,
                    "severity": rca.get("severity", "P1"),
                    "root_cause": root_cause,
                    "affected_cases": [item["case_id"]],
                }
            )
            filed.append(created)

        ctx.save("bugs.json", filed)
        return {
            "filed": len([b for b in filed if not b.get("deduped")]),
            "deduped": len([b for b in filed if b.get("deduped")]),
            "bugs": filed,
        }


# ---------------------------------------------------------------- 9. 人机确认闭环


class ConfirmStage(Stage):
    name = "confirm"
    label = "确认闭环"

    def run(self, ctx: RunContext, rt: Runtime) -> dict[str, Any]:
        bugs = ctx.data.get("bugfile", {}).get("bugs", [])
        if not bugs:
            return {"confirmed": 0, "pending": 0, "note": "无缺陷需确认"}

        auto = rt.models.mode == "mock" and rt.settings.auto_confirm_in_demo
        results = []
        for bug in bugs:
            if bug.get("deduped"):
                results.append({"id": bug["id"], "status": "deduped"})
                continue
            if auto:
                # 演示模式：模拟开发者在 issue 下确认「确实是 bug」
                updated = rt.tracker.confirm(bug["id"], True, "演示模式自动确认")
                bug["status"] = updated.get("status", "confirmed")
                results.append({"id": bug["id"], "status": bug["status"]})
            else:
                results.append(
                    {
                        "id": bug["id"],
                        "status": "pending_human_confirm",
                        "action": "请到 issue 页面确认是否为有效缺陷，确认后回写",
                    }
                )
        # 确认结果必须回写到 bugs.json，否则产物里的状态会停在 open
        ctx.save("bugs.json", bugs)
        ctx.save("confirmation.json", results)
        pending = len([r for r in results if r["status"] == "pending_human_confirm"])
        return {"results": results, "pending": pending, "confirmed": len([r for r in results if r["status"] == "confirmed"])}


def default_stages() -> list[Stage]:
    return [
        TriggerStage(),
        BuildStage(),
        DeployStage(),
        PlanStage(),
        ExecuteStage(),
        JudgeStage(),
        ReportStage(),
        BugFileStage(),
        ConfirmStage(),
    ]
