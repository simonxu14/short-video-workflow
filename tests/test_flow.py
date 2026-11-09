#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""flow.py（v2 轻量守卫）回归测试：只测真风险拦截 + 正常路径不被误拦。"""
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent / "scripts" if (HERE.parent / "scripts" / "flow.py").is_file() else HERE
sys.path.insert(0, str(ROOT))
import flow as F  # noqa: E402

pytestmark = pytest.mark.flow


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.setattr(F, "ROOT", tmp_path)
    monkeypatch.setattr(F, "WORKSPACE", tmp_path / "workspace")
    monkeypatch.setattr(F, "POLICY_PATH", tmp_path / "video_production_policy.json")
    monkeypatch.setattr(F, "RULES_PATH", tmp_path / "platform_rules.json")
    monkeypatch.setattr(F, "GEN_LEDGER", tmp_path / "workspace" / "production_ledger.jsonl")
    monkeypatch.setattr(F, "HISTORY_PATH", tmp_path / "performance_history.jsonl")
    F.main(["init", "--series", "测试系列", "--model", "seedance_2.5", "--account", "测试号"])
    rules = json.loads((tmp_path / "platform_rules.json").read_text())
    rules["platforms"]["xiaohongshu"]["original_declaration_target"] = "已启用原创声明"
    (tmp_path / "platform_rules.json").write_text(json.dumps(rules, ensure_ascii=False))
    return tmp_path


def run(*argv):
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = F.main(list(argv))
    return code, json.loads(buf.getvalue())


def plan(p: Path, **kw):
    d = {"candidate_id": "cand-x", "prompt": "画面", "user_approval": {"quote": "可以，生成吧"},
         "locked_parameters": {"model_version": "seedance_2.5", "fallback_allowed": False,
                               "target_duration_seconds": 29, "aspect_ratio": "9:16",
                               "video_count": 1, "segments_seconds": [29]}}
    for k, v in kw.items():
        if k in d["locked_parameters"]:
            d["locked_parameters"][k] = v
        else:
            d[k] = v
    p.write_text(json.dumps(d, ensure_ascii=False))
    return str(p)


# ---------------------------------------------------------------- 生成
def test_gen_check_ok_then_blocks_repeat_and_unknown(proj):
    pp = plan(proj / "plan.json")
    assert run("gen-check", "--plan", pp)[0] == 0
    run("gen-log", "--plan", pp, "--status", "unknown")
    code, r = run("gen-check", "--plan", pp)
    assert code == 2 and "结果未知" in r["error"]
    receipt = proj / "receipt.json"
    receipt.write_text(json.dumps({"status": "succeeded", "url": "https://example.test/video.mp4"}), encoding="utf-8")
    assert run("gen-log", "--plan", pp, "--status", "succeeded", "--evidence", str(receipt),
               "--url", "https://example.test/video.mp4")[0] == 0
    code, r = run("gen-check", "--plan", pp)
    assert code == 2 and "重生" in r["error"]
    assert run("gen-check", "--plan", pp, "--regen-approved", "重做一版")[0] == 0


@pytest.mark.parametrize("kw,needle", [
    ({"model_version": "seedance_2.0"}, "禁止降级"),
    ({"fallback_allowed": True}, "fallback"),
    ({"segments_seconds": [15, 14]}, "单条"),
    ({"aspect_ratio": "16:9"}, "画幅"),
    ({"user_approval": {}}, "原话"),
])
def test_gen_check_real_risks(proj, kw, needle):
    code, r = run("gen-check", "--plan", plan(proj / "p.json", **kw))
    assert code == 2 and needle in r["error"]


# ---------------------------------------------------------------- 生成等待/回执留痕
@pytest.mark.parametrize("status", ["submitting", "submitted", "unknown"])
def test_inflight_cannot_bypass_with_regen_approval(proj, status):
    pp = plan(proj / "p.json")
    if status == "submitting":
        assert run("gen-start", "--plan", pp)[0] == 0
    else:
        assert run("gen-log", "--plan", pp, "--status", status)[0] == 0
    before = F.GEN_LEDGER.read_bytes()
    for cmd in ("gen-check", "gen-start"):
        code, result = run(cmd, "--plan", pp, "--regen-approved", "重做一版")
        assert code == 2 and "重生" in result["error"]
    assert F.GEN_LEDGER.read_bytes() == before


# ---------------------------------------------------------------- 一次性正式授权例外
def _exc_evidence(proj, quote="按要求生成"):
    p = proj / "exception-authorization.json"
    p.write_text(json.dumps({
        "question": "是否接受可能重复消耗额度，授权本次例外：在原生成结果仍未知的情况下，按已确认方案新生成一次？",
        "selected_option": "按要求生成",
        "source": "本轮正式工具封闭单选确认（MainAgent 传达）",
        "authorization_quote": quote,
    }, ensure_ascii=False), encoding="utf-8")
    return p


def test_exception_once_allows_new_attempt_keeps_old_unknown(proj):
    pp = plan(proj / "p.json")
    assert run("gen-log", "--plan", pp, "--status", "unknown")[0] == 0
    old_first = F.GEN_LEDGER.read_bytes().splitlines()[0]
    ev = _exc_evidence(proj)
    # 无例外标志：unknown 仍阻断，regen-approved 也不能越过
    assert run("gen-check", "--plan", pp, "--regen-approved", "重做")[0] == 2
    assert run("gen-start", "--plan", pp, "--exception-approved", "按要求生成")[0] == 2  # 缺证据
    assert run("gen-start", "--plan", pp, "--exception-evidence", str(ev))[0] == 2  # 缺原话
    code, r = run("gen-check", "--plan", pp, "--exception-approved", "按要求生成", "--exception-evidence", str(ev))
    assert code == 0 and r["exception"]["evidence"]["sha256"] == F.sha256_file(ev)
    code, r = run("gen-start", "--plan", pp, "--exception-approved", "按要求生成", "--exception-evidence", str(ev))
    assert code == 0 and r["logged"]["attempt_id"] == "attempt-2"
    # 旧 unknown 行字节未改
    assert F.GEN_LEDGER.read_bytes().splitlines()[0] == old_first
    # 新 attempt 记成功回执；同一旧 unknown 的第二次例外被重复消费阻断
    receipt = proj / "receipt.json"
    receipt.write_text(json.dumps({"status": "succeeded", "url": "https://example.test/v.mp4"}), encoding="utf-8")
    assert run("gen-log", "--plan", pp, "--status", "succeeded", "--evidence", str(receipt),
               "--url", "https://example.test/v.mp4")[0] == 0
    last = F.read_jsonl(F.GEN_LEDGER)[-1]
    assert last["attempt_id"] == "attempt-2" and last["status"] == "succeeded"
    code, r = run("gen-start", "--plan", pp, "--exception-approved", "按要求生成", "--exception-evidence", str(ev))
    assert code == 2 and "重复消耗" in r["error"]


def test_exception_blocks_missing_or_empty_evidence_and_non_unknown(proj):
    pp = plan(proj / "p.json")
    assert run("gen-log", "--plan", pp, "--status", "unknown")[0] == 0
    ev = _exc_evidence(proj)
    code, r = run("gen-check", "--plan", pp, "--exception-approved", "按要求生成", "--exception-evidence", "missing.json")
    assert code == 2 and "证据" in r["error"]
    (proj / "empty.json").write_text("", encoding="utf-8")
    assert run("gen-check", "--plan", pp, "--exception-approved", "按要求生成",
               "--exception-evidence", str(proj / "empty.json"))[0] == 2
    # 末态不是 unknown：例外路径不适用，按常规规则走
    pp2 = plan(proj / "p2.json", candidate_id="cand-y")
    assert run("gen-check", "--plan", pp2, "--exception-approved", "按要求生成", "--exception-evidence", str(ev))[0] == 0


def test_gen_start_validates_before_reserving(proj):
    pp = plan(proj / "p.json", user_approval={})
    assert run("gen-start", "--plan", pp)[0] == 2
    assert not F.GEN_LEDGER.exists()
    pp = plan(proj / "p.json")
    code, result = run("gen-start", "--plan", pp)
    assert code == 0 and result["logged"]["status"] == "submitting"
    assert result["logged"]["plan_sha256"] == F.sha256_file(Path(pp))
    assert "不代表服务端" in result["logged"]["note"]


def test_gen_start_two_processes_allow_only_one_reservation(proj):
    import os
    pp = plan(proj / "p.json")
    cmd = [sys.executable, "-B", str(Path(F.__file__).resolve()), "gen-start", "--plan", pp]
    env = dict(os.environ, FLOW_ROOT=str(proj))
    workers = [subprocess.Popen(cmd, cwd=proj, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
               for _ in range(2)]
    outputs = [w.communicate(timeout=15) for w in workers]
    assert sorted(w.returncode for w in workers) == [0, 2]
    assert all(json.loads(stdout)["ok"] == (w.returncode == 0)
               for w, (stdout, _) in zip(workers, outputs))
    assert len(F.read_jsonl(F.GEN_LEDGER)) == 1
    # 新进程也不能凭重做批准覆盖未结束占位。
    retry = subprocess.run(cmd + ["--regen-approved", "重做"], cwd=proj, env=env,
                           capture_output=True, text=True, timeout=15)
    assert retry.returncode == 2 and "等待结果" in json.loads(retry.stdout)["error"]


@pytest.mark.parametrize("status", ["failed", "succeeded"])
def test_gen_terminal_requires_nonempty_evidence(proj, status):
    pp = plan(proj / "p.json")
    assert run("gen-log", "--plan", pp, "--status", "unknown")[0] == 0
    before = F.GEN_LEDGER.read_bytes()
    args = ["gen-log", "--plan", pp, "--status", status, "--url", "https://example.test/video.mp4"]
    assert run(*args)[0] == 2
    assert run(*args, "--evidence", "missing.txt")[0] == 2
    (proj / "empty.txt").write_text("", encoding="utf-8")
    assert run(*args, "--evidence", "empty.txt")[0] == 2
    assert F.GEN_LEDGER.read_bytes() == before


def test_gen_receipt_success_and_failure_recovery(proj):
    pp = plan(proj / "p.json")
    assert run("gen-start", "--plan", pp)[0] == 0
    assert run("gen-log", "--plan", pp, "--status", "unknown")[0] == 0
    assert run("gen-log", "--plan", pp, "--status", "submitted")[0] == 2
    receipt = proj / "receipt.json"
    receipt.write_text(json.dumps({"status": "failed", "reason": "fixture: not accepted"}), encoding="utf-8")
    code, result = run("gen-log", "--plan", pp, "--status", "failed", "--evidence", "receipt.json")
    assert code == 0 and result["logged"]["evidence"] == {"path": "receipt.json", "sha256": F.sha256_file(receipt)}
    assert run("gen-start", "--plan", pp)[0] == 0
    receipt.write_text(json.dumps({"status": "succeeded", "url": "https://example.test/video.mp4"}), encoding="utf-8")
    before = F.GEN_LEDGER.read_bytes()
    assert run("gen-log", "--plan", pp, "--status", "succeeded", "--evidence", "receipt.json")[0] == 2
    assert F.GEN_LEDGER.read_bytes() == before
    assert run("gen-log", "--plan", pp, "--status", "succeeded", "--evidence", "receipt.json",
               "--url", "https://example.test/video.mp4")[0] == 0
    assert run("gen-start", "--plan", pp)[0] == 2
    assert run("gen-start", "--plan", pp, "--regen-approved", "重做一版")[0] == 0


# ---------------------------------------------------------------- 成片
def _make_video(path: Path, seconds=29, w=720, h=1280, vol="2.0"):
    # vol=2.0：内容响度约 -16 LUFS，能过 -18 LUFS 下限；测试需要安静片时显式传 0.005
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:d={seconds}:r=24",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-af", f"volume={vol}",
                    "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)


ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="需要 ffmpeg")


@ffmpeg
def test_master_check_ok_and_quiet_and_short(proj):
    ok = proj / "ok.mp4"; _make_video(ok)
    code, r = run("master-check", "--master", str(ok))
    assert code == 0 and r["sha256"].startswith("sha256:")
    short = proj / "short.mp4"; _make_video(short, seconds=26)
    assert run("master-check", "--master", str(short))[0] == 2
    quiet = proj / "quiet.mp4"; _make_video(quiet, vol="0.005")
    code, r = run("master-check", "--master", str(quiet))
    assert code == 2 and "响度" in r["error"]


# ---------------------------------------------------------------- 发布
def _day(proj, platforms=("douyin", "kuaishou", "xiaohongshu")):
    day = proj / "workspace" / "2026-11-01"
    day.mkdir(parents=True)
    m = day / "m.mp4"; m.write_bytes(b"fake-master")
    (day / "c.jpg").write_bytes(b"cover")
    plats = {}
    for p in platforms:
        plats[p] = {"title": "贝多芬拒弹", "description": "简介", "topics": ["#贝多芬"],
                    "ai_disclosure": "含AI生成内容", "original_declaration": "NOT_SUPPORTED"}
    if "xiaohongshu" in plats:
        plats["xiaohongshu"]["original_declaration"] = "已启用原创声明"
    pack = {"episode_id": "ep20261101-x", "master": {"path": "m.mp4", "sha256": F.sha256_file(m),
                                                    "duration_seconds": 29, "cover_path": "c.jpg"},
            "platforms": plats}
    (day / "publish_pack.json").write_text(json.dumps(pack, ensure_ascii=False))
    (day / "shot.png").write_bytes(b"png")
    return day, pack


def _write_pack(day, pack):
    (day / "publish_pack.json").write_text(json.dumps(pack, ensure_ascii=False))


@pytest.mark.parametrize("mutate,needle", [
    (lambda pk: pk["platforms"]["xiaohongshu"].update(original_declaration="NOT_SUPPORTED"), "原创声明"),
    (lambda pk: pk["platforms"]["douyin"].update(ai_disclosure=""), "AI 标识"),
    (lambda pk: pk["platforms"]["xiaohongshu"].update(title="一" * 21), "上限 20"),
    (lambda pk: pk["platforms"]["kuaishou"].update(topics=["#a", "#b", "#c", "#d", "#e"]), "上限 4"),
    (lambda pk: pk["platforms"]["douyin"].update(description="TODO"), "占位"),
    (lambda pk: pk["master"].update(sha256="sha256:" + "0" * 64), "sha256"),
])
def test_pack_check_blocks(proj, mutate, needle):
    day, pack = _day(proj)
    mutate(pack); _write_pack(day, pack)
    code, r = run("pack-check", "--day-dir", str(day))
    assert code == 2 and needle in r["error"]


def test_publish_happy_path_and_windows(proj):
    day, _ = _day(proj)
    code, r = run("pack-check", "--day-dir", str(day))
    assert code == 0 and r["release_approved"] is False
    code, r = run("start", "--day-dir", str(day), "--platform", "douyin")
    assert code == 2 and "批准" in r["error"]
    assert run("pack-check", "--day-dir", str(day), "--approved", "可以，全平台发布")[0] == 0
    assert run("start", "--day-dir", str(day), "--platform", "douyin")[0] == 0
    code, r = run("done", "--day-dir", str(day), "--platform", "douyin", "--result", "under_review")
    assert code == 2 and "截图" in r["error"]
    assert run("done", "--day-dir", str(day), "--platform", "douyin", "--result", "under_review", "--evidence", "shot.png")[0] == 0
    pub = (datetime.now().astimezone() - timedelta(hours=30)).isoformat(timespec="seconds")
    code, r = run("done", "--day-dir", str(day), "--platform", "douyin", "--result", "published", "--post-id", "123", "--published-at", pub)
    assert code == 0 and r["state"] == "PUBLISHED"
    # 一期一平台一条
    code, r = run("start", "--day-dir", str(day), "--platform", "douyin")
    assert code == 2 and "已投稿" in r["error"]
    # 窗口：w24 到期、w48 未到
    due = run("metrics-due")[1]
    assert [(x["platform"], x["window"]) for x in due["due"]] == [("douyin", "w24")]
    code, r = run("metric-add", "--day-dir", str(day), "--platform", "douyin", "--window", "w48", "--evidence", "shot.png", "--plays", "5")
    assert code == 2 and "提前" in r["error"]
    code, r = run("metric-add", "--day-dir", str(day), "--platform", "douyin", "--window", "w24", "--evidence", "shot.png")
    assert code == 2 and "补零" in r["error"]
    code, r = run("metric-add", "--day-dir", str(day), "--platform", "douyin", "--window", "w24", "--evidence", "shot.png", "--plays", "120", "--likes", "3")
    assert code == 0 and r["history_appended"]
    hist = [json.loads(x) for x in (proj / "performance_history.jsonl").read_text().splitlines()]
    assert hist[-1]["metric_cells"]["plays"]["value"] == 120 and hist[-1]["metric_cells"]["completion_rate"]["status"] == "NOT_PROVIDED"
    assert run("metric-add", "--day-dir", str(day), "--platform", "douyin", "--window", "w24", "--evidence", "shot.png", "--plays", "1")[0] == 2


def test_unknown_result_blocks_resubmit_until_verified(proj):
    day, _ = _day(proj)
    run("pack-check", "--day-dir", str(day), "--approved", "发吧")
    run("start", "--day-dir", str(day), "--platform", "kuaishou")
    # 中断：仍是 SUBMITTING
    code, r = run("start", "--day-dir", str(day), "--platform", "kuaishou")
    assert code == 2 and "禁止重新投稿" in r["error"]
    run("done", "--day-dir", str(day), "--platform", "kuaishou", "--result", "unknown")
    code, r = run("done", "--day-dir", str(day), "--platform", "kuaishou", "--result", "published")
    assert code == 2 and "核查" in r["error"]
    assert run("done", "--day-dir", str(day), "--platform", "kuaishou", "--result", "published", "--evidence", "shot.png")[0] == 0


def test_no_post_id_still_opens_windows(proj):
    day, _ = _day(proj, platforms=("douyin",))
    run("pack-check", "--day-dir", str(day), "--approved", "发吧")
    run("start", "--day-dir", str(day), "--platform", "douyin")
    pub = "2026-11-01T12:30:00+08:00"
    code, r = run("done", "--day-dir", str(day), "--platform", "douyin", "--result", "published", "--evidence", "shot.png", "--published-at", pub, "--time-precision", "minute")
    assert code == 0 and "warning" in r
    st = json.loads((day / "publish_status.json").read_text())["platforms"]["douyin"]
    assert set(st["windows"]) == {"w24", "w48", "w7d"}
    assert st["published_time"]["precision"] == "minute"


def test_cross_day_duplicate_master_blocked(proj):
    day, pack = _day(proj, platforms=("douyin",))
    run("pack-check", "--day-dir", str(day), "--approved", "发吧")
    run("start", "--day-dir", str(day), "--platform", "douyin")
    run("done", "--day-dir", str(day), "--platform", "douyin", "--result", "under_review", "--evidence", "shot.png")
    day2 = proj / "workspace" / "2026-11-02"; day2.mkdir()
    shutil.copy(day / "m.mp4", day2 / "m.mp4"); shutil.copy(day / "c.jpg", day2 / "c.jpg")
    pack["episode_id"] = "ep20261102-y"; _write_pack(day2, pack)
    run("pack-check", "--day-dir", str(day2), "--approved", "发吧")
    code, r = run("start", "--day-dir", str(day2), "--platform", "douyin")
    assert code == 2 and "其他日期" in r["error"]


def test_v1_status_is_converted_with_backup(proj):
    day = proj / "workspace" / "2026-10-15"; day.mkdir(parents=True)
    v1 = {"schema_version": "1.0", "episode_id": "ep-old", "master": {"sha256": "sha256:abc"},
          "platforms": {"douyin": {"state": "PUBLIC_VERIFIED", "attempts": [{"platform_published_at": "2026-10-18T15:14:15+08:00"}],
                                   "evidence": [{"path": "a.png", "post_id": "77", "public_url": "https://x"}], "events": []},
                        "wechat_channels": {"state": "SUBMITTING", "attempts": [{}], "evidence": [], "events": []}}}
    (day / "publish_status.json").write_text(json.dumps(v1))
    code, r = run("status", "--day-dir", str(day))
    assert code == 0
    p = r["episodes"]["2026-10-15"]["platforms"]
    assert p["douyin"] == {"state": "PUBLISHED", "post_id": "77", "published_at": "2026-10-18T15:14:15+08:00"}
    assert p["wechat_channels"]["state"] == "UNKNOWN"
    # 只读命令不改文件
    assert json.loads((day / "publish_status.json").read_text())["schema_version"] == "1.0"
    assert not (day / "publish_status.v1-backup.json").exists()
    # 首次写入才落盘并备份
    (day / "shot.png").write_bytes(b"png")
    assert run("done", "--day-dir", str(day), "--platform", "wechat_channels", "--result", "published", "--evidence", "shot.png")[0] == 0
    assert (day / "publish_status.v1-backup.json").exists()
    assert json.loads((day / "publish_status.json").read_text())["schema_version"] == "2"


# ---------------------------------------------------------------- 选题
def test_topics_dedup_rejected_and_history(proj):
    bank = {"topics": {
        "ravel-concerto-left-hand-wittgenstein": {"composer": "ravel", "title": "拉威尔", "_evidence": {"editorial_score": 41}},
        "liszt-gloves": {"composer": "liszt", "title": "李斯特", "_evidence": {"editorial_score": 30}},
        "chopin-x": {"composer": "chopin", "title": "肖邦", "_evidence": {"editorial_score": 30}},
        "fanny-y": {"composer": "mendelssohn", "title": "范妮", "_evidence": {"editorial_score": 50}}},
        "rejected": ["fanny-y"]}
    (proj / "topic_bank.json").write_text(json.dumps(bank, ensure_ascii=False))
    day = proj / "workspace" / "2026-10-10"; day.mkdir(parents=True)
    (day / "publish_status.json").write_text(json.dumps({"schema_version": "2", "episode_id": "ep20261010-ravel-left-hand-concerto", "platforms": {}}))
    rows = []
    for plat in ("douyin", "kuaishou", "bilibili"):
        for subj, plays in (("liszt", 900), ("chopin", 100)):
            rows.append({"object_type": "performance_observation", "platform": plat, "work_id": f"{plat}-{subj}",
                         "collected_at": "2026-10-01T00:00:00+08:00", "features": {"composer": subj},
                         "metric_cells": {"plays": {"value": plays}}})
    (proj / "performance_history.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    code, r = run("topics")
    assert code == 0
    slugs = [c["slug"] for c in r["candidates"]]
    assert "ravel-concerto-left-hand-wittgenstein" not in slugs and "fanny-y" not in slugs
    assert slugs == ["liszt-gloves", "chopin-x"]
    assert r["subject_history"]["liszt"] == {"score": 1.0, "n": 3} and r["candidates"][0]["history_used"]


def test_rejected_can_resubmit_but_counts_attempt(proj):
    day, _ = _day(proj, platforms=("douyin",))
    run("pack-check", "--day-dir", str(day), "--approved", "发吧")
    run("start", "--day-dir", str(day), "--platform", "douyin")
    run("done", "--day-dir", str(day), "--platform", "douyin", "--result", "rejected", "--evidence", "shot.png")
    code, r = run("start", "--day-dir", str(day), "--platform", "douyin")
    assert code == 0 and r["attempt"] == 2 and r["resubmit_after_reject"]


def test_corrupt_json_and_missing_ffmpeg_are_friendly(proj, monkeypatch):
    day, _ = _day(proj, platforms=("douyin",))
    (day / "publish_pack.json").write_text("{坏的")
    code, r = run("pack-check", "--day-dir", str(day))
    assert code == 2 and "损坏" in r["error"]
    monkeypatch.setenv("PATH", "/nonexistent")
    (day / "v.mp4").write_bytes(b"x")
    code, r = run("master-check", "--master", str(day / "v.mp4"))
    assert code == 2 and "ffmpeg" in r["error"]


def test_concurrent_writer_blocked(proj):
    import fcntl
    (proj / "workspace").mkdir(exist_ok=True)
    fh = open(proj / "workspace" / ".flow.lock", "w"); fcntl.flock(fh, fcntl.LOCK_EX)
    try:
        code, r = run("gen-log", "--plan", plan(proj / "p.json"), "--status", "submitted")
        assert code == 2 and "正在写入" in r["error"]
        assert run("status")[0] == 0  # 只读不受锁影响
    finally:
        fh.close()


# ---------------------------------------------------------------- v2.2 声画/响度/烂尾
def test_shot_alignment_logic():
    # 6 句旁白（5 个起点边界），切点全部错开 ≥1.5s
    tl = [{"start": 0.0, "end": 4.0}, {"start": 4.5, "end": 8.0}, {"start": 9.5, "end": 10.0},
          {"start": 14.7, "end": 17.0}, {"start": 23.5, "end": 24.0}, {"start": 27.5, "end": 28.0}]
    bad = F._shot_alignment([6.0, 11.0, 16.0, 25.0, 29.0], tl)
    assert bad["checked"] and bad["cut_proximity_ok"] is False and len(bad["misses"]) >= 2
    good = F._shot_alignment([4.45, 9.4, 14.6, 23.4, 27.4], tl, tolerance=0.2)
    assert good["cut_proximity_ok"] is True and good["misses"] == []
    # 数据不足不判
    assert F._shot_alignment([5.0], tl[:2])["checked"] is False


@ffmpeg
def test_master_check_alignment_warns_but_passes(proj):
    # 多镜头视频（xstack 4 段黑/红/绿/蓝各 8s），给一份全错开的时间轴：应放行但报警
    v = proj / "multi.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=red:s=720x1280:d=8:r=24",
                    "-f", "lavfi", "-i", "color=c=green:s=720x1280:d=8:r=24",
                    "-f", "lavfi", "-i", "color=c=blue:s=720x1280:d=8:r=24",
                    "-f", "lavfi", "-i", "color=c=white:s=720x1280:d=5.6:r=24",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=29", "-af", "volume=2.0",
                    "-filter_complex", "[0:v][1:v][2:v][3:v]concat=n=4:v=1[v]",
                    "-map", "[v]", "-map", "4:a", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(v)], check=True)
    tl = proj / "tl.json"
    tl.write_text(json.dumps([{"start": 0.0, "end": 4.0}, {"start": 4.4, "end": 8.0},
                              {"start": 8.4, "end": 10.0}, {"start": 16.0, "end": 17.0},
                              {"start": 24.5, "end": 27.0}]), encoding="utf-8")
    code, r = run("master-check", "--master", str(v), "--timeline", str(tl), "--align-tolerance", "0.8")
    assert code == 0 and r["shot_alignment"]["checked"]
    assert any("句界/切点接近度提示" in w for w in r["warnings"])
    assert r["loudness"]["integrated_lufs"] is not None


@ffmpeg
def test_raw_check_outputs_sheet_and_report(proj):
    day = proj / "workspace" / "2026-11-01"; day.mkdir(parents=True)
    raw = day / "raw.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "color=c=black:s=720x1280:d=6:r=24",
                    "-f", "lavfi", "-i", "color=c=red:s=720x1280:d=6:r=24",
                    "-f", "lavfi", "-i", "color=c=green:s=720x1280:d=6:r=24",
                    "-f", "lavfi", "-i", "color=c=blue:s=720x1280:d=6:r=24",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=24",
                    "-filter_complex", "[0:v][1:v][2:v][3:v]concat=n=4:v=1[v]",
                    "-map", "[v]", "-map", "4:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(raw)], check=True)
    code, r = run("raw-check", "--raw", str(raw), "--day-dir", str(day), "--frames", "8")
    assert code == 0 and r["shot_count"] >= 3 and len(r["sample_times_s"]) == 8
    assert (day / "_raw_check" / "raw-contact-sheet.png").is_file()
    assert (day / "raw-check.json").is_file()


def test_status_followups_stale_review_and_pending_plan(proj, monkeypatch):
    from datetime import datetime, timedelta
    day, _ = _day(proj, platforms=("douyin",))
    run("pack-check", "--day-dir", str(day), "--approved", "发吧")
    run("start", "--day-dir", str(day), "--platform", "douyin")
    run("done", "--day-dir", str(day), "--platform", "douyin", "--result", "under_review", "--evidence", "shot.png")
    # 把最后一条日志时间改到 49 小时前
    st = json.loads((day / "publish_status.json").read_text())
    old = (datetime.now().astimezone() - timedelta(hours=49)).isoformat(timespec="seconds")
    st["platforms"]["douyin"]["log"][-1]["at"] = old
    (day / "publish_status.json").write_text(json.dumps(st, ensure_ascii=False))
    # 一份 8 天前的确认①方案，且本期没有任何 PUBLISHED
    plan_f = day / "cand-old-plan-approved.json"
    plan_f.write_text(json.dumps({"candidate_id": "cand-old", "locked_parameters": {}}), encoding="utf-8")
    import os
    os.utime(plan_f, ((datetime.now() - timedelta(days=8)).timestamp(),) * 2)
    code, r = run("status")
    assert code == 0 and r["followup_count"] >= 2
    kinds = " ".join(x.get("day", "") + x.get("candidate_id", "") + x.get("platform", "") for x in r["followups"])
    assert "douyin" in kinds and "cand-old" in kinds


# ---------------------------------------------------------------- v2.3 时间、断点和批准版本

def test_unknown_publish_time_never_uses_verification_time(proj):
    day, _ = _day(proj, platforms=("douyin",))
    run("pack-check", "--day-dir", str(day), "--approved", "发吧")
    run("start", "--day-dir", str(day), "--platform", "douyin")
    code, r = run("done", "--day-dir", str(day), "--platform", "douyin", "--result", "published", "--evidence", "shot.png")
    assert code == 0 and r["published_at"] is None and r["verified_at"]
    ps = F.load_status(day)["platforms"]["douyin"]
    assert "windows" not in ps and not run("metrics-due")[1]["due"]
    code, r = run("metric-add", "--day-dir", str(day), "--platform", "douyin", "--window", "w24", "--evidence", "shot.png", "--plays", "1")
    assert code == 2 and "发布时间未知" in r["error"]
    pub = "2026-11-01T12:30:00+08:00"
    assert run("done", "--day-dir", str(day), "--platform", "douyin", "--result", "published", "--published-at", pub)[0] == 0
    assert F.load_status(day)["platforms"]["douyin"]["windows"]["w24"]["due_at"] == "2026-11-02T12:30:00+08:00"
    assert run("done", "--day-dir", str(day), "--platform", "douyin", "--result", "published")[1]["published_at"] == pub


def test_v1_event_time_is_not_publication_time():
    v = {"events": [{"to": "PUBLIC_VERIFIED", "at": "2026-11-09T12:00:00+08:00"}]}
    assert F._v1_published_at(v) is None


def test_invalid_publish_time_writes_nothing(proj):
    day, _ = _day(proj, platforms=("douyin",))
    run("pack-check", "--day-dir", str(day), "--approved", "发")
    run("start", "--day-dir", str(day), "--platform", "douyin")
    before = (day / "publish_status.json").read_bytes()
    assert run("done", "--day-dir", str(day), "--platform", "douyin", "--result", "published", "--evidence", "shot.png", "--published-at", "2026-11-01T12:00:00")[0] == 2
    assert (day / "publish_status.json").read_bytes() == before


def test_prepared_auth_resume_does_not_increment_attempts(proj):
    day, _ = _day(proj, platforms=("douyin",))
    run("pack-check", "--day-dir", str(day), "--approved", "发")
    args = ("checkpoint", "--day-dir", str(day), "--platform", "douyin", "--evidence", "shot.png")
    assert run(*args, "--stage", "prepared", "--editor-url", "https://creator.douyin.com/post/video")[1]["attempts"] == 0
    assert run(*args, "--stage", "waiting_auth")[1]["next"] == "handoff_auth_then_observe_original"
    assert run("start", "--day-dir", str(day), "--platform", "douyin")[0] == 2
    assert run(*args, "--stage", "ready")[0] == 0
    before = (day / "publish_status.json").read_bytes()
    assert run("resume", "--day-dir", str(day))[1]["platforms"]["douyin"]["next"] == "observe_original_editor_then_start_once"
    assert (day / "publish_status.json").read_bytes() == before
    assert run("start", "--day-dir", str(day), "--platform", "douyin")[1]["attempt"] == 1
    run(*args, "--stage", "waiting_auth")
    run(*args, "--stage", "ready")
    assert run("start", "--day-dir", str(day), "--platform", "douyin")[0] == 2
    assert F.load_status(day)["platforms"]["douyin"]["attempts"] == 1


@pytest.mark.parametrize("state", ["SUBMITTING", "SUBMITTED", "UNKNOWN"])
def test_old_unresolved_states_not_unlocked_by_checkpoint(proj, state):
    day, _ = _day(proj, platforms=("douyin",))
    run("pack-check", "--day-dir", str(day), "--approved", "发")
    run("start", "--day-dir", str(day), "--platform", "douyin")
    s = F.load_status(day); s["platforms"]["douyin"]["state"] = state; F.save_status(day, s)
    assert run("checkpoint", "--day-dir", str(day), "--platform", "douyin", "--stage", "prepared", "--evidence", "shot.png")[0] == 2
    assert run("checkpoint", "--day-dir", str(day), "--platform", "douyin", "--stage", "ready", "--evidence", "shot.png")[0] == 0
    assert run("start", "--day-dir", str(day), "--platform", "douyin")[0] == 2
    assert run("resume", "--day-dir", str(day))[1]["platforms"]["douyin"]["next"] == "verify_original_result_no_resubmit"


def test_submitted_receipt_blocks_resubmit(proj):
    day, _ = _day(proj, platforms=("douyin",))
    run("pack-check", "--day-dir", str(day), "--approved", "发")
    run("start", "--day-dir", str(day), "--platform", "douyin")
    assert run("done", "--day-dir", str(day), "--platform", "douyin", "--result", "submitted")[0] == 2
    assert run("done", "--day-dir", str(day), "--platform", "douyin", "--result", "submitted", "--evidence", "shot.png")[0] == 0
    assert run("done", "--day-dir", str(day), "--platform", "douyin", "--result", "not_submitted", "--evidence", "shot.png")[0] == 2
    assert run("start", "--day-dir", str(day), "--platform", "douyin")[0] == 2


@pytest.mark.parametrize("field", ["master", "description", "title", "topics", "cover", "account", "scope"])
def test_content_change_invalidates_old_approval(proj, field):
    day, pk = _day(proj, platforms=("douyin",))
    run("pack-check", "--day-dir", str(day), "--approved", "发")
    if field == "master":
        (day / "m.mp4").write_bytes(b"new-master"); pk["master"]["sha256"] = F.sha256_file(day / "m.mp4")
    elif field == "cover":
        (day / "c.jpg").write_bytes(b"new-cover")
    elif field == "account":
        pk["account_display_name"] = "另一个号"
    elif field == "scope":
        pk["platforms"]["kuaishou"] = dict(pk["platforms"]["douyin"])
    else:
        pk["platforms"]["douyin"][field] = ["#新题"] if field == "topics" else "新文案"
    _write_pack(day, pk)
    assert run("start", "--day-dir", str(day), "--platform", "douyin")[0] == 2
    code, r = run("pack-check", "--day-dir", str(day))
    assert code == 0 and r["release_approved"] is False
    assert F.load_status(day)["approval_history"]
    assert run("pack-check", "--day-dir", str(day), "--approved", "发布新版")[1]["release_approved"]


def test_revision_ack_does_not_authorize_publish_and_scope_is_enforced(proj):
    day, pk = _day(proj)
    r = run("pack-check", "--day-dir", str(day), "--approved", "认可", "--approval-kind", "revision")[1]
    assert not r["release_approved"]
    assert run("start", "--day-dir", str(day), "--platform", "douyin")[0] == 2
    run("pack-check", "--day-dir", str(day), "--approved", "只发抖音", "--platforms", "douyin")
    assert run("start", "--day-dir", str(day), "--platform", "kuaishou")[0] == 2
    pk["platforms"]["douyin"]["_plan_note"] = "内部备注改变"; _write_pack(day, pk)
    assert run("start", "--day-dir", str(day), "--platform", "douyin")[0] == 0


@pytest.mark.parametrize("state", ["PREPARED", "SUBMITTING", "SUBMITTED", "UNKNOWN", "UNDER_REVIEW", "PUBLISHED"])
def test_active_original_pack_cannot_be_overwritten(proj, state):
    day, pk = _day(proj, platforms=("douyin",))
    run("pack-check", "--day-dir", str(day), "--approved", "发")
    s = F.load_status(day); s["platforms"]["douyin"]["state"] = state; F.save_status(day, s)
    before = (day / "publish_status.json").read_bytes()
    (day / "m.mp4").write_bytes(b"new-master"); pk["master"]["sha256"] = F.sha256_file(day / "m.mp4"); _write_pack(day, pk)
    assert run("pack-check", "--day-dir", str(day), "--approved", "发新版")[0] == 2
    assert (day / "publish_status.json").read_bytes() == before
    assert run("start", "--day-dir", str(day), "--platform", "douyin")[0] == 2


def test_completed_status_does_not_recommend_start_or_mutate_history(proj):
    day, _ = _day(proj, platforms=("douyin",))
    run("pack-check", "--day-dir", str(day), "--approved", "发")
    run("start", "--day-dir", str(day), "--platform", "douyin")
    run("done", "--day-dir", str(day), "--platform", "douyin", "--result", "published", "--evidence", "shot.png")
    before = (day / "publish_status.json").read_bytes()
    assert run("status", "--day-dir", str(day))[1]["episodes"][day.name]["next"] == "completed"
    assert run("resume", "--day-dir", str(day))[1]["platforms"]["douyin"]["next"] == "completed"
    assert (day / "publish_status.json").read_bytes() == before
    assert run("pack-check", "--day-dir", str(day))[1]["next"] == "completed"
    for result in ("unknown", "rejected", "under_review", "not_submitted"):
        assert run("done", "--day-dir", str(day), "--platform", "douyin", "--result", result, "--evidence", "shot.png")[0] == 2


def test_long_action_exempts_sentence_cuts_but_never_proves_semantics():
    tl = [{"start": t, "end": t + .7} for t in (0, 5.3, 7.8, 13.9, 14.4, 15.3, 17.9, 18.8, 21.6, 26.2)]
    r = F._shot_alignment([5.292, 7.833, 12.708, 21.417, 26.208], tl,
                          action_ranges=[{"start": 12.708, "end": 21.417}])
    assert r["cut_proximity_ok"] and len(r["exempt_action_bounds"]) == 5
    assert r["semantic_alignment_verified"] is False and r["action_internal_cuts"] == []
    cut = F._shot_alignment([15.0], tl, action_ranges=[{"start": 12.708, "end": 21.417}])
    assert cut["action_internal_cuts"] == [15.0]
    with pytest.raises(F.FlowError):
        F._shot_alignment([], tl, action_ranges=[{"start": 2, "end": 1}])


@pytest.mark.parametrize("url", ["https://user:secret@example.test/editor", "https://example.test/editor?token=secret", "https://example.test/editor#secret"])
def test_checkpoint_rejects_potential_auth_urls(proj, url):
    day, _ = _day(proj, platforms=("douyin",))
    run("pack-check", "--day-dir", str(day), "--approved", "发")
    before = (day / "publish_status.json").read_bytes()
    assert run("checkpoint", "--day-dir", str(day), "--platform", "douyin", "--stage", "prepared", "--evidence", "shot.png", "--editor-url", url)[0] == 2
    assert (day / "publish_status.json").read_bytes() == before


def test_submitted_unknown_cannot_be_reset_and_legacy_approval_not_rebound(proj):
    day, _ = _day(proj, platforms=("douyin",))
    run("pack-check", "--day-dir", str(day), "--approved", "发")
    run("start", "--day-dir", str(day), "--platform", "douyin")
    run("done", "--day-dir", str(day), "--platform", "douyin", "--result", "submitted", "--evidence", "shot.png")
    run("done", "--day-dir", str(day), "--platform", "douyin", "--result", "unknown")
    assert run("done", "--day-dir", str(day), "--platform", "douyin", "--result", "not_submitted", "--evidence", "shot.png")[0] == 2
    s = F.load_status(day); s["approval"] = {"quote": "发", "at": "2026-11-01T00:00:00+08:00"}; F.save_status(day, s)
    before = (day / "publish_status.json").read_bytes()
    assert not run("status", "--day-dir", str(day))[1]["episodes"][day.name]["release_approved"]
    assert (day / "publish_status.json").read_bytes() == before
    assert run("start", "--day-dir", str(day), "--platform", "douyin")[0] == 2


def test_time_correction_with_metrics_and_resolved_blocker(proj):
    day, _ = _day(proj, platforms=("douyin",))
    run("pack-check", "--day-dir", str(day), "--approved", "发")
    run("start", "--day-dir", str(day), "--platform", "douyin")
    s = F.load_status(day); s["platforms"]["douyin"]["blocker"] = "旧登录阻塞"; F.save_status(day, s)
    run("done", "--day-dir", str(day), "--platform", "douyin", "--result", "published", "--evidence", "shot.png", "--published-at", "2026-11-01T00:00:00+08:00")
    ps = F.load_status(day)["platforms"]["douyin"]
    assert "blocker" not in ps and any("旧登录阻塞" in e["note"] for e in ps["log"])
    s = F.load_status(day); s["platforms"]["douyin"]["metrics"] = {"w24": {"validity": "VALID"}}; F.save_status(day, s)
    before = (day / "publish_status.json").read_bytes()
    assert run("done", "--day-dir", str(day), "--platform", "douyin", "--result", "published", "--published-at", "2026-11-01T01:00:00+08:00")[0] == 2
    assert (day / "publish_status.json").read_bytes() == before
