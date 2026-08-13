#!/usr/bin/env python3
"""viewer_data 를 import 해서 /api/index, /api/episode 응답을 dist/api 로 굽는다."""
import json
import os
import sys
import time

VIEWER = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, VIEWER)
sys.argv = [sys.argv[0]]          # server.py 의 argparse 가 건드리지 않도록

import viewer_data as S  # noqa: E402

DIST = os.path.join(VIEWER, "dist")
API = os.path.join(DIST, "api")
EPDIR = os.path.join(API, "episode")
os.makedirs(EPDIR, exist_ok=True)

S.MB.load()
print("metrics bridge:", S.MB.status(), flush=True)

# cache/ 는 다른 담당자 소유 → 빌드 중 어떤 쓰기도 하지 않는다.
_orig_atomic_write = S.atomic_write
_CACHE = os.path.abspath(S.CACHE_DIR) + os.sep


def _guarded_atomic_write(path, data, *a, **kw):
    if os.path.abspath(path).startswith(_CACHE):
        return None
    return _orig_atomic_write(path, data, *a, **kw)


S.atomic_write = _guarded_atomic_write

# ---- index ----
idx = S.INDEX.get(refresh=False)
with open(os.path.join(API, "index.json"), "wb") as f:
    f.write(S.dumps(idx))
print("index: groups=%d episodes=%d runs=%d steps=%d"
      % (len(idx["groups"]), idx["totals"]["episodes"], len(idx["runs"]),
         len(idx["steps"])), flush=True)

# ---- episodes ----
raw = json.load(open(os.path.join(VIEWER, "cache", "index.json")))
eps = [(e["run"], int(e["ep"])) for e in raw["episodes"]]
print("에피소드 %d개" % len(eps), flush=True)

t0 = time.time()
bad = []
for i, (run, ep) in enumerate(eps, 1):
    try:
        p = S.episode_payload(run, ep, use_cache=True)
        out = os.path.join(EPDIR, "%s_%04d.json" % (run, ep))
        with open(out, "wb") as f:
            f.write(S.dumps(p))
    except Exception as exc:
        bad.append((run, ep, "%s: %s" % (type(exc).__name__, exc)))
        print("FAIL %s/ep%04d: %s" % (run, ep, exc), flush=True)
    if i % 25 == 0:
        print("  %d/%d (%.1fs)" % (i, len(eps), time.time() - t0), flush=True)

print("에피소드 완료 %d개, 실패 %d개, %.1fs"
      % (len(eps) - len(bad), len(bad), time.time() - t0))
if bad:
    print(bad)
sys.exit(1 if bad else 0)
