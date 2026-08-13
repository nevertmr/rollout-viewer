"""rollout_viewer 클립 베이킹 — JPEG 프레임을 ffmpeg 로 mp4 클립으로 굽는다 (/clip)."""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import threading

from viewer_data import CLIP_CACHE_DIR, FPS, NotFound, _lock_for, cache_key, ep_dir

_CLIP_LOCKS: dict[str, threading.Lock] = {}
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"


def clip_path(run: str, ep: int, cam: str) -> str:
    return os.path.join(CLIP_CACHE_DIR, "%s_%s.mp4" % (cache_key(run, ep), cam))


def ensure_clip(run: str, ep: int, cam: str) -> str:
    out = clip_path(run, ep, cam)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return out
    with _lock_for(_CLIP_LOCKS, os.path.basename(out)):
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out
        frames = os.path.join(ep_dir(run, ep), "frames")
        pattern = os.path.join(frames, "f*_%s.jpg" % cam)
        if not glob.glob(pattern):
            raise NotFound("프레임 없음: %s" % pattern)
        if not os.path.exists(FFMPEG):
            raise RuntimeError("ffmpeg 실행파일을 찾을 수 없음: %s" % FFMPEG)
        os.makedirs(CLIP_CACHE_DIR, exist_ok=True)
        tmp = out + ".part%d.mp4" % os.getpid()
        cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
               "-framerate", str(int(FPS)),
               "-pattern_type", "glob", "-i", pattern,
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23",
               "-movflags", "+faststart", tmp]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=600)
        except subprocess.TimeoutExpired:
            _rm(tmp)
            raise RuntimeError("ffmpeg 타임아웃(600s): %s/ep%04d %s" % (run, ep, cam))
        if proc.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            err = (proc.stderr or b"").decode("utf-8", "replace").strip()[-2000:]
            _rm(tmp)
            raise RuntimeError("ffmpeg 실패(rc=%s): %s" % (proc.returncode, err or "(stderr 없음)"))
        os.replace(tmp, out)
        return out


def _rm(p: str) -> None:
    try:
        os.unlink(p)
    except OSError:
        pass
