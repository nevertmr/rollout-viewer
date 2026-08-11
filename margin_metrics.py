"""마진(아슬아슬함) 지표 — 성공 롤아웃끼리 "얼마나 여유 있게 성공했나"를 정량화한다.

성공/실패 판정기가 아니다. 149개 성공 사이의 **서열**을 만들기 위한 연속값 지표다.
전제: 실패는 이미 라벨로 걸러져 있고, 우리가 알고 싶은 것은 "성공했지만 실패에
얼마나 가까웠나(margin-to-failure)"이다.

설계 원칙 (조사 결론 반영)
  1) **에피소드 길이·소요시간은 쓰지 않는다.** 운영자가 손으로 끊어 꼬리가 자의적이다.
     시간은 오직 정책 자신이 만든 내부 이벤트(그리퍼 개폐 전이, 램프 first_lit) 사이의
     국소 구간에서만 쓴다.
  2) **평균 집계 금지, 극값 집계.** 마진은 min/p05 로 모은다. 10스텝 중 9개가 여유로워도
     한 스텝이 아슬아슬했으면 그 롤아웃은 아슬아슬한 성공이다.
  3) **σ 정규화.** 서로 다른 단위(px, deg, frame)를 z 로 통일해야 합칠 수 있다.
     z = (m - m_crit) / sigma  — m_crit 은 가능하면 실패 에피소드에서 관측된 경계값.
  4) 센서 제약: 힘/토크 센서 없음, 물체 추적기 없음. front 640x480 JPEG + 관절 6축뿐.

씬이 전 런 고정이므로 front 픽셀 좌표를 그대로 물리 랜드마크로 쓴다.
컵 검출은 색 기준이 아니라 **에피소드 내부 참조 프레임과의 차분**으로 한다
(컵의 색·밝기가 시점/조명에 따라 크게 변해 색 임계가 깨지기 때문).

스텝 유형
  pick  (1,4,7,9)  : 접근 중 컵 교란, 파지 전 최소 클리어런스, 파지 후 미끄러짐, 재파지
  place (2,5,8,10) : 릴리즈 오프타깃, 릴리즈 후 컵 이동/흔들림, 복귀 중 최소 클리어런스
  press (3,6)      : 램프 점등 유지, 점등 시점 대비 압박 깊이 여유, 접촉 위치(중앙/가장자리),
                     재접근 횟수, 접촉→점등 지연

사용
  python3 margin_metrics.py --all            # 200 에피소드 전부 계산 → cache/margins.json
  python3 margin_metrics.py --validate       # 실패 대비 성공 서열 검증 표 출력
  python3 margin_metrics.py --ep coffee_new10 11 --dump-frames
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent))
import metrics as M  # noqa: E402

BASE = Path("/Users/gimminseo/kai_pj/intern_coffee")
HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"

FPS = 30
W, H = 640, 480

# ── 씬 랜드마크 (front 640x480, 전 런 고정) ────────────────────────────────
# (cx, cy, rx, ry)  — 원근 때문에 타원. rx/ry 는 반지름.
LANDMARKS = {
    "pink_circle": (71.0, 337.0, 26.0, 30.0),
    "blue_circle": (479.0, 368.0, 29.0, 30.0),
    "tray_left":   (175.0, 215.0, 40.0, 32.0),   # 왼쪽 머신 트레이 위 컵 자리
    "tray_right":  (404.0, 212.0, 40.0, 32.0),   # 오른쪽 머신 트레이 위 컵 자리
    "btn_left":    (26.0, 162.0, 26.0, 21.0),    # 왼쪽 머신 파란 버튼 (좌측 잘림)
    "btn_right":   (304.5, 140.5, 35.0, 24.0),   # 오른쪽 머신 파란 버튼
}

STEP_KIND = {1: "pick", 4: "pick", 7: "pick", 9: "pick",
             2: "place", 5: "place", 8: "place", 10: "place",
             3: "press", 6: "press"}

# 그 스텝에서 컵이 놓이는/집히는 자리 (pick=출발지, place=목적지)
STEP_TARGET = {1: "blue_circle", 4: "blue_circle",
               2: "tray_left", 5: "tray_right",
               7: "tray_left", 9: "tray_right",
               8: "pink_circle", 10: "pink_circle",
               3: "btn_left", 6: "btn_right"}

TABLE_Y0 = 275          # 이 아래는 흰 천 — 어두운 픽셀 = 팔 (머신 없음)
DARK_V = 95             # 팔(검은 링크) 임계
RED_S, RED_V = 60, 80   # 그리퍼 붉은 조 임계
GRIP_DILATE = 11        # dark 를 이만큼 부풀려 그 안의 red 를 그리퍼로 인정

DIFF_THR = 26           # 참조 프레임 대비 채널평균 절대차 임계 (컵 검출)
MIN_CUP_PX = 120        # 컵으로 인정할 최소 픽셀 수
ROI_PAD = 34            # 랜드마크 반지름에 더할 ROI 여유 (px)


# ── 프레임 I/O ──────────────────────────────────────────────────────────────
def _dilate(m: np.ndarray, r: int = 1) -> np.ndarray:
    """반경 r 사각 구조요소 팽창 (분리형 시프트 — PIL MaxFilter 보다 15배 빠름)."""
    if r <= 0:
        return m
    o = m.copy()
    for k in range(1, r + 1):
        o[:, k:] |= m[:, :-k]
        o[:, :-k] |= m[:, k:]
    m2 = o
    o = m2.copy()
    for k in range(1, r + 1):
        o[k:, :] |= m2[:-k, :]
        o[:-k, :] |= m2[k:, :]
    return o


def _erode(m: np.ndarray, r: int = 1) -> np.ndarray:
    return ~_dilate(~m, r)


def _edge(m: np.ndarray) -> np.ndarray:
    return m & ~_erode(m, 1)


class Frames:
    """front 프레임 지연 로딩 + 마스크 캐시."""

    def __init__(self, frames_dir):
        self.dir = Path(frames_dir)
        self.files = sorted(self.dir.glob("f*_front.jpg"))
        self.n = len(self.files)
        self._rgb = {}
        self._arm = {}
        self._bg = None

    def _put(self, d, k, v, cap=260):
        d[k] = v
        if len(d) > cap:
            d.pop(next(iter(d)))
        return v

    def rgb(self, i) -> np.ndarray:
        i = int(np.clip(i, 0, self.n - 1))
        if i in self._rgb:
            return self._rgb[i]
        return self._put(self._rgb, i,
                         np.asarray(Image.open(self.files[i]).convert("RGB"), np.uint8))

    def hsv(self, i) -> np.ndarray:
        return np.asarray(Image.fromarray(self.rgb(i)).convert("HSV"), np.uint8)

    def arm(self, i) -> np.ndarray:
        """팔+그리퍼 실루엣 (bool, 640x480).

        흰 천 위에서는 어두운 픽셀이 곧 팔이다. 그리퍼의 붉은 조는 어둡지 않으므로
        dark 를 부풀린 영역 안의 붉은 픽셀을 그리퍼로 인정해 합친다.
        y<TABLE_Y0(머신 영역)은 검은 머신 본체와 구분이 안 되므로 제외한다.
        """
        i = int(np.clip(i, 0, self.n - 1))
        if i in self._arm:
            return self._arm[i]
        h = self.hsv(i)
        Hh = h[:, :, 0].astype(np.int16)
        S = h[:, :, 1].astype(np.int16)
        V = h[:, :, 2].astype(np.int16)
        dark = V < DARK_V
        dark[:TABLE_Y0] = False
        red = ((Hh >= 220) | (Hh <= 14)) & (S >= RED_S) & (V >= RED_V)
        red[:TABLE_Y0] = False
        m = dark | (red & _dilate(dark, GRIP_DILATE))
        return self._put(self._arm, i, m)

    def bg(self) -> np.ndarray:
        if self._bg is None:
            self._bg = self.median([0, 1, 2, 3, 4]).astype(np.float32)
        return self._bg

    def arm_full(self, i) -> np.ndarray:
        """머신 영역까지 이어붙인 팔 실루엣.

        머신 본체가 검어서 밝기 임계로는 팔을 못 가른다. 대신 (a) 테이블 영역의
        확실한 팔 마스크를 씨앗으로 (b) 배경 대비 변화 마스크 안에서만 번져 나가
        (grassfire) 팔의 연결된 부분만 취한다. 계산량을 위해 반해상도에서 돈다.
        머신 위 컵을 팔이 잡고 있으면 컵도 함께 흡수되는데, 그 프레임은 어차피
        '측정 불가(팔 침범)'로 처리되므로 문제가 되지 않는다.
        """
        i = int(np.clip(i, 0, self.n - 1))
        key = ("full", i)
        if key in self._arm:
            return self._arm[key]
        base = self.arm(i)
        cand = np.abs(self.rgb(i).astype(np.float32) - self.bg()).mean(2) > 30
        cand[:60] = False
        cand |= base
        b2, c2 = base[::2, ::2].copy(), cand[::2, ::2]
        cur = b2 & c2
        for _ in range(100):
            nxt = _dilate(cur, 1) & c2
            if nxt.sum() == cur.sum():
                break
            cur = nxt
        up = np.zeros_like(base)
        up[::2, ::2] = cur
        m = (_dilate(up, 2) & cand) | base
        return self._put(self._arm, key, m)

    def median(self, idxs) -> np.ndarray:
        idxs = [int(np.clip(i, 0, self.n - 1)) for i in idxs]
        return np.median(np.stack([self.rgb(i).astype(np.float32) for i in idxs]), 0)


def mask_gap(a: np.ndarray, b: np.ndarray, cap: float = 60.0) -> float:
    """두 마스크 윤곽 사이 최소 유클리드 거리(px). 겹치면 0."""
    if not a.any() or not b.any():
        return float(cap)
    if (a & b).any():
        return 0.0
    ay, ax = np.nonzero(_edge(a))
    by, bx = np.nonzero(_edge(b))
    if ax.size == 0 or bx.size == 0:
        return float(cap)
    if ax.size > 900:
        s = np.linspace(0, ax.size - 1, 900).astype(int); ax, ay = ax[s], ay[s]
    if bx.size > 1800:
        s = np.linspace(0, bx.size - 1, 1800).astype(int); bx, by = bx[s], by[s]
    d2 = (ax[:, None] - bx[None, :]) ** 2 + (ay[:, None] - by[None, :]) ** 2
    return round(float(min(math.sqrt(d2.min()), cap)), 2)


# ── ROI / 컵 검출 ───────────────────────────────────────────────────────────
def roi_box(name: str, pad: int = ROI_PAD):
    cx, cy, rx, ry = LANDMARKS[name]
    x0 = int(max(0, cx - rx - pad)); x1 = int(min(W, cx + rx + pad))
    y0 = int(max(0, cy - ry - pad)); y1 = int(min(H, cy + ry + pad))
    return x0, y0, x1, y1


def cup_mask(fr: Frames, i: int, ref: np.ndarray, box, armfn=None):
    """참조(빈 자리) 대비 달라진 픽셀 중 팔이 아닌 것 = 컵.

    ref 는 box 크기의 float32 RGB. 반환 (mask 640x480 bool, arm_touch bool).
    arm_touch = 팔이 컵 후보 영역을 침범해 측정이 오염됐는지.
    """
    x0, y0, x1, y1 = box
    cur = fr.rgb(i)[y0:y1, x0:x1].astype(np.float32)
    d = np.abs(cur - ref).mean(2) > DIFF_THR
    am = (armfn or fr.arm)(i)[y0:y1, x0:x1]
    d &= ~_dilate(am, 3)
    d = _erode(d, 2)          # 얇은 그림자/주름 제거
    d = _dilate(d, 3)
    # 팔이 컵 후보 자체에 붙어 있을 때만 '측정 오염'. 팔이 ROI 안에 있기만 한 것은
    # 오염이 아니다 (초기 구현의 버그 — 팔도 diff 마스크에 들어가므로 항상 참이 됐다).
    touch = bool((_dilate(d, 6) & am).any())
    out = np.zeros((H, W), bool)
    out[y0:y1, x0:x1] = d
    return out, touch


def centroid(mask: np.ndarray):
    ys, xs = np.nonzero(mask)
    if xs.size < MIN_CUP_PX:
        return None
    return float(xs.mean()), float(ys.mean()), int(xs.size)


def wobble_series(fr: Frames, box, frames, dt=2, thr=24, armpad=11, armfn=None):
    """컵 영역의 프레임간 변화율 — 팔이 가리지 않은 픽셀만으로 측정.

    컵이 가만히 있으면 센서 잡음 수준(≈0), 흔들리면 컵 윤곽 픽셀이 대거 바뀐다.
    중심점 추적과 달리 팔에 부분 가림이 있어도 편향되지 않는 것이 요점이다.
    (i, ratio|None) 리스트. ratio = 관측 가능 픽셀 중 변한 픽셀 비율.
    """
    x0, y0, x1, y1 = box
    out = []
    for i in frames:
        j = min(i + dt, fr.n - 1)
        if j <= i:
            continue
        a = fr.rgb(i)[y0:y1, x0:x1].astype(np.int16)
        b = fr.rgb(j)[y0:y1, x0:x1].astype(np.int16)
        af = armfn or fr.arm
        ok = (~_dilate(af(i), armpad)[y0:y1, x0:x1]
              & ~_dilate(af(j), armpad)[y0:y1, x0:x1])
        if ok.sum() < 400:
            out.append((i, None))
            continue
        ch = (np.abs(a - b).mean(2) > thr) & ok
        out.append((i, round(float(ch.sum()) / float(ok.sum()), 5)))
    return out


def cup_track(fr: Frames, ref, box, frames, armfn=None):
    """프레임별 컵 중심/면적/팔침범 여부. [(i, cx, cy, npx, touch), ...]"""
    out = []
    for i in frames:
        m, touch = cup_mask(fr, i, ref, box, armfn)
        c = centroid(m)
        if c is None:
            out.append((i, None, None, 0, touch))
        else:
            out.append((i, c[0], c[1], c[2], touch))
    return out


# ── 이벤트 앵커 (관절) ──────────────────────────────────────────────────────
def grip_events(g: np.ndarray) -> dict:
    """그리퍼 각도 시계열에서 개폐 전이 프레임을 히스테리시스로 뽑는다.

    절대 임계는 세션마다 다르므로 에피소드 내부 분위수로 정한다.
    반환 {"open_th","close_th","opens":[i,...],"closes":[i,...],"range":float}
    opens[k] = 닫힘→열림 전이가 확정된 프레임, closes[k] = 열림→닫힘.
    """
    g = np.asarray(g, float)
    lo, hi = float(np.percentile(g, 5)), float(np.percentile(g, 95))
    rng = hi - lo
    out = {"open_th": None, "close_th": None, "opens": [], "closes": [], "range": round(rng, 2)}
    if rng < 6.0 or g.size < 5:
        return out
    open_th = lo + 0.60 * rng
    close_th = lo + 0.30 * rng
    out["open_th"] = round(open_th, 2); out["close_th"] = round(close_th, 2)
    st = "closed" if g[0] < (lo + hi) / 2 else "open"
    for i in range(1, g.size):
        if st == "closed" and g[i] > open_th:
            out["opens"].append(i); st = "open"
        elif st == "open" and g[i] < close_th:
            out["closes"].append(i); st = "closed"
    return out


def release_frame(g: np.ndarray, end: int):
    """place: 컵을 놓는 프레임 = 잡고 있다가 처음 열리는 전이."""
    ev = grip_events(g[:end])
    return (ev["opens"][0] if ev["opens"] else None), ev


def grasp_frame(g: np.ndarray, end: int):
    """pick: 컵을 잡는 프레임 = 접근용으로 연 뒤 처음 닫히는 전이."""
    ev = grip_events(g[:end])
    if not ev["opens"]:
        return None, ev
    c = [i for i in ev["closes"] if i > ev["opens"][0]]
    return (c[0] if c else None), ev


# ── 공통 유틸 ───────────────────────────────────────────────────────────────
def _norm_off(cx, cy, name):
    """랜드마크 중심에서의 오프셋(px)과 반지름 정규화 값."""
    lx, ly, rx, ry = LANDMARKS[name]
    dx, dy = cx - lx, cy - ly
    px = math.hypot(dx, dy)
    nz = math.hypot(dx / rx, dy / ry)          # 1.0 = 원 경계
    return round(px, 1), round(nz, 3), round(dx, 1), round(dy, 1)


# ── place 마진 ──────────────────────────────────────────────────────────────
SEP_PX = 20.0        # 릴리즈 후 "팔이 컵에서 떨어졌다"고 볼 최소 간격
GAP_CAP = 60.0
TAMPER_PX = 14.0     # 팔이 멀리 있는데 컵이 이만큼 움직이면 사람 손 개입
TAMPER_GAP = 30.0    # "팔이 멀다"의 기준

# 테이블(흰 천) 위 목표는 밝기 임계 팔 마스크로 충분하고, 머신 트레이 위 목표는
# grassfire 로 이어붙인 팔 마스크가 필요하다.
TABLE_TARGETS = {"pink_circle", "blue_circle"}


def _armfn(fr: Frames, tgt: str):
    return fr.arm if tgt in TABLE_TARGETS else fr.arm_full


def arm_rest_frame(state: np.ndarray, end: int, after: int, quiet=0.15, hold=15):
    """after 이후 팔이 hold 프레임 연속 정지한 첫 시점(정지 시작 + hold).

    이 시점 이후에 씬이 바뀌면 그것은 로봇이 한 일이 아니다(운영자가 컵을 치우는 등).
    에피소드 길이를 지표로 쓰는 것이 아니라, 관측 창을 정책 자신의 행동 구간으로
    한정하기 위한 장치다.
    """
    v = np.linalg.norm(np.diff(state[:end, :M.N_ARM], axis=0), axis=1)
    run = 0
    for i in range(max(0, after), v.size):
        run = run + 1 if v[i] < quiet else 0
        if run >= hold:
            return int(i + 1)
    return int(end - 1)


def _cut_tamper(clean, gapmap):
    """사람 손 개입 지점에서 관측열을 자른다.

    컵이 크게 움직였는데 그 시점 근처에 팔이 없었다면 로봇이 한 일이 아니다.
    (실측: 운영자가 에피소드 말미에 손으로 컵을 치운다 — r09e8·r12e10·r01e11 등)
    반환 (잘라낸 관측열, 개입 프레임|None)
    """
    if len(clean) < 3:
        return clean, None
    ref = np.array([[c[1], c[2]] for c in clean[:3]]).mean(0)
    for k, (i, cx, cy, n) in enumerate(clean):
        if math.hypot(cx - ref[0], cy - ref[1]) > TAMPER_PX:
            near = [gv for j, gv in gapmap if abs(j - i) <= 8]
            if near and min(near) > TAMPER_GAP:
                return clean[:k], int(i)
        ref = 0.7 * ref + 0.3 * np.array([cx, cy])
    return clean, None


def place_margins(ep, fr, end, prof=None) -> dict:
    """place(2,5,8,10): 릴리즈 정확도 + 릴리즈 후 컵 교란 + 복귀 클리어런스.

    prof 가 dict 면 진단용 시계열(간격/컵중심/흔들림)을 담아준다.
    """
    step = ep["step"]
    tgt = STEP_TARGET[step]
    box = roi_box(tgt)
    x0, y0, x1, y1 = box
    af = _armfn(fr, tgt)
    g = ep["state"][:, 5]
    t_rel, ev = release_frame(g, end)
    out = {"t_release": t_rel, "n_grip_open": len(ev["opens"]),
           "n_grip_close": len(ev["closes"]), "grip_range": ev["range"],
           "target": tgt}
    if t_rel is None or fr.n == 0:
        out["error"] = "no release event"
        return out
    last = min(end, fr.n) - 1

    # 참조 = 에피소드 앞부분. 이때 목적지는 비어 있고 팔은 멀리 있다.
    ref = fr.median([0, 1, 2, 3, 4])[y0:y1, x0:x1]
    out["ref_clean"] = bool(not af(2)[y0:y1, x0:x1].any())

    # 관측 창: 릴리즈 ~ 팔이 완전히 멈춘 직후까지. 그 뒤는 운영자 개입 구간.
    rest = arm_rest_frame(ep["state"], end, t_rel)
    last = min(last, rest + 8)
    out["t_rest"] = int(rest)
    scan = list(range(t_rel, last + 1, 2))
    if not scan or scan[-1] != last:
        scan.append(last)
    tr = cup_track(fr, ref, box, scan, af)
    clean_all = [(i, cx, cy, n) for i, cx, cy, n, t in tr if cx is not None and not t]
    if not clean_all:
        out["error"] = "cup never observed arm-free after release"
        return out

    # 팔-컵 간격 시계열. 첫 팔-free 관측의 컵 마스크를 고정 기준으로 쓴다.
    i0 = clean_all[0][0]
    cm0, _ = cup_mask(fr, i0, ref, box, af)
    gaps = [(i, mask_gap(cm0, af(i), cap=GAP_CAP)) for i in scan]

    clean, t_tamper = _cut_tamper(clean_all, gaps)
    out["t_tamper"] = t_tamper
    if len(clean) < 2:
        out["error"] = "observation window too short after tamper cut"
        return out
    if prof is not None:
        prof.update(gaps=gaps, track=tr, clean=clean)

    # 1) 안착 위치 — 유효 관측 후반부의 중앙값(단일 프레임 잡음 회피)
    tailobs = clean[max(0, len(clean) - 8):]
    ex = float(np.median([c[1] for c in tailobs]))
    ey = float(np.median([c[2] for c in tailobs]))
    px, nz, dx, dy = _norm_off(ex, ey, tgt)
    out.update(place_off_px=px, place_off_norm=nz, place_dx=dx, place_dy=dy,
               cup_px_end=int(np.median([c[3] for c in tailobs])),
               cup_obs_end=int(clean[-1][0]))

    # 2) 릴리즈 후 컵 이동 — 첫 팔-free 관측(안착) 대비
    _, sx, sy, sn = clean[0]
    drifts = [math.hypot(cx - sx, cy - sy) for _, cx, cy, _ in clean]
    out["cup_settle_frame"] = int(i0)
    out["cup_shift_px"] = round(float(math.hypot(ex - sx, ey - sy)), 2)
    out["cup_maxdrift_px"] = round(float(max(drifts)), 2)
    areas = [n for _, _, _, n in clean]
    a0 = float(np.median(areas[:max(1, len(areas) // 3)]))
    out["cup_area_ratio"] = round(float(max(areas) / a0), 3) if a0 > 0 else None

    # 3) 분리 시점 / 복귀 클리어런스 / 되접근
    lim = t_tamper if t_tamper is not None else last + 1
    gaps_v = [(i, gv) for i, gv in gaps if i < lim]
    sep = next((i for i, gv in gaps_v if i >= i0 and gv >= SEP_PX), None)
    out["t_separate"] = sep
    out["linger_frames"] = (None if sep is None else int(sep - t_rel))
    if sep is not None:
        after = [(i, gv) for i, gv in gaps_v if i > sep]
        if after:
            gi, gmin = min(after, key=lambda z: z[1])
            out["retract_min_clear_px"] = round(float(gmin), 2)
            out["clear_frame"] = int(gi)
        seq = [gv for i, gv in gaps_v if i >= sep]
        peak, re = -1e9, 0
        for v_ in seq:
            peak = max(peak, v_)
            if peak - v_ > 8.0:
                re += 1
                peak = v_
        out["n_reapproach"] = int(re)

    # 4) 컵 흔들림 — 릴리즈 이후 팔 비가림 픽셀의 프레임간 변화율
    wob = wobble_series(fr, box, [i for i in range(t_rel, last, 2) if i < lim], armfn=af)
    if prof is not None:
        prof["wobble"] = wob
    wv = [w for _, w in wob if w is not None]
    if wv:
        out["wobble_max"] = round(float(max(wv)), 5)
        out["wobble_hot"] = int(sum(1 for w in wv if w > 0.02))
        tail = [w for i, w in wob if w is not None and sep is not None and i > sep]
        out["wobble_tail_max"] = round(float(max(tail)), 5) if tail else None
        out["wobble_tail_hot"] = int(sum(1 for w in tail if w > 0.02)) if tail else None

    # 5) 관절 기반 — 릴리즈 순간/최근접 순간의 팔 속도
    v = np.linalg.norm(np.diff(ep["state"][:end, :M.N_ARM], axis=0), axis=1)
    a, b = max(0, t_rel - 8), max(1, t_rel)
    out["release_speed"] = round(float(v[a:b].max()), 3) if b > a else None
    if out.get("clear_frame") is not None:
        gi = out["clear_frame"]
        lo_, hi_ = max(0, gi - 3), min(v.size, gi + 4)
        out["clear_speed"] = round(float(v[lo_:hi_].max()), 3) if hi_ > lo_ else None
    return out


# ── pick 마진 ───────────────────────────────────────────────────────────────
def pick_margins(ep, fr, end, prof=None) -> dict:
    """pick(1,4,7,9): 접근 중 컵 교란 + 파지 전 클리어런스 + 파지 후 미끄러짐."""
    step = ep["step"]
    tgt = STEP_TARGET[step]
    box = roi_box(tgt)
    x0, y0, x1, y1 = box
    af = _armfn(fr, tgt)
    g = ep["state"][:, 5]
    t_cl, ev = grasp_frame(g, end)
    out = {"t_grasp": t_cl, "n_grip_open": len(ev["opens"]),
           "n_grip_close": len(ev["closes"]), "grip_range": ev["range"],
           "target": tgt}
    if fr.n == 0:
        return out
    last = min(end, fr.n) - 1

    # 참조 = 에피소드 끝 (컵이 들려 나가 자리가 빈 상태)
    ref = fr.median([last, last - 1, last - 2])[y0:y1, x0:x1]
    out["ref_clean"] = bool(not af(last)[y0:y1, x0:x1].any())

    upto = t_cl if t_cl is not None else last
    scan = list(range(0, max(2, upto), 2))
    tr = cup_track(fr, ref, box, scan, af)
    clean = [(i, cx, cy, n) for i, cx, cy, n, t in tr if cx is not None and not t]
    if prof is not None:
        prof["track"] = tr
    if len(clean) >= 2:
        i0, sx, sy, sn = clean[0]
        d = [math.hypot(cx - sx, cy - sy) for _, cx, cy, _ in clean]
        out["approach_cup_shift_px"] = round(float(max(d)), 2)
        out["cup_start_off_px"], out["cup_start_off_norm"], _, _ = _norm_off(sx, sy, tgt)
        cm0, _ = cup_mask(fr, i0, ref, box, af)
        gaps = [(i, mask_gap(cm0, af(i), cap=GAP_CAP))
                for i in range(0, max(2, upto), 2)]
        if prof is not None:
            prof["gaps"] = gaps
        # 파지 직전까지 팔이 컵에 접근하는 것은 정상 — 대신 "언제부터" 붙었나가 마진
        touch = [i for i, gv in gaps if gv <= 2.0]
        out["t_first_touch"] = int(touch[0]) if touch else None
        if t_cl is not None and touch:
            out["touch_before_close"] = int(t_cl - touch[0])   # 클수록 오래 밀고 있었음
        # 접근 중 흔들림: 그리퍼가 처음 닿기 전까지 컵이 이미 움직였는가
        pre = [c for c in clean if touch and c[0] < touch[0]]
        if len(pre) >= 2:
            out["preTouch_cup_shift_px"] = round(
                float(max(math.hypot(c[1] - pre[0][1], c[2] - pre[0][2]) for c in pre)), 2)
        wob = wobble_series(fr, box, [i for i in range(0, max(2, upto), 2)], armfn=af)
        if prof is not None:
            prof["wobble"] = wob
        wv = [w for _, w in wob if w is not None]
        if wv:
            out["wobble_max"] = round(float(max(wv)), 5)
            out["wobble_hot"] = int(sum(1 for w in wv if w > 0.02))
    # 파지 품질: 닫힌 뒤 그리퍼가 더 닫히면 미끄러짐/얕은 파지
    if t_cl is not None and t_cl + 5 < end:
        seg = g[t_cl:end]
        gc = float(g[min(t_cl + 3, end - 1)])
        out["grip_at_close"] = round(gc, 2)
        out["grip_post_drift"] = round(gc - float(seg.min()), 2)
        out["grip_reopen"] = int(np.count_nonzero(np.diff(seg) > 1.5))
    out["regrasp"] = max(0, len(ev["opens"]) - 1)
    return out


# ── press 마진 ──────────────────────────────────────────────────────────────
def press_axis(state: np.ndarray) -> np.ndarray:
    """버튼을 향한 신전 깊이 대리치. elbow_flex 가 작을수록 깊게 뻗은 것이므로 부호 반전."""
    return -state[:, 2]


def press_margins(ep, fr, end, lit_frame, prof=None) -> dict:
    """press(3,6): 램프 점등 유지 + 점등 시점 대비 여유 깊이 + 접촉 위치 + 재접근."""
    step = ep["step"]
    tgt = STEP_TARGET[step]
    s = ep["state"][:end]
    depth = press_axis(s)
    out = {"first_lit": lit_frame}

    imax = int(np.argmax(depth))
    out["depth_max"] = round(float(depth[imax]), 2)
    out["depth_max_frame"] = imax

    # 접근 신전 횟수 (재시도 카운트): 깊이 곡선의 국소 최대 개수
    d = depth - depth.min()
    if d.max() > 1e-6:
        dn = d / d.max()
        peaks = 0
        i = 1
        while i < dn.size - 1:
            if dn[i] > 0.55 and dn[i] >= dn[i - 1] and dn[i] > dn[i + 1]:
                peaks += 1
                i += 8
            else:
                i += 1
        out["n_extension"] = int(peaks)

    if lit_frame is not None and lit_frame < end:
        out["depth_at_lit"] = round(float(depth[lit_frame]), 2)
        # 점등 시점 이후 얼마나 더 밀어붙였나 = 발동 여유
        out["depth_margin"] = round(float(depth[:end].max() - depth[lit_frame]), 2)
        # 점등 유지: 점등 후 끝까지 램프가 임계 위에 머문 비율
        out["lit_hold_frames"] = int(end - lit_frame)
        # 접촉 추정 → 점등 지연: 깊이가 최대의 90% 를 처음 넘은 시점부터
        thr = depth.min() + 0.90 * (depth.max() - depth.min())
        idx = np.flatnonzero(depth >= thr)
        if idx.size:
            out["contact_frame"] = int(idx[0])
            out["lit_latency"] = int(lit_frame - idx[0])

    # 접촉 위치: 최심 시점 전후 팔 실루엣의 apex(최상단) — 버튼 디스크 중심 대비
    fr_i = lit_frame if lit_frame is not None else imax
    apex = None
    for i in range(max(0, fr_i - 6), min(fr.n, fr_i + 7)):
        am = arm_reach_mask(fr, i)
        ys, xs = np.nonzero(am)
        if xs.size < 60:
            continue
        top = ys.min()
        sel = ys <= top + 3
        cand = (float(xs[sel].mean()), float(top))
        if apex is None or cand[1] < apex[1]:
            apex = cand
    if apex:
        out["apex_x"], out["apex_y"] = round(apex[0], 1), round(apex[1], 1)
        px, nz, dx, dy = _norm_off(apex[0], apex[1], tgt)
        out["press_off_px"], out["press_off_norm"] = px, nz
        out["press_dx"], out["press_dy"] = dx, dy

    # 램프 시계열이 있으면 점등 안정성(점등 후 최저값의 임계 대비 여유)
    return out


def arm_reach_mask(fr: Frames, i: int) -> np.ndarray:
    """머신 영역까지 포함한 팔 검출 — 에피소드 첫 프레임(배경) 대비 변화.

    머신 본체가 검어서 dark 임계로는 팔을 못 가른다. 대신 팔이 들어오면
    파란/초록 디스크와 밝은 트레이가 가려지므로 변화량으로 잡는다.
    """
    if fr._bg is None:
        fr._bg = fr.median([0, 1, 2, 3, 4]).astype(np.float32)
    d = np.abs(fr.rgb(i).astype(np.float32) - fr._bg).mean(2) > 34
    d[:60] = False          # 천장/벽
    d = _erode(d, 2)
    d = _dilate(d, 3)
    return d


# ── 에피소드 1건 ────────────────────────────────────────────────────────────
def compute_episode(run: str, ep_dir: str, step: int, lit_frame=None, prof=None) -> dict:
    ep = M.load_episode(BASE / run, ep_dir)
    ep["step"] = step
    end = M.stop_trim(ep["state"])
    fr = Frames(ep["frames_dir"])
    kind = STEP_KIND.get(step)
    base = {"run": run, "dir": ep_dir, "step": step, "kind": kind,
            "n": ep["n"], "end": int(end), "n_front": fr.n}
    try:
        if kind == "place":
            base.update(place_margins(ep, fr, end, prof))
        elif kind == "pick":
            base.update(pick_margins(ep, fr, end, prof))
        elif kind == "press":
            base.update(press_margins(ep, fr, end, lit_frame, prof))
    except Exception as exc:        # 개별 실패가 전체 배치를 죽이지 않게
        base["error"] = f"{type(exc).__name__}: {exc}"
    return base


# ── 배치 ────────────────────────────────────────────────────────────────────
def load_index() -> dict:
    return json.loads((CACHE / "index.json").read_text())


def run_all(steps=None, only=None, workers=6) -> list:
    from concurrent.futures import ProcessPoolExecutor
    idx = load_index()
    eps = [e for e in idx["episodes"] if e["step"] in STEP_KIND]
    if steps:
        eps = [e for e in eps if e["step"] in steps]
    if only:
        eps = [e for e in eps if (e["run"], e["ep"]) in only]
    args = [(e["run"], e["dir"], e["step"], e.get("lamp_lit_frame")) for e in eps]
    out = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for r, e in zip(pool.map(_worker, args), eps):
            r["ep"] = e["ep"]
            r["label"] = e["label"]
            r["deleted"] = e["deleted"]
            r["memo"] = e["memo"]
            out.append(r)
    return out


def _worker(a):
    return compute_episode(*a)


# ── z 정규화 / 집계 ────────────────────────────────────────────────────────
# 마진 방향: +1 = 값이 클수록 여유(좋음), -1 = 값이 클수록 아슬아슬(나쁨)
MARGIN_FIELDS = {
    "place": [("place_off_norm", -1), ("cup_shift_px", -1), ("cup_maxdrift_px", -1),
              ("retract_min_clear_px", +1), ("release_speed", -1)],
    "pick":  [("approach_cup_shift_px", -1), ("approach_min_clear_px", +1),
              ("grip_post_drift", -1), ("regrasp", -1)],
    "press": [("depth_margin", +1), ("press_off_norm", -1), ("n_extension", -1),
              ("lit_latency", -1)],
}


def zscore_table(rows: list) -> list:
    """스텝 유형별로 각 마진을 robust z (중앙값/MAD) 로 바꾸고 병목(min) 집계."""
    by_kind = {}
    for r in rows:
        by_kind.setdefault(r.get("kind"), []).append(r)
    for kind, rs in by_kind.items():
        fields = MARGIN_FIELDS.get(kind, [])
        for f, sgn in fields:
            v = np.array([r[f] for r in rs if isinstance(r.get(f), (int, float))], float)
            if v.size < 5:
                continue
            med = float(np.median(v))
            mad = float(np.median(np.abs(v - med))) * 1.4826
            if mad < 1e-9:
                mad = float(v.std()) or 1.0
            for r in rs:
                x = r.get(f)
                if isinstance(x, (int, float)):
                    r[f"z_{f}"] = round(sgn * (x - med) / mad, 3)
        for r in rs:
            zs = [r[f"z_{f}"] for f, _ in fields if f"z_{f}" in r]
            r["z_min"] = round(float(min(zs)), 3) if zs else None
            r["z_mean"] = round(float(np.mean(zs)), 3) if zs else None
    return rows


# ── CLI ─────────────────────────────────────────────────────────────────────
def main(argv):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--steps", type=str, default="")
    ap.add_argument("--ep", nargs=2, metavar=("RUN", "EP"))
    ap.add_argument("--out", default=str(CACHE / "margins.json"))
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args(argv)

    steps = [int(x) for x in a.steps.split(",")] if a.steps else None
    only = None
    if a.ep:
        only = {(a.ep[0], int(a.ep[1]))}
    rows = run_all(steps=steps, only=only, workers=a.workers)
    rows = zscore_table(rows)
    Path(a.out).write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    print(f"wrote {len(rows)} rows -> {a.out}")
    return rows


if __name__ == "__main__":
    main(sys.argv[1:])
