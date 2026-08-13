#!/usr/bin/env python3
"""
rollout_viewer server  ---  SO-101 coffee rollout 뷰어 백엔드.

- 표준 라이브러리만 사용 (http.server.ThreadingHTTPServer), 127.0.0.1:8760 바인드.
- 데이터층(metrics.py)이 있으면 그쪽 구현을 우선 사용하고, 없거나 호출에 실패하면
  내장 폴백 구현으로 계속 동작한다(서버가 죽지 않는다).
- 계약(API 스펙)은 프론트(index.html)와 공유. 응답 형태를 바꾸지 말 것.

실행:  python3 server.py [--port 8760] [--no-warm] [--rebuild]
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import traceback
from http.server import ThreadingHTTPServer

from viewer_data import (
    CACHE_DIR,
    CLIP_CACHE_DIR,
    EP_CACHE_DIR,
    INDEX,
    LAMP_CACHE_DIR,
    MB,
    ROOT,
    log,
    warm_lamps,
)
from viewer_clips import FFMPEG
from viewer_http import Handler


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="rollout_viewer 백엔드")
    ap.add_argument("--port", type=int, default=8760)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-warm", action="store_true", help="램프 백그라운드 계산 비활성화")
    ap.add_argument("--rebuild", action="store_true", help="시작 시 인덱스 강제 재생성")
    args = ap.parse_args()

    for d in (CACHE_DIR, EP_CACHE_DIR, LAMP_CACHE_DIR, CLIP_CACHE_DIR):
        os.makedirs(d, exist_ok=True)

    MB.load()
    if not os.path.exists(FFMPEG):
        log("경고: ffmpeg 를 찾을 수 없습니다 (%s). /clip 은 500 을 반환합니다." % FFMPEG)

    def prepare():
        try:
            INDEX.get(refresh=args.rebuild)
        except Exception:
            log("인덱스 준비 실패:\n%s" % traceback.format_exc().rstrip())
            return
        if not args.no_warm:
            try:
                warm_lamps()
            except Exception:
                log("램프 warmer 실패:\n%s" % traceback.format_exc().rstrip())

    threading.Thread(target=prepare, name="index-prepare", daemon=True).start()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    log("listening on http://%s:%d  (root=%s)" % (args.host, args.port, ROOT))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("종료")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
