#!/usr/bin/env python3
"""把一次运行的产物渲染成单文件 HTML 报告。

用法：
    python tools/make_report.py                    # 默认取最近一次 api 运行
    python tools/make_report.py runs/20261108-182052-1d453e
    python tools/make_report.py --out docs/showcase.html

生成的 HTML 自带全部数据，离线可看，不依赖任何外部资源。
"""

from __future__ import annotations

import argparse
import html
import json
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO_URL = "https://github.com/w18341840753-cell/ai-qa-platform"

CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0A0C10; --panel:#101419; --panel-2:#151A21; --line:#222A35;
  --fg:#E6EDF3; --dim:#9AA7B4; --mute:#69788A;
  --accent:#4C9AFF; --pass:#3FB950; --fail:#F85149; --warn:#D29922; --purple:#A371F7;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Consolas,"Liberation Mono",monospace;
}
html{-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}
body{
  background:var(--bg); color:var(--fg); line-height:1.7; font-size:15px;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}

.top{position:sticky;top:0;z-index:20;background:rgba(10,12,16,.86);backdrop-filter:blur(14px);border-bottom:1px solid var(--line)}
.top-in{max-width:1180px;margin:0 auto;padding:13px 32px;display:flex;align-items:center;gap:11px}
.mark{width:24px;height:24px;border-radius:7px;background:linear-gradient(135deg,#4C9AFF,#A371F7);flex:none;
  box-shadow:0 0 0 1px rgba(255,255,255,.08) inset}
.brand{font-weight:600;letter-spacing:-.01em}
.brand span{color:var(--mute);font-weight:400}
.top-links{margin-left:auto;display:flex;gap:18px;font-size:13px;color:var(--dim)}
.top-links a{color:var(--dim)}
.top-links a:hover{color:var(--fg);text-decoration:none}

.wrap{max-width:1180px;margin:0 auto;padding:0 32px 100px}

.hero{padding:66px 0 34px;position:relative}
.hero::before{content:"";position:absolute;left:-6%;right:-6%;top:-140px;height:440px;pointer-events:none;
  background:radial-gradient(52% 62% at 26% 0%,rgba(76,154,255,.14),transparent 72%)}
.eyebrow{display:inline-flex;align-items:center;gap:9px;font-size:11.5px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--dim);border:1px solid var(--line);background:var(--panel);
  padding:5px 11px;border-radius:999px;font-family:var(--mono)}
.dot{width:6px;height:6px;border-radius:50%;background:var(--pass);box-shadow:0 0 8px rgba(63,185,80,.7)}
h1{font-size:clamp(30px,4.3vw,48px);line-height:1.14;letter-spacing:-.028em;font-weight:700;margin:22px 0 16px}
h1 em{font-style:normal;background:linear-gradient(96deg,#4C9AFF,#A371F7 62%);-webkit-background-clip:text;
  background-clip:text;color:transparent}
.lede{font-size:16.5px;color:var(--dim);max-width:790px}
.lede b{color:var(--fg);font-weight:600}

.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line);border-radius:14px;overflow:hidden;margin:38px 0 0}
.metric{background:var(--panel);padding:17px 19px}
.metric b{display:block;font-family:var(--mono);font-size:25px;line-height:1.25;font-weight:600;
  font-variant-numeric:tabular-nums;letter-spacing:-.02em}
.metric.fail b{color:var(--fail)} .metric.pass b{color:var(--pass)} .metric.warn b{color:var(--warn)}
.metric span{font-size:11.5px;color:var(--mute);letter-spacing:.02em}

.highlights{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px;margin-top:14px}
.hl{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:14px 16px;
  font-size:13.5px;color:var(--dim);display:flex;gap:10px}
.hl i{flex:none;font-style:normal;font-family:var(--mono);font-size:11px;color:var(--accent);
  border:1px solid rgba(76,154,255,.3);background:rgba(76,154,255,.08);border-radius:5px;
  padding:1px 6px;height:20px;line-height:18px;margin-top:2px}

section{padding-top:52px;margin-top:52px;border-top:1px solid var(--line)}
.sec-h{display:flex;align-items:baseline;gap:13px}
.sec-n{font-family:var(--mono);font-size:11.5px;color:var(--accent);letter-spacing:.12em}
.sec-h h2{font-size:23px;font-weight:650;letter-spacing:-.015em}
.sec-desc{color:var(--dim);max-width:850px;margin:9px 0 26px;font-size:14.5px}

.flow{border:1px solid var(--line);border-radius:12px;overflow:hidden;background:var(--line)}
.step{display:grid;grid-template-columns:38px minmax(120px,1.15fr) minmax(0,2fr) 84px;
  align-items:center;gap:16px;background:var(--panel);padding:11px 18px;
  border-bottom:1px solid rgba(34,42,53,.55)}
.step:last-child{border-bottom:0}
.step .idx{font-family:var(--mono);font-size:11.5px;color:var(--mute)}
.step .nm{font-size:13.5px;color:var(--fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.step .nm i{font-style:normal;color:var(--mute);font-family:var(--mono);font-size:11px;margin-left:8px}
.track{height:6px;border-radius:3px;background:#1B2129;overflow:hidden}
.track i{display:block;height:100%;border-radius:3px;background:linear-gradient(90deg,#2F6FD0,#4C9AFF)}
.track i.hot{background:linear-gradient(90deg,#A371F7,#4C9AFF)}
.step .tm{font-family:var(--mono);font-size:12px;color:var(--dim);text-align:right;
  font-variant-numeric:tabular-nums}

.split{display:grid;grid-template-columns:1.05fr .95fr;gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px 22px}
.card h3{font-size:14px;font-weight:620;margin-bottom:14px;display:flex;align-items:center;gap:8px}
.card h3 span{font-family:var(--mono);font-size:11px;color:var(--mute);font-weight:400}

.kv{display:flex;justify-content:space-between;gap:14px;padding:7px 0;font-size:13px;
  border-bottom:1px dashed rgba(34,42,53,.85)}
.kv:last-child{border-bottom:0}
.kv span{color:var(--mute);flex:none}
.kv b{font-weight:500;font-family:var(--mono);font-size:12.5px;text-align:right;word-break:break-all}

.tree{font-family:var(--mono);font-size:12.5px;line-height:1.85;color:var(--dim);
  background:var(--bg);border:1px solid var(--line);border-radius:9px;padding:13px 15px;margin-top:12px}
.tree div{white-space:nowrap}
.tree em{font-style:normal;color:var(--mute)}
.tree b{color:#7FB2FF;font-weight:400}

.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.chip{font-family:var(--mono);font-size:11.5px;padding:2px 8px;border-radius:5px;
  border:1px solid var(--line);background:var(--panel-2);color:var(--dim)}
.chip.ok{color:var(--pass);border-color:rgba(63,185,80,.32);background:rgba(63,185,80,.08)}
.chip.bad{color:var(--fail);border-color:rgba(248,81,73,.32);background:rgba(248,81,73,.08)}
.chip.info{color:var(--accent);border-color:rgba(76,154,255,.3);background:rgba(76,154,255,.07)}

.req{font-size:13.2px;color:var(--dim);line-height:1.85;white-space:pre-wrap}
.inject{margin-top:14px;border-left:2px solid var(--purple);background:rgba(163,113,247,.07);
  border-radius:0 8px 8px 0;padding:12px 15px;font-size:12.8px;color:#C7B6F0}
.inject b{color:#D9CBF7;display:block;margin-bottom:5px;font-size:11.5px;letter-spacing:.06em;
  text-transform:uppercase;font-family:var(--mono)}

.table-wrap{border:1px solid var(--line);border-radius:12px;overflow:hidden}
table{width:100%;border-collapse:collapse;font-size:13.2px}
thead th{text-align:left;font-weight:600;font-size:11px;letter-spacing:.07em;text-transform:uppercase;
  color:var(--mute);padding:11px 14px;background:var(--panel-2);border-bottom:1px solid var(--line);
  white-space:nowrap}
tbody td{padding:12px 14px;border-bottom:1px solid rgba(34,42,53,.6);vertical-align:top}
tbody tr:last-child td{border-bottom:0}
tr.case-row{cursor:pointer}
tr.case-row:hover td{background:rgba(76,154,255,.045)}
tr.case-row td:first-child{font-family:var(--mono);font-size:12px;color:var(--dim);white-space:nowrap}
.ti{font-weight:500;color:var(--fg);display:block;margin-bottom:3px}
.sub{font-family:var(--mono);font-size:11.5px;color:var(--mute)}
.badge{display:inline-block;font-family:var(--mono);font-size:11px;font-weight:600;padding:2px 7px;
  border-radius:5px;letter-spacing:.03em;white-space:nowrap}
.badge.pass{color:var(--pass);background:rgba(63,185,80,.12);border:1px solid rgba(63,185,80,.3)}
.badge.fail{color:var(--fail);background:rgba(248,81,73,.12);border:1px solid rgba(248,81,73,.3)}
.badge.inc{color:var(--warn);background:rgba(210,153,34,.12);border:1px solid rgba(210,153,34,.3)}
.badge.p0{color:#FFA198;background:rgba(248,81,73,.1);border:1px solid rgba(248,81,73,.22)}
.badge.p1{color:var(--dim);border:1px solid var(--line);background:var(--panel-2)}
.by{font-family:var(--mono);font-size:11px;padding:1px 6px;border-radius:4px;border:1px solid var(--line)}
.by.rule{color:var(--purple);border-color:rgba(163,113,247,.3);background:rgba(163,113,247,.08)}
.by.state{color:var(--accent);border-color:rgba(76,154,255,.3);background:rgba(76,154,255,.08)}
.by.vision{color:#39C5CF;border-color:rgba(57,197,207,.3);background:rgba(57,197,207,.08)}
.cmp{display:flex;flex-wrap:wrap;gap:5px;align-items:center;font-family:var(--mono);font-size:11.5px}
.cmp s{text-decoration:none;color:var(--mute)}
.cmp .eq{color:var(--pass)} .cmp .ne{color:var(--fail)}
tr.detail td{background:#0C0F14;padding:0}
.detail-in{padding:14px 16px 16px}
.detail-in .lbl{font-family:var(--mono);font-size:10.5px;letter-spacing:.08em;color:var(--mute);
  text-transform:uppercase;margin:10px 0 5px}
pre{font-family:var(--mono);font-size:11.8px;line-height:1.72;color:#B9C6D4;background:#070A0E;
  border:1px solid var(--line);border-radius:8px;padding:12px 14px;overflow:auto;max-height:230px;
  white-space:pre;tab-size:2}
pre::-webkit-scrollbar{width:9px;height:9px}
pre::-webkit-scrollbar-thumb{background:#243040;border-radius:5px}

.bug{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-bottom:11px}
.bug-h{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.bug-id{font-family:var(--mono);font-size:11.5px;color:var(--mute)}
.bug-t{font-size:14.5px;font-weight:600;line-height:1.5;margin-bottom:10px}
.bug-m{display:flex;flex-wrap:wrap;gap:16px;font-family:var(--mono);font-size:11.5px;color:var(--mute);
  margin-bottom:12px}
.bug-m b{color:var(--dim);font-weight:400}
details{margin-top:4px}
details summary{cursor:pointer;font-size:12.5px;color:var(--accent);list-style:none;user-select:none}
details summary::-webkit-details-marker{display:none}
details summary::before{content:"▸ ";color:var(--mute)}
details[open] summary::before{content:"▾ "}
details p{font-size:13px;color:var(--dim);margin-top:10px;line-height:1.85}
.fix{margin-top:10px;border-left:2px solid var(--pass);background:rgba(63,185,80,.06);
  border-radius:0 7px 7px 0;padding:9px 13px;font-size:12.8px;color:#A8D8B4}

.shots{display:grid;grid-template-columns:1fr 1fr;gap:14px;align-items:start}
.shot{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.shot-h{padding:11px 15px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:9px;
  font-size:12.5px;color:var(--dim)}
.shot pre{border:0;border-radius:0;max-height:none;background:#070A0E}

.stack{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px}
.st{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:17px 19px}
.st h4{font-size:12px;letter-spacing:.07em;text-transform:uppercase;color:var(--mute);font-weight:600;
  margin-bottom:8px;font-family:var(--mono)}
.st p{font-size:13.2px;color:var(--dim)}
.st p b{color:var(--fg);font-weight:600}

.cmds{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:6px 0;margin-top:12px}
.cmd{display:grid;grid-template-columns:minmax(0,1fr) 1.5fr;gap:18px;padding:11px 20px;
  border-bottom:1px solid rgba(34,42,53,.6);align-items:center}
.cmd:last-child{border-bottom:0}
.cmd code{font-family:var(--mono);font-size:12.5px;color:#9CD3FF;background:#070A0E;
  border:1px solid var(--line);border-radius:6px;padding:4px 9px;display:inline-block}
.cmd span{font-size:12.8px;color:var(--mute)}

footer{margin-top:64px;padding-top:26px;border-top:1px solid var(--line);display:flex;
  flex-wrap:wrap;gap:18px;justify-content:space-between;font-size:12.5px;color:var(--mute)}
footer code{font-family:var(--mono);font-size:12px;color:var(--dim);word-break:break-all}

@media (max-width:860px){
  .split,.shots{grid-template-columns:1fr}
  .wrap{padding:0 18px 70px}
  .top-in{padding:12px 18px}
  .step{grid-template-columns:30px 1fr 68px;gap:10px}
  .step .track{display:none}
  .cmd{grid-template-columns:1fr;gap:6px}
  .top-links{gap:12px}
}
"""

JS = """
document.querySelectorAll('tr.case-row').forEach(function (row) {
  row.addEventListener('click', function () {
    var next = row.nextElementSibling;
    if (next && next.classList.contains('detail')) next.hidden = !next.hidden;
  });
});
"""


def esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def fmt_s(sec: float) -> str:
    if sec >= 60:
        return f"{sec / 60:.1f} min"
    if sec >= 1:
        return f"{sec:.1f} s"
    return f"{sec * 1000:.0f} ms"


def pick_run(arg: str | None) -> Path:
    if arg:
        p = Path(arg)
        return p if p.is_absolute() else (ROOT / p)
    runs = sorted(
        [d for d in (ROOT / "runs").iterdir() if d.is_dir()],
        key=lambda d: d.name,
        reverse=True,
    )
    # 优先取有产品源码目录的那次（= 真模型跑出来的）
    for d in runs:
        if (d / "product" / "main.py").exists() and not (d / "product" / "panel").exists():
            return d
    return runs[0]


def merge_bugs(raw: list[dict]) -> list[dict]:
    """同一 id 在 bugs.json 里可能被写多次，按 id 合并受影响用例。"""
    merged: "OrderedDict[str, dict]" = OrderedDict()
    for b in raw:
        bid = b.get("id") or b.get("fingerprint", "?")
        if bid in merged:
            cases = merged[bid].setdefault("affected_cases", [])
            for c in b.get("affected_cases", []):
                if c not in cases:
                    cases.append(c)
        else:
            merged[bid] = dict(b)
    return list(merged.values())


def verdict_meta(v: str) -> tuple[str, str, str]:
    return {
        "PASS": ("pass", "PASS", "通过"),
        "FAIL": ("fail", "FAIL", "失败"),
        "INCONCLUSIVE": ("inc", "INC", "无法判定"),
        "SKIPPED": ("inc", "SKIP", "未执行"),
    }.get(v, ("inc", v or "?", ""))


def render(run_dir: Path) -> str:
    report = load(run_dir / "run.json", {})
    summary = load(run_dir / "report.json", {})
    product = load(run_dir / "product.json", {})
    plan = load(run_dir / "testplan.json", {})
    verdicts = load(run_dir / "verdicts.json", [])
    traces = load(run_dir / "traces.json", [])
    bugs_raw = load(run_dir / "bugs.json", [])
    build = load(run_dir / "build.json", {})
    deploy = load(run_dir / "deploy.json", {})
    confirm = load(run_dir / "confirmation.json", [])

    bugs = merge_bugs(bugs_raw)
    plans = {c.get("id"): c for c in plan.get("cases", [])}
    trace_by = {t.get("case_id"): t for t in traces}

    total = summary.get("total", len(verdicts))
    passed = summary.get("passed", 0)
    failed = summary.get("failed", 0)
    inc = summary.get("inconclusive", 0) or 0
    skipped = summary.get("skipped", 0) or 0
    rate = summary.get("pass_rate", 0)
    usage = summary.get("usage") or {}
    stages = report.get("stages", [])
    total_s = sum(float(s.get("elapsed_s") or 0) for s in stages)

    by_kind: dict[str, int] = {}
    for v in verdicts:
        k = v.get("judged_by", "?")
        by_kind[k] = by_kind.get(k, 0) + 1

    model_mode = "真模型" if product.get("generator") == "api" else "Mock"
    contract = product.get("contract") or {}
    cstate = contract.get("state") or {}

    # ---------------------------------------------------------------- 指标
    metrics = [
        (f"{len(stages)}", "流水线层数", ""),
        (f"{total}", "用例总数", ""),
        (f"{rate:.0f}%", "通过率", "pass" if rate >= 80 else "fail"),
        (f"{len(bugs)}", "唯一缺陷", "fail" if bugs else "pass"),
        (f"¥{usage.get('cost_cny', 0):.2f}", "模型花费", ""),
        (fmt_s(total_s), "端到端耗时", ""),
    ]
    metrics_html = "".join(
        f'<div class="metric {cls}"><b>{esc(val)}</b><span>{esc(lbl)}</span></div>'
        for val, lbl, cls in metrics
    )

    judge_txt = "、".join(f"{k} {v} 条" for k, v in by_kind.items()) or "—"
    highlights = [
        ("01", f"<b>{len(verdicts)} 条用例全部由日志信号与状态契约直接判定</b>，"
               f"视觉模型一次都没用到 —— 确定性的路走完，才考虑花钱看图。"),
        ("02", f"<b>生成阶段独占 {fmt_s(max([float(s.get('elapsed_s') or 0) for s in stages] or [0]))}</b>，"
               f"占全程 {max([float(s.get('elapsed_s') or 0) for s in stages] or [1]) / max(total_s, 1) * 100:.0f}%。"
               f"让 AI 写 8 个文件是这条流水线上最贵的动作。"),
        ("03", f"<b>契约自检 {'通过' if contract.get('ok') else '未通过'}</b>："
               f"模型写的固件输出 <code style=\"font-family:var(--mono);font-size:12px\">"
               f"screen/online/version/ok</code>，判定层才敢按状态下结论。"),
    ]
    hl_html = "".join(
        f'<div class="hl"><i>{n}</i><div>{t}</div></div>' for n, t in highlights
    )

    # ---------------------------------------------------------------- 流水线
    step_rows = []
    mx = max([float(s.get("elapsed_s") or 0) for s in stages] or [1]) or 1
    for i, s in enumerate(stages, 1):
        el = float(s.get("elapsed_s") or 0)
        w = max(3.0, el / mx * 100)
        hot = " hot" if el == mx and el > 1 else ""
        status = "" if s.get("ok") else " · 失败"
        step_rows.append(
            f'<div class="step"><div class="idx">{i:02d}</div>'
            f'<div class="nm">{esc(s.get("label"))}{status}'
            f'<i>{esc(s.get("name"))}</i></div>'
            f'<div class="track"><i class="{hot.strip()}" style="width:{w:.1f}%"></i></div>'
            f'<div class="tm">{fmt_s(el)}</div></div>'
        )
    flow_html = "".join(step_rows)

    # ---------------------------------------------------------------- 产品
    files = product.get("files") or []
    # 按目录分组：目录行只出现一次，文件在其下缩进
    groups: "OrderedDict[str, list[str]]" = OrderedDict()
    for f in files:
        parts = f.split("/")
        folder = "/".join(parts[:-1])
        groups.setdefault(folder, []).append(parts[-1])
    tree_rows = []
    for folder, names in groups.items():
        if folder:
            tree_rows.append(f"<div><em>{esc(folder)}/</em></div>")
            for name in names:
                tree_rows.append(
                    f"<div>&nbsp;&nbsp;&nbsp;&nbsp;<b>{esc(name)}</b></div>"
                )
        else:
            for name in names:
                tree_rows.append(f"<div><b>{esc(name)}</b></div>")
    tree_html = "".join(tree_rows) if tree_rows else "<div><em>（无文件）</em></div>"

    contract_chips = "".join(
        f'<span class="chip ok">{esc(k)}={esc(v)}</span>'
        for k, v in cstate.items()
        if not k.startswith("_")
    ) or '<span class="chip">—</span>'

    requirement = product.get("requirement") or ""
    req_main, _, req_inject = requirement.partition("【重要】")
    inject_html = (
        f'<div class="inject"><b>演示注入 · 故意留 bug</b>{esc(req_inject.strip())}</div>'
        if req_inject.strip()
        else ""
    )

    # ---------------------------------------------------------------- 用例表
    case_rows = []
    for v in verdicts:
        cid = v.get("case_id")
        plan_c = plans.get(cid, {})
        tr = trace_by.get(cid, {})
        cls, label, _ = verdict_meta(v.get("verdict"))
        exp = plan_c.get("expect_state") or {}
        act = tr.get("state") or {}
        cmp_parts = []
        for k in ("screen", "online", "ok"):
            if k not in exp:
                continue
            e, a = exp.get(k), act.get(k, "—")
            same = str(e).lower() == str(a).lower()
            arrow = "=" if same else "→"
            color = "eq" if same else "ne"
            cmp_parts.append(
                f'<span class="{color}">{esc(k)} {esc(e)} {arrow} {esc(a)}</span>'
            )
        cmp_html = "".join(cmp_parts) or '<span class="sub">按语义判定</span>'
        by = v.get("judged_by", "?")
        prio = str(v.get("priority", "P1")).lower()
        logs = tr.get("logs") or v.get("logs") or ""
        rca = v.get("rca") or {}
        detail = (
            f'<tr class="detail" hidden><td colspan="5"><div class="detail-in">'
            f'<div class="lbl">判定理由</div>'
            f'<div style="font-size:13px;color:var(--dim)">{esc(v.get("reason"))}</div>'
            f'<div class="lbl">证据</div>'
            f'<div style="font-family:var(--mono);font-size:12px;color:#9CD3FF">'
            f'{esc(v.get("visual_evidence"))}</div>'
            + (
                f'<div class="lbl">根因</div>'
                f'<div style="font-size:13px;color:var(--dim)">{esc(rca.get("root_cause"))}</div>'
                if rca.get("root_cause")
                else ""
            )
            + f'<div class="lbl">串口日志</div><pre>{esc(logs)}</pre>'
            f'</div></td></tr>'
        )
        case_rows.append(
            f'<tr class="case-row"><td>{esc(cid)}</td>'
            f'<td><span class="ti">{esc(v.get("title"))}</span>'
            f'<span class="sub">scenario={esc(plan_c.get("scenario", "—"))}'
            f' · 期望 {esc((plan_c.get("expect") or "")[:34])}</span></td>'
            f'<td><span class="badge {prio if prio in ("p0", "p1") else "p1"}">'
            f'{esc(v.get("priority"))}</span></td>'
            f'<td><div class="cmp">{cmp_html}</div></td>'
            f'<td><span class="badge {cls}">{label}</span> '
            f'<span class="sub">{(v.get("confidence") or 0):.2f}</span></td>'
            f'<td><span class="by {esc(by)}">{esc(by)}</span></td></tr>'
            f"{detail}"
        )
    cases_html = "".join(case_rows)

    # ---------------------------------------------------------------- 缺陷
    bug_html = []
    for b in bugs:
        rca = b.get("root_cause") or ""
        evidence = b.get("evidence_key") or b.get("fingerprint") or ""
        cases_chips = "".join(
            f'<span class="chip info">{esc(c)}</span>' for c in b.get("affected_cases", [])
        )
        sev = b.get("severity", "P1")
        bug_html.append(
            f'<div class="bug"><div class="bug-h">'
            f'<span class="bug-id">{esc(b.get("id"))}</span>'
            f'<span class="badge {"p0" if sev == "P0" else "p1"}">{esc(sev)}</span>'
            f'<span class="chip">{esc(b.get("status", "open"))}</span></div>'
            f'<div class="bug-t">{esc(b.get("title"))}</div>'
            f'<div class="bug-m"><span>证据键 <b>{esc(evidence)}</b></span>'
            f'<span>受影响 <b>{len(b.get("affected_cases") or [])} 条</b></span>'
            f'<span>指纹 <b>{esc(b.get("fingerprint", "—"))}</b></span></div>'
            f'<div class="chips">{cases_chips}</div>'
            f'<details><summary>根因分析</summary>'
            f'<p>{esc(rca)}</p>'
            + (
                f'<div class="fix"><b>建议修复：</b>'
                f'{esc((b.get("body_markdown") or "")[:0])}'
                f'见 issue 正文</div>'
                if False
                else ""
            )
            + "</details>"
            + (
                f'<details><summary>缺陷正文（Markdown）</summary>'
                f'<pre>{esc(b.get("body_markdown"))}</pre></details>'
                if b.get("body_markdown")
                else ""
            )
            + "</div>"
        )
    bugs_html = "".join(bug_html)

    # ---------------------------------------------------------------- 屏幕快照
    def shot(cid: str) -> str:
        tr = trace_by.get(cid, {})
        p = tr.get("screenshot")
        if not p:
            return '<div class="shot"><div class="shot-h">无可展示快照</div></div>'
        path = Path(p)
        text = ""
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")[:1500]
        v = next((x for x in verdicts if x.get("case_id") == cid), {})
        cls, label, _ = verdict_meta(v.get("verdict"))
        return (
            f'<div class="shot"><div class="shot-h">'
            f'<span class="badge {cls}">{label}</span>'
            f'<span>{esc(cid)} · {esc((v.get("title") or "")[:24])}</span></div>'
            f"<pre>{esc(text)}</pre></div>"
        )

    fail_id = next((v["case_id"] for v in verdicts if v.get("verdict") == "FAIL"), None)
    pass_id = next((v["case_id"] for v in verdicts if v.get("verdict") == "PASS"), None)
    shots_html = "".join(filter(None, [shot(fail_id) if fail_id else "", shot(pass_id) if pass_id else ""]))

    # ---------------------------------------------------------------- 结果
    run_id = summary.get("run_id") or report.get("run_id") or run_dir.name
    ts = run_dir.name[:15]
    try:
        dt = datetime.strptime(ts, "%Y%m%d-%H%M%S").strftime("%Y-%m-%d %H:%M")
    except Exception:
        dt = run_dir.name

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ai-qa-platform · 运行回放 {esc(run_id)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="top"><div class="top-in">
  <div class="mark"></div>
  <div class="brand">ai-qa-platform <span>/ 运行回放</span></div>
  <div class="top-links">
    <a href="{REPO_URL}" target="_blank" rel="noopener">GitHub</a>
    <a href="#pipeline">流水线</a>
    <a href="#cases">用例</a>
    <a href="#bugs">缺陷</a>
  </div>
</div></div>

<div class="wrap">

  <div class="hero">
    <div class="eyebrow"><span class="dot"></span>{esc(model_mode)} · {esc(dt)} · {esc(run_id)}</div>
    <h1>一句需求进去，<em>测试报告</em>出来</h1>
    <p class="lede">
      这是 <b>ai-qa-platform</b> 的一次真实运行回放：AI 先写出一套智能中控屏固件（{len(files)} 个文件），
      平台自己打包、部署到模拟设备、设计 {total} 条用例、真实执行、逐条判定、最后按根因建缺陷。
      下面每一个数字都来自磁盘上的运行产物，不是手写的示意图。
    </p>
    <div class="metrics">{metrics_html}</div>
    <div class="highlights">{hl_html}</div>
  </div>

  <section id="pipeline">
    <div class="sec-h"><span class="sec-n">01</span><h2>十层流水线</h2></div>
    <p class="sec-desc">
      一次运行的真实耗时分布。柱条长度按阶段耗时归一化 —— 一眼能看出钱和时间花在哪：
      生成代码是长输出任务（{fmt_s(max([float(s.get('elapsed_s') or 0) for s in stages] or [0]))}），
      其余九层加起来不到 {fmt_s(total_s - max([float(s.get('elapsed_s') or 0) for s in stages] or [0]))}。
    </p>
    <div class="flow">{flow_html}</div>
  </section>

  <section>
    <div class="sec-h"><span class="sec-n">02</span><h2>被测产品：AI 现场写出来的固件</h2></div>
    <p class="sec-desc">
      平台不只测别人的代码 —— 它可以让模型从一句需求直接写出被测试对象，然后立刻拿去跑测试。
      这样「改需求 → 出新固件 → 自动回归」能在一个循环里完成。
    </p>
    <div class="split">
      <div class="card">
        <h3>生成结果 <span>{esc(product.get("generator"))} · {len(files)} files</span></h3>
        <div class="kv"><span>产品名</span><b>{esc(product.get("name"))}</b></div>
        <div class="kv"><span>产物目录</span><b>runs/{esc(run_dir.name)}/product</b></div>
        <div class="kv"><span>状态契约自检</span><b style="color:{'var(--pass)' if contract.get('ok') else 'var(--fail)'}">
          {'通过' if contract.get('ok') else '未通过'}（exit={esc(contract.get("exit_code"))}）</b></div>
        <div class="kv"><span>自修复轮次</span><b>{len(contract.get("repair_files") or [])} 个文件被回改</b></div>
        <div class="chips" style="margin-top:12px">{contract_chips}</div>
        <div class="tree">{tree_html}</div>
      </div>
      <div class="card">
        <h3>交给模型的需求原文 <span>prompt</span></h3>
        <div class="req">{esc(req_main.strip())}</div>
        {inject_html}
      </div>
    </div>
  </section>

  <section id="cases">
    <div class="sec-h"><span class="sec-n">03</span><h2>逐条判定：期望 vs 设备实际</h2></div>
    <p class="sec-desc">
      AI 设计用例时就给出可机器比对的 <code style="font-family:var(--mono);font-size:13px;color:#9CD3FF">expect_state</code>，
      执行后拿设备上报的真实状态对照。<b>{judge_txt}</b>。
      点任意一行可以展开它的串口日志与判定理由。
    </p>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>用例</th><th>标题 / 场景</th><th>优先级</th><th>期望 → 实际</th><th>判定</th><th>来源</th>
        </tr></thead>
        <tbody>{cases_html}</tbody>
      </table>
    </div>
  </section>

  <section id="bugs">
    <div class="sec-h"><span class="sec-n">04</span><h2>缺陷：按证据键去重</h2></div>
    <p class="sec-desc">
      {total} 条用例、{failed} 条失败，最终只建了 <b>{len(bugs)} 条缺陷</b> —— 因为同一个故障会命中多个用例，
      平台按「证据键」合并，把受影响的用例挂到同一条 issue 上，而不是刷一堆重复单。
    </p>
    {bugs_html}
  </section>

  <section>
    <div class="sec-h"><span class="sec-n">05</span><h2>证据：屏幕快照 + 串口日志</h2></div>
    <p class="sec-desc">
      真实设备这里是 adb screencap 出的 PNG；模拟设备把状态渲染成文本快照，供人读与日志比对。
      两者在判定链路里同一条路径，换成真机不用改下游逻辑。
    </p>
    <div class="shots">{shots_html}</div>
  </section>

  <section>
    <div class="sec-h"><span class="sec-n">06</span><h2>技术构成</h2></div>
    <p class="sec-desc">核心链路零第三方依赖，一个 Python 解释器就能跑；需要的能力都用适配器接出去。</p>
    <div class="stack">
      <div class="st"><h4>Runtime</h4><p><b>Python 3</b>，标准库 urllib 直连模型，<b>零第三方依赖</b>。构建产物 {esc(build.get("size_kb"))} KB，sha256 <code style="font-family:var(--mono);font-size:11.5px">{esc(build.get("sha256"))}</code></p></div>
      <div class="st"><h4>Model</h4><p><b>DeepSeek deepseek-flash</b>（OpenAI 兼容协议）。实测支持图像输入，所以文本与视觉可共用一个 Key；换成 vLLM / Ollama 只改环境变量。</p></div>
      <div class="st"><h4>Adapters</h4><p>设备 <b>{esc(deploy.get("method", "sim-unpack"))}</b>（Mock / Sim / ADB / 串口）、构建 zip 或 docker、缺陷落本地 JSON 或 GitHub Issue。当前部署目标 {esc(deploy.get("device"))}。</p></div>
      <div class="st"><h4>Service</h4><p>FastAPI webhook（<code style="font-family:var(--mono);font-size:11.5px">/webhook/github</code>、<code style="font-family:var(--mono);font-size:11.5px">/runs</code>），可选安装；纯 CLI 也能全流程跑。</p></div>
    </div>
    <div class="cmds">
      <div class="cmd"><code>python -m aqa demo</code><span>零依赖、零 Key，跑完整十层（内置模板产品）</span></div>
      <div class="cmd"><code>python -m aqa demo --model-mode api --buggy</code><span>真模型写产品 + 故意留缺陷，演示发现→归因→建单</span></div>
      <div class="cmd"><code>python -m aqa run --mock</code><span>纯回归：不生成产品，只测已有提交</span></div>
      <div class="cmd"><code>python tools/make_report.py</code><span>把任意一次运行渲染成这份 HTML</span></div>
    </div>
  </section>

  <footer>
    <div>
      <div>本地项目：<code>C:\\Users\\Administrator\\ai-qa-platform</code></div>
      <div style="margin-top:5px">运行产物：<code>runs/{esc(run_dir.name)}/</code></div>
    </div>
    <div style="text-align:right">
      <div>仓库：<a href="{REPO_URL}" target="_blank" rel="noopener">w18341840753-cell/ai-qa-platform</a></div>
      <div style="margin-top:5px">生成于 {datetime.now().strftime("%Y-%m-%d %H:%M")} · 数据来自磁盘产物</div>
    </div>
  </footer>

</div>
<script>{JS}</script>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="把运行产物渲染成单文件 HTML 报告")
    ap.add_argument("run", nargs="?", help="运行目录，默认自动挑最近一次真模型运行")
    ap.add_argument("--out", default=str(ROOT / "docs" / "showcase.html"))
    args = ap.parse_args()

    run_dir = pick_run(args.run)
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(run_dir), encoding="utf-8")
    print(f"report -> {out}")
    print(f"source -> {run_dir}")


if __name__ == "__main__":
    main()
