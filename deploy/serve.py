#!/usr/bin/env python3
"""rollout_viewer 정적 배포 서버 (표준 라이브러리만 사용).

미리 구워둔 산출물만 서빙한다. 원본 프레임(JPEG)은 배포에 포함하지 않으므로
/frame 은 항상 404 다.

  GET /                              → index.html
  GET /api/index                     → api/index.json
  GET /api/episode?eid=<run>/ep<NNNN>→ api/episode/<run>_<NNNN>.json
  GET /clip?eid=...&cam=front|wrist  → clips/<run>_<NNNN>_<cam>.mp4 (Range 206 지원)
  GET /frame?...                     → 404 (원본 프레임 미배포)
  GET /healthz                       → ok

환경변수: VIEW_PORT(기본 8081), VIEW_HOST(기본 0.0.0.0), VIEW_ROOT(기본 이 파일의 디렉터리)
"""
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.abspath(os.environ.get("VIEW_ROOT")
                       or os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("VIEW_PORT") or 8081)
HOST = os.environ.get("VIEW_HOST") or "0.0.0.0"

API_DIR = os.path.join(ROOT, "api")
EP_DIR = os.path.join(API_DIR, "episode")
CLIP_DIR = os.path.join(ROOT, "clips")
INDEX_JSON = os.path.join(API_DIR, "index.json")

EID_RE = re.compile(r"^(?P<run>[A-Za-z0-9_\-]+)/(?:ep)?(?P<ep>\d+)$")
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)(?:,.*)?$")
CHUNK = 256 * 1024
CAMS = ("front", "wrist")


def log(*a):
    sys.stderr.write(" ".join(str(x) for x in a) + "\n")
    sys.stderr.flush()


class BadRequest(Exception):
    pass


class NotFound(Exception):
    pass


def parse_eid(eid: str):
    if not eid:
        raise BadRequest("eid 파라미터가 필요합니다")
    m = EID_RE.match(eid.strip())
    if not m:
        raise BadRequest("eid 형식이 잘못됨: %r (예: coffee_new01/ep0002)" % eid)
    return m.group("run"), int(m.group("ep"))


class Handler(BaseHTTPRequestHandler):
    server_version = "rollout-viewer-static/1.0"
    protocol_version = "HTTP/1.1"
    head_only = False

    def log_message(self, fmt, *args):
        log("%s - %s" % (self.address_string(), fmt % args))

    # ---- 응답 헬퍼 ----
    def _send(self, code, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not self.head_only and body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, obj, code=200, extra=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        ex = {"Cache-Control": "no-store"}
        ex.update(extra or {})
        self._send(code, body, "application/json; charset=utf-8", ex)

    def _json_file(self, path: str, what: str):
        if not os.path.exists(path):
            raise NotFound("%s 없음" % what)
        with open(path, "rb") as f:
            body = f.read()
        self._send(200, body, "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def _err(self, code, msg):
        self._json({"ok": False, "error": msg}, code)

    # ---- 라우팅 ----
    def do_GET(self):
        self.head_only = False
        self._route()

    def do_HEAD(self):
        self.head_only = True
        self._route()

    def do_POST(self):
        # 읽기 전용 배포: 라벨/점수 쓰기는 지원하지 않는다(프론트가 깨지지 않게 JSON 으로 응답).
        self.head_only = False
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 0:
                self.rfile.read(min(n, 1 << 20))
        except Exception:
            pass
        self._err(405, "읽기 전용 배포입니다 (쓰기 API 미지원)")

    def _route(self):
        u = urlparse(self.path)
        path = u.path
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        q = parse_qs(u.query)
        try:
            if path == "/healthz":
                return self._send(200, b"ok", "text/plain; charset=utf-8",
                                  {"Cache-Control": "no-store"})
            if path in ("/", "/index.html"):
                return self._index_html()
            if path == "/api/index":
                return self._json_file(INDEX_JSON, "api/index.json")
            if path == "/api/episode":
                return self._api_episode(q)
            if path == "/clip":
                return self._clip(q)
            if path == "/frame":
                # 원본 프레임은 배포에 포함되지 않는다. 빈 200 을 주면 뷰어가
                # 깨진 이미지를 계속 재시도할 수 있으므로 명확히 404 로 끝낸다.
                raise NotFound("원본 프레임은 이 배포에 포함되지 않습니다 (/clip 을 쓰세요)")
            raise NotFound("알 수 없는 엔드포인트: %s" % path)
        except BadRequest as exc:
            self._err(400, str(exc))
        except NotFound as exc:
            self._err(404, str(exc))
        except Exception as exc:
            log("GET %s 실패: %s: %s" % (self.path, type(exc).__name__, exc))
            self._err(500, "%s: %s" % (type(exc).__name__, exc))

    # ---- 핸들러 ----
    def _index_html(self):
        p = os.path.join(ROOT, "index.html")
        if not os.path.exists(p):
            msg = ("<!doctype html><meta charset='utf-8'><title>rollout_viewer</title>"
                   "<body style='font:14px ui-monospace,monospace;padding:24px'>"
                   "<h2>index.html 이 아직 배치되지 않았습니다</h2>"
                   "<p>API 는 동작 중입니다: <a href='/api/index'>/api/index</a></p>"
                   "</body>").encode("utf-8")
            return self._send(200, msg, "text/html; charset=utf-8",
                              {"Cache-Control": "no-store"})
        with open(p, "rb") as f:
            body = f.read()
        self._send(200, body, "text/html; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def _api_episode(self, q):
        run, ep = parse_eid((q.get("eid", [""])[0] or "").strip())
        p = os.path.join(EP_DIR, "%s_%04d.json" % (run, ep))
        if not self._inside(p, EP_DIR):
            raise NotFound("잘못된 경로")
        if not os.path.exists(p):
            raise NotFound("에피소드 없음: %s/ep%04d" % (run, ep))
        self._json_file(p, "episode")

    def _clip(self, q):
        run, ep = parse_eid((q.get("eid", [""])[0] or "").strip())
        cam = (q.get("cam", ["front"])[0] or "front").strip()
        if cam not in CAMS:
            raise BadRequest("cam 은 front|wrist 여야 합니다 (받은 값: %r)" % cam)
        p = os.path.join(CLIP_DIR, "%s_%04d_%s.mp4" % (run, ep, cam))
        if not self._inside(p, CLIP_DIR):
            raise NotFound("잘못된 경로")
        if not os.path.exists(p):
            raise NotFound("클립 없음: %s/ep%04d %s" % (run, ep, cam))
        self._serve_file_range(p, "video/mp4", "max-age=3600")

    @staticmethod
    def _inside(path: str, base: str) -> bool:
        return os.path.abspath(path).startswith(os.path.abspath(base) + os.sep)

    # ---- Range 지원 파일 전송 ----
    def _serve_file_range(self, path: str, ctype: str, cache: str = "no-store") -> None:
        size = os.path.getsize(path)
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if rng:
            m = RANGE_RE.match(rng.strip())
            if not m or (not m.group(1) and not m.group(2)):
                return self._send(416, b"", "text/plain; charset=utf-8",
                                  {"Content-Range": "bytes */%d" % size})
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:                                   # suffix range: bytes=-N
                length = int(m.group(2))
                start = max(0, size - length)
                end = size - 1
            if start >= size or start > end:
                return self._send(416, b"", "text/plain; charset=utf-8",
                                  {"Content-Range": "bytes */%d" % size})
            end = min(end, size - 1)
            partial = True
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        if self.head_only:
            return
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    buf = f.read(min(CHUNK, left))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    left -= len(buf)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main() -> int:
    n_clips = len(os.listdir(CLIP_DIR)) if os.path.isdir(CLIP_DIR) else 0
    n_eps = len(os.listdir(EP_DIR)) if os.path.isdir(EP_DIR) else 0
    log("root=%s  clips=%d  episodes=%d  index=%s"
        % (ROOT, n_clips, n_eps, "ok" if os.path.exists(INDEX_JSON) else "MISSING"))
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    log("listening on http://%s:%d" % (HOST, PORT))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("종료")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
