"""성공 에피소드 '안에서' 품질을 서열화하기 위한 키네매틱 지표 계산기.

성공/실패 판정기가 아니다. 이미 success 로 확정된 롤아웃들 사이에
연속 점수를 매겨 DPO 선호쌍 / quality-weighted BC 가중치로 쓰는 것이 목적이다.

입력
  /Users/gimminseo/kai_pj/intern_coffee/coffee_newNN/raw/epNNNN_cC_tT/steps.jsonl
  state/action 6축(deg), 30fps. 라벨은 run_meta.json 의 episode_update 최종값.

설계 규약 (전부 근거 있음, 어기면 지표가 무효가 된다)
  1. 정지 트림 필수 — metrics.stop_trim() 을 항상 적용한다. 다만 이 데이터에서
     트림은 사실상 발동하지 않는다(149개 중 137개가 고정분 10프레임만 잘림,
     rho(n_raw, n_used)=1.00). 이유: 임계 0.08 deg/frame 이 엔코더 양자화
     바닥(5관절 1카운트 지터 L2 = 0.196 deg/frame)보다 낮고, 실제로 마지막
     20프레임의 85%가 임계 위에서 움직이고 있다 = 운영자가 팔이 아직 움직이는
     중에 끊는다. 따라서 n_used 는 여전히 운영자 종료 시점을 그대로 담고 있고,
     n_used 와 상관이 큰 지표(LDLJ rho=-0.88, NoP +0.60)는 서열화에 쓸 수 없다.
     해결책은 트림 튜닝이 아니라 이벤트 앵커드 구간이다 —
       a_* : [에피소드 시작, 앵커) 접근 구간. 시작=task_start(자동),
             끝=정책이 만든 이벤트(grasp close / release open / 램프 first_lit)
             → 양끝이 모두 운영자와 무관하므로 duration 계열까지 유효.
       w_* : 앵커 ±(15,30)프레임 고정창. T가 상수라 LDLJ의 T^3 교란이 소거됨.
     t_anchor_s(=시작→앵커 시간)는 이 데이터에서 유일하게 정당한 duration 지표.
  2. 미분 필터 — 30fps 는 Nyquist 15Hz 뿐이고 STS3215 encoder 4096 counts
     (0.0879 deg/count) 때문에 1카운트 지터만으로 2.64 deg/s 속도 양자화 바닥이
     깔린다. 그래서
        * 속도/가속도/저크(LDLJ, jerk_p95, NoP, 반전율) = Savitzky-Golay
          (window=9, polyorder=3, zero-lag, 해석적 도함수).
          SG 컷오프 근사 f_c ≈ (p+1)/(3.2W-4.6)·fs = 4/(24.2)·30 ≈ 5.0 Hz
          → 문헌 표준(6Hz 2차 zero-lag Butterworth)과 같은 대역.
          np.diff 반복(유한차분)은 노이즈를 f^k 로 증폭하므로 쓰지 않는다.
        * SPARC = 사전 필터 없음. amp_th 기반 적응 컷오프가 내장 저역통과
          역할을 하므로 추가 필터는 이중 처리가 된다(Cornec 2024가 SPARC만
          명시적으로 예외 처리). 원 신호는 유한차분 속도를 쓴다.
          비교용으로 sparc_sg(=SG 평활 속도에 적용)도 같이 산출한다.
  3. 모든 지표는 스텝(에피소드) 단위로만 계산한다. 스텝을 이어붙인 통짜
     신호에 SPARC/LDLJ 를 걸면 dwell 길이에 값이 지배당한다.
  4. 측정 노이즈 하한 검증 — --noise 로 엔코더 양자화 노이즈(±0.5 count)를
     주입해 지표별 측정 SD 를 구한다. 성공 내 SD / 측정 SD < 3 이면 그 지표는
     서열화에 쓸 수 없다(순위가 사실상 난수).

사용법
  python3 quality_metrics.py compute     # cache/quality.json 생성 (149 success 포함 200개)
  python3 quality_metrics.py noise       # 엔코더 양자화 노이즈 기반 측정 SD → quality_noise.json
  python3 quality_metrics.py summary     # 지표별 CV·판별비 D·길이오염도 + 채택/기각 판정
  python3 quality_metrics.py report      # 스텝별 전체 분포표
  python3 quality_metrics.py corr        # 지표 간 Spearman (통합/t10/t3)
  python3 quality_metrics.py trimsens    # 트림 파라미터 민감도(순위 안정성)
  python3 quality_metrics.py topbot      # t10/t3 지표별 상·하위 3개 에피소드
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import metrics as M  # noqa: E402

BASE = Path("/Users/gimminseo/kai_pj/intern_coffee")
CACHE = Path(__file__).resolve().parent / "cache"
OUT = CACHE / "quality.json"
NOISE_OUT = CACHE / "quality_noise.json"

FPS = 30.0
DT = 1.0 / FPS
N_ARM = 5          # gripper 제외 (개폐는 계단형이라 스펙트럼을 오염시킨다)
N_REV = 4          # 반전율은 wrist_roll 제외 (회전 노이즈)

SG_WIN = 9
SG_POLY = 3

# STS3215: 4096 counts / 360deg
COUNT_DEG = 360.0 / 4096.0            # 0.0879 deg
VEL_QUANT = COUNT_DEG * FPS           # 2.64 deg/s — 속도 양자화 바닥

# gripper 개폐 히스테리시스 (deg). 실측 분포가 <6 / >25 로 이봉형.
GRIP_CLOSED = 8.0
GRIP_OPEN = 22.0

PICK_STEPS = {1, 4, 7, 9}
PLACE_STEPS = {2, 5, 8, 10}
PRESS_STEPS = {3, 6}

# 이벤트 앵커 창 (프레임). 릴리즈 직전 정렬 + 직후 복귀 초기를 함께 덮는다.
ANCHOR_PRE = 15
ANCHOR_POST = 30

IDLE_THRESH = M.TRIM_THRESH   # 0.08 — 정지 트림과 같은 임계 재사용
IDLE_WIN = M.TRIM_WIN         # 5


# ══════════════════════════════════════════════════════════════════════════
# 신호처리 (scipy 없이 numpy 만)
# ══════════════════════════════════════════════════════════════════════════
def _sg_coeffs(window: int, poly: int, deriv: int, delta: float) -> np.ndarray:
    half = window // 2
    x = np.arange(-half, half + 1, dtype=np.float64)
    A = np.vander(x, poly + 1, increasing=True)
    pinv = np.linalg.pinv(A)
    return pinv[deriv] * math.factorial(deriv) / (delta ** deriv)


def savgol(y: np.ndarray, window: int = SG_WIN, poly: int = SG_POLY,
           deriv: int = 0, delta: float = DT) -> np.ndarray:
    """zero-lag Savitzky-Golay 평활/해석적 미분. y: (n,) 또는 (n,k).

    가장자리는 scipy 의 mode='interp' 와 동일하게 첫/끝 window 구간에
    적합한 다항식을 그대로 외삽해 채운다(패딩으로 인한 인공 저크 방지).
    """
    y = np.asarray(y, dtype=np.float64)
    single = y.ndim == 1
    if single:
        y = y[:, None]
    n = y.shape[0]
    if n < window:                      # 짧으면 창을 홀수로 줄인다
        window = max(3, (n // 2) * 2 - 1)
        if window > n:
            window = n if n % 2 else n - 1
        if window < poly + 2:
            poly = max(1, window - 2)
    half = window // 2
    c = _sg_coeffs(window, poly, deriv, delta)
    out = np.empty_like(y)
    for j in range(y.shape[1]):
        core = np.convolve(y[:, j], c[::-1], mode="same")
        out[:, j] = core
    # 가장자리: 첫/끝 window 에 다항식 적합 후 해석적 도함수 평가
    x = np.arange(-half, half + 1, dtype=np.float64)
    A = np.vander(x, poly + 1, increasing=True)
    pinv = np.linalg.pinv(A)
    for side in (0, 1):
        seg = y[:window] if side == 0 else y[-window:]
        a = pinv @ seg                                    # (poly+1, k)
        idxs = range(0, half) if side == 0 else range(n - half, n)
        for i in idxs:
            xi = (i - half) if side == 0 else (i - (n - window) - half)
            val = np.zeros(y.shape[1])
            for k in range(deriv, poly + 1):
                val += a[k] * (math.factorial(k) / math.factorial(k - deriv)) * (xi ** (k - deriv))
            out[i] = val / (delta ** deriv)
    return out[:, 0] if single else out


def sparc(v: np.ndarray, fs: float = FPS, padlevel: int = 4,
          fc: float = 10.0, amp_th: float = 0.05):
    """Spectral arc length (Balasubramanian 2015 레퍼런스 구현 그대로).

    v: 속도(speed) 프로파일. 반드시 velocity 에만 적용할 것.
    값이 클수록(0에 가까울수록) 부드럽다.
    """
    v = np.asarray(v, dtype=np.float64)
    n = v.size
    if n < 8 or not np.isfinite(v).all() or np.allclose(v, 0):
        return None
    nfft = int(2 ** (math.ceil(math.log2(n)) + padlevel))
    f = np.arange(0, fs, fs / nfft)
    Mf = np.abs(np.fft.fft(v, nfft))
    mx = Mf.max()
    if mx <= 0:
        return None
    Mf = Mf / mx
    sel = np.where(f <= fc)[0]
    if sel.size < 3:
        return None
    f_sel, Mf_sel = f[sel], Mf[sel]
    inx = np.where(Mf_sel >= amp_th)[0]
    if inx.size < 2:
        return None
    lo, hi = inx[0], inx[-1] + 1
    f_sel, Mf_sel = f_sel[lo:hi], Mf_sel[lo:hi]
    span = f_sel[-1] - f_sel[0]
    if span <= 0:
        return None
    return float(-np.sum(np.sqrt((np.diff(f_sel) / span) ** 2 + np.diff(Mf_sel) ** 2)))


def ldlj(v: np.ndarray, fs: float = FPS, fixed_T: float | None = None):
    """log dimensionless jerk.  LDLJ = -ln( T^3/vpeak^2 · ∫ (d2v/dt2)^2 dt ).

    fixed_T 를 주면 T 를 데이터에서 읽지 않고 상수로 못박는다(FW-LDLJ).
    모든 시행이 같은 T 를 쓰므로 T^3 항이 공통 상수가 되어 서열에서 소거된다.
    """
    v = np.asarray(v, dtype=np.float64)
    n = v.size
    if n < SG_WIN or not np.isfinite(v).all():
        return None
    vpeak = float(np.abs(v).max())
    if vpeak <= 1e-9:
        return None
    T = (n / fs) if fixed_T is None else float(fixed_T)
    jv = savgol(v, deriv=2)                      # d2v/dt2  (deg/s^3)
    integ = float(np.sum(jv ** 2) * (1.0 / fs))
    if integ <= 0:
        return None
    dj = (T ** 3 / vpeak ** 2) * integ
    return float(-math.log(dj))


def count_peaks(v: np.ndarray, rel_h: float = 0.05, rel_prom: float = 0.05,
                min_dist: int = 5) -> int:
    """속도 프로파일의 submovement(속도 피크) 수.

    조건: (a) 높이 >= rel_h·vpeak, (b) 프로미넌스 >= rel_prom·vpeak
    (좌우 인접 골까지의 낙차 중 작은 쪽), (c) 피크 간 최소 min_dist 프레임.
    v 는 SG 평활된 것을 넣을 것 — raw 에 쓰면 노이즈 카운터가 된다.
    """
    v = np.asarray(v, dtype=np.float64)
    n = v.size
    if n < 5:
        return 0
    vpeak = float(v.max())
    if vpeak <= 1e-9:
        return 0
    hmin, pmin = rel_h * vpeak, rel_prom * vpeak
    cand = [i for i in range(1, n - 1) if v[i] >= v[i - 1] and v[i] > v[i + 1] and v[i] >= hmin]
    keep = []
    for i in cand:
        l = i
        while l > 0 and v[l - 1] <= v[l]:
            l -= 1
        r = i
        while r < n - 1 and v[r + 1] <= v[r]:
            r += 1
        prom = v[i] - max(v[l], v[r])
        if prom >= pmin:
            keep.append((v[i], i))
    keep.sort(reverse=True)
    sel: list[int] = []
    for _, i in keep:
        if all(abs(i - j) >= min_dist for j in sel):
            sel.append(i)
    return len(sel)


def hf_power_ratio(v: np.ndarray, fs: float = FPS, cut: float = 3.0) -> float | None:
    """속도 스펙트럼에서 cut Hz 초과 대역이 차지하는 파워 비율(떨림/채터링)."""
    v = np.asarray(v, dtype=np.float64)
    if v.size < 16:
        return None
    v = v - v.mean()
    n = v.size
    P = np.abs(np.fft.rfft(v * np.hanning(n))) ** 2
    f = np.fft.rfftfreq(n, d=1.0 / fs)
    tot = P[1:].sum()
    if tot <= 0:
        return None
    return float(P[1:][f[1:] > cut].sum() / tot)


def _trailing_mean(x, win):
    return M._trailing_mean(np.asarray(x, dtype=np.float64), win)


def spearman(a, b):
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < 4:
        return None
    ra, rb = _rank(a[ok]), _rank(b[ok])
    ra = ra - ra.mean()
    rb = rb - rb.mean()
    d = math.sqrt(float((ra ** 2).sum()) * float((rb ** 2).sum()))
    if d <= 0:
        return None
    return float((ra * rb).sum() / d)


def _rank(x):
    order = np.argsort(x, kind="mergesort")
    r = np.empty(x.size, dtype=np.float64)
    r[order] = np.arange(1, x.size + 1, dtype=np.float64)
    # 동점 평균 순위
    sx = x[order]
    i = 0
    while i < x.size:
        j = i
        while j + 1 < x.size and sx[j + 1] == sx[i]:
            j += 1
        if j > i:
            r[order[i:j + 1]] = (i + j + 2) / 2.0
        i = j + 1
    return r


# ══════════════════════════════════════════════════════════════════════════
# 이벤트 앵커
# ══════════════════════════════════════════════════════════════════════════
def grip_state(g: np.ndarray) -> np.ndarray:
    """히스테리시스로 gripper 를 0(closed)/1(open) 로 이산화."""
    s = np.zeros(g.size, dtype=np.int8)
    cur = 1 if g[0] > GRIP_OPEN else 0
    for i, v in enumerate(g):
        if cur == 0 and v > GRIP_OPEN:
            cur = 1
        elif cur == 1 and v < GRIP_CLOSED:
            cur = 0
        s[i] = cur
    return s


def anchor_frame(state: np.ndarray, step: int, lit_frame=None):
    """스텝 유형별 이벤트 앵커 프레임.

      pick  : 마지막 open→close 전이 (grasp close)
      place : 첫 close→open 전이 (release)
      press : 램프 first_lit (외부에서 받음)
    """
    if step in PRESS_STEPS:
        return (int(lit_frame), "first_lit") if lit_frame is not None else (None, None)
    g = grip_state(state[:, 5])
    d = np.diff(g.astype(np.int8))
    if step in PICK_STEPS:
        idx = np.where(d == -1)[0]
        return (int(idx[-1]) + 1, "grasp_close") if idx.size else (None, None)
    if step in PLACE_STEPS:
        idx = np.where(d == 1)[0]
        return (int(idx[0]) + 1, "release_open") if idx.size else (None, None)
    return (None, None)


# ══════════════════════════════════════════════════════════════════════════
# 에피소드 지표
# ══════════════════════════════════════════════════════════════════════════
def episode_metrics(state: np.ndarray, action=None, step: int = 0,
                    lit_frame=None, lag: int = 1, trim_extra=None) -> dict:
    """정지 트림 후 한 에피소드(=한 스텝)의 전 지표. 값은 JSON 직렬화 가능."""
    state = np.asarray(state, dtype=np.float64)
    n = int(state.shape[0])
    out: dict = {k: None for k in (
        "sparc", "sparc_sg", "ldlj", "nop", "nop_rate", "jerk_p50", "jerk_p95",
        "acc_p95_legacy", "trk_mean", "trk_p95", "rev_rate", "rev_rate_f",
        "idle_ratio", "idle_longest_s", "path_eff", "path_ratio", "path_len",
        "hf_ratio", "grip_events", "v_peak", "v_mean",
        "anchor_kind", "anchor_frame", "w_sparc", "w_jerk_p95", "w_ldlj_fw",
        "w_nop", "t_lit_s", "pre_lit_idle", "pre_lit_rev", "pre_lit_nop",
        "t_anchor_s", "a_sparc", "a_ldlj", "a_nop", "a_jerk_p95", "a_rev_f",
        "a_idle", "a_path_eff", "a_trk_mean")}
    out["n_used"] = 0
    out["trim"] = 0
    if n == 0:
        return out

    end = M.stop_trim(state) if trim_extra is None else M.stop_trim(state, extra=int(trim_extra))
    s = state[:end]
    out["n_used"] = int(end)
    out["trim"] = int(n - end)
    out["dur_used_s"] = round(end / FPS, 3)
    if end < 12:
        return out

    q = s[:, :N_ARM]

    # ── 속도 프로파일 두 갈래 ──────────────────────────────────────────
    vraw = np.linalg.norm(np.diff(q, axis=0), axis=1) * FPS      # 유한차분, SPARC 용
    dq = savgol(q, deriv=1)                                      # SG 해석적 1차 도함수
    vsg = np.linalg.norm(dq, axis=1)                             # 평활 속도

    out["v_peak"] = round(float(vsg.max()), 3)
    out["v_mean"] = round(float(vsg.mean()), 3)

    out["sparc"] = _r(sparc(vraw))
    out["sparc_sg"] = _r(sparc(vsg))
    out["ldlj"] = _r(ldlj(vsg))
    npk = count_peaks(vsg)
    out["nop"] = int(npk)
    out["nop_rate"] = round(npk * FPS / end, 4)

    # ── 저크 (SG deriv=3, deg/s^3) ────────────────────────────────────
    # 가장자리 half 프레임은 다항 외삽 오차가 커서 백분위에서 제외한다.
    j3 = _interior(np.linalg.norm(savgol(q, deriv=3), axis=1))
    out["jerk_p50"] = _r(float(np.percentile(j3, 50)))
    out["jerk_p95"] = _r(float(np.percentile(j3, 95)))
    # 기존 파이프라인 호환: 2차 차분 L2 (deg/frame^2) — 사실은 가속도
    a2 = np.linalg.norm(np.diff(q, n=2, axis=0), axis=1)
    out["acc_p95_legacy"] = _r(float(np.percentile(a2, 95)))

    # ── 추종오차 (action[i] vs state[i+lag]) ──────────────────────────
    if action is not None:
        a = np.asarray(action, dtype=np.float64)[:end]
        if a.shape[0] > lag:
            err = np.linalg.norm(a[:-lag] - s[lag:], axis=1)
            ok = np.isfinite(err)
            if ok.sum() > 4:
                out["trk_mean"] = _r(float(err[ok].mean()))
                out["trk_p95"] = _r(float(np.percentile(err[ok], 95)))

    # ── 반전율 ────────────────────────────────────────────────────────
    vfd = np.diff(q[:, :N_REV], axis=0)
    if vfd.shape[0] >= 2:
        sg = M._sign_ffill(vfd)
        chg = (sg[1:] != sg[:-1]) & (sg[1:] != 0) & (sg[:-1] != 0)
        out["rev_rate"] = _r(float(chg.sum()) * 100.0 / end)
    # 필터판: SG 속도 + 데드밴드(양자화 바닥의 1.5배) — 양자화 잡음 반전 제거
    dband = 1.5 * VEL_QUANT
    vs = dq[:, :N_REV].copy()
    vs[np.abs(vs) < dband] = 0.0
    sg2 = M._sign_ffill(vs)
    chg2 = (sg2[1:] != sg2[:-1]) & (sg2[1:] != 0) & (sg2[:-1] != 0)
    out["rev_rate_f"] = _r(float(chg2.sum()) * 100.0 / end)

    # ── 정지/머뭇거림 ─────────────────────────────────────────────────
    d1 = np.linalg.norm(np.diff(q, axis=0), axis=1)
    mov = _trailing_mean(d1, IDLE_WIN)
    quiet = mov < IDLE_THRESH
    out["idle_ratio"] = _r(float(quiet.mean()))
    out["idle_longest_s"] = _r(_longest_run(quiet) / FPS)

    # ── 경로 효율 ─────────────────────────────────────────────────────
    arc = float(d1.sum())
    out["path_len"] = _r(arc)
    straight = float(np.linalg.norm(q[-1] - q[0]))
    out["path_eff"] = _r(arc / straight) if straight > 1e-6 else None
    dev = float(np.linalg.norm(q - q[0], axis=1).max())
    out["path_ratio"] = _r(arc / dev) if dev > 1e-6 else None

    out["hf_ratio"] = _r(hf_power_ratio(vsg))

    dg = np.abs(np.diff(s[:, 5]))
    smg = _trailing_mean(dg, M.GRIP_WIN)
    hot = smg > M.GRIP_THRESH
    out["grip_events"] = int(np.count_nonzero(hot[1:] & ~hot[:-1]) + (1 if hot.size and hot[0] else 0))

    # ── 이벤트 앵커 창 지표 ───────────────────────────────────────────
    af, kind = anchor_frame(s, step, lit_frame)
    out["anchor_kind"] = kind
    out["anchor_frame"] = af
    if af is not None:
        lo, hi = max(0, af - ANCHOR_PRE), min(end, af + ANCHOR_POST)
        if hi - lo >= 20:
            wq = q[lo:hi]
            wvraw = np.linalg.norm(np.diff(wq, axis=0), axis=1) * FPS
            wvsg = np.linalg.norm(savgol(wq, deriv=1), axis=1)
            out["w_sparc"] = _r(sparc(wvraw))
            out["w_ldlj_fw"] = _r(ldlj(wvsg, fixed_T=(ANCHOR_PRE + ANCHOR_POST) / FPS))
            out["w_jerk_p95"] = _r(float(np.percentile(
                _interior(np.linalg.norm(savgol(wq, deriv=3), axis=1)), 95)))
            out["w_nop"] = int(count_peaks(wvsg))

        # approach phase [0, anchor) — 시작(task_start)도 끝(정책 이벤트)도
        # 운영자와 무관하므로 duration 계열까지 전부 유효해지는 유일한 구간.
        out["t_anchor_s"] = round(af / FPS, 3)
        if af >= 20:
            aq = q[:af]
            avraw = np.linalg.norm(np.diff(aq, axis=0), axis=1) * FPS
            advq = savgol(aq, deriv=1)
            avsg = np.linalg.norm(advq, axis=1)
            out["a_sparc"] = _r(sparc(avraw))
            out["a_ldlj"] = _r(ldlj(avsg))
            out["a_nop"] = int(count_peaks(avsg))
            out["a_jerk_p95"] = _r(float(np.percentile(
                _interior(np.linalg.norm(savgol(aq, deriv=3), axis=1)), 95)))
            av = advq[:, :N_REV].copy()
            av[np.abs(av) < dband] = 0.0
            asg = M._sign_ffill(av)
            achg = (asg[1:] != asg[:-1]) & (asg[1:] != 0) & (asg[:-1] != 0)
            out["a_rev_f"] = _r(float(achg.sum()) * 100.0 / af)
            ad1 = np.linalg.norm(np.diff(aq, axis=0), axis=1)
            out["a_idle"] = _r(float((_trailing_mean(ad1, IDLE_WIN) < IDLE_THRESH).mean()))
            astr = float(np.linalg.norm(aq[-1] - aq[0]))
            out["a_path_eff"] = _r(float(ad1.sum()) / astr) if astr > 1e-6 else None
            if action is not None:
                aa = np.asarray(action, dtype=np.float64)[:af]
                if aa.shape[0] > lag:
                    er = np.linalg.norm(aa[:-lag] - s[lag:af], axis=1)
                    er = er[np.isfinite(er)]
                    if er.size > 4:
                        out["a_trk_mean"] = _r(float(er.mean()))

    # ── press 전용 ────────────────────────────────────────────────────
    if step in PRESS_STEPS and lit_frame is not None:
        L = int(min(lit_frame, end))
        out["t_lit_s"] = round(L / FPS, 3)
        if L > 12:
            pq = q[:L]
            pd = np.linalg.norm(np.diff(pq, axis=0), axis=1)
            pquiet = _trailing_mean(pd, IDLE_WIN) < IDLE_THRESH
            out["pre_lit_idle"] = _r(float(pquiet.mean()))
            pv = savgol(pq, deriv=1)[:, :N_REV]
            pv[np.abs(pv) < dband] = 0.0
            psg = M._sign_ffill(pv)
            pchg = (psg[1:] != psg[:-1]) & (psg[1:] != 0) & (psg[:-1] != 0)
            out["pre_lit_rev"] = _r(float(pchg.sum()) * 100.0 / L)
            out["pre_lit_nop"] = int(count_peaks(np.linalg.norm(savgol(pq, deriv=1), axis=1)))
    return out


def _interior(x, pad: int = SG_WIN // 2):
    """SG 가장자리 외삽 구간을 잘라낸다. 너무 짧으면 원본 유지."""
    x = np.asarray(x)
    return x[pad:-pad] if x.size > 4 * pad else x


def _longest_run(mask) -> int:
    best = cur = 0
    for v in mask:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best


def _r(x, nd=4):
    if x is None:
        return None
    x = float(x)
    if not math.isfinite(x):
        return None
    return round(x, nd)


# ══════════════════════════════════════════════════════════════════════════
# 서보 지연 추정
# ══════════════════════════════════════════════════════════════════════════
def estimate_lag(max_lag: int = 6, limit: int = 60) -> int:
    """action[i] 와 state[i+k] 의 거리가 최소가 되는 k 를 데이터에서 추정."""
    tot = np.zeros(max_lag + 1)
    cnt = 0
    for run in sorted(BASE.glob("coffee_new*")):
        for d in sorted((run / "raw").glob("ep*")):
            if not (d / "episode.json").exists():
                continue
            e = M.load_episode(run, d.name)
            if e["action"] is None or e["n"] < 60:
                continue
            end = M.stop_trim(e["state"])
            a, s = e["action"][:end], e["state"][:end]
            for k in range(max_lag + 1):
                if end - k < 20:
                    continue
                err = np.linalg.norm(a[:end - k] - s[k:], axis=1)
                err = err[np.isfinite(err)]
                if err.size:
                    tot[k] += err.mean()
            cnt += 1
            if cnt >= limit:
                return int(np.argmin(tot))
    return int(np.argmin(tot)) if cnt else 1


# ══════════════════════════════════════════════════════════════════════════
# 전 에피소드 계산
# ══════════════════════════════════════════════════════════════════════════
def epname(run: str, ep: int) -> str:
    return f"r{int(run[-2:]):02d}e{ep}"


def compute_all(lag: int | None = None) -> dict:
    idx = json.loads((CACHE / "index.json").read_text())
    lit = {(e["run"], e["ep"]): e.get("lamp_lit_frame") for e in idx["episodes"]}
    if lag is None:
        lag = estimate_lag()
    rows = []
    for run_dir in sorted(BASE.glob("coffee_new*")):
        mp = run_dir / "run_meta.json"
        if not mp.exists():
            continue
        meta = json.loads(mp.read_text())
        labels = M.resolve_labels(meta)
        for d in sorted((run_dir / "raw").glob("ep*")):
            if not (d / "episode.json").exists():
                continue
            e = M.load_episode(run_dir, d.name)
            lab = labels.get(e["id"], {})
            m = episode_metrics(e["state"], e["action"], step=e["step"],
                                lit_frame=lit.get((run_dir.name, e["id"])), lag=lag)
            m.update({"run": run_dir.name, "ep": e["id"], "name": epname(run_dir.name, e["id"]),
                      "dir": d.name, "step": e["step"], "n_raw": e["n"],
                      "label": lab.get("label", "unlabeled"),
                      "deleted": bool(lab.get("deleted", False)),
                      "memo": lab.get("memo", "")})
            rows.append(m)
    return {"lag": int(lag), "fps": FPS, "sg_window": SG_WIN, "sg_poly": SG_POLY,
            "vel_quant_deg_s": round(VEL_QUANT, 4), "episodes": rows}


# ── 측정 노이즈 SD (엔코더 양자화 재주입) ────────────────────────────────
NOISE_KEYS = ["sparc", "ldlj", "nop", "jerk_p95", "acc_p95_legacy", "trk_mean",
              "trk_p95", "rev_rate", "rev_rate_f", "idle_ratio", "path_eff",
              "path_ratio", "hf_ratio", "t_anchor_s", "a_sparc", "a_ldlj",
              "a_nop", "a_jerk_p95", "a_rev_f", "a_idle", "a_path_eff",
              "a_trk_mean", "w_sparc", "w_jerk_p95", "w_ldlj_fw"]


def noise_floor(reps: int = 8, per_step: int = 6, lag: int = 1) -> dict:
    """각 지표에 엔코더 양자화 노이즈(±0.5 count 균등)를 주입했을 때의 측정 SD.

    성공 내 SD 를 이 값과 비교해 '스프레드가 진짜인가'를 판정한다.
    """
    idx = json.loads((CACHE / "index.json").read_text())
    lit = {(e["run"], e["ep"]): e.get("lamp_lit_frame") for e in idx["episodes"]}
    picked: dict[int, list] = {}
    for e in idx["episodes"]:
        if e["label"] != "success" or e["deleted"]:
            continue
        picked.setdefault(e["step"], [])
        if len(picked[e["step"]]) < per_step:
            picked[e["step"]].append(e)
    rng = np.random.default_rng(0)
    acc: dict[str, list] = {k: [] for k in NOISE_KEYS}
    for step, eps in picked.items():
        for e in eps:
            ep = M.load_episode(BASE / e["run"], e["dir"])
            vals: dict[str, list] = {k: [] for k in NOISE_KEYS}
            for _ in range(reps):
                st = ep["state"] + rng.uniform(-0.5, 0.5, ep["state"].shape) * COUNT_DEG
                ac = None if ep["action"] is None else ep["action"] + rng.uniform(-0.5, 0.5, ep["action"].shape) * COUNT_DEG
                m = episode_metrics(st, ac, step=e["step"],
                                    lit_frame=lit.get((e["run"], e["ep"])), lag=lag)
                for k in NOISE_KEYS:
                    if m.get(k) is not None:
                        vals[k].append(m[k])
            for k in NOISE_KEYS:
                if len(vals[k]) >= reps - 1:
                    acc[k].append(float(np.std(vals[k], ddof=1)))
    return {k: (round(float(np.mean(v)), 5) if v else None) for k, v in acc.items()}


# ══════════════════════════════════════════════════════════════════════════
# 리포트
# ══════════════════════════════════════════════════════════════════════════
METRIC_ORDER = ["sparc", "sparc_sg", "ldlj", "nop", "jerk_p95", "acc_p95_legacy",
                "trk_mean", "trk_p95", "rev_rate", "rev_rate_f", "idle_ratio",
                "idle_longest_s", "path_eff", "path_ratio", "hf_ratio",
                "t_anchor_s", "a_sparc", "a_ldlj", "a_nop", "a_jerk_p95",
                "a_rev_f", "a_idle", "a_path_eff", "a_trk_mean",
                "w_sparc", "w_jerk_p95", "w_ldlj_fw", "w_nop",
                "grip_events", "v_peak", "n_used"]
# 값이 클수록 좋은 지표(서열 방향). 나머지는 작을수록 좋다.
HIGHER_BETTER = {"sparc", "sparc_sg", "ldlj", "a_sparc", "a_ldlj", "w_sparc", "w_ldlj_fw"}


def load_success(path=OUT):
    d = json.loads(Path(path).read_text())
    eps = [e for e in d["episodes"] if e["label"] == "success" and not e["deleted"]]
    return d, eps


def dist_table(eps, noise=None):
    steps = sorted({e["step"] for e in eps})
    lines = []
    hdr = f"{'step':>4} {'metric':<15} {'n':>3} {'min':>9} {'p25':>9} {'med':>9} {'p75':>9} {'max':>9} {'mean':>9} {'sd':>8} {'CV%':>7} {'D':>6}"
    lines.append(hdr)
    for st in steps:
        sub = [e for e in eps if e["step"] == st]
        for k in METRIC_ORDER:
            v = np.array([e[k] for e in sub if e.get(k) is not None], dtype=np.float64)
            if v.size < 4:
                continue
            mean, sd = v.mean(), v.std(ddof=1)
            cv = abs(sd / mean) * 100 if abs(mean) > 1e-9 else float("nan")
            nz = (noise or {}).get(k)
            D = (sd / nz) if nz else float("nan")
            lines.append(f"{st:>4} {k:<15} {v.size:>3} {v.min():>9.3f} {np.percentile(v,25):>9.3f} "
                         f"{np.median(v):>9.3f} {np.percentile(v,75):>9.3f} {v.max():>9.3f} "
                         f"{mean:>9.3f} {sd:>8.3f} {cv:>7.1f} {D:>6.1f}")
    return "\n".join(lines)


def corr_table(eps, step, keys=None):
    sub = [e for e in eps if e["step"] == step]
    keys = keys or [k for k in METRIC_ORDER
                    if sum(1 for e in sub if e.get(k) is not None) >= max(6, len(sub) * 0.6)]
    m = {}
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            va = [e.get(a) for e in sub]
            vb = [e.get(b) for e in sub]
            pair = [(x, y) for x, y in zip(va, vb) if x is not None and y is not None]
            if len(pair) < 6:
                continue
            r = spearman([p[0] for p in pair], [p[1] for p in pair])
            if r is not None:
                m[(a, b)] = r
    return keys, m


def top_bottom(eps, step, k=3):
    sub = [e for e in eps if e["step"] == step]
    out = {}
    for met in METRIC_ORDER:
        vals = [(e[met], e["name"]) for e in sub if e.get(met) is not None]
        if len(vals) < 6:
            continue
        vals.sort()
        if met in HIGHER_BETTER:
            best = [n for _, n in vals[-k:]][::-1]
            worst = [n for _, n in vals[:k]]
            bv = [round(v, 3) for v, _ in vals[-k:]][::-1]
            wv = [round(v, 3) for v, _ in vals[:k]]
        else:
            best = [n for _, n in vals[:k]]
            worst = [n for _, n in vals[-k:]][::-1]
            bv = [round(v, 3) for v, _ in vals[:k]]
            wv = [round(v, 3) for v, _ in vals[-k:]][::-1]
        out[met] = {"best": list(zip(best, bv)), "worst": list(zip(worst, wv))}
    return out


def trim_sensitivity(extras=(0, 10, 25, 40), lag: int = 4):
    """정지 트림 강도를 바꿔가며 지표 '순위'가 얼마나 흔들리는지 본다.

    운영자 수동 종료 = 꼬리 길이가 자의적이라는 뜻이므로, 트림 파라미터를
    바꿨을 때 순위가 무너지는 지표는 그 자의성을 그대로 상속한다.
    기준(extra=10) 대비 스텝별 Spearman 의 중앙값을 지표별로 돌려준다.
    """
    idx = json.loads((CACHE / "index.json").read_text())
    lit = {(e["run"], e["ep"]): e.get("lamp_lit_frame") for e in idx["episodes"]}
    succ = [e for e in idx["episodes"] if e["label"] == "success" and not e["deleted"]]
    vals = {x: {} for x in extras}
    for e in succ:
        ep = M.load_episode(BASE / e["run"], e["dir"])
        for x in extras:
            m = episode_metrics(ep["state"], ep["action"], step=e["step"],
                                lit_frame=lit.get((e["run"], e["ep"])), lag=lag, trim_extra=x)
            vals[x][(e["run"], e["ep"])] = m
    base = vals[extras[1] if len(extras) > 1 else extras[0]]
    keys = [k for k in CORE if k != "n_used"] + ["n_used"]
    out = {}
    for k in keys:
        row = {}
        for x in extras:
            rs = []
            for st in sorted({e["step"] for e in succ}):
                ids = [(e["run"], e["ep"]) for e in succ if e["step"] == st]
                a = [base[i].get(k) for i in ids]
                b = [vals[x][i].get(k) for i in ids]
                pair = [(p, q) for p, q in zip(a, b) if p is not None and q is not None]
                if len(pair) >= 8:
                    rs.append(spearman([p[0] for p in pair], [p[1] for p in pair]))
            rs = [r for r in rs if r is not None]
            row[x] = round(float(np.median(rs)), 3) if rs else None
        out[k] = row
    return out, extras


CORE = ["sparc", "ldlj", "nop", "jerk_p95", "trk_mean", "trk_p95", "rev_rate_f",
        "idle_ratio", "path_eff", "hf_ratio",
        "t_anchor_s", "a_sparc", "a_ldlj", "a_nop", "a_jerk_p95", "a_idle",
        "w_sparc", "w_jerk_p95", "w_ldlj_fw", "v_peak", "n_used"]


def _fisher_mean(rs):
    rs = [r for r in rs if r is not None and abs(r) < 0.9999]
    if not rs:
        return None
    z = np.mean([np.arctanh(r) for r in rs])
    return float(np.tanh(z))


def pooled_corr(eps, keys=CORE):
    """스텝별 Spearman 을 Fisher-z 평균한 통합 상관행렬."""
    steps = sorted({e["step"] for e in eps})
    out = {}
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            rs = []
            for st in steps:
                sub = [e for e in eps if e["step"] == st]
                pair = [(e.get(a), e.get(b)) for e in sub
                        if e.get(a) is not None and e.get(b) is not None]
                if len(pair) >= 8:
                    rs.append(spearman([p[0] for p in pair], [p[1] for p in pair]))
            out[(a, b)] = (_fisher_mean(rs), len(rs))
    return out


def print_matrix(keys, cell):
    w = max(len(k) for k in keys) + 1
    print(" " * w + "".join(f"{k[:7]:>8}" for k in keys))
    for a in keys:
        row = f"{a:<{w}}"
        for b in keys:
            if a == b:
                row += f"{'.':>8}"
            else:
                v = cell(a, b)
                row += f"{v:>8.2f}" if v is not None else f"{'-':>8}"
        print(row)


# 축 대표 지표 1개씩 (중복 제거). 부호는 '클수록 좋은 성공' 으로 통일.
AXES = {
    "smooth_approach": ("a_sparc", +1),      # 접근 구간 스펙트럼 부드러움
    "smooth_event":    ("w_sparc", +1),      # 앵커(파지/릴리즈/점등) 창 부드러움
    "aggression":      ("a_jerk_p95", -1),   # 급가감속
    "chatter":         ("hf_ratio", -1),     # >3Hz 떨림
    "hesitation":      ("a_idle", -1),       # 정체 비율
    "economy":         ("a_path_eff", -1),   # 접근 경로 낭비
    "submovement":     ("a_nop", -1),        # 재조준 횟수
}


def borda(eps, step, axes=AXES):
    """축별 순위를 z-정규화해 등가중 합산한 시드 서열 (사람 라벨 0개).

    이 값을 GUI 초기 정렬 / 능동 쌍선택의 시드로 쓰고, 나중에 사람 BT 점수와의
    Kendall tau 로 자동지표 세트 자체의 유효성을 검증한다.
    """
    sub = [e for e in eps if e["step"] == step]
    if len(sub) < 4:
        return []
    Z = np.zeros(len(sub))
    used = 0
    for k, sgn in axes.values():
        v = np.array([e.get(k) if e.get(k) is not None else np.nan for e in sub], dtype=np.float64)
        if not np.isfinite(v).all():
            continue
        r = _rank(v) * sgn
        Z += (r - r.mean()) / r.std(ddof=1)
        used += 1
    Z /= max(1, used)
    return sorted(zip([e["name"] for e in sub], [round(float(z), 3) for z in Z]),
                  key=lambda t: -t[1])


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "compute"
    CACHE.mkdir(exist_ok=True)
    if cmd == "compute":
        d = compute_all()
        OUT.write_text(json.dumps(d, ensure_ascii=False))
        ok = [e for e in d["episodes"] if e["label"] == "success" and not e["deleted"]]
        print(f"lag={d['lag']}  episodes={len(d['episodes'])}  success={len(ok)} -> {OUT}")
    elif cmd == "noise":
        lag = json.loads(OUT.read_text())["lag"] if OUT.exists() else 4
        nz = noise_floor(lag=lag)
        NOISE_OUT.write_text(json.dumps(nz, ensure_ascii=False, indent=1))
        print(json.dumps(nz, ensure_ascii=False, indent=1))
    elif cmd == "report":
        _, eps = load_success()
        nz = json.loads(NOISE_OUT.read_text()) if NOISE_OUT.exists() else None
        print(dist_table(eps, nz))
    elif cmd == "corr":
        _, eps = load_success()
        pc = pooled_corr(eps)
        print("=== 10스텝 Fisher-z 평균 Spearman (성공만) ===")
        print_matrix(CORE, lambda a, b: pc.get((a, b), pc.get((b, a), (None, 0)))[0])
        for st in (10, 3):
            print(f"\n=== step t{st} Spearman ===")
            keys, m = corr_table(eps, st, CORE)
            print_matrix(keys, lambda a, b: m.get((a, b), m.get((b, a))))
    elif cmd == "summary":
        _, eps = load_success()
        nz = json.loads(NOISE_OUT.read_text()) if NOISE_OUT.exists() else {}
        steps = sorted({e["step"] for e in eps})
        print(f"{'metric':<15}{'CVmed%':>8}{'CVrange':>14}{'D_med':>7}{'D_min':>7}{'|r|n_used':>11}  verdict")
        for k in METRIC_ORDER:
            cvs, Ds, rs = [], [], []
            for st in steps:
                sub = [e for e in eps if e["step"] == st]
                v = np.array([e[k] for e in sub if e.get(k) is not None], dtype=np.float64)
                if v.size < 6:
                    continue
                m, sd = v.mean(), v.std(ddof=1)
                if abs(m) > 1e-9:
                    cvs.append(100 * abs(sd / m))
                if nz.get(k):
                    Ds.append(sd / nz[k])
                pr = [(e.get(k), e.get("n_used")) for e in sub if e.get(k) is not None]
                r = spearman([p[0] for p in pr], [p[1] for p in pr])
                if r is not None:
                    rs.append(abs(r))
            if not cvs:
                continue
            Dm = min(Ds) if Ds else float("nan")
            rr = float(np.median(rs))
            Dmed = float(np.median(Ds)) if Ds else float("nan")
            if Ds and Dmed < 3:
                vd = "REJECT(측정노이즈)"
            elif Ds and Dm < 3:
                vd = f"PARTIAL(일부 스텝 D<3) r={rr:.2f}"
            elif rr >= 0.6:
                vd = "REJECT(길이 대리변수)"
            elif rr >= 0.4:
                vd = "CAUTION(길이 오염)"
            else:
                vd = "OK"
            print(f"{k:<15}{np.median(cvs):>8.1f}{f'{min(cvs):.0f}-{max(cvs):.0f}':>14}"
                  f"{(np.median(Ds) if Ds else float('nan')):>7.1f}{Dm:>7.1f}{rr:>11.2f}  {vd}")
    elif cmd == "trimsens":
        lag = json.loads(OUT.read_text())["lag"] if OUT.exists() else 4
        res, extras = trim_sensitivity(lag=lag)
        print("트림 extra 프레임을 바꿨을 때 기준(extra=10) 대비 순위 Spearman (스텝별 중앙값)")
        print(f"{'metric':<14}" + "".join(f"{'e='+str(x):>9}" for x in extras))
        for k, row in res.items():
            print(f"{k:<14}" + "".join(f"{(row[x] if row[x] is not None else float('nan')):>9.3f}" for x in extras))
    elif cmd == "borda":
        _, eps = load_success()
        for st in sorted({e["step"] for e in eps}):
            r = borda(eps, st)
            print(f"t{st} (n={len(r)}): " + ", ".join(f"{n}({z:+.2f})" for n, z in r))
    elif cmd == "topbot":
        _, eps = load_success()
        for st in (10, 3):
            print(f"\n=== t{st}  n={sum(1 for e in eps if e['step']==st)} ===")
            tb = top_bottom(eps, st)
            for k, v in tb.items():
                b = ", ".join(f"{n}({x})" for n, x in v["best"])
                w = ", ".join(f"{n}({x})" for n, x in v["worst"])
                print(f"{k:<15} BEST: {b:<46} WORST: {w}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
