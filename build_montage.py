#!/usr/bin/env python3
"""Task 몽타주 베이크 — Task(= Run(실험) × step) 하나의 카드 그리드를 mp4 한 편으로 미리 굽는다.

프론트가 카드마다 <video> 를 40~60개 띄우면 하드웨어 디코더 한계로 렉이 걸린다.
대신 Task × 모드(attn|causal|orig) × (all|final) 조합마다 타일 몽타주 1편을 굽고,
프론트는 그 영상 한 편 위에 HTML 오버레이(테두리·라벨·메모)만 얹는다.

  출력: dist/montage/<experiment>__t<step>__<mode>__<all|final>.mp4
        dist/montage/<같은 이름>.json      (타일 레이아웃 메타 — 프론트가 이걸로 오버레이를 그린다)
        dist/montage/manifest.json         (존재하는 몽타주 이름 목록 — 프론트가 404 프로브 없이 폴백 판단)

  타일 = 카드 1장 = [상단 라벨 띠 22px] + front 200×150 + wrist 200×150 + [하단 메모 띠 28px] = 200×350.
  타일 간격 8px, 열 수 = min(n, 8), 배경 #0b0f17. 타일 순서 = 프론트 renderTask() 와 동일
  (Try 오름차순 → 같은 Try 안 그룹은 run 순·cycle 순 → 시도 순; final 은 그룹당 대표 시도 1개).
  길이 = 가장 긴 클립 기준, 짧은 클립은 마지막 프레임 정지(tpad). 30fps, libx264 crf23 veryfast.

  사용:  python3 build_montage.py [RUNS=exp1,exp2] [STEPS=1,6] [MODES=attn,causal,orig] [ALL=0|1] [FORCE=1] [JOBS=6]
         (env 또는 인자 KEY=VAL 둘 다 받는다. RUNS 는 실험 이름(NorRec_RW___Red 등) 기준)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

VIEWER = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, VIEWER)

# KEY=VAL 인자를 env 로 승격 (python3 build_montage.py RUNS=a,b STEPS=1,6)
for _a in sys.argv[1:]:
    if "=" in _a:
        k, v = _a.split("=", 1)
        os.environ[k] = v
sys.argv = [sys.argv[0]]

import viewer_data as VD  # noqa: E402  (server.py 의 argparse 가 건드리지 않게 argv 정리 후 import)

OUT_DIR = os.path.join(VIEWER, "dist", "montage")
CACHE_CLIPS = os.path.join(VIEWER, "cache", "clips")
DIST_CLIPS = os.path.join(VIEWER, "dist", "clips")
FFMPEG = os.environ.get("FFMPEG") or shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = os.environ.get("FFPROBE") or os.path.join(os.path.dirname(FFMPEG), "ffprobe")

JOBS = int(os.environ.get("JOBS") or 6)
FORCE = os.environ.get("FORCE") == "1"
ONLY_RUNS = {r for r in (os.environ.get("RUNS") or "").split(",") if r}
ONLY_STEPS = {int(s) for s in (os.environ.get("STEPS") or "").split(",") if s.strip()}
MODES = [m for m in (os.environ.get("MODES") or "attn,causal,orig").split(",") if m]
ALLS = [int(a) for a in (os.environ.get("ALL") or "1,0").split(",") if a.strip()]

FPS = 30
TILE_W = 200
CAM_H = 150
BAND_TOP = 22        # 라벨 띠 (오버레이 텍스트 자리, 영상 없음)
BAND_BOT = 28        # 메모 띠
TILE_H = BAND_TOP + CAM_H * 2 + BAND_BOT   # 350
GAP = 8
MAX_COLS = 8
BG = "#0b0f17"
CRF = "23"
PRESET = "veryfast"


# ---------------------------------------------------------------------------
# 인덱스 → Task 목록 (프론트 buildExpTree / renderTask 와 같은 순서)
# ---------------------------------------------------------------------------
def pick_default_attempt(atts):
    """main.js pickDefaultAttempt 와 동일: 삭제 안 된 시도 중 마지막 success → 마지막 unstable → 마지막."""
    if not atts:
        return None
    by_no = sorted(atts, key=lambda a: a.get("attempt") or 0)
    live = [a for a in by_no if not a.get("deleted")]
    pool = live if live else by_no
    for oc in ("success", "unstable"):
        for a in reversed(pool):
            if a.get("outcome") == oc:
                return a
    return pool[-1]


def build_tasks(idx):
    """[(exp, step, [ {g, try_no, multi_cycle, ti} … 그룹 순서 ]) …]"""
    groups = idx.get("groups") or []
    run_order = list(idx.get("runs") or [])
    exp_order = []
    for x in idx.get("experiments") or []:
        if x["name"] not in exp_order:
            exp_order.append(x["name"])
    emap = {}
    for g in groups:
        en, tn = g.get("experiment") or g["run"], int(g.get("try_no") or 0)
        if en not in emap:
            emap[en] = {}
            if en not in exp_order:
                exp_order.append(en)
        emap[en].setdefault(tn, []).append(g)

    def by_group(g):
        r = run_order.index(g["run"]) if g["run"] in run_order else 10**6
        return (r, g.get("cycle") or 0, g.get("step") or 0)

    tasks = []
    for en in exp_order:
        if en not in emap:
            continue
        tm = emap[en]
        tries = sorted(tm.keys())
        # step -> {try_no -> groups}
        smap = {}
        for tn in tries:
            for g in sorted(tm[tn], key=by_group):
                smap.setdefault(int(g["step"]), {}).setdefault(tn, []).append(g)
        for step in sorted(smap.keys()):
            rows = []
            for ti, tn in enumerate(sorted(smap[step].keys())):
                gs = sorted(smap[step][tn], key=by_group)
                multi = len({g.get("cycle") for g in gs}) > 1
                for g in gs:
                    rows.append({"g": g, "try_no": tn, "multi_cycle": multi, "ti": ti})
            tasks.append((en, step, rows))
    return tasks


def tiles_for(rows, show_all):
    tiles = []
    for r in rows:
        g = r["g"]
        atts = list(g.get("attempts") or [])
        vis = atts if (show_all or len(atts) <= 1) else [pick_default_attempt(atts)]
        for a in vis:
            if a is None:
                continue
            tiles.append({"eid": a["eid"], "gid": g.get("gid"), "run": a.get("run") or g["run"],
                          "ep": int(a.get("ep") if a.get("ep") is not None else VD.parse_eid(a["eid"])[1]),
                          "try_no": r["try_no"], "cycle": g.get("cycle"), "ti": r["ti"],
                          "multi_cycle": r["multi_cycle"],
                          "attempt": a.get("attempt") or 1, "total": len(atts),
                          "outcome": "deleted" if a.get("deleted") else (a.get("outcome") or "unlabeled"),
                          "deleted": bool(a.get("deleted")), "memo": a.get("memo") or ""})
    return tiles


# ---------------------------------------------------------------------------
# 클립 찾기 / 길이
# ---------------------------------------------------------------------------
def find_clip(run, ep, cam):
    base = "%s_%04d_%s.mp4" % (run, ep, cam)
    for d in (CACHE_CLIPS, DIST_CLIPS):
        p = os.path.join(d, base)
        if os.path.isfile(p) and os.path.getsize(p) > 0:     # 심링크도 isfile 로 따라간다
            return p
    return None


_DUR = {}
_DUR_LOCK = threading.Lock()


def clip_dur(path):
    with _DUR_LOCK:
        if path in _DUR:
            return _DUR[path]
    cmd = [FFPROBE, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=nb_frames,duration,r_frame_rate", "-of", "json", path]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=60).stdout
        st = (json.loads(out or b"{}").get("streams") or [{}])[0]
        nb = int(st.get("nb_frames") or 0)
        d = float(st.get("duration") or 0)
        dur = nb / FPS if nb > 0 else d
    except Exception:
        dur = 0.0
    with _DUR_LOCK:
        _DUR[path] = dur
    return dur


# ---------------------------------------------------------------------------
# 몽타주 하나 굽기
# ---------------------------------------------------------------------------
def montage_name(exp, step, mode, show_all):
    return "%s__t%d__%s__%s" % (exp, step, mode, "all" if show_all else "final")


def plan(exp, step, rows, mode, show_all):
    tiles = tiles_for(rows, show_all)
    n = len(tiles)
    cols = max(1, min(n, MAX_COLS))
    nrows = (n + cols - 1) // cols
    W = cols * TILE_W + (cols - 1) * GAP
    H = nrows * TILE_H + (nrows - 1) * GAP
    total = 0.0
    for i, t in enumerate(tiles):
        run, ep = t["run"], t["ep"]
        fo, wo = find_clip(run, ep, "front"), find_clip(run, ep, "wrist")
        fa, wa = find_clip(run, ep, "front_attn"), find_clip(run, ep, "wrist_attn")
        fc, wc = find_clip(run, ep, "front_causal"), find_clip(run, ep, "wrist_causal")
        t["has_attn"] = bool(fa)
        t["has_causal"] = bool(fc)
        t["has_causal_wrist"] = bool(wc)
        if mode == "attn" and fa:
            src_f, src_w = fa, (wa or wo)
        elif mode == "causal" and fc:
            # wrist 인과 지도가 있으면 그것도 함께(없으면 손목은 원본)
            src_f, src_w = fc, (wc or wo)
        else:
            src_f, src_w = fo, wo
        t["src"] = {"front": os.path.basename(src_f) if src_f else None,
                    "wrist": os.path.basename(src_w) if src_w else None}
        t["mode_used"] = ("attn" if (mode == "attn" and fa) else "causal" if (mode == "causal" and fc) else "orig")
        t["_f"], t["_w"] = src_f, src_w
        d = max(clip_dur(src_f) if src_f else 0.0, clip_dur(src_w) if src_w else 0.0)
        t["dur"] = round(d, 3)
        total = max(total, d)
        c, r = i % cols, i // cols
        t["x"], t["y"], t["w"], t["h"] = c * (TILE_W + GAP), r * (TILE_H + GAP), TILE_W, TILE_H
        t["col"], t["row"] = c, r
    if total <= 0:
        total = 1.0 / FPS
    meta = {
        "name": montage_name(exp, step, mode, show_all), "exp": exp, "step": step, "mode": mode,
        "show_all": bool(show_all), "fps": FPS, "width": W, "height": H, "cols": cols, "rows": nrows,
        "gap": GAP, "bg": BG, "dur": round(total, 3), "n": n,
        "tile": {"w": TILE_W, "h": TILE_H, "band_top": BAND_TOP, "band_bottom": BAND_BOT,
                 "cam_h": CAM_H, "front_y": BAND_TOP, "wrist_y": BAND_TOP + CAM_H},
        "tiles": tiles,
    }
    return meta


def bake(meta):
    """meta(plan 결과) → mp4. 입력 2N 개(없는 캠은 검은 color 소스), 타일별 vstack+pad, 전체 xstack."""
    name = meta["name"]
    out = os.path.join(OUT_DIR, name + ".mp4")
    tmp = out + ".part.mp4"
    script = os.path.join(OUT_DIR, "." + name + ".filter")
    D = meta["dur"]
    args = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-nostdin"]
    flt = []
    labels = []
    k = -1                                     # ffmpeg 입력 번호
    for i, t in enumerate(meta["tiles"]):
        parts = []
        for cam in ("front", "wrist"):
            src = t["_" + cam[0]]
            if src:
                args += ["-i", src]
            else:
                args += ["-f", "lavfi", "-i", "color=c=black:s=%dx%d:r=%d:d=%.3f" % (TILE_W, CAM_H, FPS, D)]
            k += 1
            lab = "%s%d" % (cam[0], i)
            # 4:3 원본 → 200×150. 길이가 짧은 클립은 마지막 프레임을 D 까지 복제(정지), 긴 쪽은 D 에서 자른다.
            flt.append("[%d:v]fps=%d,scale=%d:%d:flags=bicubic,setsar=1,"
                       "tpad=stop_mode=clone:stop_duration=%.3f,trim=duration=%.3f,setpts=PTS-STARTPTS[%s]"
                       % (k, FPS, TILE_W, CAM_H, D + 1.0, D, lab))
            parts.append("[%s]" % lab)
        # front 위 + wrist 아래, 위 22px·아래 28px 띠는 배경색으로 패딩
        flt.append("%svstack=inputs=2,pad=%d:%d:0:%d:color=%s[t%d]"
                   % ("".join(parts), TILE_W, TILE_H, BAND_TOP, BG, i))
        labels.append("[t%d]" % i)
    n = len(labels)
    if n == 1:
        flt.append("[t0]pad=%d:%d:0:0:color=%s,format=yuv420p[out]" % (meta["width"], meta["height"], BG))
    else:
        layout = "|".join("%d_%d" % (t["x"], t["y"]) for t in meta["tiles"])
        flt.append("%sxstack=inputs=%d:layout=%s:fill=%s,format=yuv420p[out]"
                   % ("".join(labels), n, layout, BG))
    with open(script, "w") as f:
        f.write(";\n".join(flt) + "\n")
    args += ["-filter_complex_script", script, "-map", "[out]", "-r", str(FPS), "-t", "%.3f" % D,
             "-c:v", "libx264", "-preset", PRESET, "-crf", CRF, "-pix_fmt", "yuv420p",
             "-movflags", "+faststart", "-an", tmp]
    t0 = time.time()
    p = subprocess.run(args, capture_output=True, timeout=3600)
    el = time.time() - t0
    if p.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        if os.path.exists(tmp):
            os.remove(tmp)
        return {"ok": False, "name": name, "err": (p.stderr or b"").decode("utf-8", "replace")[-1500:], "sec": el}
    os.replace(tmp, out)
    try:
        os.remove(script)
    except OSError:
        pass
    # 메타 JSON (내부 경로 키 제거)
    m = dict(meta)
    m["tiles"] = [{k: v for k, v in t.items() if not k.startswith("_")} for t in meta["tiles"]]
    m["bytes"] = os.path.getsize(out)
    with open(os.path.join(OUT_DIR, name + ".json"), "w") as f:
        json.dump(m, f, ensure_ascii=False, separators=(",", ":"))
    return {"ok": True, "name": name, "sec": el, "bytes": os.path.getsize(out), "n": n}


def write_manifest():
    names = sorted(os.path.splitext(f)[0] for f in os.listdir(OUT_DIR)
                   if f.endswith(".json") and not f.startswith(".") and f != "manifest.json"
                   and os.path.exists(os.path.join(OUT_DIR, os.path.splitext(f)[0] + ".mp4")))
    with open(os.path.join(OUT_DIR, "manifest.json"), "w") as f:
        json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "names": names,
                   "tile": {"w": TILE_W, "h": TILE_H}}, f, ensure_ascii=False)
    return names


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    if not os.path.exists(FFMPEG):
        print("ffmpeg 없음:", FFMPEG)
        return 2
    VD.MB.load()
    idx = VD.INDEX.get(refresh=False)
    tasks = build_tasks(idx)
    jobs, skipped = [], 0
    for exp, step, rows in tasks:
        if ONLY_RUNS and exp not in ONLY_RUNS:
            continue
        if ONLY_STEPS and step not in ONLY_STEPS:
            continue
        for mode in MODES:
            for sa in ALLS:
                name = montage_name(exp, step, mode, sa)
                out = os.path.join(OUT_DIR, name + ".mp4")
                if (not FORCE and os.path.exists(out) and os.path.getsize(out) > 0
                        and os.path.exists(os.path.join(OUT_DIR, name + ".json"))):
                    skipped += 1
                    continue
                jobs.append((exp, step, rows, mode, sa))
    print("Task %d개 · 할 일 %d개 · 이미 있음 %d개 · %d 병렬" % (len(tasks), len(jobs), skipped, JOBS), flush=True)

    # 메타(ffprobe 포함)는 먼저 순차/병렬로 계획 — 길이 캐시를 공유한다
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=JOBS) as ex:
        metas = list(ex.map(lambda j: plan(*j), jobs))
    print("계획 완료 %.1fs (ffprobe %d 클립)" % (time.time() - t0, len(_DUR)), flush=True)
    # 타일 많은 것부터 — 긴 작업이 꼬리에 남지 않게
    metas.sort(key=lambda m: -m["n"])

    done, fails, total_bytes = 0, [], 0
    with ThreadPoolExecutor(max_workers=JOBS) as ex:
        for r in ex.map(bake, metas):
            done += 1
            if r["ok"]:
                total_bytes += r["bytes"]
                print("  [%d/%d] %s  %d타일 %.1fs %.1fMB" % (done, len(metas), r["name"], r["n"], r["sec"], r["bytes"] / 1e6), flush=True)
            else:
                fails.append(r)
                print("  FAIL %s (%.1fs): %s" % (r["name"], r["sec"], r["err"]), flush=True)
    names = write_manifest()
    sz = sum(os.path.getsize(os.path.join(OUT_DIR, f)) for f in os.listdir(OUT_DIR) if f.endswith(".mp4"))
    print("완료 %d개, 실패 %d개, %.1fs · 이번 회차 %.1fMB · dist/montage 전체 %d편 %.1fMB"
          % (done - len(fails), len(fails), time.time() - t0, total_bytes / 1e6, len(names), sz / 1e6))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
