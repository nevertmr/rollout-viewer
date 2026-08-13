#!/usr/bin/env python3
"""dist/clips/<run>_<ep4>_<cam>.mp4 를 전부 굽는다 (6 병렬)."""
import glob
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

VIEWER = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(VIEWER, "dist", "clips")
FFMPEG = os.environ.get("FFMPEG") or __import__("shutil").which("ffmpeg") or "ffmpeg"
CAMS = ("front", "wrist")
JOBS = 6

os.makedirs(OUT, exist_ok=True)
idx = json.load(open(os.path.join(VIEWER, "cache", "index.json")))
# 원본 데이터 루트: DATA_ROOT env > index.json 에 기록된 root
DATA_ROOT = os.environ.get("DATA_ROOT") or idx["root"]

tasks = []
skipped = []
for e in idx["episodes"]:
    run, ep = e["run"], int(e["ep"])
    d = os.path.join(DATA_ROOT, run, "raw", e["dir"], "frames")
    for cam in CAMS:
        pat = os.path.join(d, "f*_%s.jpg" % cam)
        n = len(glob.glob(pat))
        if n == 0:
            skipped.append({"run": run, "ep": ep, "cam": cam, "dir": e["dir"],
                            "reason": "프레임 0개"})
            continue
        tasks.append((run, ep, cam, pat, n))

print("할 일 %d개 / 스킵 %d개" % (len(tasks), len(skipped)), flush=True)


def encode(t):
    run, ep, cam, pat, n = t
    out = os.path.join(OUT, "%s_%04d_%s.mp4" % (run, ep, cam))
    tmp = out + ".part.mp4"
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-framerate", "30", "-pattern_type", "glob", "-i", pat,
           "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", tmp]
    p = subprocess.run(cmd, capture_output=True, timeout=900)
    if p.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        if os.path.exists(tmp):
            os.remove(tmp)
        return {"ok": False, "out": os.path.basename(out), "n": n,
                "err": (p.stderr or b"").decode("utf-8", "replace")[-500:]}
    os.replace(tmp, out)
    return {"ok": True, "out": os.path.basename(out), "n": n}


t0 = time.time()
done = 0
fails = []
with ThreadPoolExecutor(max_workers=JOBS) as ex:
    for r in ex.map(encode, tasks):
        done += 1
        if not r["ok"]:
            fails.append(r)
            print("FAIL %s: %s" % (r["out"], r["err"]), flush=True)
        if done % 50 == 0:
            print("  %d/%d (%.1fs)" % (done, len(tasks), time.time() - t0), flush=True)

print("완료 %d개, 실패 %d개, %.1fs" % (done - len(fails), len(fails), time.time() - t0))
json.dump({"skipped": skipped, "fails": fails},
          open(os.path.join(VIEWER, "dist", "clips_report.json"), "w"),
          ensure_ascii=False, indent=2)
sys.exit(1 if fails else 0)
