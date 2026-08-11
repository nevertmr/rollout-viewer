"""intern_coffee 롤아웃 데이터 로딩 · 품질지표 계산 · 램프 판독 · 라벨 확정.

데이터층 단일 진입점. 서버(api)·인덱서(build_index)는 모두 이 모듈만 쓴다.

용어
  run   : coffee_new01 … coffee_new15
  ep    : 런 내부 에피소드 id (episode.json 의 "id")
  step  : episode.json 의 task_index (1..12). press 스텝은 3(왼쪽 머신)·6(오른쪽 머신).
  arm   : 앞 5개 관절(shoulder_pan, shoulder_lift, elbow_flex, wrist_flex, wrist_roll).
          6번째는 gripper 로 성격이 달라 arm 지표에서 제외한다.

주의: 에피소드 길이/duration 은 운영자가 손으로 끊은 정지 구간을 포함하므로
      품질 지표로 쓰지 않는다. 모든 지표는 stop_trim() 적용 후 계산한다.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

# ── 상수 ────────────────────────────────────────────────────────────────────
FPS = 30
JOINT_NAMES = ["shoulder_pan", "shoulder_lift", "elbow_flex",
               "wrist_flex", "wrist_roll", "gripper"]
N_ARM = 5          # 지표 계산에 쓰는 팔 관절 수 (gripper 제외)
N_REV = 4          # reversals 는 앞 4관절만 (wrist_roll 은 회전 노이즈가 커서 제외)

# 정지 트림 규칙
TRIM_THRESH = 0.08     # arm 프레임간 이동 L2 의 이동평균 임계
TRIM_WIN = 5           # 이동평균 창 (trailing)
TRIM_EXTRA = 10        # 정지 시작점에서 추가로 더 자르는 프레임 수
TRIM_MIN_KEEP = 10     # 트림 후 최소 보존 프레임 수

# gripper 개폐 이벤트 판정
GRIP_WIN = 5           # |Δgripper| 이동평균 창
GRIP_THRESH = 0.5      # 이동평균이 이 값을 넘는 연속 구간 1개 = 이벤트 1회

# 램프 ROI (front 640x480 그레이스케일 평균) — m1=왼쪽 머신, m2=오른쪽 머신
LAMP_ROI = {"m1": (58, 48, 102, 98), "m2": (398, 42, 442, 92)}
LAMP_THRESH = {"m1": 113.0, "m2": 162.0}
PRESS_STEPS = {3: "m1", 6: "m2"}   # press 스텝 → 그 스텝이 눌러야 하는 머신 램프


# ── 로딩 ────────────────────────────────────────────────────────────────────
def load_episode(run_dir, ep_dirname) -> dict:
    """episode.json + steps.jsonl 을 읽어 배열까지 만들어 돌려준다.

    반환 dict:
      run, dir, id, cycle, step, task, started_at, ended_at, duration_s,
      n_frames_meta : episode.json 에 기록된 프레임 수(신뢰하지 말 것, 참고용)
      n             : 실제 steps.jsonl 행 수
      f             : (n,) int   프레임 번호 (frames/fNNNNN_*.jpg 의 NNNNN)
      t             : (n,) float 첫 프레임 0 기준 경과 초
      state         : (n,6) float
      action        : (n,6) float — 없는 행은 NaN. 전부 없으면 None
      has_action    : (n,) bool
      frames_dir    : Path
    """
    run_dir = Path(run_dir)
    d = run_dir / "raw" / ep_dirname
    ej = json.loads((d / "episode.json").read_text())

    rows = []
    sp = d / "steps.jsonl"
    if sp.exists():
        for line in sp.read_text().splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))

    n = len(rows)
    f = np.array([r.get("f", i) for i, r in enumerate(rows)], dtype=np.int64) if n else np.zeros(0, np.int64)
    ts = np.array([r.get("ts", 0.0) for r in rows], dtype=np.float64) if n else np.zeros(0)
    t = (ts - ts[0]) if n else ts

    state = np.array([r["state"] for r in rows], dtype=np.float64) if n else np.zeros((0, 6))
    has_action = np.array([r.get("action") is not None for r in rows], dtype=bool) if n else np.zeros(0, bool)
    if n and has_action.any():
        action = np.full((n, 6), np.nan)
        for i, r in enumerate(rows):
            if r.get("action") is not None:
                action[i] = r["action"]
    else:
        action = None

    return {
        "run": run_dir.name,
        "dir": ep_dirname,
        "id": int(ej["id"]),
        "cycle": int(ej.get("cycle", 0)),
        "step": int(ej.get("task_index", 0)),
        "task": ej.get("task", ""),
        "started_at": ej.get("started_at"),
        "ended_at": ej.get("ended_at"),
        "duration_s": ej.get("duration_s"),
        "n_frames_meta": int(ej.get("n_frames", 0)),
        "n": n,
        "f": f,
        "t": t,
        "state": state,
        "action": action,
        "has_action": has_action,
        "frames_dir": d / "frames",
    }


# ── 정지 트림 ───────────────────────────────────────────────────────────────
def _trailing_mean(x: np.ndarray, win: int) -> np.ndarray:
    """x[i] 를 끝으로 하는 최대 win 길이 구간의 평균 (앞부분은 짧은 창으로 계산)."""
    c = np.concatenate([[0.0], np.cumsum(x)])
    i = np.arange(x.size)
    lo = np.maximum(0, i - win + 1)
    return (c[i + 1] - c[lo]) / (i - lo + 1)


def stop_trim(state: np.ndarray, thresh: float = TRIM_THRESH, win: int = TRIM_WIN,
              extra: int = TRIM_EXTRA, min_keep: int = TRIM_MIN_KEEP) -> int:
    """정지 꼬리를 잘라낸 뒤의 유효 끝 인덱스(exclusive)를 돌려준다.

    운영자가 손으로 에피소드를 끝내기 때문에 꼬리에 로봇이 멈춰 있는 구간이 남는다.
    arm 프레임간 이동 L2 의 5프레임 이동평균이 thresh 미만인 꼬리 구간을 잘라내고,
    거기서 추가로 extra 프레임 더 자른다. state[:end] 로 쓰면 된다.
    """
    state = np.asarray(state, dtype=np.float64)
    n = int(state.shape[0])
    if n <= min_keep:
        return n

    d = np.linalg.norm(np.diff(state[:, :N_ARM], axis=0), axis=1)   # (n-1,)
    if d.size == 0:
        return n
    mov = _trailing_mean(d, win)
    quiet = mov < thresh

    k = d.size
    while k > 0 and quiet[k - 1]:
        k -= 1
    # d[k-1] 이 마지막으로 '움직인' 전이 → state 인덱스 k 가 마지막 유효 프레임
    end = n if k == d.size else k + 1
    end -= extra
    return int(min(n, max(min_keep, end)))


# ── 품질 지표 ───────────────────────────────────────────────────────────────
def _sign_ffill(v: np.ndarray) -> np.ndarray:
    """부호 배열에서 0 을 직전 비영 부호로 채운다 (정지 구간이 방향전환으로 세지 않게)."""
    sg = np.sign(v)
    m, k = sg.shape
    idx = np.where(sg != 0, np.arange(m)[:, None], 0)
    np.maximum.accumulate(idx, axis=0, out=idx)
    return sg[idx, np.arange(k)[None, :]]


def compute_metrics(state: np.ndarray, action=None) -> dict:
    """정지 트림 적용 후 궤적 품질 지표를 계산한다. 값은 모두 JSON 직렬화 가능.

      jerk_p50 / jerk_p95 : arm 5관절 2차 차분 L2 의 백분위 (deg/frame^2)
      track_err           : ||action[i] - state[i+1]|| 의 평균 (6관절). action 없으면 None
      reversals           : arm 앞 4관절 속도 부호변화 총 횟수 / 100프레임
      grip_events         : |Δgripper| 이동평균이 임계를 넘는 연속 구간 수
      n_used              : 트림 후 프레임 수
      trim                : 잘라낸 프레임 수
    """
    state = np.asarray(state, dtype=np.float64)
    n = int(state.shape[0])
    out = {"jerk_p50": None, "jerk_p95": None, "track_err": None,
           "reversals": None, "grip_events": None, "n_used": 0, "trim": 0}
    if n == 0:
        return out

    end = stop_trim(state)
    s = state[:end]
    out["n_used"] = int(end)
    out["trim"] = int(n - end)
    if end < 3:
        return out

    # jerk: 2차 차분 L2
    j = np.linalg.norm(np.diff(s[:, :N_ARM], n=2, axis=0), axis=1)
    if j.size:
        out["jerk_p50"] = round(float(np.percentile(j, 50)), 4)
        out["jerk_p95"] = round(float(np.percentile(j, 95)), 4)

    # track_err: 같은 프레임 action 과 다음 프레임 state 의 거리
    if action is not None:
        a = np.asarray(action, dtype=np.float64)[:end]
        if a.shape[0] >= 2:
            err = np.linalg.norm(a[:-1] - s[1:], axis=1)
            ok = np.isfinite(err)
            if ok.any():
                out["track_err"] = round(float(err[ok].mean()), 4)

    # reversals: 앞 4관절 속도 부호변화 (100프레임 정규화)
    v = np.diff(s[:, :N_REV], axis=0)
    if v.shape[0] >= 2:
        sg = _sign_ffill(v)
        chg = (sg[1:] != sg[:-1]) & (sg[1:] != 0) & (sg[:-1] != 0)
        out["reversals"] = round(float(chg.sum()) * 100.0 / float(end), 3)

    # grip_events: |Δgripper| 이동평균 임계 초과 구간 수
    dg = np.abs(np.diff(s[:, 5]))
    if dg.size:
        sm = _trailing_mean(dg, GRIP_WIN)
        hot = sm > GRIP_THRESH
        out["grip_events"] = int(np.count_nonzero(hot[1:] & ~hot[:-1]) + (1 if hot.size and hot[0] else 0))

    return out


# ── 램프 ────────────────────────────────────────────────────────────────────
def _roi_mean(gray: np.ndarray, box) -> float:
    x1, y1, x2, y2 = box
    return float(gray[y1:y2, x1:x2].mean())


def lamp_series(frames_dir, stride: int = 3) -> dict:
    """front 프레임에서 m1/m2 ROI 그레이 평균 시계열을 뽑는다.

    stride 간격으로 샘플링한 뒤 전체 프레임 수에 맞춰 선형보간하므로,
    반환되는 두 리스트의 길이는 항상 front 프레임 개수와 같다.
    """
    from PIL import Image

    frames_dir = Path(frames_dir)
    files = sorted(frames_dir.glob("f*_front.jpg"))
    n = len(files)
    if n == 0:
        return {"m1": [], "m2": []}

    idx = list(range(0, n, max(1, int(stride))))
    if idx[-1] != n - 1:
        idx.append(n - 1)

    xs, m1s, m2s = [], [], []
    for i in idx:
        try:
            g = np.asarray(Image.open(files[i]).convert("L"))
        except Exception:
            continue
        xs.append(i)
        m1s.append(_roi_mean(g, LAMP_ROI["m1"]))
        m2s.append(_roi_mean(g, LAMP_ROI["m2"]))

    if not xs:
        return {"m1": [], "m2": []}

    grid = np.arange(n, dtype=np.float64)
    xs = np.asarray(xs, dtype=np.float64)
    m1 = np.interp(grid, xs, np.asarray(m1s))
    m2 = np.interp(grid, xs, np.asarray(m2s))
    return {"m1": [round(float(v), 2) for v in m1],
            "m2": [round(float(v), 2) for v in m2]}


def lamp_summary(m1, m2, step) -> dict:
    """램프 시계열 요약. step 이 press(3=m1, 6=m2)일 때만 점등 프레임을 찾는다."""
    out = {"lamp_m1_end": None, "lamp_m2_end": None, "lamp_lit_frame": None}
    if m1:
        out["lamp_m1_end"] = round(float(m1[-1]), 2)
    if m2:
        out["lamp_m2_end"] = round(float(m2[-1]), 2)

    key = PRESS_STEPS.get(int(step) if step is not None else -1)
    if key:
        series = m1 if key == "m1" else m2
        thr = LAMP_THRESH[key]
        for i, v in enumerate(series):
            if v > thr:
                out["lamp_lit_frame"] = int(i)
                break
    return out


# ── 라벨 확정 ───────────────────────────────────────────────────────────────
def resolve_labels(run_meta: dict) -> dict:
    """런의 이벤트에서 에피소드별 최종 라벨을 확정한다.

    규칙
      1) 같은 episode 에 episode_update 가 여러 번이면 ts 기준 마지막이 최종.
      2) episode_update 가 하나도 없는 에피소드는, 그 런에 bulk_label 이벤트가 있으면
         "success", 없으면 "unlabeled".
      3) deleted 는 라벨과 별개 플래그 — deleted=True 여도 label 은 그대로 남긴다.

    반환 {ep_id: {"label", "memo", "deleted", "source"}}
      source: "update"(개별 라벨) | "bulk"(일괄) | "none"(미라벨)
    """
    events = sorted(run_meta.get("events", []) or [], key=lambda e: e.get("ts", 0.0))
    has_bulk = any(e.get("type") == "bulk_label" for e in events)

    ep_ids = set()
    for e in run_meta.get("episodes", []) or []:
        if e.get("id") is not None:
            ep_ids.add(int(e["id"]))
    for e in events:
        if e.get("type") in ("episode_end", "episode_update") and e.get("episode") is not None:
            ep_ids.add(int(e["episode"]))

    last: dict[int, dict] = {}
    for e in events:
        if e.get("type") == "episode_update" and e.get("episode") is not None:
            last[int(e["episode"])] = e

    out = {}
    for i in sorted(ep_ids):
        e = last.get(i)
        if e is None:
            out[i] = {"label": "success" if has_bulk else "unlabeled",
                      "memo": "", "deleted": False,
                      "source": "bulk" if has_bulk else "none"}
        else:
            out[i] = {"label": e.get("label") or "unlabeled",
                      "memo": e.get("memo") or "",
                      "deleted": bool(e.get("deleted", False)),
                      "source": "update"}
    return out
