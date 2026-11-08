"""九层流水线阶段实现。

每层只有一个职责，输入输出都落在 RunContext 里，方便单层替换、单层重跑。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import contract
from .adapters.bugtracker import BugTracker
from .adapters.build import BuildAdapter
from .adapters.device import DeviceAdapter, Trace
from .cache import ArtifactCache
from .context import RunContext
from .generator import MockProductGenerator, build_generator, list_files
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


# 改了生成/计划提示词或产物结构时递增，让旧缓存自然失效
CACHE_VERSION = "1"


# ---------------------------------------------------------------- 0. AI 生成产品代码


def _contract_record(check: dict[str, Any], repair_files: list[str]) -> dict[str, Any]:
    """把契约探针结果收敛成 product.json 里的稳定摘要。"""
    return {
        "ok": check["ok"],
        "summary": contract.describe(check),
        "entry_command": check.get("command"),
        "exit_code": check.get("exit_code"),
        "missing": check.get("missing", []),
        "state": check.get("state", {}),
        "repair_files": repair_files,
    }


class ProductStage(Stage):
    """AI 原生的起点：让模型根据需求直接写出一套可运行的产品代码。

    生成之后**立刻实跑自检**，检查它是否遵守设备状态契约（见 contract.py）。
    自检不通过时把违规点喂回模型修一轮 —— 这是「AI 写代码 + AI 自检 + AI 修复」
    的完整闭环，也避免下游把「字段名不一样」误判成设备故障。

    api 模式下若最终仍不可用（缺 main.py / 非法 JSON），自动降级到内置模板，
    保证流水线不会因为「AI 写代码失败」而整轮白跑 —— 但降级事实会记录在产物里。
    同一 (repo, commit, 需求, 模型) 命中缓存时直接复用，省掉整次生成调用。
    """

    name = "product"
    label = "AI 生成产品代码"

    def run(self, ctx: RunContext, rt: Runtime) -> dict[str, Any]:
        out_dir = ctx.artifacts / "product"
        cache = ArtifactCache(rt.settings)
        key = cache.key(
            CACHE_VERSION,
            ctx.repo,
            ctx.commit,
            rt.settings.product_requirement,
            rt.settings.text_model,
        )

        cached = cache.get_json("product", key)
        if cached is not None and cache.get_dir("product", key, out_dir):
            payload = dict(cached)
            payload["path"] = str(out_dir)
            payload["cached"] = True
            # 契约探针照跑：contract.py 升级后旧缓存也必须重新验收
            check = contract.probe(
                out_dir, python=sys.executable, timeout=min(rt.settings.case_timeout, 60)
            )
            previous = payload.get("contract")
            repair_files = (
                list(previous.get("repair_files") or ())
                if isinstance(previous, dict)
                else []
            )
            payload["contract"] = _contract_record(check, repair_files)
            payload["file_count"] = len(payload.get("files", []))
            ctx.save("product.json", payload)
            ctx.log(self.name, "ok", "命中 product 缓存")
            if not check["ok"]:
                ctx.log(self.name, "warn", f"产品状态契约不通过，判定层将按无法判定处理：{payload['contract']['summary']}")
            return payload

        generator = build_generator(rt.models, rt.settings)
        try:
            product = generator.generate(rt.settings.product_requirement, out_dir)
        except Exception as exc:  # noqa: BLE001 - 生成失败必须可降级
            if rt.models.mode != "api":
                raise
            ctx.log(self.name, "warn", f"模型生成失败，降级到内置模板：{exc}")
            product = MockProductGenerator().generate(rt.settings.product_requirement, out_dir)
            product.generator = "mock(fallback)"
            generator = None  # 模板产品无需自修复

        check = contract.probe(
            product.path, python=sys.executable, timeout=min(rt.settings.case_timeout, 60)
        )
        repair_files: list[str] = []
        if not check["ok"] and generator is not None and hasattr(generator, "repair"):
            ctx.log(self.name, "warn", f"产品自检未通过，触发自修复：{contract.describe(check)}")
            try:
                repair_files = generator.repair(product.path, check, rt.settings.product_requirement)
            except Exception as exc:  # noqa: BLE001 - 修复失败不能让整轮挂掉
                ctx.log(self.name, "warn", f"自修复调用失败：{exc}")
            if repair_files:
                check = contract.probe(
                    product.path, python=sys.executable, timeout=min(rt.settings.case_timeout, 60)
                )
                product.files = list_files(product.path)

        payload = product.to_dict()
        payload["file_count"] = len(payload.get("files", []))
        payload["contract"] = _contract_record(check, repair_files)
        ctx.save("product.json", payload)
        if check["ok"]:
            # 只缓存通过契约验收的产品，避免把坏产物固化成「正确答案」
            cache.put_dir("product", key, Path(product.path))
            cache.put_json("product", key, payload)
        if not check["ok"]:
            ctx.log(self.name, "warn", f"产品状态契约不通过，判定层将按无法判定处理：{payload['contract']['summary']}")
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
    '"priority": "P0", "scenario": "boot|provision|ota|reconnect", '
    '"steps": ["..."], "expect": "...", "expect_state": {"online": true}, '
    '"needs_injection": false}]}。'
    "scenario 必须取自这四个值之一，它是驱动设备的执行入口；"
    "expect 写一句人话的预期；expect_state 只能包含 online / ok 两个布尔键，"
    "给出可客观比对的状态（反向用例尤其要写，例如错误凭据用例给 {\"online\": false}）；"
    "需要平台注入错误凭据、断电、坏镜像等故障才能验证的用例，把 needs_injection 设为 true。"
)


class PlanStage(Stage):
    name = "plan"
    label = "AI 设计测试"

    def run(self, ctx: RunContext, rt: Runtime) -> dict[str, Any]:
        trigger = ctx.data.get("trigger", {})
        caps = rt.device.capabilities()
        cache = ArtifactCache(rt.settings)
        key = cache.key(
            CACHE_VERSION,
            trigger.get("repo") or ctx.repo,
            trigger.get("commit") or ctx.commit,
            sorted(str(item) for item in (trigger.get("changed_files") or ())),
            rt.settings.device_kind,
            rt.settings.device_id,
            list(caps.get("scenarios") or ()),
            bool(caps.get("injection")),
            rt.settings.text_model,
        )

        cached = cache.get_json("plan", key)
        if cached is not None:
            plan = dict(cached)
            plan["cached"] = True
            ctx.save("testplan.json", plan)
            ctx.log(self.name, "ok", "命中 plan 缓存")
            return plan

        scenarios = ", ".join(caps.get("scenarios") or ())
        prompt = (
            f"请为本次提交设计测试计划（test_plan）。\n"
            f"仓库：{trigger.get('repo')}\n提交：{trigger.get('commit')}\n"
            f"变更文件：{', '.join(trigger.get('changed_files', []))}\n"
            f"被测设备：{rt.settings.device_kind} / {rt.settings.device_id}\n"
            f"设备支持场景：{scenarios}；"
            f"故障注入能力：{'支持' if caps.get('injection') else '不支持'}\n"
            f"要求：覆盖变更直接影响的主路径，P0 用例聚焦启动、配网、升级。\n"
            f"每个用例必须带 scenario 字段，取值只能是 boot / provision / ota / reconnect 之一，"
            f"用于驱动设备执行对应场景。\n"
            f"不要因为设备能力不足就省略或改写用例：如实标注 needs_injection，"
            f"平台会把当前设备跑不了的用例标成「未执行」，而不是判失败。"
        )
        raw = rt.models.text.complete(PLAN_SYSTEM, prompt)
        plan = extract_json(raw)
        if not plan.get("cases"):
            raise RuntimeError("测试计划为空，模型输出不可用")
        for idx, case in enumerate(plan["cases"], 1):
            case.setdefault("id", f"TC-{idx:02d}")
            case.setdefault("priority", "P1")
            # 模型给的 expect_state 只保留可比对的 screen/online/ok，其余键丢掉
            expect_state = contract.sanitize_expectation(case.get("expect_state"))
            if expect_state:
                case["expect_state"] = expect_state
            else:
                case.pop("expect_state", None)
        ctx.save("testplan.json", plan)
        cache.put_json("plan", key, plan)
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
            # 设备能力不支持就不执行：拿「跑了正常路径」的结果去判「注入类用例」，
            # 只会把环境限制写成固件缺陷。
            supported, why = rt.device.supports(case)
            if not supported:
                trace = Trace(case_id=case["id"], skipped=True, skip_reason=why)
                trace.steps = [f"skipped: {why}"]
                traces.append(trace.to_dict())
                continue
            try:
                trace = rt.device.run_case(case, shot_dir)
            except Exception as exc:  # noqa: BLE001 - 单个用例失败不应中断整轮
                trace = Trace(case_id=case["id"], ok_to_proceed=False, logs=f"执行异常：{exc}")
            traces.append(trace.to_dict())
        ctx.save("traces.json", traces)
        skipped = len([t for t in traces if t.get("skipped")])
        return {"total": len(traces), "skipped": skipped, "traces": traces}


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

# 只采信「不可能是预期行为」的硬故障信号；泛化的 ERROR 可能出现在负向用例里，交给模型判断
FAIL_SIGNALS = (
    "panic",
    "FATAL",
    "system_reset",
    "watchdog",
    "boot_loop",
    "ota_failed",
    "mismatch",
)


def rule_fail_signal(trace: dict[str, Any]) -> str:
    """从串口日志/STATE 里找确定性失败信号，返回命中的信号，没有则返回空串。"""
    logs = trace.get("logs", "") or ""
    for signal in FAIL_SIGNALS:
        if signal in logs:
            return signal
    if contract.extract_state(logs).get("ok") is False:
        return "STATE ok=false"
    return ""


# 各场景的判定依据：只依赖设备客观上报的状态，不做主观推断
SCENARIO_EXPECT: dict[str, dict[str, Any]] = {
    "boot": {"screen_not_in": contract.BAD_SCREENS},
    "provision": {"online": True},
    "ota": {"ok": True},
    "reconnect": {"online": True},
}


def judge_by_state(
    case: dict[str, Any], state: dict[str, Any], trace: dict[str, Any]
) -> dict[str, Any] | None:
    """状态判定：设备自己上报的状态能对上用例预期就 PASS，反之 FAIL。

    判据优先级：
      1. 用例自带 expect_state（模型设计时给出的可观测预期，反向用例就靠它）
      2. 场景默认预期（配网/重连要求在线，启动要求界面正常）

    只有在状态满足契约时才可用 —— 字段缺失/无法解析时返回 None，
    把判定权交给视觉模型，绝不拿「读不到」当「坏了」。
    """
    if not contract.is_usable(state):
        return None

    scenario = str(case.get("scenario", "")).strip().lower()
    evidence = (
        f"STATE screen={state.get('screen')} online={state.get('online')} "
        f"version={state.get('version')} ok={state.get('ok')}"
    )

    # 1. 用例显式声明了期望状态：逐字段比对，反向用例（错误密码应离线）靠这条兜住
    expected = contract.sanitize_expectation(case.get("expect_state"))
    if expected:
        mismatched = {
            key: (expected[key], state.get(key))
            for key in expected
            if state.get(key) != expected[key]
        }
        if mismatched:
            detail = "，".join(
                f"{k} 期望 {exp} 实际 {act}" for k, (exp, act) in mismatched.items()
            )
            return {
                "verdict": "FAIL",
                "confidence": 0.9,
                "reason": f"设备状态与用例预期不一致：{detail}",
                "visual_evidence": evidence,
                "evidence_key": "state:"
                + (scenario or "unknown")
                + ":"
                + ",".join(sorted(mismatched)),
            }
        return {
            "verdict": "PASS",
            "confidence": 0.9,
            "reason": "设备状态与用例预期一致",
            "visual_evidence": evidence,
        }

    # 2. 没给 expect_state 时退回场景默认预期
    if state.get("ok") is False:
        return {
            "verdict": "FAIL",
            "confidence": 0.9,
            "reason": f"设备自报场景执行失败（ok=false，场景 {scenario or '未知'}）",
            "visual_evidence": evidence,
            "evidence_key": f"state:{scenario or 'unknown'}:ok=false",
        }

    expect = SCENARIO_EXPECT.get(scenario)
    if expect is None:
        # 场景不在已知集合里：只凭 ok=true 不足以判 PASS，交给视觉
        return None

    if expect.get("online") is not None and state.get("online") is not expect["online"]:
        return {
            "verdict": "FAIL",
            "confidence": 0.9,
            "reason": f"场景 {scenario} 要求云端在线，设备状态为 online={state.get('online')}",
            "visual_evidence": evidence,
            "evidence_key": f"state:{scenario}:online={state.get('online')}",
        }

    screen = str(state.get("screen", "")).strip().lower()
    if screen in expect.get("screen_not_in", ()):
        return {
            "verdict": "FAIL",
            "confidence": 0.9,
            "reason": f"界面停在异常状态 {screen}",
            "visual_evidence": evidence,
            "evidence_key": f"state:{scenario}:screen={screen}",
        }

    return {
        "verdict": "PASS",
        "confidence": 0.88,
        "reason": f"设备状态与场景 {scenario} 预期一致",
        "visual_evidence": evidence,
    }


def judge_by_vision(
    rt: Runtime, system: str, prompt: str, shot: Path | None
) -> dict[str, Any]:
    """视觉判定；返回不可用时重试一次，仍失败才按 FAIL 处理（宁可误报不漏报）。"""
    error = ""
    for _ in range(2):
        try:
            return extract_json(rt.models.vision.analyze(system, prompt, shot))
        except Exception as exc:  # noqa: BLE001 - 网络异常与 JSON 解析失败都按可重试处理
            error = str(exc)
    return {
        "verdict": "FAIL",
        "confidence": 0.0,
        "reason": f"判定过程异常：{error}",
        "visual_evidence": "",
    }


class JudgeStage(Stage):
    name = "judge"
    label = "AI 判定"

    def run(self, ctx: RunContext, rt: Runtime) -> dict[str, Any]:
        plan = {c["id"]: c for c in ctx.data.get("plan", {}).get("cases", [])}
        traces = ctx.data.get("execute", {}).get("traces", [])
        state_first = rt.settings.judge_mode != "vision_first"
        verdicts: list[dict[str, Any]] = []

        for trace in traces:
            case = plan.get(trace["case_id"], {})

            if trace.get("skipped"):
                verdicts.append(
                    {
                        "case_id": trace["case_id"],
                        "title": case.get("title", ""),
                        "priority": case.get("priority", "P1"),
                        "verdict": "SKIPPED",
                        "confidence": 1.0,
                        "reason": trace.get("skip_reason", "设备能力不支持该用例"),
                        "visual_evidence": "",
                        "judged_by": "capability",
                        "contract_ok": True,
                        "evidence_key": "",
                        "logs": "",
                    }
                )
                continue

            signal = rule_fail_signal(trace)

            # 三级证据链：硬故障信号（0 成本）→ 设备状态契约 → 视觉模型
            if signal:
                # 日志已能确定性判定失败，跳过模型调用（这条链路里最贵的一次请求）
                result = {
                    "verdict": "FAIL",
                    "confidence": 0.95,
                    "reason": f"串口日志命中失败信号：{signal}",
                    "visual_evidence": f"RULE_HIT={signal}",
                    "evidence_key": f"rule:{signal}:{case.get('scenario', 'unknown')}",
                }
                judged_by = "rule"
            else:
                result = None
                judged_by = ""
                if state_first:
                    result = judge_by_state(case, trace.get("state") or {}, trace)
                    if result is not None:
                        judged_by = "state"

                if result is None:
                    # 屏幕快照：真实设备是 PNG；模拟设备是文本渲染，读进来一起喂给模型
                    shot = Path(trace["screenshot"]) if trace.get("screenshot") else None
                    screen_text = ""
                    if shot and shot.exists() and shot.suffix.lower() not in (
                        ".png",
                        ".jpg",
                        ".jpeg",
                        ".webp",
                    ):
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
                    result = judge_by_vision(rt, JUDGE_SYSTEM, prompt, shot)
                    judged_by = "vision"

            record = {
                "case_id": trace["case_id"],
                "title": case.get("title", ""),
                "priority": case.get("priority", "P1"),
                "verdict": result.get("verdict", "FAIL"),
                "confidence": result.get("confidence", 0),
                "reason": result.get("reason", ""),
                "visual_evidence": result.get("visual_evidence", ""),
                "judged_by": judged_by,
                "contract_ok": trace.get("contract_ok", True),
                "evidence_key": result.get("evidence_key", ""),
                "logs": trace.get("logs", ""),
            }

            # 状态读不出来、又不是日志能定的硬故障 → 证据不足，不能当缺陷报。
            # 误报会污染缺陷库，比漏报更贵；标成无法判定，让人或下一轮补证据。
            if record["verdict"] == "FAIL" and not record["contract_ok"] and judged_by != "rule":
                record["verdict"] = "INCONCLUSIVE"
                record["reason"] = "设备状态不可解析（契约不符），证据不足：" + record["reason"]

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
        unknown = [v for v in verdicts if v["verdict"] == "INCONCLUSIVE"]
        skipped = [v for v in verdicts if v["verdict"] == "SKIPPED"]
        return {
            "total": len(verdicts),
            "passed": len(verdicts) - len(failed) - len(unknown) - len(skipped),
            "failed": len(failed),
            "inconclusive": len(unknown),
            "skipped": len(skipped),
            "verdicts": verdicts,
        }


# ---------------------------------------------------------------- 7. 报告


VERDICT_LABEL = {"PASS": "✅ PASS", "FAIL": "❌ FAIL", "INCONCLUSIVE": "⚠️ 无法判定", "SKIPPED": "⏭ 未执行"}


class ReportStage(Stage):
    name = "report"
    label = "出报告"

    def run(self, ctx: RunContext, rt: Runtime) -> dict[str, Any]:
        judge = ctx.data.get("judge", {})
        verdicts = judge.get("verdicts", [])
        total = judge.get("total", 0)
        passed, failed = judge.get("passed", 0), judge.get("failed", 0)
        unknown = judge.get("inconclusive", 0)
        skipped = judge.get("skipped", 0)
        executed = total - skipped
        # 通过率只看真正执行过的用例，跳过的不能算进分母来"抬分"或"拖分"
        rate = (passed / executed * 100) if executed else 0.0

        rows = "\n".join(
            f"| {v['case_id']} | {v['title']} | {v['priority']} | "
            f"{VERDICT_LABEL.get(v['verdict'], v['verdict'])} | {v['confidence']:.2f} | "
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
        product_contract = (product.get("contract") or {}).get("summary")
        contract_line = f"**状态契约**：{product_contract}\n" if product_contract else ""

        usage = rt.models.usage(
            getattr(rt.settings, "price_in_per_m", 0.0),
            getattr(rt.settings, "price_out_per_m", 0.0),
        )
        usage_line = (
            f"**模型用量**（截至出报告时）：{usage['calls']} 次调用 / "
            f"{usage['total_tokens']} tokens，估算 {usage['cost_cny']:.4f} 元\n"
            if usage["calls"]
            else ""
        )

        md = f"""# 自动化测试报告

**仓库**：{ctx.repo} · **提交**：`{ctx.commit}` · **分支**：{ctx.branch}
{product_line}{contract_line}**设备**：{rt.settings.device_kind} / {rt.settings.device_id}
**模型**：{rt.models.mode}（文本 {rt.settings.text_model} + 视觉 {rt.settings.vision_model}）
{usage_line}**Run ID**：`{ctx.run_id}`

## 结论

| 指标 | 数值 |
|---|---|
| 用例总数 | {total} |
| 通过 | {passed} |
| 失败 | {failed} |
| 无法判定 | {unknown} |
| 未执行（设备不支持） | {skipped} |
| 通过率 | {rate:.1f}%（按已执行 {executed} 条计） |

> 无法判定 = 设备没能输出可解析的状态，证据不足以定性；未执行 = 用例需要的能力
> （错误凭据注入、掉电、坏镜像等）当前设备不具备。这两类都不建缺陷，
> 避免把「读不到」和「跑不了」当成「坏了」污染缺陷库。

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
            "inconclusive": unknown,
            "skipped": skipped,
            "pass_rate": round(rate, 1),
            "usage": usage,
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

        # 根因去重只按「根因文本」会让模型的措辞差异变成新缺陷：
        # 同一批里 7 个用例其实只对应 2 个故障。优先用判定层给出的证据键
        # （如 rule:system_reset:provision），同一现象必然同键。
        for item in failures:
            rca = item.get("rca") or {}
            root_cause = rca.get("root_cause", item["reason"])
            fp = rt.tracker.fingerprint(item.get("evidence_key") or root_cause)

            existing = rt.tracker.find_existing(fp)
            if existing:
                # 同一根因：不新建 issue，只登记受影响的用例，便于统计影响面。
                # 这里提前返回，省掉一次「写缺陷描述」的模型调用。
                affected = list(existing.get("affected_cases", []))
                if item["case_id"] not in affected:
                    affected.append(item["case_id"])
                merged = {**existing, "affected_cases": affected}
                rt.tracker.record(fp, merged)
                filed.append({**merged, "deduped": True})
                continue

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

            created = rt.tracker.create(
                {
                    **draft,
                    "fingerprint": fp,
                    "run_id": ctx.run_id,
                    "severity": rca.get("severity", "P1"),
                    "root_cause": root_cause,
                    "evidence_key": item.get("evidence_key", ""),
                    "affected_cases": [item["case_id"]],
                }
            )
            filed.append(created)

        # bugs.json 只放唯一缺陷（同一证据键合并），重复命中的用例并进 affected_cases，
        # 否则产物里看着像建了 7 条，实际只有 2 条。
        created_ids = {b["id"] for b in filed if not b.get("deduped")}
        unique: dict[str, dict[str, Any]] = {}
        deduped_cases: list[dict[str, Any]] = []
        for bug in filed:
            if bug.get("deduped"):
                deduped_cases.append(
                    {"id": bug["id"], "affected_cases": bug.get("affected_cases", [])}
                )
            unique[bug["fingerprint"]] = {
                **bug,
                "created_this_run": bug["id"] in created_ids,
            }

        bugs_saved = list(unique.values())
        ctx.save("bugs.json", bugs_saved)
        return {
            "filed": len(created_ids),
            "deduped": len(filed) - len(created_ids),
            # 后续阶段（人工确认）只处理唯一缺陷
            "bugs": bugs_saved,
            "deduped_cases": deduped_cases,
            "unique": len(bugs_saved),
        }


# ---------------------------------------------------------------- 9. 人机确认闭环


class ConfirmStage(Stage):
    name = "confirm"
    label = "确认闭环"

    def run(self, ctx: RunContext, rt: Runtime) -> dict[str, Any]:
        bugs = ctx.data.get("bugfile", {}).get("bugs", [])
        if not bugs:
            return {"confirmed": 0, "pending": 0, "note": "无缺陷需确认"}

        # 真实模式下默认停下来等人确认：缺陷是否有效由人拍板，
        # 平台只负责把证据准备齐（--no-confirm 可跳过）。
        auto = (not rt.settings.require_human_confirm) or (
            rt.models.mode == "mock" and rt.settings.auto_confirm_in_demo
        )
        results = []
        for bug in bugs:
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
        return {
            "results": results,
            "pending": pending,
            "confirmed": len([r for r in results if r["status"] == "confirmed"]),
            "merged": ctx.data.get("bugfile", {}).get("deduped", 0),
        }


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
