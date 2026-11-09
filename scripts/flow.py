#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""短视频系列流水线 · 轻量守卫（flow v2）。

只做本地检查与记账：不联网、不调用模型、不操作浏览器、不发布。
设计原则：只在真风险处拦截（模型降级、重复生成/投稿、结果不明重投、原创/AI 声明漏填、
发布无证据、数据提前采集或补零），其余一律放行并给出提示。

子命令（在项目根目录运行，python3 -B flow.py <cmd> -h 查看参数）：
  init            新项目补齐最小配置（只补缺，不覆盖）
  gen-check       生成前检查计划：模型/时长/画幅/单条/用户批准原话/无重复生成
  gen-start       提交前原子检查并记 submitting（仅本地占位，不代表服务端受理）
  gen-log         记录原请求的回执/终态（submitted/succeeded/failed/unknown）
  master-check    成片实测：一视一音、时长、画幅、响度
  pack-check      发布包检查（含原创/AI声明、标题/话题上限）并记录发布批准
  start           某平台开始投稿（查重、拦截结果不明重投）
  done            记录某平台投稿结果；可多次调用推进 审核中→已发布
  status          单期或全部期次状态 + 到期数据窗口
  metrics-due     列出到期未采集的数据窗口
  metric-add      写入一个窗口的数据（到期才收、缺失不补零），同步追加历史
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

VERSION = "2.3.0"
def _find_root() -> Path:
    """项目根：环境变量 FLOW_ROOT > 当前目录（含配置时）> 脚本所在目录。避免在错误目录里静默新建配置。"""
    env = os.environ.get("FLOW_ROOT")
    if env:
        return Path(env).resolve()
    cwd = Path.cwd()
    if (cwd / "video_production_policy.json").exists() or (cwd / "workspace").is_dir():
        return cwd
    here = Path(__file__).resolve().parent
    if (here / "video_production_policy.json").exists():
        return here
    return cwd


ROOT = _find_root()
WORKSPACE = ROOT / "workspace"
POLICY_PATH = ROOT / "video_production_policy.json"
RULES_PATH = ROOT / "platform_rules.json"
GEN_LEDGER = WORKSPACE / "production_ledger.jsonl"
HISTORY_PATH = ROOT / "performance_history.jsonl"

PLATFORMS = ["douyin", "kuaishou", "bilibili", "xiaohongshu", "wechat_channels"]
METRICS = ("plays", "likes", "completion_rate", "jump_off_2s", "engagement_rate")
# 窗口：到期时刻（距发布）与可采集宽限期；过了宽限期记为 missed，不再补采
WINDOWS = {"w24": (timedelta(hours=24), timedelta(hours=24)),
           "w48": (timedelta(hours=48), timedelta(hours=24)),
           "w7d": (timedelta(days=7), timedelta(days=3))}
# 平台已知硬限制（实测）；platform_rules.json 里的同名字段优先
DEFAULT_LIMITS = {"xiaohongshu": {"max_title_chars": 20}, "kuaishou": {"max_topics": 4}}
NOT_SUPPORTED = "NOT_SUPPORTED"

V1_STATE_MAP = {
    "PUBLIC_VERIFIED": "PUBLISHED", "PUBLISHED_BACKEND": "PUBLISHED",
    "UNDER_REVIEW": "UNDER_REVIEW", "ACCEPTED": "UNDER_REVIEW",
    "RESULT_UNKNOWN": "UNKNOWN", "SUBMITTING": "UNKNOWN",
    "REJECTED": "REJECTED",
}


class FlowError(Exception):
    pass


# ----------------------------------------------------------------------------- 工具
def now() -> datetime:
    return datetime.now().astimezone()


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def parse_time(s: str) -> datetime:
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except Exception as e:  # noqa: BLE001
        raise FlowError(f"时间格式不对（需 ISO8601 带时区）: {s}") from e
    if dt.tzinfo is None:
        raise FlowError(f"时间必须带时区: {s}")
    return dt


def read_json(p: Path, default: Any = None) -> Any:
    if not p.exists():
        if default is not None:
            return default
        raise FlowError(f"文件不存在: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def write_json(p: Path, data: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def append_jsonl(p: Path, row: Dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(p: Path) -> List[Dict[str, Any]]:
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def day_path(day_dir: str) -> Path:
    p = Path(day_dir)
    p = p if p.is_absolute() else ROOT / p
    if not p.is_dir():
        raise FlowError(f"日期目录不存在: {day_dir}")
    return p


def rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


# ----------------------------------------------------------------------------- 配置
def load_policy() -> Dict[str, Any]:
    vg = read_json(POLICY_PATH).get("video_generation", {})
    dur = vg.get("duration_seconds", {})
    return {"model": vg.get("required_model"), "min": float(dur.get("minimum", 5)),
            "max": float(dur.get("maximum", 29)), "single_shot": float(vg.get("single_shot_seconds", dur.get("maximum", 29))),
            "aspect": vg.get("aspect_ratio", "9:16"), "tolerance": float(vg.get("media_validation", {}).get("duration_tolerance_seconds", 0.6))}


def load_rules() -> Dict[str, Any]:
    r = read_json(RULES_PATH)
    out = {"account": r.get("account_display_name"), "forbidden": r.get("global", {}).get("forbidden_text_fragments", []), "platforms": {}}
    for p in PLATFORMS:
        pr = dict(DEFAULT_LIMITS.get(p, {}))
        src = r.get("platforms", {}).get(p, {})
        for k in ("max_title_chars", "max_topics"):
            if src.get(k) is not None:
                pr[k] = src[k]
        pr["display"] = src.get("display_name", p)
        pr["original_target"] = src.get("original_declaration_target") or NOT_SUPPORTED
        pr["ai_target"] = src.get("ai_disclosure_target") or ""
        out["platforms"][p] = pr
    return out


# ----------------------------------------------------------------------------- 发布状态（读兼容 v1）
def _v1_published_at(v: Dict[str, Any]) -> Optional[str]:
    for att in reversed(v.get("attempts", []) or []):
        if att.get("platform_published_at"):
            return att["platform_published_at"]
    return None  # 状态转换/复核事件时间不是平台发布时间，不能拿来建数据窗口


def convert_v1(s: Dict[str, Any]) -> Dict[str, Any]:
    plats = {}
    for p, v in s.get("platforms", {}).items():
        ev = v.get("evidence", []) or []
        post_id = next((e.get("post_id") for e in ev if e.get("post_id")), "") or ""
        url = next((e.get("public_url") for e in ev if e.get("public_url")), "") or ""
        state = V1_STATE_MAP.get(v.get("state", ""), "NOT_STARTED")
        plats[p] = {"state": state, "attempts": len(v.get("attempts", []) or []),
                    "post_id": post_id, "public_url": url,
                    "published_at": _v1_published_at(v) if state == "PUBLISHED" else None,
                    "evidence": [e.get("path") for e in ev if e.get("path")],
                    "metrics": {}, "log": [{"at": iso(now()), "note": f"由 v1 状态 {v.get('state')} 转换"}]}
    return {"schema_version": "2", "episode_id": s.get("episode_id"), "master": s.get("master", {}),
            "approval": None, "created_at": s.get("created_at"), "platforms": plats}


def load_status(day: Path, create: bool = False) -> Dict[str, Any]:
    p = day / "publish_status.json"
    if not p.exists():
        if not create:
            raise FlowError(f"{rel(day)} 还没有发布状态，先跑 pack-check")
        return {}
    s = read_json(p)
    if str(s.get("schema_version")) != "2":
        s = convert_v1(s)  # 只在内存转换；首次写入时才落盘并备份原件
        s["_converted_from_v1"] = True
    return s


def save_status(day: Path, s: Dict[str, Any]) -> None:
    if s.pop("_converted_from_v1", False):
        backup = day / "publish_status.v1-backup.json"
        if not backup.exists() and (day / "publish_status.json").exists():
            shutil.copy2(day / "publish_status.json", backup)
    s["updated_at"] = iso(now())
    write_json(day / "publish_status.json", s)


def all_day_dirs() -> List[Path]:
    if not WORKSPACE.is_dir():
        return []
    return sorted(d for d in WORKSPACE.iterdir() if d.is_dir() and len(d.name) == 10 and d.name[4] == "-")


# ----------------------------------------------------------------------------- 生成
def _plan_fields(plan: Dict[str, Any]) -> Dict[str, Any]:
    lp = plan.get("locked_parameters", plan)
    return {"model": lp.get("model_version"), "fallback": lp.get("fallback_allowed", False),
            "duration": lp.get("target_duration_seconds"), "aspect": lp.get("aspect_ratio"),
            "count": lp.get("video_count", 1), "segments": lp.get("segments_seconds") or [lp.get("target_duration_seconds")]}


def _plan_approval(plan: Dict[str, Any]) -> str:
    a = plan.get("user_approval") or {}
    if isinstance(a, str):
        return a.strip()
    q = (a.get("quote") or "").strip()
    if q:
        return q
    old = plan.get("generation_approval") or {}  # v1 计划兼容
    return (old.get("confirmation_id") or "").strip() if old.get("decision") == "APPROVED" else ""


def _gen_key(plan: Dict[str, Any], plan_path: Path) -> str:
    return plan.get("candidate_id") or plan_path.stem


def _resolve_exception(a: Any, prior: List[Dict[str, Any]], key: str) -> Optional[Dict[str, Any]]:
    """一次性正式授权例外：仅当 last=unknown 时，凭本轮封闭单选确认原文+非空证据开一个独立新 attempt。

    旧 unknown 行原样保留不改写；同一旧 unknown 只允许一次例外，防止重复消费。
    普通 --regen-approved 不进入此路径，仍被 unknown 阻断。
    """
    quote = (getattr(a, "exception_approved", "") or "").strip()
    ev_arg = (getattr(a, "exception_evidence", "") or "").strip()
    if not prior:
        return None
    if not quote and not ev_arg:
        return None
    if not quote or not ev_arg:
        raise FlowError("一次性例外必须同时给 --exception-approved 本轮封闭单选授权原话与 --exception-evidence 证据文件")
    ev_path = Path(ev_arg)
    ev_path = ev_path if ev_path.is_absolute() else ROOT / ev_arg
    if not ev_path.is_file() or ev_path.stat().st_size == 0:
        raise FlowError("例外授权证据不存在或为空：先保存本轮封闭单选确认原文、来源与时间，再登记例外")
    used = [r for r in prior if r.get("exception")]
    if used:
        raise FlowError(f"候选 {key} 已有一次正式授权的例外 attempt（{used[-1].get('attempt_id')}），禁止重复消耗；旧 unknown 保持不改写")
    last = prior[-1]
    if last.get("status") != "unknown":
        raise FlowError("一次性例外只用于上次结果 unknown 的冻结请求；当前末态不是 unknown，走常规重生批准路径")
    return {"quote": quote,
            "evidence": {"path": rel(ev_path.resolve()), "sha256": sha256_file(ev_path)},
            "parent_unknown_at": last.get("at"),
            "parent_unknown_note": (last.get("note") or "")[:300]}


def _next_attempt_id(prior: List[Dict[str, Any]]) -> str:
    n = sum(1 for r in prior if r.get("attempt_id")) + 1
    return f"attempt-{n + 1}"


def cmd_gen_check(a) -> Dict[str, Any]:
    pol = load_policy()
    path = Path(a.plan)
    plan = read_json(path)
    f = _plan_fields(plan)
    errs = []
    if f["model"] != pol["model"]:
        errs.append(f"模型必须是 {pol['model']}（计划写的是 {f['model']}），禁止降级/替换")
    if f["fallback"]:
        errs.append("fallback_allowed 必须为 false")
    try:
        d = float(f["duration"])
        if not pol["min"] <= d <= pol["max"]:
            errs.append(f"时长 {d}s 超出 {pol['min']}-{pol['max']}s")
        if d <= pol["single_shot"] and (len(f["segments"]) != 1 or int(f["count"]) != 1):
            errs.append(f"≤{pol['single_shot']}s 必须单条生成、不拆段")
    except (TypeError, ValueError):
        errs.append("缺少 target_duration_seconds")
    if f["aspect"] != pol["aspect"]:
        errs.append(f"画幅必须是 {pol['aspect']}")
    if not (plan.get("prompt") or "").strip():
        errs.append("缺少 prompt（提交给模型的原文）")
    if not _plan_approval(plan):
        errs.append("缺少 user_approval.quote：用户在对话里同意生成的原话")
    key = _gen_key(plan, path)
    prior = [r for r in read_jsonl(GEN_LEDGER) if r.get("key") == key]
    last = prior[-1]["status"] if prior else None
    exception = _resolve_exception(a, prior, key)
    if last == "unknown" and exception is None:
        errs.append("上次生成结果未知：先在生成记录里核实，再附 --evidence 用 gen-log 记成 succeeded/failed，禁止直接重生；仅本轮正式封闭单选授权可走一次性例外（--exception-approved/--exception-evidence）")
    elif last in ("submitting", "submitted"):
        errs.append(f"原生成请求仍在等待结果（{last}）：只能核查原请求，禁止重生；--regen-approved 不能越过此状态")
    elif last == "succeeded" and not (a.regen_approved or "").strip():
        errs.append(f"该候选已有生成记录（{last}）。确需重生，须带 --regen-approved \"用户同意重生的原话\"")
    if errs:
        raise FlowError("；".join(errs))
    return {"ok": True, "generation_allowed": True, "key": key, "model": f["model"], "duration": f["duration"],
            "exception": exception,
            "next": "提交前 gen-start 原子占位；只调用一次并静默等待，先保存回执，再 gen-log 记录原请求结果"}


def cmd_gen_start(a) -> Dict[str, Any]:
    # CLI 写锁覆盖检查与占位，防止两个执行者同时通过只读 gen-check 后各自提交。
    res = cmd_gen_check(a)
    prior = [r for r in read_jsonl(GEN_LEDGER) if r.get("key") == res["key"]]
    exc = _resolve_exception(a, prior, res["key"])
    row: Dict[str, Any] = {"object_type": "generation_log", "key": res["key"], "status": "submitting",
                           "model": res["model"], "plan_path": rel(Path(a.plan).resolve()),
                           "plan_sha256": sha256_file(Path(a.plan)), "result_url": "",
                           "note": "提交前本地占位；不代表服务端已受理。只允许本次执行者随后提交一次。",
                           "regen_approved": a.regen_approved or "", "at": iso(now())}
    if exc:
        row["attempt_id"] = _next_attempt_id(prior)
        row["exception"] = exc
        row["parent_unknown_at"] = exc["parent_unknown_at"]
        row["note"] = "一次性正式授权例外新 attempt；旧 unknown 原样保留不改写，仅本次执行一次。占位不代表服务端受理。"
    append_jsonl(GEN_LEDGER, row)
    return {"ok": True, "logged": row,
            "next": "单次调用生成工具；等待期间不接收例行更新，返回后先保存回执再 gen-log；中断记 unknown，不重试"}


def cmd_gen_log(a) -> Dict[str, Any]:
    path = Path(a.plan)
    plan = read_json(path)
    key = _gen_key(plan, path)
    prior = [r for r in read_jsonl(GEN_LEDGER) if r.get("key") == key]
    last = prior[-1] if prior else {}
    last_status = last.get("status")
    # 打开中的例外 attempt：只给它记终态，旧 unknown 行原样保留。
    open_exc = (last_status == "submitting" and last.get("attempt_id") and last.get("exception"))
    evidence = None
    if a.evidence:
        ep = Path(a.evidence)
        ep = ep if ep.is_absolute() else ROOT / ep
        if not ep.is_file() or ep.stat().st_size == 0:
            raise FlowError("生成回执证据不存在或为空：请保存实际工具返回/原请求核查结果后再记账")
        evidence = {"path": rel(ep.resolve()), "sha256": sha256_file(ep)}
    if last_status == "unknown" and a.status == "submitted" and not open_exc:
        raise FlowError("结果未知不能改为 submitted 解冻；先核查原请求并附证据记录终态；一次性例外须经 gen-start 登记新 attempt")
    if a.status in ("succeeded", "failed") and not evidence:
        raise FlowError("生成终态必须附 --evidence 原始回执或核查证据；超时/中断不能当成失败")
    if a.status == "succeeded" and not (a.url or "").strip():
        raise FlowError("生成成功必须保存 --url 原始视频结果地址；不能只记成功而丢失结果")
    row: Dict[str, Any] = {"object_type": "generation_log", "key": key, "status": a.status,
                           "model": _plan_fields(plan)["model"], "result_url": a.url or "", "note": a.note or "",
                           "regen_approved": a.regen_approved or "", "at": iso(now())}
    if open_exc:
        row["attempt_id"] = last["attempt_id"]
        row["exception"] = {"quote": last["exception"]["quote"],
                            "evidence": last["exception"]["evidence"],
                            "parent_unknown_at": last["exception"].get("parent_unknown_at")}
        row["parent_unknown_at"] = last.get("parent_unknown_at")
    if evidence:
        row["evidence"] = evidence
    append_jsonl(GEN_LEDGER, row)
    return {"ok": True, "logged": row}


# ----------------------------------------------------------------------------- 成片
def _ffprobe(path: Path) -> Dict[str, Any]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def _mean_volume(path: Path) -> Optional[float]:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True)
    for line in r.stderr.splitlines():
        if "mean_volume:" in line:
            return float(line.split("mean_volume:")[1].split("dB")[0])
    return None


def _loudness(path: Path) -> Dict[str, Optional[float]]:
    """EBU R128 响度（integrated LUFS / true peak dBTP / LRA）。解析不到时各字段为 None。"""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128=peak=true",
                        "-f", "null", "-"], capture_output=True, text=True)
    out: Dict[str, Optional[float]] = {"integrated_lufs": None, "true_peak_db": None, "loudness_range_lu": None}
    summary = r.stderr.split("Summary:")[-1] if "Summary:" in r.stderr else ""
    for line in summary.splitlines():
        s = line.strip()
        try:
            if s.startswith("I:"):
                out["integrated_lufs"] = float(s.split()[1])
            elif s.startswith("LRA:"):
                out["loudness_range_lu"] = float(s.split()[1])
            elif s.startswith("Peak:") and "True peak" not in s and "dBTP" in s:
                out["true_peak_db"] = float(s.split()[1])
        except (IndexError, ValueError):
            continue
    # 有的 ffmpeg 版本 true peak 行形如 “    Peak:   -1.5 dBFS”
    for line in summary.splitlines():
        if line.strip().startswith("Peak:") and out["true_peak_db"] is None:
            try:
                out["true_peak_db"] = float(line.split("Peak:")[1].split("dB")[0].strip())
            except (IndexError, ValueError):
                pass
    return out


def _scene_cuts(path: Path, threshold: float = 0.25) -> List[float]:
    """ffmpeg scene 检测的镜头切点（秒，去掉 0）。需要 ffmpeg；返回 [] 表示没检测到或只有一个镜头。"""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
                        "-vf", f"select='gt(scene,{threshold})',showinfo", "-an", "-f", "null", "-"],
                       capture_output=True, text=True)
    cuts = []
    for line in r.stderr.splitlines():
        m = re.search(r"pts_time:([0-9.]+)", line)
        if m:
            cuts.append(float(m.group(1)))
    return sorted(round(t, 3) for t in cuts if t > 0.2)


def _read_timeline(path: Path) -> Optional[List[Dict[str, Any]]]:
    """逐句旁白时间轴（[{start,end,text?}]）。读不到返回 None，不报错（不强制每个项目都有）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, list):
        return None
    rows = []
    for x in data:
        if isinstance(x, dict) and isinstance(x.get("start"), (int, float)) and isinstance(x.get("end"), (int, float)):
            rows.append({"start": float(x["start"]), "end": float(x["end"]), "text": x.get("text", "")})
    return rows or None


def _shot_alignment(cuts: List[float], timeline: List[Dict[str, Any]], tolerance: float = 1.2,
                    action_ranges: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """仅统计句界与切点的接近度，不判断语义对齐；完整动作区间内允许多句连续旁白。"""
    protected = action_ranges or []
    for x in protected:
        if (not isinstance(x, dict) or not isinstance(x.get("start"), (int, float)) or
                not isinstance(x.get("end"), (int, float)) or x["start"] < 0 or x["end"] <= x["start"]):
            raise FlowError("完整动作区间需要有效 start/end（0≤start<end）")
    all_bounds = [round(float(x["start"]), 3) for x in timeline[1:]]
    exempt = [b for b in all_bounds if any(x["start"] < b < x["end"] for x in protected)]
    bounds = [b for b in all_bounds if b not in exempt]
    info = {"semantic_alignment_verified": False, "exempt_action_bounds": exempt,
            "action_internal_cuts": [c for c in cuts if any(x["start"] < c < x["end"] for x in protected)]}
    if not cuts or len(bounds) < 2:
        return {**info, "checked": False, "reason": "可比较切点/句界不足；语义与动作连续性仍须复核"}
    misses = [b for b in bounds if all(abs(b - c) > tolerance for c in cuts)]
    return {**info, "checked": True, "tolerance_s": tolerance, "shot_cuts": cuts, "speech_bounds": bounds,
            "misses": misses, "miss_ratio": round(len(misses) / len(bounds), 3),
            "cut_proximity_ok": len(misses) < max(2, len(bounds) / 2)}


def cmd_master_check(a) -> Dict[str, Any]:
    pol = load_policy()
    path = Path(a.master)
    if not path.is_file():
        raise FlowError(f"母版不存在: {a.master}")
    target = float(a.duration) if a.duration else pol["max"]
    info = _ffprobe(path)
    v = [s for s in info["streams"] if s.get("codec_type") == "video"]
    au = [s for s in info["streams"] if s.get("codec_type") == "audio"]
    errs, warns = [], []
    if len(v) != 1 or len(au) != 1:
        errs.append(f"需要恰好一条视频轨和一条音频轨（实际 {len(v)} 视 {len(au)} 音）")
    durs = {"container": float(info["format"].get("duration", 0))}
    if v:
        durs["video"] = float(v[0].get("duration") or durs["container"])
        w, h = int(v[0]["width"]), int(v[0]["height"])
        aw, ah = (int(x) for x in pol["aspect"].split(":"))
        if abs(w / h - aw / ah) > 0.01:
            errs.append(f"画幅 {w}x{h} 不是 {pol['aspect']}")
    if au:
        durs["audio"] = float(au[0].get("duration") or durs["container"])
    for k, d in durs.items():
        if abs(d - target) > pol["tolerance"]:
            errs.append(f"{k} 时长 {d:.3f}s 与目标 {target}s 相差超过 {pol['tolerance']}s")
    # 响度：优先 EBU R128（integrated LUFS / true peak），没有 ebur128 时回退 mean_volume
    loud: Dict[str, Any] = {}
    if au:
        loud = _loudness(path)
    i_lufs = loud.get("integrated_lufs")
    tp = loud.get("true_peak_db")
    mv = _mean_volume(path) if au and i_lufs is None else None
    loudness_mode = "ebu_r128" if i_lufs is not None else ("mean_volume_fallback" if mv is not None else "unavailable")
    if i_lufs is not None and i_lufs < a.min_lufs:
        errs.append(f"整体响度 {i_lufs:.1f} LUFS 低于 {a.min_lufs:.0f} LUFS，手机外放会听不清")
    elif mv is not None and mv < a.min_mean_db:
        errs.append(f"整体响度 {mv:.1f}dB 低于 {a.min_mean_db}dB（ebur128 不可用，回退均值判定），手机外放会听不清")
    if tp is not None and tp > a.max_tp:
        errs.append(f"真峰值 {tp:.1f} dBTP 超过 {a.max_tp:.1f} dBTP，有削波风险")
    # 声画对齐：镜头切点与逐句旁白边界比对（启发式；没有时间轴/切点不足时不拦）
    alignment: Dict[str, Any] = {"checked": False}
    if v and a.timeline:
        timeline = _read_timeline(Path(a.timeline))
        if timeline is None:
            raise FlowError(f"逐句时间轴读不出（需要 start/end JSON 列表）: {a.timeline}")
        actions = read_json(Path(a.action_ranges)) if a.action_ranges else []
        if not isinstance(actions, list):
            raise FlowError("完整动作区间必须是JSON列表")
        alignment = _shot_alignment(_scene_cuts(path, threshold=a.scene_threshold), timeline,
                                    tolerance=a.align_tolerance, action_ranges=actions)
        if alignment.get("checked") and not alignment.get("cut_proximity_ok"):
            warns.append(
                f"句界/切点接近度提示：{len(alignment['misses'])}/{len(alignment['speech_bounds'])} 个句界附近 "
                f"±{a.align_tolerance}s 无切点；这不是声画语义错位证明。"
                "先连续回看动作与因果，仅修已确认错位，禁止为消除报警碎切/重排")
        if alignment.get("action_internal_cuts"):
            warns.append("完整动作区间检测到切点：连续回看确认是否动作被切断或检测误差，不自动修改")
    if errs:
        raise FlowError("；".join(errs))
    if i_lufs is not None and i_lufs > -9:
        warns.append(f"响度 {i_lufs:.1f} LUFS 偏大，注意削波")
    elif mv is not None and mv > -10:
        warns.append(f"响度 {mv:.1f}dB 偏大，注意削波")
    if au and loudness_mode == "mean_volume_fallback":
        warns.append("本机 ffmpeg 无 ebur128，响度按 mean_volume 兜底；建议升级 ffmpeg")
    return {"ok": True, "sha256": sha256_file(path), "durations": durs,
            "loudness": {"mode": loudness_mode, "integrated_lufs": i_lufs, "true_peak_db": tp,
                         "loudness_range_lu": loud.get("loudness_range_lu"), "mean_volume_db_fallback": mv},
            "shot_alignment": alignment, "warnings": warns,
            "remind": "仍需人工/自动回听：旁白逐句完整、字幕不裁切、画面无伪文字/伪品牌；声画对齐为启发式报警，不替代人工复核"}


# ----------------------------------------------------------------------------- 发布包
def cmd_raw_check(a) -> Dict[str, Any]:
    """
    原片（生成直出）核验：规格 + 镜头切点表 + 均匀抽帧拼图。
    拼图必须人工看：伪品牌/伪文字/不可读道具文字在琴盖、信笺、门牌等高光区域最常见；
    守卫不做 OCR（无可靠本地依赖时不误判），只把证据备好。
    """
    pol = load_policy()
    raw = Path(a.raw)
    if not raw.is_file():
        raise FlowError(f"原片不存在: {a.raw}")
    info = _ffprobe(raw)
    v = [s for s in info["streams"] if s.get("codec_type") == "video"]
    if len(v) != 1:
        raise FlowError(f"原片需要恰好一条视频轨（实际 {len(v)}）")
    vs = v[0]
    w, h = int(vs["width"]), int(vs["height"])
    dur = float(info["format"].get("duration") or 0)
    aw, ah = (int(x) for x in pol["aspect"].split(":"))
    spec_errs = []
    if abs(w / h - aw / ah) > 0.03:
        spec_errs.append(f"画幅 {w}x{h} 与 {pol['aspect']} 不符")
    if dur > pol["max"] + pol["tolerance"]:
        spec_errs.append(f"时长 {dur:.2f}s 超过单条上限 {pol['max']}s（应单条直出，不要拆段）")
    fps_txt = vs.get("r_frame_rate", "24/1")
    try:
        num, den = fps_txt.split("/")
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        fps = 24.0
    cuts = _scene_cuts(raw, threshold=a.threshold)
    day = day_path(a.day_dir) if a.day_dir else None
    outdir = (day / "_raw_check") if day else raw.parent / "_raw_check"
    outdir.mkdir(parents=True, exist_ok=True)
    n = max(2, int(a.frames))
    # 均匀取帧（避开 0 和结尾黑帧），tile 成拼图
    times = [round(dur * (i + 0.5) / n, 2) for i in range(n)]
    frames_dir = outdir / "frames"
    frames_dir.mkdir(exist_ok=True)
    frame_paths: List[Path] = []
    for i, t in enumerate(times):
        fp = frames_dir / f"raw_{i:02d}_{t:05.2f}s.png"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i", str(raw),
                        "-frames:v", "1", "-vf", f"scale={a.thumb_width}:-1", str(fp)], check=True)
        frame_paths.append(fp)
    cols = 4
    rows = (n + cols - 1) // cols
    sheet = outdir / "raw-contact-sheet.png"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *sum([["-i", str(p)] for p in frame_paths], []),
                    "-filter_complex", f"tile={cols}x{rows}", str(sheet)], check=True)
    report = {"ok": not spec_errs, "raw": rel(raw) if day else str(raw), "spec": {"width": w, "height": h,
              "duration_s": round(dur, 3), "fps": round(fps, 3), "expected_aspect": pol["aspect"],
              "max_seconds": pol["max"]}, "shot_cuts": cuts, "shot_count": len(cuts) + 1,
              "sample_times_s": times, "contact_sheet": rel(sheet) if day else str(sheet),
              "visual_review_required": ["伪品牌/logo（琴盖、标牌）", "生成文字/乱码（信笺、书页、门牌）",
                                          "人物口型/对白", "与旁白叙事顺序是否一致"],
              "errors": spec_errs,
              "remind": "拼图必须人工过一遍；发现伪文字优先用裁剪/模糊/换帧在后期解决，不为此重新生成"}
    if day:
        write_json(day / "raw-check.json", report)
    if spec_errs:
        raise FlowError("；".join(spec_errs) + f"；镜头与抽帧证据已生成：{report['contact_sheet']}")
    return report


# ----------------------------------------------------------------------------- 发布包
def _check_pack(day: Path, pack: Dict[str, Any], rules: Dict[str, Any]) -> List[str]:
    errs = []
    m = pack.get("master", {})
    mp = day / (m.get("path") or "")
    if not m.get("path") or not mp.is_file():
        errs.append(f"母版文件不存在: {m.get('path')}")
    elif m.get("sha256") and sha256_file(mp) != m["sha256"]:
        errs.append("母版 sha256 与发布包不一致（文件被改过？重新 master-check 并更新发布包）")
    if m.get("cover_path") and not (day / m["cover_path"]).is_file():
        errs.append(f"封面不存在: {m['cover_path']}")
    if not pack.get("episode_id"):
        errs.append("缺 episode_id")
    for p, c in (pack.get("platforms") or {}).items():
        if p not in PLATFORMS:
            errs.append(f"未知平台 {p}")
            continue
        r = rules["platforms"][p]
        name = r["display"]
        title = (c.get("title") or "").strip()
        if not title:
            errs.append(f"{name}：缺标题")
        if r.get("max_title_chars") and len(title) > r["max_title_chars"]:
            errs.append(f"{name}：标题 {len(title)} 字，超过上限 {r['max_title_chars']}")
        topics = c.get("topics") or []
        if r.get("max_topics") and len(topics) > r["max_topics"]:
            errs.append(f"{name}：话题 {len(topics)} 个，超过上限 {r['max_topics']}")
        text = " ".join([title, c.get("description") or ""] + list(topics))
        for frag in rules["forbidden"]:
            if frag in text:
                errs.append(f"{name}：文案含占位/禁用片段 {frag!r}")
        if not (c.get("ai_disclosure") or "").strip():
            errs.append(f"{name}：缺 AI 标识（ai_disclosure）")
        if r["original_target"] != NOT_SUPPORTED and c.get("original_declaration") != r["original_target"]:
            errs.append(f"{name}：平台支持原创声明，必须启用（original_declaration 应为 {r['original_target']!r}）")
    if not pack.get("platforms"):
        errs.append("发布包没有任何平台")
    return errs


def _release_binding(day: Path, pack: Dict[str, Any], scope: List[str]) -> Dict[str, Any]:
    """只绑定会外发的内容及真实资产；忽略 _ 前缀内部备注。"""
    def public_fields(value):
        if isinstance(value, dict):
            return {k: public_fields(v) for k, v in value.items() if not k.startswith("_")}
        if isinstance(value, list):
            return [public_fields(v) for v in value]
        return value
    master = dict(pack["master"])
    master["sha256"] = sha256_file(day / master["path"])
    cover = dict(pack.get("cover") or {})
    cover_path = cover.get("path") or master.get("cover_path")
    if cover_path:
        if not (day / cover_path).is_file():
            raise FlowError(f"封面不存在: {cover_path}")
        cover["sha256"] = sha256_file(day / cover_path)
    payload = {"episode_id": pack["episode_id"], "master": master, "cover": cover,
               "account": pack.get("account_display_name") or load_rules()["account"],
               "cta": pack.get("cta"), "scope": sorted(scope),
               "platforms": {p: public_fields(pack["platforms"][p]) for p in sorted(scope)}}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "master_sha256": master["sha256"], "scope": sorted(scope)}


def _approval_valid(day: Path, pack: Dict[str, Any], s: Dict[str, Any]) -> bool:
    a = s.get("approval") or {}
    b = a.get("binding") or {}
    scope = b.get("scope") or []
    if not a.get("quote") or a.get("kind") != "publication" or not scope:
        return False  # 旧的无版本绑定批准不可用于新的外发
    if any(p not in pack.get("platforms", {}) for p in scope):
        return False
    return b == _release_binding(day, pack, scope)


def _archive_approval(s: Dict[str, Any], reason: str) -> None:
    if s.get("approval"):
        s.setdefault("approval_history", []).append({**s["approval"], "invalidated_at": iso(now()), "reason": reason})
        s["approval"] = None


def _next_release(s: Dict[str, Any], approved: bool) -> str:
    states = [v.get("state") for v in s.get("platforms", {}).values()]
    if states and all(x == "PUBLISHED" for x in states):
        return "completed"
    if any(x in ("PREPARED", "SUBMITTING", "SUBMITTED", "UNKNOWN", "UNDER_REVIEW") for x in states):
        return "resume"
    return "prepare" if approved else "review_current_master_and_copy"


def cmd_pack_check(a) -> Dict[str, Any]:
    day = day_path(a.day_dir)
    pack = read_json(day / "publish_pack.json")
    errs = _check_pack(day, pack, load_rules())
    if errs:
        raise FlowError("；".join(errs))
    s = load_status(day, create=True)
    if not s:
        s = {"schema_version": "2", "episode_id": pack["episode_id"], "master": pack["master"],
             "approval": None, "created_at": iso(now()), "platforms": {}}
    elif s.get("episode_id") != pack["episode_id"]:
        raise FlowError(f"状态文件期号 {s.get('episode_id')} 与发布包 {pack['episode_id']} 不一致，期号一经使用不改")
    current = _release_binding(day, pack, list(pack["platforms"]))
    old = s.get("pack_binding")
    active = any(v.get("state") in ("PREPARED", "SUBMITTING", "SUBMITTED", "UNKNOWN", "UNDER_REVIEW", "PUBLISHED")
                 for v in s.get("platforms", {}).values())
    master_changed = bool(s.get("master", {}).get("sha256") and
                          s["master"]["sha256"] != current["master_sha256"])
    if active and (master_changed or (old and old != current)):
        raise FlowError("已有原表单/投稿绑定旧版：先核查原结果，禁止更换发布包覆盖；已发布平台不可换版重投")
    if s.get("approval") and (not _approval_valid(day, pack, s) or (old and old != current)):
        _archive_approval(s, "发布内容变化或旧批准缺版本绑定")
    s["master"] = {**pack["master"], "sha256": current["master_sha256"]}
    s["pack_binding"] = current
    for p in pack["platforms"]:
        s["platforms"].setdefault(p, {"state": "NOT_STARTED", "attempts": 0, "post_id": "", "public_url": "",
                                      "published_at": None, "evidence": [], "metrics": {}, "log": []})
    quote = a.approved.strip()
    if quote:
        if a.approval_kind == "publication":
            scope = a.platforms or list(pack["platforms"])
            if any(p not in pack["platforms"] for p in scope):
                raise FlowError("批准范围包含发布包之外的平台")
            _archive_approval(s, "由本次明确发布批准替代")
            s["approval"] = {"quote": quote, "at": iso(now()), "kind": "publication",
                             "binding": _release_binding(day, pack, scope)}
        else:
            s.setdefault("revision_approvals", []).append({"quote": quote, "at": iso(now()), "binding": current})
    approved = _approval_valid(day, pack, s)
    save_status(day, s)
    return {"ok": True, "episode_id": pack["episode_id"], "platforms": list(pack["platforms"]),
            "release_approved": approved, "next": _next_release(s, approved)}


# ----------------------------------------------------------------------------- 投稿
def _other_submissions(platform: str, master_sha: str, episode_id: str, this_day: Path) -> List[str]:
    hits = []
    for d in all_day_dirs():
        if d == this_day or not (d / "publish_status.json").exists():
            continue
        try:
            s = load_status(d)
        except Exception:  # noqa: BLE001
            continue
        ps = s.get("platforms", {}).get(platform, {})
        same = (s.get("master", {}).get("sha256") == master_sha) or (s.get("episode_id") == episode_id)
        if same and ps.get("state") in ("PREPARED", "SUBMITTING", "SUBMITTED", "UNDER_REVIEW", "PUBLISHED", "UNKNOWN"):
            hits.append(f"{d.name}:{ps.get('state')}")
    return hits


def _assert_release(day: Path, s: Dict[str, Any], platform: str) -> Dict[str, Any]:
    pack = read_json(day / "publish_pack.json")
    errs = _check_pack(day, pack, load_rules())
    if errs:
        raise FlowError("发布包已不合规：" + "；".join(errs))
    if not _approval_valid(day, pack, s):
        raise FlowError("还没有当前版本的发布批准（母版/文案/封面/账号变化或旧批准未绑定）：重新交付后 pack-check --approved 原话")
    if platform not in s["approval"]["binding"]["scope"]:
        raise FlowError("该平台不在本次发布批准范围内")
    if s.get("pack_binding") != _release_binding(day, pack, list(pack["platforms"])):
        raise FlowError("发布包变化，先 pack-check；禁止覆盖原表单/投稿")
    return pack


def _screenshot_evidence(day: Path, paths: List[str]) -> List[str]:
    ev = []
    for e in paths:
        ep = Path(e) if Path(e).is_absolute() else day / e
        if not ep.is_file() or ep.stat().st_size == 0:
            raise FlowError(f"证据截图不存在或为空: {e}（放在当日目录）")
        ev.append(rel(ep) if ep.parent != day else ep.name)
    return ev


def _resume_action(ps: Dict[str, Any]) -> str:
    st = ps.get("state")
    if st == "PUBLISHED":
        return "completed"
    if st == "UNDER_REVIEW":
        return "verify_original_post"
    if ps.get("checkpoint") == "WAITING_AUTH":
        return "handoff_auth_then_observe_original"
    if st in ("SUBMITTING", "SUBMITTED", "UNKNOWN"):
        return "verify_original_result_no_resubmit"
    if st == "PREPARED":
        return "observe_original_editor_then_start_once"
    if st == "REJECTED":
        return "review_rejection_before_new_attempt"
    return "prepare_after_current_release_approval"


def cmd_resume(a) -> Dict[str, Any]:
    s = load_status(day_path(a.day_dir))
    return {"ok": True, "platforms": {p: {"state": ps.get("state"), "checkpoint": ps.get("checkpoint"),
              "attempts": ps.get("attempts", 0), "editor_url": ps.get("editor_url"),
              "next": _resume_action(ps)} for p, ps in s["platforms"].items()
              if not a.platform or p == a.platform}}


def cmd_checkpoint(a) -> Dict[str, Any]:
    day = day_path(a.day_dir)
    s = load_status(day)
    ps = s["platforms"].get(a.platform)
    if ps is None:
        raise FlowError("发布包里没有该平台")
    ev = _screenshot_evidence(day, a.evidence)
    if not ev:
        raise FlowError("检查点必须有本次现场截图")
    if a.editor_url:
        u = urlsplit(a.editor_url)
        if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise FlowError("编辑页地址须为无凭据/无查询参数/无片段的https地址，不保存认证链接")
    if ps["state"] in ("PUBLISHED", "UNDER_REVIEW", "REJECTED"):
        raise FlowError("已投稿终态/审核中不登记编辑检查点，只核查原作品")
    if a.stage == "prepared":
        _assert_release(day, s, a.platform)
        if ps["state"] not in ("NOT_STARTED", "PREPARED"):
            raise FlowError("旧SUBMITTING/UNKNOWN只能核查原结果，禁止用prepared解锁")
        dup = _other_submissions(a.platform, s["master"].get("sha256"), s["episode_id"], day)
        if dup:
            raise FlowError(f"同一母版/期号在其他日期已有投稿/表单: {dup}")
        ps["state"] = "PREPARED"
        ps["checkpoint"] = "EDITOR_READY"
    elif a.stage == "waiting_auth":
        ps["checkpoint"] = "WAITING_AUTH"  # 不覆盖提交状态，验证码超时仍不能重投
    else:
        ps["checkpoint"] = "OBSERVED_READY"  # 用户返回后观察已通过；不解锁UNKNOWN
    if a.editor_url:
        ps["editor_url"] = a.editor_url
    ps["evidence"] = list(dict.fromkeys((ps.get("evidence") or []) + ev))
    ps["verified_at"] = iso(now())
    ps.setdefault("log", []).append({"at": ps["verified_at"], "note": f"checkpoint={a.stage}；{a.note}"})
    save_status(day, s)
    return {"ok": True, "state": ps["state"], "attempts": ps.get("attempts", 0), "next": _resume_action(ps)}


def cmd_start(a) -> Dict[str, Any]:
    day = day_path(a.day_dir)
    s = load_status(day)
    _assert_release(day, s, a.platform)
    ps = s["platforms"].get(a.platform)
    if ps is None:
        raise FlowError(f"发布包里没有 {a.platform}")
    st = ps["state"]
    if st in ("SUBMITTING", "SUBMITTED", "UNKNOWN"):
        raise FlowError("上次投稿结果未知：先到平台后台核查原作品，用 done 记录实际结果；禁止重新投稿")
    if st in ("UNDER_REVIEW", "PUBLISHED"):
        raise FlowError(f"该平台已投稿（{st}），一期一平台只允许一条有效作品")
    if ps.get("checkpoint") == "WAITING_AUTH":
        raise FlowError("等待登录/验证码：用户完成后观察并登记ready，不能直接投稿")
    resubmit = st == "REJECTED"
    dup = _other_submissions(a.platform, s["master"].get("sha256"), s["episode_id"], day)
    if dup:
        raise FlowError(f"同一母版/期号已在其他日期投过该平台：{dup}")
    ps["state"] = "SUBMITTING"
    ps["checkpoint"] = "SUBMIT_RESERVED"
    ps["attempts"] = int(ps.get("attempts", 0)) + 1
    ps["started_at"] = iso(now())
    ps["submission_binding"] = s["approval"]["binding"]
    ps.setdefault("log", []).append({"at": ps["started_at"], "note": f"开始投稿 attempt={ps['attempts']}（本地占位，未证明受理）"})
    save_status(day, s)
    return {"ok": True, "platform": a.platform, "attempt": ps["attempts"], "resubmit_after_reject": resubmit,
            "remind": "输入准备与现场核验已完成后再start；仅点击原表单提交一次，然后观察并done；中断只查原结果"}


def _due_windows(published_at: str) -> Dict[str, Dict[str, str]]:
    t0 = parse_time(published_at)
    return {w: {"due_at": iso(t0 + off), "close_at": iso(t0 + off + grace)} for w, (off, grace) in WINDOWS.items()}


def cmd_done(a) -> Dict[str, Any]:
    day = day_path(a.day_dir)
    s = load_status(day)
    ps = s["platforms"].get(a.platform)
    if ps is None:
        raise FlowError(f"发布包里没有 {a.platform}")
    ev = _screenshot_evidence(day, a.evidence or [])
    prev = ps["state"]
    if prev in ("NOT_STARTED", "PREPARED") and a.result != "not_submitted":
        raise FlowError("该平台还没 start，先 start 再投稿")
    if prev == "UNKNOWN" and not ev:
        raise FlowError("结果未知的作品要附上本次核查的后台截图（--evidence）才能改状态")
    target = {"under_review": "UNDER_REVIEW", "published": "PUBLISHED", "rejected": "REJECTED",
              "submitted": "SUBMITTED", "unknown": "UNKNOWN", "not_submitted": "NOT_STARTED"}[a.result]
    if target == "SUBMITTED" and (prev not in ("SUBMITTING", "SUBMITTED", "UNKNOWN") or not ev):
        raise FlowError("已提交待核查必须附本次受理证据，不能凭编辑页截图推断受理")
    if target == "NOT_STARTED" and (prev in ("SUBMITTED", "UNDER_REVIEW") or
            any("→SUBMITTED" in e.get("note", "") or "→UNDER_REVIEW" in e.get("note", "") for e in ps.get("log", []))):
        raise FlowError("已有受理/审核记录，不能回退未提交解锁重投")
    if target in ("UNDER_REVIEW", "PUBLISHED", "REJECTED") and not (ev or ps.get("evidence")):
        raise FlowError("需要至少一张后台列表/公开页截图（--evidence）")
    if prev in ("UNKNOWN", "SUBMITTING") and a.result == "not_submitted" and not ev:
        raise FlowError("恢复未提交必须附本次原表单/原结果核查截图，超时不是未受理证据")
    if prev == "PUBLISHED" and target != "PUBLISHED":
        raise FlowError("已发布的作品不能回退状态")
    if a.published_at and target != "PUBLISHED":
        raise FlowError("只有已发布结果可登记发布时间")
    stamp = iso(now())
    pub = iso(parse_time(a.published_at)) if a.published_at else ps.get("published_at")
    if pub and a.published_at and ps.get("published_at") and pub != ps["published_at"] and ps.get("metrics"):
        raise FlowError("发布时间变化会影响已采数据窗口，先审计纠错，不能直接覆盖")
    ps["state"] = target
    ps["verified_at"] = stamp
    ps["evidence"] = list(dict.fromkeys((ps.get("evidence") or []) + ev))
    if a.post_id:
        ps["post_id"] = a.post_id
    if a.url:
        ps["public_url"] = a.url
    if target == "PUBLISHED":
        ps["published_at"] = pub  # 不以核验时间兜底
        if a.published_at:
            if ps.get("published_time"):
                ps.setdefault("published_time_history", []).append(ps["published_time"])
            ps["published_time"] = {"value": pub, "source": a.time_source, "precision": a.time_precision,
                                    "utc_offset": parse_time(pub).strftime("%z"), "timezone_basis": a.timezone_basis,
                                    "evidence": ev or ps["evidence"], "verified_at": stamp}
        if pub:
            ps["windows"] = _due_windows(pub)
        else:
            ps.pop("windows", None)
        ps["checkpoint"] = "BACKEND_VERIFIED"
        blocker = ps.pop("blocker", None)
        if blocker:
            ps.setdefault("log", []).append({"at": stamp, "note": "已解决旧阻塞：" + str(blocker)})
    elif target == "NOT_STARTED":
        ps.pop("checkpoint", None)
    ps.setdefault("log", []).append({"at": stamp, "note": f"{prev}→{target}" + (f"；{a.note}" if a.note else "")})
    save_status(day, s)
    res = {"ok": True, "platform": a.platform, "state": target, "post_id": ps.get("post_id"),
           "published_at": ps.get("published_at"), "verified_at": stamp, "next": _resume_action(ps)}
    if target == "PUBLISHED" and not pub:
        res["warning"] = "后台已发布，但发布时间未知；不建立精确数据窗口，后续凭可靠时间证据补记"
    elif target == "PUBLISHED" and not ps.get("post_id"):
        res["warning"] = "没有作品ID：按已核定发布时间建窗口；取得公开链接后可补记，不等于已公开验证"
    if target == "UNKNOWN":
        res["remind"] = "结果未知：之后只能核查原作品，不可重新投稿"
    return res


# ----------------------------------------------------------------------------- 数据窗口
def _window_rows(at: datetime) -> List[Dict[str, Any]]:
    rows = []
    for d in all_day_dirs():
        if not (d / "publish_status.json").exists():
            continue
        try:
            s = load_status(d)
        except Exception:  # noqa: BLE001
            continue
        for p, ps in s.get("platforms", {}).items():
            if ps.get("state") != "PUBLISHED" or not ps.get("published_at"):
                continue
            wins = ps.get("windows") or _due_windows(ps["published_at"])
            for w, t in wins.items():
                cell = (ps.get("metrics") or {}).get(w)
                got = bool(cell) and cell.get("validity", "VALID") == "VALID"
                due, close = parse_time(t["due_at"]), parse_time(t["close_at"])
                state = "collected" if got else ("not_yet" if at < due else ("due" if at <= close else "missed"))
                rows.append({"day": d.name, "episode_id": s.get("episode_id"), "platform": p, "window": w,
                             "state": state, "due_at": t["due_at"], "close_at": t["close_at"]})
    return rows


def cmd_metrics_due(a) -> Dict[str, Any]:
    rows = _window_rows(now())
    due = [r for r in rows if r["state"] == "due"]
    nxt = sorted((r for r in rows if r["state"] == "not_yet"), key=lambda r: r["due_at"])[:5]
    return {"ok": True, "due": due, "next_up": nxt,
            "summary": {k: sum(1 for r in rows if r["state"] == k) for k in ("due", "not_yet", "collected", "missed")}}


def cmd_metric_add(a) -> Dict[str, Any]:
    day = day_path(a.day_dir)
    s = load_status(day)
    ps = s["platforms"].get(a.platform)
    if not ps or ps.get("state") != "PUBLISHED":
        raise FlowError("只能给已发布的作品记数据")
    if not ps.get("published_at"):
        raise FlowError("发布时间未知，不能采精确窗口数据；先凭可靠时间证据补记")
    wins = ps.get("windows") or _due_windows(ps["published_at"])
    t = wins[a.window]
    at = now()
    if at < parse_time(t["due_at"]):
        raise FlowError(f"{a.window} 窗口 {t['due_at']} 才到期，不能提前采集")
    old_cell = (ps.get("metrics") or {}).get(a.window)
    archived = None
    if old_cell:
        if old_cell.get("validity", "VALID") == "VALID":
            raise FlowError(f"{a.window} 已采集过，不覆盖")
        # 非 VALID（纠错排除）原记录先归档，到期后才允许补一条 VALID 新证据，旧值/截图/纠错链不抹
        archived = dict(old_cell)
    ep = day / a.evidence
    if not ep.is_file():
        raise FlowError(f"数据截图不存在: {a.evidence}")
    late = at > parse_time(t["close_at"])
    cells = {}
    for m in METRICS:
        v = getattr(a, m)
        cells[m] = ({"status": "NORMAL", "value": v, "reason": ""} if v is not None
                    else {"status": "NOT_PROVIDED", "value": None, "reason": "平台未展示或未采到"})
    if all(c["value"] is None for c in cells.values()):
        raise FlowError("至少要有一项真实数值；全都看不到就别记，不得补零")
    new_cell = {"collected_at": iso(at), "late": late, "evidence": a.evidence,
                "validity": "VALID", "cells": cells}
    if archived:
        new_cell["archived_excluded"] = [archived]
    ps.setdefault("metrics", {})[a.window] = new_cell
    save_status(day, s)
    pack = read_json(day / "publish_pack.json", default={})
    pc = (pack.get("platforms") or {}).get(a.platform, {})
    work_id = ps.get("post_id") or f"{s['episode_id']}:{a.platform}"
    obs = {"object_type": "performance_observation", "schema_version": "1",
           "series": read_json(POLICY_PATH).get("series"), "snapshot_id": f"flow-{s['episode_id']}-{a.window}",
           "platform": a.platform, "work_id": work_id, "episode": s["episode_id"],
           "published_at": ps["published_at"], "collected_at": iso(at), "collection_status": "SUCCESS",
           "validity": "VALID",
           "title": pc.get("title"), "hook": pack.get("hook"), "features": pack.get("features") or {},
           "duration_sec": (pack.get("master") or {}).get("duration_seconds"),
           "evidence": {"path": rel(ep), "sha256": sha256_file(ep), "observed_view": a.window},
           "metric_cells": cells}
    # 仅 VALID 旧观测构成重复；非 VALID（纠错排除）旧记录不吞掉新的 VALID 观测，也不删除旧行
    dup = any(r.get("work_id") == work_id and r.get("snapshot_id") == obs["snapshot_id"]
              and r.get("validity", "VALID") == "VALID" for r in read_jsonl(HISTORY_PATH))
    if not dup:
        append_jsonl(HISTORY_PATH, obs)
    return {"ok": True, "platform": a.platform, "window": a.window, "late": late, "history_appended": not dup}


# ----------------------------------------------------------------------------- 状态
# ----------------------------------------------------------------------------- 烂尾提醒
def _stale_review_rows(at: datetime, max_hours: float = 48.0) -> List[Dict[str, Any]]:
    """UNDER_REVIEW 超 max_hours 未复查（时间锚点：状态日志最后一条的 at；拿不到则跳过，不猜）。"""
    rows = []
    for d in all_day_dirs():
        if not (d / "publish_status.json").exists():
            continue
        try:
            s = load_status(d)
        except Exception:  # noqa: BLE001
            continue
        for p, ps in s.get("platforms", {}).items():
            if ps.get("state") != "UNDER_REVIEW":
                continue
            logs = ps.get("log") or []
            last = None
            for e in reversed(logs):
                if isinstance(e, dict) and e.get("at"):
                    try:
                        last = parse_time(e["at"])
                        break
                    except Exception:  # noqa: BLE001
                        pass
            age_h = round((at - last).total_seconds() / 3600, 1) if last else None
            if age_h is not None and age_h > max_hours:
                rows.append({"day": d.name, "episode_id": s.get("episode_id"), "platform": p,
                             "state": "UNDER_REVIEW", "age_hours": age_h,
                             "action": "到该平台后台复查原作品（已发布则 done published；被驳回/仍在审据实记），不重新投稿"})
    return rows


def _pending_plan_rows(at: datetime, max_days: float = 7.0) -> List[Dict[str, Any]]:
    """
    已写好确认①方案（*-plan-approved.json）但超过 max_days 仍没有任何平台 PUBLISHED：
    方案在等用户拍板，提醒别烂尾。无法读取时间时用文件修改时间。
    """
    rows = []
    for d in all_day_dirs():
        has_pub = (d / "publish_status.json").exists()
        already_published = False
        if has_pub:
            try:
                st = load_status(d)
                already_published = any(ps.get("state") == "PUBLISHED"
                                        for ps in (st.get("platforms") or {}).values())
            except Exception:  # noqa: BLE001
                pass
        for plan in sorted(d.glob("*-plan-approved.json")):
            if already_published:
                continue
            key = ""
            try:
                key = str((read_json(plan) or {}).get("candidate_id") or "")
            except Exception:  # noqa: BLE001
                pass
            age_d = (at - datetime.fromtimestamp(plan.stat().st_mtime, tz=at.tzinfo)).total_seconds() / 86400
            if age_d > max_days:
                rows.append({"day": d.name, "candidate_id": key or plan.stem,
                             "plan": rel(plan), "age_days": round(age_d, 1),
                             "action": "确认①方案长期未批准：今天做就给用户确认①，不做就在项目记录里显式搁置，别一直挂着"})
    return rows


def cmd_status(a) -> Dict[str, Any]:
    days = [day_path(a.day_dir)] if a.day_dir else [d for d in all_day_dirs() if (d / "publish_status.json").exists()]
    out = {}
    for d in days:
        s = load_status(d)
        pack = read_json(d / "publish_pack.json", default={})
        approved = _approval_valid(d, pack, s) if pack else False
        out[d.name] = {"episode_id": s.get("episode_id"), "release_approved": approved,
                       "next": _next_release(s, approved),
                       "platforms": {p: {k: v.get(k) for k in ("state", "post_id", "published_at")}
                                     for p, v in s.get("platforms", {}).items()},
                       "resume_actions": {p: _resume_action(v) for p, v in s.get("platforms", {}).items()}}
    due = [r for r in _window_rows(now()) if r["state"] == "due"]
    if a.day_dir:
        reminders = []
    else:
        reminders = [*_stale_review_rows(now()), *_pending_plan_rows(now())]
    return {"ok": True, "episodes": out, "metrics_due": due, "followups": reminders,
            "followup_count": len(reminders)}


# ----------------------------------------------------------------------------- 选题
def _done_subjects() -> Dict[str, List[str]]:
    """已做过的期次 slug（来自各期 publish_status 的 episode_id），用于题库去重。"""
    slugs = set()
    for d in all_day_dirs():
        if (d / "publish_status.json").exists():
            try:
                eid = load_status(d).get("episode_id") or ""
            except Exception:  # noqa: BLE001
                eid = ""
            if eid.startswith("ep") and "-" in eid:
                slugs.add(eid.split("-", 1)[1])
    return {"slugs": sorted(slugs)}


def _already_done(slug: str, done: set) -> bool:
    """同主体且 slug 关键词重合 ≥2 个即视为做过（如 ravel-left-hand-concerto ≈ ravel-concerto-left-hand-*）。"""
    words = set(slug.split("-"))
    for d in done:
        dw = set(d.split("-"))
        if slug == d or (slug.split("-")[0] == d.split("-")[0] and len(words & dw) >= 3):
            return True
    return False


def _history_scores() -> Dict[str, Dict[str, Any]]:
    """每个主体（作曲家）的同平台分位均值：只做端内比较，不跨平台加总。"""
    rows = [r for r in read_jsonl(HISTORY_PATH) if r.get("object_type") == "performance_observation"
            and r.get("validity", "VALID") == "VALID"]
    latest: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        k = (r["platform"], r["work_id"])
        if k not in latest or r["collected_at"] > latest[k]["collected_at"]:
            latest[k] = r
    by_plat: Dict[str, List[tuple]] = {}
    for (p, _), r in latest.items():
        subj = (r.get("features") or {}).get("composer")
        plays = ((r.get("metric_cells") or {}).get("plays") or {}).get("value")
        if subj and plays is not None:
            by_plat.setdefault(p, []).append((subj, float(plays)))
    acc: Dict[str, List[float]] = {}
    for p, items in by_plat.items():
        if len(items) < 2:
            continue
        vals = sorted(v for _, v in items)
        for subj, v in items:
            pct = sum(1 for x in vals if x < v) / (len(vals) - 1)
            acc.setdefault(subj, []).append(pct)
    return {s: {"score": round(sum(v) / len(v), 3), "n": len(v)} for s, v in acc.items()}


def cmd_topics(a) -> Dict[str, Any]:
    tb = read_json(ROOT / "topic_bank.json", default={"topics": {}})
    bank = tb.get("topics", {})
    rejected = set(tb.get("rejected", []))
    done = set(_done_subjects()["slugs"])
    hist = _history_scores()
    out = []
    for slug, t in bank.items():
        if slug in rejected or _already_done(slug, done):
            continue
        subj = t.get("composer")
        ed = (t.get("_evidence") or {}).get("editorial_score")
        h = hist.get(subj)
        enough = bool(h and h["n"] >= 3)
        score = None
        if ed:
            score = round(0.65 * (float(ed) / 50) + 0.35 * h["score"], 3) if enough else round(float(ed) / 50, 3)
        out.append({"slug": slug, "subject": subj, "title": t.get("title"), "hook": t.get("hook_2s"),
                    "facts": len(t.get("facts") or []), "editorial_score": ed,
                    "history": h, "score": score, "history_used": enough})
    out.sort(key=lambda x: (x["score"] is None, -(x["score"] or 0)))
    return {"ok": True, "done_episodes": sorted(done), "subject_history": hist, "candidates": out,
            "remind": "这是题库参考，不是结论：每期仍需当日联网找新题，按 制作要点.md 挑最抓马、主角大众熟知的一个，在确认①交用户"}


# ----------------------------------------------------------------------------- init
def cmd_init(a) -> Dict[str, Any]:
    made = []
    if not POLICY_PATH.exists():
        write_json(POLICY_PATH, {"schema_version": "2", "series": a.series or "",
                                 "video_generation": {"required_model": a.model or "", "fallback_allowed": False,
                                                      "duration_seconds": {"minimum": 5, "maximum": a.max_seconds, "preferred": a.max_seconds},
                                                      "single_shot_seconds": a.max_seconds, "video_count": 1, "aspect_ratio": a.aspect,
                                                      "media_validation": {"duration_tolerance_seconds": 0.6}}})
        made.append(POLICY_PATH.name)
    if not RULES_PATH.exists():
        write_json(RULES_PATH, {"schema_version": "2", "series": a.series or "", "account_display_name": a.account or "",
                                "global": {"forbidden_text_fragments": ["<待回填>", "待填写", "TODO", "占位符"]},
                                "platforms": {p: {"display_name": p, "original_declaration_target": NOT_SUPPORTED,
                                                  "ai_disclosure_target": ""} for p in PLATFORMS}})
        made.append(RULES_PATH.name)
    WORKSPACE.mkdir(exist_ok=True)
    return {"ok": True, "created": made,
            "remind": "首次发布时在各平台发布页现场确认原创声明/AI标识选项，把支持的选项原文写进 platform_rules.json"}


# ----------------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="flow.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=VERSION)
    sp = ap.add_subparsers(dest="cmd", required=True)

    p = sp.add_parser("init"); p.add_argument("--series"); p.add_argument("--model"); p.add_argument("--account")
    p.add_argument("--max-seconds", type=int, default=29); p.add_argument("--aspect", default="9:16"); p.set_defaults(func=cmd_init)

    p = sp.add_parser("gen-check"); p.add_argument("--plan", required=True)
    p.add_argument("--regen-approved", default="", help="已生成过仍要重生时，用户同意重生的原话")
    p.add_argument("--exception-approved", default="", help="仅本轮正式封闭单选授权：对冻结 unknown 开一次性新 attempt 的原话")
    p.add_argument("--exception-evidence", default="", help="本轮授权证据文件（确认原文/来源/时间），非空且绑定 sha256")
    p.set_defaults(func=cmd_gen_check)

    p = sp.add_parser("gen-start"); p.add_argument("--plan", required=True)
    p.add_argument("--regen-approved", default="", help="仅原请求终态已核实后使用的重生批准原话")
    p.add_argument("--exception-approved", default="", help="仅本轮正式封闭单选授权：对冻结 unknown 开一次性新 attempt 的原话")
    p.add_argument("--exception-evidence", default="", help="本轮授权证据文件（确认原文/来源/时间），非空且绑定 sha256")
    p.set_defaults(func=cmd_gen_start)

    p = sp.add_parser("gen-log"); p.add_argument("--plan", required=True)
    p.add_argument("--status", required=True, choices=["submitted", "succeeded", "failed", "unknown"])
    p.add_argument("--url", default=""); p.add_argument("--note", default=""); p.add_argument("--regen-approved", default="")
    p.add_argument("--evidence", default="", help="实际生成回执/核查证据文件（相对项目根或绝对路径），终态必填")
    p.set_defaults(func=cmd_gen_log)

    p = sp.add_parser("master-check"); p.add_argument("--master", required=True); p.add_argument("--duration", type=float)
    p.add_argument("--min-mean-db", type=float, default=-24.0, help="ebur128 不可用时的均值响度兜底下限，默认 -24dB")
    p.add_argument("--min-lufs", type=float, default=-18.0, help="EBU R128 integrated 响度下限，默认 -18 LUFS")
    p.add_argument("--max-tp", type=float, default=-1.0, help="真峰值上限，默认 -1 dBTP")
    p.add_argument("--timeline", default="", help="字幕/旁白时间轴JSON；仅统计句界切点接近度，不证明语义对齐")
    p.add_argument("--action-ranges", default="", help="完整动作区间JSON列表[{start,end}]，内部旁白句界不要求切镜")
    p.add_argument("--align-tolerance", type=float, default=1.2, help="旁白起点与镜头切点的对齐容差（秒）")
    p.add_argument("--scene-threshold", type=float, default=0.25, help="ffmpeg scene 检测阈值，默认 0.25")
    p.set_defaults(func=cmd_master_check)

    p = sp.add_parser("raw-check", description="原片规格+镜头切点+抽帧拼图；拼图需人工看伪品牌/伪文字")
    p.add_argument("--raw", required=True); p.add_argument("--day-dir", default="", help="证据写到该日目录的 _raw_check/ 下")
    p.add_argument("--frames", type=int, default=12, help="均匀抽帧数，默认 12")
    p.add_argument("--thumb-width", type=int, default=240, help="拼图单帧宽，默认 240")
    p.add_argument("--threshold", type=float, default=0.25, help="ffmpeg scene 检测阈值")
    p.set_defaults(func=cmd_raw_check)

    p = sp.add_parser("pack-check"); p.add_argument("--day-dir", required=True)
    p.add_argument("--approved", default="", help="用户看过当前成片及文案后同意发布的原话")
    p.add_argument("--approval-kind", choices=["publication", "revision"], default="publication", help="修改方向认可用revision，不授予外发")
    p.add_argument("--platforms", nargs="+", choices=PLATFORMS, help="本次明确批准的平台范围；省略=发布包全部平台")
    p.set_defaults(func=cmd_pack_check)

    p = sp.add_parser("checkpoint"); p.add_argument("--day-dir", required=True)
    p.add_argument("--platform", choices=PLATFORMS, required=True)
    p.add_argument("--stage", choices=["prepared", "waiting_auth", "ready"], required=True)
    p.add_argument("--evidence", nargs="+", required=True); p.add_argument("--editor-url", default="")
    p.add_argument("--note", default=""); p.set_defaults(func=cmd_checkpoint)
    p = sp.add_parser("resume"); p.add_argument("--day-dir", required=True)
    p.add_argument("--platform", choices=PLATFORMS); p.set_defaults(func=cmd_resume)

    p = sp.add_parser("start"); p.add_argument("--day-dir", required=True); p.add_argument("--platform", choices=PLATFORMS, required=True)
    p.set_defaults(func=cmd_start)

    p = sp.add_parser("done"); p.add_argument("--day-dir", required=True); p.add_argument("--platform", choices=PLATFORMS, required=True)
    p.add_argument("--result", required=True, choices=["under_review", "published", "rejected", "submitted", "unknown", "not_submitted"])
    p.add_argument("--evidence", nargs="*", default=[], help="当日目录下的截图文件名，可多个")
    p.add_argument("--post-id", default=""); p.add_argument("--url", default="")
    p.add_argument("--published-at", default="", help="已核定平台发布时间（带时区）；省略保留旧值，没有旧值就未知，不用现在兜底")
    p.add_argument("--time-source", default="platform_backend", choices=["platform_backend", "public_page", "verified_receipt"])
    p.add_argument("--time-precision", default="unknown", choices=["second", "minute", "unknown"])
    p.add_argument("--timezone-basis", default="caller_verified", help="时区核定依据；不猜测平台/设备时区")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_done)

    p = sp.add_parser("status"); p.add_argument("--day-dir", default=""); p.set_defaults(func=cmd_status)
    p = sp.add_parser("metrics-due"); p.set_defaults(func=cmd_metrics_due)
    p = sp.add_parser("topics"); p.set_defaults(func=cmd_topics)

    p = sp.add_parser("metric-add"); p.add_argument("--day-dir", required=True); p.add_argument("--platform", choices=PLATFORMS, required=True)
    p.add_argument("--window", choices=list(WINDOWS), required=True); p.add_argument("--evidence", required=True)
    for m in METRICS:
        p.add_argument("--" + m.replace("_", "-"), dest=m, type=float, default=None)
    p.set_defaults(func=cmd_metric_add)
    return ap


READONLY_CMDS = {"status", "resume", "metrics-due", "topics", "gen-check", "master-check", "raw-check"}


def _lock():
    """写操作串行：同一项目同时只允许一个 flow.py 写状态（单机 flock，进程退出自动释放）。"""
    import fcntl
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    fh = open(WORKSPACE / ".flow.lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as e:
        raise FlowError("另一个 flow.py 正在写入，稍后再试") from e
    return fh


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    lock = None
    try:
        if args.cmd not in READONLY_CMDS:
            lock = _lock()
        res = args.func(args)
        code = 0
    except FlowError as e:
        res, code = {"ok": False, "error": str(e)}, 2
    except subprocess.CalledProcessError as e:
        res, code = {"ok": False, "error": f"外部命令失败: {e}"}, 2
    except FileNotFoundError as e:
        res, code = {"ok": False, "error": f"找不到文件或命令（ffmpeg/ffprobe 是否已安装？）: {e}"}, 2
    except json.JSONDecodeError as e:
        res, code = {"ok": False, "error": f"JSON 文件格式损坏，请人工检查: {e}"}, 2
    except KeyError as e:
        res, code = {"ok": False, "error": f"数据缺少字段 {e}（文件可能被手改过）"}, 2
    finally:
        if lock:
            lock.close()
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
