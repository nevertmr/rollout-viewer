#!/usr/bin/env python3
"""소스별 인과 기여도(source ablation) → 뷰어용 JSON 베이크.

입력
  analysis/attention_pilot/source_ablation/summary.json   에피소드·그룹 요약
  analysis/attention_pilot/source_ablation/npz/*.npz       에피소드별 프레임 시계열
출력
  dist/api/srcabl/<run>_<ep4>.json    프레임 시계열 + 에피소드 요약
  dist/api/srcabl/index.json          데이터가 있는 eid 목록 + 해석 규약

프레임 해상도는 원본 stride(5) 를 그대로 둔다 — 프론트가 보간한다.
지표 = ‖ablated 액션청크 − 원본 액션청크‖₂ (50스텝×6관절, deg). 노이즈 시드 3회 평균.

해석 규약(REPORT.md §8-3): 소스 간 절대 배수 비교 금지. 제거되는 정보량이 소스마다 달라서
"state 가 front 의 3.6배"는 주장할 수 없다. 유효한 것은 ① deg 값과 자연 드리프트 대비 배율
② 4소스 합 대비 상대 비중의 순위·시간 추세 ③ 같은 소스의 조건 간 변화다.

사용:  python3 build_srcabl.py [--src DIR] [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

VIEWER = os.path.dirname(os.path.abspath(__file__))
DEF_SRC = os.path.abspath(os.path.join(
    VIEWER, "..", "..", "analysis", "attention_pilot", "source_ablation"))
DEF_OUT = os.path.join(VIEWER, "dist", "api", "srcabl")
EPI_DIR = os.path.join(VIEWER, "dist", "api", "episode")

# 4소스 = 상대 비중의 분모. both_gray / lang_swap 은 참고·별도 계열
SOURCES = ["front_gray", "wrist_gray", "state_mid", "lang_empty"]
EXTRA = ["both_gray", "lang_swap"]
SHORT = {"front_gray": "front", "wrist_gray": "wrist",
         "state_mid": "state", "lang_empty": "lang", "lang_swap": "swap",
         "both_gray": "both"}
NOTE = ("소스 간 절대 배수 비교 금지 — 제거되는 정보량이 소스마다 다르다. "
        "같은 소스의 조건 간 변화·순위·시간 추세만 유효 (REPORT.md §8-3).")


def r3(x) -> float:
    return round(float(x), 3)


def clean(o):
    """NaN/Inf → null. summary.json 의 그룹 요약에 NaN 이 섞여 있어(빈 집계) 브라우저 JSON.parse 가 깨진다."""
    if isinstance(o, float):
        return o if o == o and abs(o) != float("inf") else None
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return o


def dump(obj, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(clean(obj), f, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def ep_num(epdir: str) -> int:
    """'ep0011_c1_t11' → 11"""
    return int(epdir[2:6])


def load_times(run: str, ep: int, n_frames: int, fps: float):
    """뷰어 에피소드 JSON 의 실측 타임스탬프. 없으면 fps 로 균등 생성."""
    p = os.path.join(EPI_DIR, "%s_%04d.json" % (run, ep))
    if os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                obj = json.load(f)
            t = obj.get("t") or []
            if len(t) >= n_frames:
                return [float(x) for x in t], float(obj.get("fps") or fps), "episode"
        except Exception as exc:                      # 깨진 JSON → 균등 폴백
            print("  ! %s/ep%04d 에피소드 JSON 읽기 실패(%s) → fps 균등" % (run, ep, exc))
    return [i / fps for i in range(n_frames)], fps, "uniform"


def bake_episode(e: dict, src: str, out: str, fps: float) -> dict | None:
    run, epdir = e["run"], e["epdir"]
    npz = os.path.join(src, "npz", "%s__%s__srcabl.npz" % (run, epdir))
    if not os.path.isfile(npz):
        print("  ! npz 없음: %s/%s" % (run, epdir))
        return None
    z = np.load(npz, allow_pickle=True)
    names = [str(x) for x in z["names"]]
    frames = [int(x) for x in z["frames"]]
    d = np.asarray(z["d_deg"], dtype=np.float64)      # (T, seeds, V)
    if d.ndim == 3:
        d = d.mean(axis=1)                            # 시드 평균
    meta = {}
    try:
        meta = json.loads(str(z["meta"]))
    except Exception:
        pass

    col = {n: d[:, i] for i, n in enumerate(names)}
    have = [s for s in SOURCES if s in col]
    if len(have) != len(SOURCES):
        print("  ! 소스 누락: %s/%s (%s)" % (run, epdir, names))
        return None

    four = np.stack([col[s] for s in SOURCES], axis=1)          # (T,4)
    tot = np.maximum(four.sum(axis=1, keepdims=True), 1e-9)
    share = four / tot                                          # 프레임별 상대 비중

    ep = ep_num(epdir)
    n_frames = int(e.get("n_frames") or (frames[-1] + 1))
    times, fps_used, tsrc = load_times(run, ep, n_frames, fps)
    sec = [round(times[min(f, len(times) - 1)], 4) for f in frames]

    gp = e.get("grasp_prog")
    grasp_frame = grasp_sec = None
    if isinstance(gp, (int, float)):
        gi = int(round(float(gp) * max(0, n_frames - 1)))
        grasp_frame = max(0, min(n_frames - 1, gi))
        grasp_sec = round(times[min(grasp_frame, len(times) - 1)], 4)

    swap_kind = "relational" if e.get("swap_relational") else "cross_task"
    payload = {
        "eid": "%s/ep%04d" % (run, ep),
        "run": run,
        "ep": ep,
        "epdir": epdir,
        "task": e.get("task"),
        "task_index": e.get("task_index"),
        "task_other": meta.get("task_other"),
        "label": e.get("label"),
        "memo": e.get("memo") or "",
        "pos": e.get("pos"),
        "deleted": bool(e.get("deleted")),
        "n_frames": n_frames,
        "stride": int(meta.get("stride") or (frames[1] - frames[0] if len(frames) > 1 else 5)),
        "fps": fps_used,
        "time_source": tsrc,
        "dur": round(float(times[n_frames - 1]), 4),
        "T": len(frames),
        "frames": frames,                                  # 원본 프레임 인덱스 (stride 유지)
        "sec": sec,                                        # 각 분석 프레임의 시각(초)
        "sources": SOURCES,
        "extra": [s for s in EXTRA if s in col],
        "short": SHORT,
        # 프레임별 기여도 (deg) — 시드 3회 평균
        "deg": {s: [r3(v) for v in col[s]] for s in SOURCES + [x for x in EXTRA if x in col]},
        # 프레임별 4소스 합 대비 상대 비중 (0..1)
        "share": {s: [round(float(share[:, i][k]), 4) for k in range(share.shape[0])]
                  for i, s in enumerate(SOURCES)},
        "sum4": [r3(v) for v in tot[:, 0]],
        # 에피소드 요약 (summary.json 원본 값 그대로)
        "mean": {k: v for k, v in (e.get("mean") or {}).items()},
        "p95": {k: v for k, v in (e.get("p95") or {}).items()},
        "share_mean": {k: v for k, v in (e.get("share") or {}).items()},
        "grasp_prog": gp,
        "grasp_frame": grasp_frame,
        "grasp_sec": grasp_sec,
        "nat_adj_frame_deg": e.get("nat_adj_frame_deg"),     # 자연 드리프트 기준선
        "nat_spread_deg": e.get("nat_spread_deg"),
        "swap_kind": swap_kind,
        "units": "action-chunk L2 (50 steps x 6 joints), degrees",
        "rms_divisor": 17.320508075688775,
        "note": NOTE,
    }
    dump(payload, os.path.join(out, "%s_%04d.json" % (run, ep)))
    return payload


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEF_SRC, help="source_ablation 디렉터리")
    ap.add_argument("--out", default=DEF_OUT, help="출력 디렉터리 (dist/api/srcabl)")
    ap.add_argument("--fps", type=float, default=30.0, help="타임스탬프 폴백 fps")
    a = ap.parse_args()

    sumf = os.path.join(a.src, "summary.json")
    if not os.path.isfile(sumf):
        print("summary.json 없음: %s" % sumf, file=sys.stderr)
        return 2
    with open(sumf, "r", encoding="utf-8") as f:
        summary = json.load(f)
    eps = summary.get("episodes") or []
    os.makedirs(a.out, exist_ok=True)

    ok, bad = [], []
    for e in eps:
        p = bake_episode(e, a.src, a.out, a.fps)
        (ok.append(p) if p else bad.append("%s/%s" % (e["run"], e["epdir"])))

    runs = sorted({p["run"] for p in ok})
    index = {
        "n": len(ok),
        "runs": runs,
        "eids": [p["eid"] for p in ok],
        "by_run": {r: [p["eid"] for p in ok if p["run"] == r] for r in runs},
        "sources": SOURCES,
        "extra": EXTRA,
        "short": SHORT,
        "units": "action-chunk L2 (50 steps x 6 joints), degrees",
        "config": summary.get("config") or {},
        "note": NOTE,
        # 그룹 요약(런·태스크 단위) — 배지/차트 툴팁에서 맥락으로 쓸 수 있게 같이 싣는다
        "groups": summary.get("groups") or {},
    }
    dump(index, os.path.join(a.out, "index.json"))

    print("srcabl 베이크: %d개 → %s" % (len(ok), a.out))
    for r in runs:
        print("  %-18s %d" % (r, len(index["by_run"][r])))
    if bad:
        print("실패 %d개: %s" % (len(bad), ", ".join(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
