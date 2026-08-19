"""
rollout_viewer 데이터층 — 원본 읽기 / 라벨·트림·메트릭·램프 / 에피소드·인덱스 / 채점.

- 표준 라이브러리만 사용. metrics.py 가 있으면 그쪽 구현을 우선 사용하고,
  없거나 호출에 실패하면 내장 폴백 구현으로 계속 동작한다(서버가 죽지 않는다).
- 계약(API 스펙)은 프론트(index.html + static/)와 공유. 응답 형태를 바꾸지 말 것.
"""

from __future__ import annotations

import csv
import glob
import inspect
import json
import math
import os
import re
import sys
import tempfile
import threading
import time
import traceback

# --------------------------------------------------------------------------
# 경로 상수
# --------------------------------------------------------------------------
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = "/Users/gimminseo/kai_pj/intern_coffee"
MERGED_LABELS_CSV = os.path.join(
    DATA_ROOT, "merged", "so101_coffee_rollouts", "episode_labels.csv"
)

CACHE_DIR = os.path.join(ROOT, "cache")
EP_CACHE_DIR = os.path.join(CACHE_DIR, "ep")
LAMP_CACHE_DIR = os.path.join(CACHE_DIR, "lamp")
CLIP_CACHE_DIR = os.path.join(CACHE_DIR, "clips")
INDEX_CACHE = os.path.join(CACHE_DIR, "index.json")           # build_index 산출물
INDEX_FALLBACK_CACHE = os.path.join(CACHE_DIR, "index.fallback.json")  # 내장 폴백 산출물
SCORES_PATH = os.path.join(ROOT, "scores.json")

FPS = 30.0
ARM = 6                       # 총 관절 수
ARM_JOINTS = 5                # 앞 5관절(그리퍼 제외)
TRIM_MA_WIN = 5
TRIM_THR = 0.08
TRIM_EXTRA = 10

# 램프 ROI: (x0, y0, x1, y1) + 임계값
LAMP_ROI = {"m1": (58, 48, 102, 98), "m2": (398, 42, 442, 92)}
LAMP_THR = {"m1": 113.0, "m2": 162.0}
LAMP_STRIDE = 3               # 매 3프레임 샘플링 후 선형보간
PRESS_STEPS = {3: "m1", 6: "m2"}   # press 태스크 → 판정 대상 머신

EID_RE = re.compile(r"^(?P<run>[A-Za-z0-9_\-]+)/(?:ep)?(?P<ep>\d+)$")
RUN_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
# build_index.EXCLUDE_RUNS 의 사본 — 폴백 인덱스·eid 해석에서도 같은 런을 숨긴다.
EXCLUDE_RUNS = {"NorRec_R__white"}

# 실험(Experiment) / Try 규칙 — build_index.EXPERIMENT_RULES 와 같은 규칙의 사본(폴백용).
# 정본은 cache/index.json 의 experiment / try_no 필드. 여기서는 그 필드가 비어 있을 때만 채운다.
EXPERIMENT_RULES = [(re.compile(r"^coffee_new\d+$"), "NorRec_RW___Red", "dirnum")]


def experiment_of(run: str) -> str:
    for rx, name, _mode in EXPERIMENT_RULES:
        if rx.match(run or ""):
            return name
    return run


def try_no_of(run: str, cycle) -> int:
    for rx, _name, mode in EXPERIMENT_RULES:
        if rx.match(run or "") and mode == "dirnum":
            m = re.search(r"(\d+)$", run)
            if m:
                return int(m.group(1))
    try:
        return int(cycle or 0)
    except (TypeError, ValueError):
        return 0

_STDERR_LOCK = threading.Lock()


def log(*parts: object) -> None:
    with _STDERR_LOCK:
        print("[rollout_viewer]", *parts, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# JSON 유틸 (NaN/Inf/numpy 제거 + 소수 자리 축소)
# --------------------------------------------------------------------------
def _sanitize(obj, nd: int = 5):
    """JSON 직렬화 가능한 순수 파이썬 구조로 변환. NaN/Inf → None."""
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        if not math.isfinite(obj):
            return None
        return round(obj, nd)
    if isinstance(obj, dict):
        return {str(k): _sanitize(v, nd) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v, nd) for v in obj]
    # numpy 등
    item = getattr(obj, "item", None)
    if callable(item) and getattr(obj, "shape", None) == ():
        return _sanitize(obj.item(), nd)
    tolist = getattr(obj, "tolist", None)
    if callable(tolist):
        return _sanitize(tolist(), nd)
    if isinstance(obj, (set, frozenset)):
        return [_sanitize(v, nd) for v in obj]
    return str(obj)


def dumps(obj) -> bytes:
    return json.dumps(
        _sanitize(obj), ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")


def atomic_write(path: str, data: bytes) -> None:
    """임시파일 + rename 원자적 쓰기."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: str):
    with open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8"))


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


# 데이터층/서버 코드가 바뀌면 계산 캐시를 자동 무효화한다(동시 개발 중 stale 방지).
CODE_MTIME = max(
    _mtime(os.path.join(ROOT, name))
    for name in ("metrics.py", "build_index.py", "server.py",
                 "viewer_data.py", "viewer_clips.py", "viewer_http.py")
)


def cache_fresh(path: str) -> bool:
    mt = _mtime(path)
    return mt > 0 and mt >= CODE_MTIME


# --------------------------------------------------------------------------
# metrics.py 브리지
#   데이터층이 동시 작성 중이라 시그니처를 확정할 수 없다.
#   → 파라미터 이름 매칭 + 위치인자 후보를 순서대로 시도하고, 전부 실패하면 폴백.
# --------------------------------------------------------------------------
class MetricsBridge:
    NEEDED = (
        "load_episode",
        "stop_trim",
        "compute_metrics",
        "lamp_series",
        "lamp_summary",
        "resolve_labels",
    )

    def __init__(self) -> None:
        self.mod = None
        self.import_error: str | None = None
        self.missing: list[str] = []
        self._warned: set[str] = set()
        self._lock = threading.Lock()

    def load(self) -> None:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        try:
            import metrics  # type: ignore
            self.mod = metrics
            self.missing = [n for n in self.NEEDED if not callable(getattr(metrics, n, None))]
            if self.missing:
                log("metrics.py 로드됨. 미구현/비호출가능 함수:", ", ".join(self.missing),
                    "→ 해당 항목은 내장 폴백 사용")
            else:
                log("metrics.py 로드 완료 (모든 함수 사용 가능)")
        except Exception as exc:
            self.import_error = (
                "metrics.py 를 import 할 수 없습니다 (%s: %s). "
                "%s 에 metrics.py 가 있어야 데이터층 구현이 사용됩니다. "
                "지금은 server.py 내장 폴백 구현으로 동작합니다."
                % (type(exc).__name__, exc, ROOT)
            )
            log(self.import_error)
            log(traceback.format_exc().rstrip())

    def status(self) -> dict:
        return {
            "loaded": self.mod is not None,
            "import_error": self.import_error,
            "missing_functions": list(self.missing),
            "fallbacks_used": sorted(self._warned),
        }

    def fn(self, name: str):
        if self.mod is None:
            return None
        f = getattr(self.mod, name, None)
        return f if callable(f) else None

    def _warn(self, name: str, exc: BaseException) -> None:
        with self._lock:
            first = name not in self._warned
            self._warned.add(name)
        if first:
            log("metrics.%s 호출 실패 → 내장 폴백 사용: %s: %s"
                % (name, type(exc).__name__, exc))
            log(traceback.format_exc().rstrip())

    @staticmethod
    def _bind_ok(sig: inspect.Signature, args: tuple) -> bool:
        try:
            sig.bind(*args)
            return True
        except (TypeError, ValueError):
            return False

    def _invoke(self, f, ctx: dict, candidates: tuple):
        sig = inspect.signature(f)
        params = list(sig.parameters.values())
        has_var_pos = any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params)
        named = [
            p for p in params
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                          inspect.Parameter.POSITIONAL_OR_KEYWORD,
                          inspect.Parameter.KEYWORD_ONLY)
        ]
        # 1) 파라미터 이름으로 매칭
        if named and not has_var_pos:
            pos: list = []
            kw: dict = {}
            ok = True
            for p in named:
                if p.kind is inspect.Parameter.POSITIONAL_ONLY:
                    if p.name in ctx:
                        pos.append(ctx[p.name])
                    elif p.default is not inspect.Parameter.empty:
                        pos.append(p.default)
                    else:
                        ok = False
                        break
                else:
                    if p.name in ctx:
                        kw[p.name] = ctx[p.name]
                    elif p.default is inspect.Parameter.empty:
                        ok = False
                        break
            if ok:
                return f(*pos, **kw)
        # 2) 위치인자 후보를 순서대로
        for args in candidates:
            if self._bind_ok(sig, args):
                return f(*args)
        raise TypeError(
            "metrics.%s 시그니처 %s 에 맞는 인자 조합을 찾지 못했습니다"
            % (getattr(f, "__name__", "?"), sig)
        )

    def call(self, name: str, ctx: dict, candidates: tuple, fallback):
        """metrics.<name> 을 시도하고 실패하면 fallback()."""
        f = self.fn(name)
        if f is not None:
            try:
                out = self._invoke(f, ctx, candidates)
                if out is not None:
                    return out, True
            except Exception as exc:
                self._warn(name, exc)
        return fallback(), False


MB = MetricsBridge()


# --------------------------------------------------------------------------
# 원본 데이터 접근
# --------------------------------------------------------------------------
_RUNS_CACHE: list[str] | None = None
_EPDIR_CACHE: dict[str, dict[int, str]] = {}
_EPDIR_LOCK = threading.Lock()


def list_runs() -> list[str]:
    global _RUNS_CACHE
    if _RUNS_CACHE is None:
        runs = []
        for name in sorted(os.listdir(DATA_ROOT)):
            p = os.path.join(DATA_ROOT, name)
            if (os.path.isdir(os.path.join(p, "raw")) and RUN_RE.match(name)
                    and name not in EXCLUDE_RUNS):
                runs.append(name)
        _RUNS_CACHE = runs
    return _RUNS_CACHE


def ep_dirs(run: str) -> dict[int, str]:
    """run 의 {episode_id: 절대경로} 매핑."""
    with _EPDIR_LOCK:
        cached = _EPDIR_CACHE.get(run)
    if cached is not None:
        return cached
    out: dict[int, str] = {}
    for p in sorted(glob.glob(os.path.join(DATA_ROOT, run, "raw", "ep*"))):
        if not os.path.isdir(p):
            continue
        m = re.match(r"^ep(\d+)", os.path.basename(p))
        if m:
            out[int(m.group(1))] = p
    with _EPDIR_LOCK:
        _EPDIR_CACHE[run] = out
    return out


class BadRequest(Exception):
    pass


class NotFound(Exception):
    pass


def parse_eid(eid: str) -> tuple[str, int]:
    if not eid:
        raise BadRequest("eid 파라미터가 필요합니다")
    m = EID_RE.match(eid.strip())
    if not m:
        raise BadRequest("eid 형식이 잘못됨: %r (예: coffee_new01/ep0002)" % eid)
    run = m.group("run")
    ep = int(m.group("ep"))
    if run not in list_runs():
        # 서버 기동 후 새 런이 추가됐을 수 있다 — 한 번 다시 스캔
        global _RUNS_CACHE
        _RUNS_CACHE = None
        if run not in list_runs():
            raise NotFound("알 수 없는 run: %s" % run)
    if ep not in ep_dirs(run):
        with _EPDIR_LOCK:
            _EPDIR_CACHE.pop(run, None)       # 런 안에 에피소드가 추가됐을 수 있다
        if ep not in ep_dirs(run):
            raise NotFound("에피소드 없음: %s/ep%04d" % (run, ep))
    return run, ep


def make_eid(run: str, ep: int) -> str:
    return "%s/ep%04d" % (run, ep)


def ep_dir(run: str, ep: int) -> str:
    d = ep_dirs(run).get(ep)
    if d is None:
        raise NotFound("에피소드 디렉터리 없음: %s/%s" % (run, ep))
    return d


def cache_key(run: str, ep: int) -> str:
    return "%s_%04d" % (run, ep)


def load_run_meta(run: str) -> dict:
    p = os.path.join(DATA_ROOT, run, "run_meta.json")
    try:
        return read_json(p)
    except Exception as exc:
        log("run_meta 로드 실패", p, exc)
        return {}


def load_ep_meta(run: str, ep: int) -> dict:
    return read_json(os.path.join(ep_dir(run, ep), "episode.json"))


def load_steps(run: str, ep: int) -> dict:
    """steps.jsonl 파싱 → {t, state, action, n_frames, frame_ids}"""
    path = os.path.join(ep_dir(run, ep), "steps.jsonl")
    ts: list[float] = []
    state: list[list[float]] = []
    action: list = []
    frames: list[int] = []
    t0 = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if t0 is None:
                t0 = r.get("ts", 0.0)
            ts.append(float(r.get("ts", 0.0)) - float(t0 or 0.0))
            st = r.get("state") or []
            state.append([float(v) for v in st])
            ac = r.get("action")
            action.append([float(v) for v in ac] if ac else None)
            frames.append(int(r.get("f", len(frames))))
    return {
        "t": ts,
        "state": state,
        "action": action,
        "n_frames": len(state),
        "frame_ids": frames,
    }


# --------------------------------------------------------------------------
# 라벨 확정 (폴백 구현)
#   규칙: 같은 에피소드의 episode_update 중 label 이 non-null 인 마지막 것이 최종.
#         그런 게 없으면 run 에 bulk_label 이 있으면 success, 없으면 unlabeled.
#         memo/deleted 는 마지막 episode_update 값을 사용.
# --------------------------------------------------------------------------
_LABEL_CACHE: dict[str, dict[int, dict]] = {}
_LABEL_LOCK = threading.Lock()


def _fallback_resolve_labels(run: str) -> dict[int, dict]:
    meta = load_run_meta(run)
    events = meta.get("events") or []
    has_bulk = any(e.get("type") == "bulk_label" for e in events)
    last_any: dict[int, dict] = {}
    last_lab: dict[int, str] = {}
    for e in events:
        if e.get("type") != "episode_update":
            continue
        try:
            i = int(e.get("episode"))
        except (TypeError, ValueError):
            continue
        last_any[i] = e
        if e.get("label"):
            last_lab[i] = str(e["label"])
    out: dict[int, dict] = {}
    for i in ep_dirs(run):
        u = last_any.get(i)
        label = last_lab.get(i) or ("success" if has_bulk else "unlabeled")
        out[i] = {
            "label": label,
            "memo": (u.get("memo") or "") if u else "",
            "deleted": bool(u.get("deleted")) if u else False,
        }
    return out


def _normalize_labels(obj, run: str) -> dict[int, dict]:
    """resolve_labels 반환값을 {ep_int: {label, memo, deleted}} 로 정규화."""
    out: dict[int, dict] = {}
    items = []
    if isinstance(obj, dict):
        items = list(obj.items())
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, dict):
                k = v.get("ep", v.get("episode", v.get("id")))
                items.append((k, v))
    for k, v in items:
        try:
            if isinstance(k, str):
                mm = re.search(r"(\d+)$", k)
                if not mm:
                    continue
                i = int(mm.group(1))
            else:
                i = int(k)
        except (TypeError, ValueError):
            continue
        if isinstance(v, dict):
            label = v.get("label") or v.get("outcome") or "unlabeled"
            memo = v.get("memo") or ""
            deleted = bool(v.get("deleted"))
        elif isinstance(v, str):
            label, memo, deleted = v, "", False
        else:
            continue
        out[i] = {"label": str(label), "memo": str(memo), "deleted": deleted}
    # 빈 dict/list 는 "라벨 없는 런"(run_meta 없음·이벤트 없음) — 전부 unlabeled 로 채운다.
    if not out and not isinstance(obj, (dict, list)):
        raise ValueError("resolve_labels(%s) 결과를 해석할 수 없음" % run)
    for i in ep_dirs(run):
        out.setdefault(i, {"label": "unlabeled", "memo": "", "deleted": False})
    return out


def resolve_labels(run: str) -> dict[int, dict]:
    with _LABEL_LOCK:
        c = _LABEL_CACHE.get(run)
    if c is not None:
        return c
    meta = load_run_meta(run)
    ctx = {"run": run, "run_name": run, "meta": meta, "run_meta": meta,
           "root": DATA_ROOT, "data_root": DATA_ROOT,
           "run_dir": os.path.join(DATA_ROOT, run)}
    raw, used = MB.call(
        "resolve_labels", ctx,
        ((run,), (meta,), (run, DATA_ROOT)),
        lambda: _fallback_resolve_labels(run),
    )
    try:
        out = _normalize_labels(raw, run)
    except Exception as exc:
        if used:
            MB._warn("resolve_labels", exc)
        out = _fallback_resolve_labels(run)
    with _LABEL_LOCK:
        _LABEL_CACHE[run] = out
    return out


# --------------------------------------------------------------------------
# 정지구간 트림 / 트래젝토리 메트릭 (폴백 구현)
# --------------------------------------------------------------------------
def _np():
    import numpy as np  # 지연 import
    return np


def _fallback_stop_trim(state) -> int:
    """arm 5관절 프레임간 이동 L2 의 5프레임 이동평균이 0.08 미만인 꼬리 제거 + 10프레임 추가 컷."""
    np = _np()
    a = np.asarray(state, dtype=float)
    n = int(a.shape[0]) if a.ndim == 2 else 0
    if n < 3:
        return max(n, 0)
    arm = a[:, :ARM_JOINTS]
    d = np.linalg.norm(np.diff(arm, axis=0), axis=1)          # 길이 n-1
    if d.size == 0:
        return n
    k = min(TRIM_MA_WIN, d.size)
    ker = np.ones(k) / k
    ma = np.convolve(d, ker, mode="same")                      # 길이 n-1
    moving = ma >= TRIM_THR
    idx = np.nonzero(moving)[0]
    if idx.size == 0:
        cut = 0
    else:
        cut = int(idx[-1]) + 1                                 # 마지막 움직임 프레임
    cut -= TRIM_EXTRA
    return int(max(1, min(cut, n)))


def stop_trim(state, run: str = "", ep: int = -1) -> int:
    ctx = {"state": state, "states": state, "arr": state, "x": state,
           "traj": state, "data": state, "q": state, "fps": FPS,
           "run": run, "ep": ep}
    out, _ = MB.call("stop_trim", ctx, ((state,), (state, FPS)),
                     lambda: _fallback_stop_trim(state))
    try:
        if isinstance(out, (tuple, list)) and out and isinstance(out[0], (int, float)):
            out = out[0]
        elif isinstance(out, dict):
            out = out.get("trim_from", out.get("trim", out.get("n_used")))
        n = len(state)
        v = int(out)
        return max(1, min(v, n))
    except Exception as exc:
        MB._warn("stop_trim", exc)
        return _fallback_stop_trim(state)


def _percentile(np, arr, q):
    if arr.size == 0:
        return 0.0
    return float(np.percentile(arr, q))


def _fallback_compute_metrics(ep_obj: dict) -> dict:
    """
    트래젝토리 품질 지표 (폴백). 단위는 deg / frame 기준.
      jerk_p50/p95 : arm 5관절 3차 차분 크기의 백분위수
      track_err    : ||state[t+1] - action[t]|| (arm) 평균
      reversals    : 관절별 속도 부호 반전 횟수 합(데드밴드 0.05 deg/frame)
      n_used/trim  : 트림 후 사용 프레임 수 / 잘라낸 프레임 수 (metrics.py 와 동일 의미)
    """
    np = _np()
    state = np.asarray(ep_obj.get("state") or [], dtype=float)
    if state.ndim != 2 or state.shape[0] == 0:
        return {"jerk_p95": 0.0, "jerk_p50": 0.0, "track_err": 0.0,
                "reversals": 0, "trim": 0, "n_used": 0}
    n = int(state.shape[0])
    trim = int(ep_obj.get("trim_from") or n)
    trim = max(1, min(trim, n))
    arm = state[:trim, :ARM_JOINTS]
    if arm.shape[0] >= 4:
        j = np.linalg.norm(np.diff(arm, n=3, axis=0), axis=1)
    else:
        j = np.zeros(0)
    jerk_p50 = _percentile(np, j, 50)
    jerk_p95 = _percentile(np, j, 95)

    actions = ep_obj.get("action") or []
    errs = []
    for i in range(min(trim, len(actions)) - 1):
        a = actions[i]
        if not a:
            continue
        av = np.asarray(a, dtype=float)[:ARM_JOINTS]
        sv = state[i + 1, :ARM_JOINTS]
        if av.shape == sv.shape:
            errs.append(float(np.linalg.norm(sv - av)))
    track_err = float(np.mean(errs)) if errs else 0.0

    rev = 0
    if arm.shape[0] >= 3:
        v = np.diff(arm, axis=0)
        for c in range(v.shape[1]):
            col = v[:, c]
            sign = np.sign(np.where(np.abs(col) < 0.05, 0.0, col))
            sign = sign[sign != 0]
            if sign.size >= 2:
                rev += int(np.count_nonzero(np.diff(sign) != 0))
    return {
        "jerk_p95": round(jerk_p95, 5),
        "jerk_p50": round(jerk_p50, 5),
        "track_err": round(track_err, 5),
        "reversals": int(rev),
        "n_used": int(trim),
        "trim": int(n - trim),
    }


METRIC_KEYS = ("jerk_p95", "jerk_p50", "track_err", "reversals", "trim", "n_used")


def compute_metrics(ep_obj: dict) -> dict:
    ctx = {"episode": ep_obj, "ep": ep_obj, "ep_obj": ep_obj, "data": ep_obj,
           "e": ep_obj, "obj": ep_obj, "d": ep_obj,
           "state": ep_obj.get("state"), "action": ep_obj.get("action"),
           "trim_from": ep_obj.get("trim_from"), "trim": ep_obj.get("trim_from"),
           "fps": FPS}
    out, _ = MB.call(
        "compute_metrics", ctx,
        ((ep_obj,), (ep_obj.get("state"), ep_obj.get("action")),
         (ep_obj.get("state"), ep_obj.get("action"), ep_obj.get("trim_from"))),
        lambda: _fallback_compute_metrics(ep_obj),
    )
    if not isinstance(out, dict):
        out = _fallback_compute_metrics(ep_obj)
    base = _sanitize(out)
    for k in METRIC_KEYS:
        base.setdefault(k, 0)
    return base


# --------------------------------------------------------------------------
# 램프 시계열 (폴백 구현: PIL 그레이스케일 ROI 평균, 3프레임 샘플링 + 선형보간)
# --------------------------------------------------------------------------
_LAMP_LOCKS: dict[str, threading.Lock] = {}
_LAMP_LOCKS_GUARD = threading.Lock()


def _lock_for(table: dict, key: str) -> threading.Lock:
    with _LAMP_LOCKS_GUARD:
        lk = table.get(key)
        if lk is None:
            lk = threading.Lock()
            table[key] = lk
        return lk


def _interp_to(values: list[float], idxs: list[int], n: int) -> list[float]:
    if n <= 0:
        return []
    if not values:
        return [0.0] * n
    np = _np()
    if len(values) == 1:
        return [float(values[0])] * n
    xs = np.asarray(idxs, dtype=float)
    ys = np.asarray(values, dtype=float)
    grid = np.arange(n, dtype=float)
    return [float(v) for v in np.interp(grid, xs, ys)]


def _fallback_lamp_series(run: str, ep: int, n_frames: int) -> dict:
    from PIL import Image  # 지연 import
    d = os.path.join(ep_dir(run, ep), "frames")
    idxs = list(range(0, max(n_frames, 1), LAMP_STRIDE))
    if n_frames > 0 and (n_frames - 1) not in idxs:
        idxs.append(n_frames - 1)
    got: dict[str, list[float]] = {"m1": [], "m2": []}
    used: list[int] = []
    for i in idxs:
        p = os.path.join(d, "f%05d_front.jpg" % i)
        if not os.path.exists(p):
            continue
        vals: dict[str, float] = {}
        try:
            with Image.open(p) as im:
                im.load()
                for m, box in LAMP_ROI.items():
                    hist = im.crop(box).convert("L").histogram()
                    tot = sum(hist)
                    s = sum(v * c for v, c in enumerate(hist))
                    vals[m] = (s / tot) if tot else 0.0
        except Exception:
            vals = {m: (got[m][-1] if got[m] else 0.0) for m in LAMP_ROI}
        for m in LAMP_ROI:
            got[m].append(float(vals.get(m, 0.0)))
        used.append(i)
    n = n_frames if n_frames > 0 else len(used)
    return {
        "m1": _interp_to(got["m1"], used, n),
        "m2": _interp_to(got["m2"], used, n),
        "m1_thr": LAMP_THR["m1"],
        "m2_thr": LAMP_THR["m2"],
    }


def _normalize_lamp(obj, n_frames: int) -> dict:
    if not isinstance(obj, dict):
        raise ValueError("lamp_series 결과가 dict 가 아님")
    m1 = obj.get("m1") if "m1" in obj else obj.get("lamp_m1")
    m2 = obj.get("m2") if "m2" in obj else obj.get("lamp_m2")
    if m1 is None or m2 is None:
        raise ValueError("lamp_series 결과에 m1/m2 가 없음")
    m1 = [float(v) for v in _sanitize(m1) or []]
    m2 = [float(v) for v in _sanitize(m2) or []]
    if n_frames > 0:
        if len(m1) != n_frames:
            m1 = _interp_to(m1, list(range(len(m1))), n_frames) if m1 else [0.0] * n_frames
        if len(m2) != n_frames:
            m2 = _interp_to(m2, list(range(len(m2))), n_frames) if m2 else [0.0] * n_frames
    return {
        "m1": m1,
        "m2": m2,
        "m1_thr": float(obj.get("m1_thr", LAMP_THR["m1"])),
        "m2_thr": float(obj.get("m2_thr", LAMP_THR["m2"])),
    }


BULK_LAMP_PATH = os.path.join(CACHE_DIR, "lamp.json")   # build_index.py 산출(press 스텝)
_BULK_LAMP: dict | None = None
_BULK_LAMP_MTIME: float = -1.0
_BULK_LAMP_LOCK = threading.Lock()


def _bulk_lamp(run: str, ep_dirname: str):
    """build_index 가 만들어 둔 cache/lamp.json 에서 시계열을 재사용."""
    global _BULK_LAMP, _BULK_LAMP_MTIME
    try:
        mt = os.path.getmtime(BULK_LAMP_PATH)
    except OSError:
        return None
    with _BULK_LAMP_LOCK:
        if _BULK_LAMP is None or mt != _BULK_LAMP_MTIME:
            try:
                d = read_json(BULK_LAMP_PATH)
                _BULK_LAMP = d if isinstance(d, dict) else {}
                _BULK_LAMP_MTIME = mt
            except Exception as exc:
                log("cache/lamp.json 로드 실패", exc)
                _BULK_LAMP = {}
                _BULK_LAMP_MTIME = mt
        table = _BULK_LAMP
    v = table.get("%s/%s" % (run, ep_dirname))
    if isinstance(v, dict) and v.get("m1") and v.get("m2"):
        return v
    return None


def lamp_series(run: str, ep: int, n_frames: int, use_cache: bool = True) -> dict:
    key = cache_key(run, ep)
    path = os.path.join(LAMP_CACHE_DIR, key + ".json")
    if use_cache and cache_fresh(path):
        try:
            return _normalize_lamp(read_json(path), n_frames)
        except Exception:
            pass
    with _lock_for(_LAMP_LOCKS, key):
        if use_cache and cache_fresh(path):
            try:
                return _normalize_lamp(read_json(path), n_frames)
            except Exception:
                pass
        d = ep_dir(run, ep)
        frames = os.path.join(d, "frames")
        bulk = _bulk_lamp(run, os.path.basename(d))
        if bulk is not None:
            try:
                out = _normalize_lamp(bulk, n_frames)
                atomic_write(path, dumps(out))
                return out
            except Exception:
                pass
        # metrics.lamp_series(frames_dir, stride=3) 이 현재 계약.
        ctx = {"frames_dir": frames, "stride": LAMP_STRIDE,
               "run": run, "ep": ep, "episode": ep, "eid": make_eid(run, ep),
               "n_frames": n_frames, "n": n_frames,
               "ep_dir": d, "dir": d, "path": d,
               "roi": LAMP_ROI, "cam": "front"}
        raw, _ = MB.call(
            "lamp_series", ctx,
            ((frames,), (frames, LAMP_STRIDE), (run, ep), (run, ep, n_frames),
             (d,), (make_eid(run, ep),)),
            lambda: _fallback_lamp_series(run, ep, n_frames),
        )
        try:
            out = _normalize_lamp(raw, n_frames)
        except Exception as exc:
            MB._warn("lamp_series", exc)
            out = _fallback_lamp_series(run, ep, n_frames)
        try:
            atomic_write(path, dumps(out))
        except Exception as exc:
            log("lamp 캐시 쓰기 실패", path, exc)
        return out


def _fallback_lamp_summary(lamp: dict, step: int | None, trim_from: int | None) -> dict:
    m1 = lamp.get("m1") or []
    m2 = lamp.get("m2") or []
    n = max(len(m1), len(m2))
    end_i = n - 1
    if trim_from:
        end_i = max(0, min(int(trim_from) - 1, n - 1))
    out = {
        "lamp_m1_end": float(m1[end_i]) if end_i < len(m1) else None,
        "lamp_m2_end": float(m2[end_i]) if end_i < len(m2) else None,
        "lamp_lit_frame": None,
    }
    which = PRESS_STEPS.get(int(step)) if step is not None else None
    if which:
        series = m1 if which == "m1" else m2
        thr = float(lamp.get(which + "_thr", LAMP_THR[which]))
        limit = min(len(series), int(trim_from) if trim_from else len(series))
        for i in range(limit):
            if series[i] > thr:
                out["lamp_lit_frame"] = int(i)
                break
    return out


LAMP_KEYS = ("lamp_m1_end", "lamp_m2_end", "lamp_lit_frame")


def lamp_summary(lamp: dict, step: int | None, trim_from: int | None) -> dict:
    m1 = lamp.get("m1") or []
    m2 = lamp.get("m2") or []
    # metrics.lamp_summary(m1, m2, step) 이 현재 계약.
    ctx = {"m1": m1, "m2": m2, "lamp": lamp, "series": lamp, "lamp_series": lamp,
           "step": step, "task_index": step, "trim_from": trim_from,
           "trim": trim_from, "thr": LAMP_THR}
    out, _ = MB.call(
        "lamp_summary", ctx,
        ((m1, m2, step), (lamp, step), (lamp,)),
        lambda: _fallback_lamp_summary(lamp, step, trim_from),
    )
    if not isinstance(out, dict):
        out = _fallback_lamp_summary(lamp, step, trim_from)
    out = _sanitize(out)
    fb = None
    for k in LAMP_KEYS:
        if k not in out:
            if fb is None:
                fb = _fallback_lamp_summary(lamp, step, trim_from)
            out[k] = fb[k]
    return out


# --------------------------------------------------------------------------
# 에피소드 페이로드 (/api/episode)
# --------------------------------------------------------------------------
def _normalize_episode(obj, run: str, ep: int) -> dict:
    """load_episode 반환값 → {t, state, action, n_frames} 정규화."""
    if obj is None:
        raise ValueError("load_episode 가 None 반환")
    if not isinstance(obj, dict):
        got = {}
        for k in ("t", "ts", "time", "state", "states", "action", "actions", "n_frames"):
            if hasattr(obj, k):
                got[k] = getattr(obj, k)
        if not got:
            raise ValueError("load_episode 반환 타입을 해석할 수 없음: %r" % type(obj))
        obj = got
    state = obj.get("state", obj.get("states"))
    if state is None:
        raise ValueError("load_episode 결과에 state 가 없음")
    state = _sanitize(state)
    if not isinstance(state, list) or (state and not isinstance(state[0], list)):
        raise ValueError("state 형태가 (N,6) 이 아님")
    n = len(state)
    action = obj.get("action", obj.get("actions"))
    action = _sanitize(action) if action is not None else [None] * n
    if not isinstance(action, list):
        action = [None] * n
    action = (action + [None] * n)[:n]
    # NaN 행(액션 없음)은 null 로. _sanitize 가 NaN → None 으로 바꿔 놓았다.
    action = [a if (isinstance(a, list) and a and any(v is not None for v in a)) else None
              for a in action]
    has = obj.get("has_action")
    has = _sanitize(has) if has is not None else None
    if isinstance(has, list) and len(has) == n:
        action = [a if has[i] else None for i, a in enumerate(action)]
    t = obj.get("t", obj.get("ts", obj.get("time")))
    t = _sanitize(t) if t is not None else None
    if not isinstance(t, list) or len(t) != n:
        t = [round(i / FPS, 5) for i in range(n)]
    else:
        t = [float(v) if isinstance(v, (int, float)) else 0.0 for v in t]
        if t and t[0] > 1e6:            # 절대 epoch → 상대시간
            t0 = t[0]
            t = [round(v - t0, 5) for v in t]
    return {"t": t, "state": state, "action": action, "n_frames": n,
            "trim_from": obj.get("trim_from", obj.get("trim"))}


def load_episode(run: str, ep: int) -> dict:
    d = ep_dir(run, ep)
    eid = make_eid(run, ep)
    run_dir = os.path.join(DATA_ROOT, run)
    ep_dirname = os.path.basename(d)
    # metrics.load_episode(run_dir, ep_dirname) 이 현재 계약. 이름 매칭이 먼저 시도된다.
    ctx = {"run_dir": run_dir, "ep_dirname": ep_dirname, "ep_name": ep_dirname,
           "eid": eid, "episode_id": eid, "run": run, "run_name": run,
           "ep": ep, "episode": ep, "episode_index": ep, "ep_index": ep,
           "idx": ep, "i": ep, "ep_dir": d, "dir": d, "path": d, "d": d,
           "root": DATA_ROOT, "data_root": DATA_ROOT}
    raw, _ = MB.call(
        "load_episode", ctx,
        ((run_dir, ep_dirname), (eid,), (d,), (run, ep)),
        lambda: load_steps(run, ep),
    )
    try:
        return _normalize_episode(raw, run, ep)
    except Exception as exc:
        MB._warn("load_episode", exc)
        return _normalize_episode(load_steps(run, ep), run, ep)


def build_episode_payload(run: str, ep: int) -> dict:
    meta = load_ep_meta(run, ep)
    labels = resolve_labels(run).get(ep, {"label": "unlabeled", "memo": "", "deleted": False})
    core = load_episode(run, ep)
    n = core["n_frames"]
    trim_from = core.get("trim_from")
    if not isinstance(trim_from, int) or not (0 < trim_from <= n):
        trim_from = stop_trim(core["state"], run, ep)
    core["trim_from"] = trim_from
    step = int(meta.get("task_index") or 0)
    metrics = compute_metrics(core)
    lamp = lamp_series(run, ep, n)
    metrics.update(lamp_summary(lamp, step, trim_from))
    cycle = int(meta.get("cycle") or 0)
    return {
        "eid": make_eid(run, ep),
        "run": run,
        "experiment": experiment_of(run),
        "try_no": try_no_of(run, cycle),
        "ep": ep,
        "cycle": cycle,
        "step": step,
        "instruction": meta.get("task") or "",
        "outcome": labels.get("label") or "unlabeled",
        "memo": labels.get("memo") or "",
        "deleted": bool(labels.get("deleted")),
        "fps": int(FPS),
        "n_frames": n,
        "trim_from": trim_from,
        "t": core["t"],
        "state": core["state"],
        "action": core["action"],
        "lamp": lamp,
        "metrics": metrics,
    }


_EP_LOCKS: dict[str, threading.Lock] = {}


def episode_payload(run: str, ep: int, use_cache: bool = True) -> dict:
    key = cache_key(run, ep)
    path = os.path.join(EP_CACHE_DIR, key + ".json")
    if use_cache and cache_fresh(path):
        try:
            return read_json(path)
        except Exception:
            pass
    with _lock_for(_EP_LOCKS, key):
        if use_cache and cache_fresh(path):
            try:
                return read_json(path)
            except Exception:
                pass
        payload = build_episode_payload(run, ep)
        try:
            atomic_write(path, dumps(payload))
        except Exception as exc:
            log("ep 캐시 쓰기 실패", path, exc)
        return payload


# --------------------------------------------------------------------------
# 인덱스 (/api/index)
# --------------------------------------------------------------------------
def _agg_index_map() -> dict[tuple[str, int], int]:
    out: dict[tuple[str, int], int] = {}
    if not os.path.exists(MERGED_LABELS_CSV):
        return out
    try:
        with open(MERGED_LABELS_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    out[(row["run"], int(row["source_episode"]))] = int(row["episode_index"])
                except (KeyError, TypeError, ValueError):
                    continue
    except Exception as exc:
        log("episode_labels.csv 읽기 실패", exc)
    return out


def _empty_metrics() -> dict:
    m = {k: 0 for k in METRIC_KEYS}
    m.update({k: None for k in LAMP_KEYS})
    return m


def _fallback_build_index(with_metrics: bool = True) -> dict:
    """cache/index.json 도 build_index 모듈도 없을 때 쓰는 내장 인덱스 빌더."""
    t_start = time.time()
    agg = _agg_index_map()
    groups_map: dict[str, dict] = {}
    steps_map: dict[int, str] = {}
    totals = {"success": 0, "fail": 0, "unstable": 0, "unlabeled": 0, "deleted": 0}
    n_eps = 0
    for run in list_runs():
        labels = resolve_labels(run)
        for ep in sorted(ep_dirs(run)):
            try:
                meta = load_ep_meta(run, ep)
            except Exception as exc:
                log("episode.json 읽기 실패", run, ep, exc)
                continue
            n_eps += 1
            lab = labels.get(ep, {"label": "unlabeled", "memo": "", "deleted": False})
            step = int(meta.get("task_index") or 0)
            cycle = int(meta.get("cycle") or 0)
            instr = meta.get("task") or ""
            steps_map.setdefault(step, instr)
            deleted = bool(lab.get("deleted"))
            outcome = lab.get("label") or "unlabeled"
            totals["deleted" if deleted else outcome] = \
                totals.get("deleted" if deleted else outcome, 0) + 1

            m = _empty_metrics()
            if with_metrics:
                try:
                    core = _normalize_episode(load_steps(run, ep), run, ep)
                    core["trim_from"] = _fallback_stop_trim(core["state"])
                    m.update(_fallback_compute_metrics(core))
                except Exception as exc:
                    log("메트릭 계산 실패", run, ep, exc)
            # 램프는 캐시가 있을 때만(느린 이미지 연산은 warmer 스레드가 채운다)
            lp = os.path.join(LAMP_CACHE_DIR, cache_key(run, ep) + ".json")
            if os.path.exists(lp):
                try:
                    lamp = _normalize_lamp(read_json(lp), int(meta.get("n_frames") or 0))
                    m.update(_fallback_lamp_summary(lamp, step, m.get("trim")))
                except Exception:
                    pass

            gid = "%s/c%d/t%d" % (run, cycle, step)
            g = groups_map.get(gid)
            if g is None:
                g = {"gid": gid, "run": run, "experiment": experiment_of(run),
                     "try_no": try_no_of(run, cycle), "cycle": cycle, "step": step,
                     "instruction": instr, "attempts": []}
                groups_map[gid] = g
            g["attempts"].append({
                "eid": make_eid(run, ep),
                "run": run,
                "ep": ep,
                "attempt": len(g["attempts"]) + 1,
                "outcome": outcome,
                "memo": lab.get("memo") or "",
                "deleted": deleted,
                "n_frames": int(meta.get("n_frames") or 0),
                "agg_index": agg.get((run, ep)),
                "metrics": m,
            })
    runs = list_runs()
    order = {r: i for i, r in enumerate(runs)}
    groups = sorted(groups_map.values(),
                    key=lambda g: (order.get(g["run"], 999), g["cycle"], g["step"]))
    steps = [{"step": s, "instruction": steps_map[s]} for s in sorted(steps_map)]
    idx = {
        "runs": runs,
        "steps": steps,
        "groups": groups,
        "totals": {"episodes": n_eps, "groups": len(groups), "by_outcome": totals},
        "built_at": time.time(),
        "built_by": "server.py:_fallback_build_index",
    }
    log("내장 인덱스 빌드 완료: %d 에피소드 / %d 그룹 (%.1fs)"
        % (n_eps, len(groups), time.time() - t_start))
    return idx


def _looks_like_contract(idx: dict) -> bool:
    gs = idx.get("groups")
    if not isinstance(gs, list):
        return False
    if not gs:
        return bool(idx.get("totals"))
    g0 = gs[0]
    return isinstance(g0, dict) and "gid" in g0 and "attempts" in g0


def _adapt_index(raw: dict) -> dict:
    """build_index.py 의 내부 스키마(group_id/eps/episodes 평면 배열)를 API 계약 스키마로 변환.

    계약: groups[].gid / groups[].attempts[] (eid, attempt, outcome, deleted,
          agg_index, metrics{...,lamp_*}), runs=[str], steps=[{step,instruction}],
          totals={episodes,groups,by_outcome{success,fail,unstable,unlabeled,deleted}}
    """
    if not isinstance(raw, dict):
        raise ValueError("index 가 dict 가 아님")
    if _looks_like_contract(raw):
        return _patch_index(raw)

    eps = raw.get("episodes")
    if not isinstance(eps, list) or not eps:
        raise ValueError("index 에 episodes 배열이 없어 계약 스키마로 변환할 수 없음")

    by_idx: dict[int, dict] = {}
    for i, r in enumerate(eps):
        if isinstance(r, dict):
            try:
                by_idx[int(r.get("idx", i))] = r
            except (TypeError, ValueError):
                by_idx[i] = r

    def to_attempt(r: dict) -> dict:
        run = str(r.get("run") or "")
        ep = int(r.get("ep") or 0)
        m = dict(r.get("metrics") or {})
        for k in LAMP_KEYS:                     # 램프 요약은 레코드 최상위에 있다
            if r.get(k) is not None or k not in m:
                m[k] = r.get(k, m.get(k))
        for k in METRIC_KEYS:
            m.setdefault(k, 0)
        for k in LAMP_KEYS:
            m.setdefault(k, None)
        return {
            "eid": make_eid(run, ep),
            "run": run,
            "ep": ep,
            "attempt": int(r.get("attempt") or 0),
            "outcome": r.get("label") or r.get("outcome") or "unlabeled",
            "memo": r.get("memo") or "",
            "deleted": bool(r.get("deleted")),
            "n_frames": int(r.get("n_frames") or 0),
            "agg_index": r.get("agg_index"),
            "metrics": m,
            "dir": r.get("dir"),
            "duration_s": r.get("duration_s"),
            "label_source": r.get("label_source"),
            "experiment": r.get("experiment") or experiment_of(run),
            "try_no": r.get("try_no") if r.get("try_no") is not None
            else try_no_of(run, r.get("cycle")),
        }

    groups: list[dict] = []
    raw_groups = raw.get("groups")
    if isinstance(raw_groups, list) and raw_groups:
        for g in raw_groups:
            if not isinstance(g, dict):
                continue
            recs = []
            for i in g.get("eps") or []:
                r = by_idx.get(int(i)) if isinstance(i, (int, float)) else None
                if r is not None:
                    recs.append(r)
            recs.sort(key=lambda r: (int(r.get("attempt") or 0), int(r.get("ep") or 0)))
            run = str(g.get("run") or (recs[0].get("run") if recs else ""))
            cycle = int(g.get("cycle") or (recs[0].get("cycle") if recs else 0) or 0)
            step = int(g.get("step") or (recs[0].get("step") if recs else 0) or 0)
            out = {k: v for k, v in g.items() if k not in ("eps",)}
            out.update({
                "gid": "%s/c%d/t%d" % (run, cycle, step),
                "run": run, "cycle": cycle, "step": step,
                "experiment": g.get("experiment") or experiment_of(run),
                "try_no": g.get("try_no") if g.get("try_no") is not None
                else try_no_of(run, cycle),
                "instruction": g.get("instruction")
                or (recs[0].get("instruction") if recs else ""),
                "attempts": [to_attempt(r) for r in recs],
            })
            groups.append(out)
    else:
        gmap: dict[str, dict] = {}
        for r in eps:
            run = str(r.get("run") or "")
            cycle = int(r.get("cycle") or 0)
            step = int(r.get("step") or 0)
            gid = "%s/c%d/t%d" % (run, cycle, step)
            g = gmap.get(gid)
            if g is None:
                g = gmap[gid] = {"gid": gid, "run": run, "cycle": cycle, "step": step,
                                 "experiment": r.get("experiment") or experiment_of(run),
                                 "try_no": r.get("try_no") if r.get("try_no") is not None
                                 else try_no_of(run, cycle),
                                 "instruction": r.get("instruction") or "", "attempts": []}
                groups.append(g)
            g["attempts"].append(to_attempt(r))
        for g in groups:
            g["attempts"].sort(key=lambda a: (a.get("attempt") or 0, a.get("ep") or 0))

    # runs: 문자열 배열 (프론트가 <option value> 로 그대로 쓴다)
    runs: list[str] = []
    for r in raw.get("runs") or []:
        if isinstance(r, dict):
            if r.get("run"):
                runs.append(str(r["run"]))
        elif isinstance(r, str):
            runs.append(r)
    if not runs:
        seen = []
        for g in groups:
            if g["run"] not in seen:
                seen.append(g["run"])
        runs = seen

    # steps: 관측된 step → instruction
    smap: dict[int, str] = {}
    for g in groups:
        if g.get("step"):
            smap.setdefault(int(g["step"]), g.get("instruction") or "")
    tasks = raw.get("tasks") or []
    for s in list(smap):
        if not smap[s] and 1 <= s <= len(tasks):
            smap[s] = tasks[s - 1]
    steps = [{"step": s, "instruction": smap[s]} for s in sorted(smap)]

    by_outcome = {"success": 0, "fail": 0, "unstable": 0, "unlabeled": 0, "deleted": 0}
    n_eps = 0
    for g in groups:
        for a in g["attempts"]:
            n_eps += 1
            k = "deleted" if a["deleted"] else (a["outcome"] or "unlabeled")
            by_outcome[k] = by_outcome.get(k, 0) + 1

    idx = {
        "runs": runs,
        "steps": steps,
        "groups": groups,
        "totals": {"episodes": n_eps, "groups": len(groups), "by_outcome": by_outcome},
    }
    # 부가 정보는 그대로 통과 (episodes 평면 배열은 attempts 와 중복이라 제외)
    for k in ("fps", "joint_names", "lamp_roi", "lamp_thresh", "press_steps",
              "tasks", "tasks_map", "experiments", "counts", "generated_at", "root", "runs_info"):
        if k in raw and k not in idx:
            idx[k] = raw[k]
    idx.setdefault("runs_info", raw.get("runs") if isinstance(raw.get("runs"), list)
                   and raw.get("runs") and isinstance(raw["runs"][0], dict) else None)
    return idx


def _try_module_build_index():
    """build_index 모듈(또는 metrics.build_index)로 인덱스 생성 시도."""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    candidates = []
    try:
        import build_index as bi  # type: ignore
        for name in ("build_index", "build", "main", "make_index", "run"):
            f = getattr(bi, name, None)
            if callable(f):
                candidates.append(("build_index.%s" % name, f))
    except Exception as exc:
        log("build_index 모듈 없음/실패:", "%s: %s" % (type(exc).__name__, exc))
    f = MB.fn("build_index")
    if f is not None:
        candidates.append(("metrics.build_index", f))
    for label, f in candidates:
        try:
            sig = inspect.signature(f)
            out = None
            called = False
            for args in ((), (DATA_ROOT,), (DATA_ROOT, CACHE_DIR)):
                try:
                    sig.bind(*args)
                except (TypeError, ValueError):
                    continue
                log("인덱스 생성 중(수 분 걸릴 수 있음):", label)
                out = f(*args)
                called = True
                break
            # build() 는 (index, lamp_cache) 튜플을 돌려준다
            if isinstance(out, (tuple, list)) and out:
                extra = out[1] if len(out) > 1 else None
                if isinstance(extra, dict) and extra and not os.path.exists(BULK_LAMP_PATH):
                    try:
                        atomic_write(BULK_LAMP_PATH, dumps(extra))
                    except Exception as exc:
                        log("lamp.json 저장 실패", exc)
                out = out[0]
            if isinstance(out, dict) and ("groups" in out or "episodes" in out):
                log("인덱스 생성:", label)
                return out
            if called and out is None and os.path.exists(INDEX_CACHE):
                log("인덱스 생성(파일 산출):", label)
                return read_json(INDEX_CACHE)
        except Exception as exc:
            log("%s 호출 실패: %s: %s" % (label, type(exc).__name__, exc))
    return None


def _patch_index(idx: dict) -> dict:
    """스펙에 필요한 키가 비어 있으면 채워 넣는다(파괴적 수정 금지)."""
    if not isinstance(idx, dict):
        raise ValueError("index 가 dict 가 아님")
    idx.setdefault("runs", list_runs())
    idx.setdefault("groups", [])
    if not idx.get("steps"):
        seen: dict[int, str] = {}
        for g in idx["groups"]:
            if g.get("step") is not None:
                seen.setdefault(int(g["step"]), g.get("instruction") or "")
        idx["steps"] = [{"step": s, "instruction": seen[s]} for s in sorted(seen)]
    tot = idx.get("totals")
    if not isinstance(tot, dict) or "by_outcome" not in tot:
        by = {"success": 0, "fail": 0, "unstable": 0, "unlabeled": 0, "deleted": 0}
        n = 0
        for g in idx["groups"]:
            for a in g.get("attempts", []):
                n += 1
                k = "deleted" if a.get("deleted") else (a.get("outcome") or "unlabeled")
                by[k] = by.get(k, 0) + 1
        idx["totals"] = {"episodes": n, "groups": len(idx["groups"]), "by_outcome": by}
    else:
        tot.setdefault("episodes", sum(len(g.get("attempts", [])) for g in idx["groups"]))
        tot.setdefault("groups", len(idx["groups"]))
    for g in idx["groups"]:
        if not g.get("experiment"):
            g["experiment"] = experiment_of(g.get("run") or "")
        if g.get("try_no") is None:
            g["try_no"] = try_no_of(g.get("run") or "", g.get("cycle"))
        for a in g.get("attempts", []):
            m = a.get("metrics")
            if not isinstance(m, dict):
                a["metrics"] = _empty_metrics()
            else:
                for k in METRIC_KEYS:
                    m.setdefault(k, 0)
                for k in LAMP_KEYS:
                    m.setdefault(k, None)
    if not idx.get("experiments"):
        idx["experiments"] = _experiments_from_groups(idx["groups"])
    return idx


def _experiments_from_groups(groups: list) -> list:
    """experiments 요약이 없을 때(폴백 인덱스) 그룹에서 최소 형태로 만든다."""
    exps: dict[str, dict] = {}
    for g in groups:
        name = g.get("experiment") or experiment_of(g.get("run") or "")
        ex = exps.get(name)
        if ex is None:
            ex = exps[name] = {"name": name, "runs": [], "n_tries": 0, "n_episodes": 0,
                               "n_groups": 0, "model": None, "tasks": {}, "tries": []}
        if g.get("run") not in ex["runs"]:
            ex["runs"].append(g.get("run"))
        ex["n_groups"] += 1
        ex["n_episodes"] += len(g.get("attempts") or [])
        if g.get("step") is not None:
            ex["tasks"].setdefault(str(g["step"]), g.get("instruction") or "")
        tn = g.get("try_no")
        tr = next((t for t in ex["tries"] if t["try_no"] == tn and t["run"] == g.get("run")), None)
        if tr is None:
            tr = {"try_no": tn, "run": g.get("run"), "cycle": [g.get("cycle")],
                  "n_episodes": 0, "n_groups": 0}
            ex["tries"].append(tr)
        tr["n_groups"] += 1
        tr["n_episodes"] += len(g.get("attempts") or [])
    for ex in exps.values():
        ex["tries"].sort(key=lambda t: ((t["try_no"] or 0), str(t["run"])))
        ex["n_tries"] = len({t["try_no"] for t in ex["tries"]})
    return list(exps.values())


class IndexStore:
    def __init__(self) -> None:
        self._idx: dict | None = None
        self._lock = threading.RLock()
        self._from_module = False
        self._src_mtime = 0.0

    def get(self, refresh: bool = False) -> dict:
        with self._lock:
            if self._idx is not None and not refresh:
                # 데이터층이 build_index 를 다시 돌렸으면 자동 재로드
                if self._from_module and _mtime(INDEX_CACHE) != self._src_mtime:
                    log("cache/index.json 갱신 감지 → 인덱스 재로드")
                else:
                    return self._idx
            self._idx = self._build(refresh)
            self._src_mtime = _mtime(INDEX_CACHE)
            return self._idx

    def _build(self, refresh: bool) -> dict:
        # 1) build_index 산출물 (cache/index.json 은 데이터층 소유 — 절대 덮어쓰지 않는다)
        if not refresh and os.path.exists(INDEX_CACHE):
            try:
                idx = _patch_index(_adapt_index(read_json(INDEX_CACHE)))
                self._from_module = True
                log("cache/index.json 로드 → 계약 스키마 변환 (%d 그룹 / %d 에피소드)"
                    % (len(idx.get("groups", [])),
                       (idx.get("totals") or {}).get("episodes", 0)))
                return idx
            except Exception as exc:
                log("cache/index.json 변환 실패 → 재생성:", exc)
                log(traceback.format_exc().rstrip())
        out = _try_module_build_index()
        if isinstance(out, dict):
            try:
                idx = _patch_index(_adapt_index(out))
                self._from_module = True
                return idx
            except Exception as exc:
                log("build_index 산출물 변환 실패 → 내장 폴백:", exc)
        # 2) 내장 폴백
        self._from_module = False
        if not refresh and os.path.exists(INDEX_FALLBACK_CACHE):
            try:
                idx = _patch_index(read_json(INDEX_FALLBACK_CACHE))
                log("cache/index.fallback.json 로드 (%d 그룹)" % len(idx.get("groups", [])))
                return idx
            except Exception as exc:
                log("fallback 인덱스 캐시 로드 실패:", exc)
        idx = _patch_index(_fallback_build_index())
        try:
            atomic_write(INDEX_FALLBACK_CACHE, dumps(idx))
        except Exception as exc:
            log("fallback 인덱스 캐시 쓰기 실패", exc)
        return idx

    def update_lamp(self, run: str, ep: int, summary: dict) -> None:
        """warmer 스레드가 램프 요약을 인덱스에 반영."""
        eid = make_eid(run, ep)
        with self._lock:
            if self._idx is None:
                return
            for g in self._idx.get("groups", []):
                if g.get("run") != run:
                    continue
                for a in g.get("attempts", []):
                    if a.get("eid") == eid:
                        a.setdefault("metrics", _empty_metrics()).update(summary)
                        return

    def persist(self) -> None:
        """내장 폴백 인덱스만 저장한다. cache/index.json 은 데이터층 소유라 건드리지 않는다."""
        with self._lock:
            if self._idx is None or self._from_module:
                return
            try:
                atomic_write(INDEX_FALLBACK_CACHE, dumps(self._idx))
            except Exception as exc:
                log("인덱스 저장 실패", exc)


INDEX = IndexStore()


def warm_lamps() -> None:
    """press 스텝(3,6) 에피소드의 램프 요약을 백그라운드로 채운다."""
    try:
        idx = INDEX.get()
    except Exception as exc:
        log("warmer: 인덱스 준비 실패", exc)
        return
    targets = []
    for g in idx.get("groups", []):
        if int(g.get("step") or 0) not in PRESS_STEPS:
            continue
        for a in g.get("attempts", []):
            m = a.get("metrics") or {}
            if m.get("lamp_m1_end") is None and m.get("lamp_m2_end") is None:
                targets.append((a["run"], a["ep"], int(g["step"]), m.get("trim")))
    if not targets:
        return
    log("warmer: 램프 계산 대상 %d 에피소드" % len(targets))
    t0 = time.time()
    done = 0
    for run, ep, step, trim in targets:
        try:
            meta = load_ep_meta(run, ep)
            n = int(meta.get("n_frames") or 0)
            lamp = lamp_series(run, ep, n)
            INDEX.update_lamp(run, ep, _fallback_lamp_summary(lamp, step, trim))
            done += 1
        except Exception as exc:
            log("warmer 실패", run, ep, exc)
    INDEX.persist()
    log("warmer: %d/%d 완료 (%.1fs)" % (done, len(targets), time.time() - t0))


# --------------------------------------------------------------------------
# 채점 저장소 (scores.json)
# --------------------------------------------------------------------------
class ScoreStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data = {"scores": {}, "pairs": []}
        self._loaded = False
        self._sig = None            # (mtime_ns, size) — 외부 편집 감지용

    def _sig_now(self):
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _ensure(self) -> None:
        # 파일이 서버 밖에서 바뀌었으면 다시 읽는다. 한 번만 읽고 캐시해 두면
        # 손으로 고친 scores.json 이 다음 POST 때 통째로 덮어써진다.
        sig = self._sig_now()
        if self._loaded and sig == self._sig:
            return
        if sig is None:                       # 파일 없음 → 빈 상태에서 시작
            self._data = {"scores": {}, "pairs": []}
        else:
            try:
                d = read_json(self.path)
                if isinstance(d, dict):
                    self._data = {
                        "scores": d.get("scores") if isinstance(d.get("scores"), dict) else {},
                        "pairs": d.get("pairs") if isinstance(d.get("pairs"), list) else [],
                    }
            except Exception as exc:
                # 깨진 파일로 기존 채점을 날리지 않는다.
                log("scores.json 로드 실패(메모리 상태 유지):", exc)
        self._loaded = True
        self._sig = sig

    def _flush(self) -> None:
        atomic_write(self.path, dumps(self._data))
        self._sig = self._sig_now()           # 우리가 쓴 건 외부 변경으로 보지 않는다

    def all(self) -> dict:
        with self._lock:
            self._ensure()
            return json.loads(json.dumps(self._data, ensure_ascii=False))

    def set_score(self, eid: str, score, note: str) -> dict:
        with self._lock:
            self._ensure()
            entry = self._data["scores"].get(eid) or {}
            entry["score"] = score
            entry["note"] = note if note is not None else entry.get("note", "")
            entry["ts"] = time.time()
            self._data["scores"][eid] = entry
            self._flush()
            return entry

    def add_pair(self, gid: str, better: str, worse: str, reason: str) -> dict:
        with self._lock:
            self._ensure()
            rec = {"gid": gid, "better": better, "worse": worse,
                   "reason": reason or "", "ts": time.time()}
            key = (gid, frozenset((better, worse)))
            for i, p in enumerate(self._data["pairs"]):
                if (p.get("gid"), frozenset((p.get("better"), p.get("worse")))) == key:
                    self._data["pairs"][i] = rec
                    break
            else:
                self._data["pairs"].append(rec)
            self._flush()
            return rec


SCORES = ScoreStore(SCORES_PATH)


def _ep_brief(eid: str) -> dict:
    """export 용 에피소드 요약."""
    try:
        run, ep = parse_eid(eid)
    except Exception:
        return {"eid": eid}
    out = {"eid": make_eid(run, ep), "run": run, "ep": ep}
    try:
        meta = load_ep_meta(run, ep)
        lab = resolve_labels(run).get(ep, {})
        out.update({
            "cycle": int(meta.get("cycle") or 0),
            "step": int(meta.get("task_index") or 0),
            "instruction": meta.get("task") or "",
            "outcome": lab.get("label") or "unlabeled",
            "memo": lab.get("memo") or "",
            "n_frames": int(meta.get("n_frames") or 0),
            "episode_dir": ep_dir(run, ep),
        })
    except Exception:
        pass
    cached = os.path.join(EP_CACHE_DIR, cache_key(run, ep) + ".json")
    if os.path.exists(cached):
        try:
            out["metrics"] = read_json(cached).get("metrics")
        except Exception:
            pass
    for g in INDEX.get().get("groups", []):
        for a in g.get("attempts", []):
            if a.get("eid") == out["eid"]:
                out.setdefault("metrics", a.get("metrics"))
                out["agg_index"] = a.get("agg_index")
                out["attempt"] = a.get("attempt")
                out["deleted"] = a.get("deleted")
                return out
    return out


def export_pairs() -> dict:
    data = SCORES.all()
    pairs = []
    for p in data.get("pairs", []):
        pairs.append({
            "chosen": _ep_brief(p.get("better", "")),
            "rejected": _ep_brief(p.get("worse", "")),
            "gid": p.get("gid", ""),
            "reason": p.get("reason", ""),
        })
    return {"pairs": pairs}
