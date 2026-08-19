"""rollout_viewer HTTP 핸들러 — 라우팅 / 정적 파일 / 프레임·클립 스트리밍."""

from __future__ import annotations

import json
import os
import re
import traceback
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from viewer_data import (
    DATA_ROOT,
    INDEX,
    MB,
    ROOT,
    SCORES,
    SCORES_PATH,
    BadRequest,
    NotFound,
    dumps,
    ep_dir,
    episode_payload,
    export_pairs,
    list_runs,
    log,
    make_eid,
    parse_eid,
)
from viewer_clips import ALL_CAMS, FFMPEG, ensure_clip

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".map": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/plain; charset=utf-8",
}
MAX_BODY = 4 * 1024 * 1024
MONTAGE_DIR = os.path.join(ROOT, "dist", "montage")
SRCABL_DIR = os.path.join(ROOT, "dist", "api", "srcabl")   # build_srcabl.py 산출물 (소스별 인과 기여도)
MONTAGE_RE = re.compile(r"^[A-Za-z0-9_\-]+\.(mp4|json)$")
CHUNK = 256 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = "rolloutviewer/1.0"
    protocol_version = "HTTP/1.1"
    head_only = False

    # ---- 로그: 접근 로그 억제, 에러만 stderr ----
    def log_message(self, fmt, *args):
        return

    def log_error(self, fmt, *args):
        try:
            log("http:", self.address_string(), fmt % args)
        except Exception:
            pass

    # ---- 응답 헬퍼 ----
    def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8",
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        if not self.head_only and body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, obj, code: int = 200, extra: dict | None = None) -> None:
        self._send(code, dumps(obj), "application/json; charset=utf-8", extra)

    def _err(self, code: int, msg: str, **kw) -> None:
        payload = {"error": msg, "status": code}
        payload.update(kw)
        self._json(payload, code)

    # ---- 라우팅 ----
    def do_GET(self):
        self.head_only = False
        self._route()

    def do_HEAD(self):
        self.head_only = True
        self._route()

    def do_POST(self):
        self.head_only = False
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        try:
            body = self._read_body()
            if path == "/api/scores":
                return self._post_scores(body)
            if path == "/api/pairs":
                return self._post_pairs(body)
            return self._err(404, "알 수 없는 POST 경로: %s" % path)
        except BadRequest as exc:
            self._err(400, str(exc))
        except NotFound as exc:
            self._err(404, str(exc))
        except Exception as exc:
            log("POST %s 처리 실패:\n%s" % (self.path, traceback.format_exc().rstrip()))
            self._err(500, "%s: %s" % (type(exc).__name__, exc))

    def _route(self):
        u = urlparse(self.path)
        path = u.path
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        q = parse_qs(u.query)
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path == "/api/index":
                return self._api_index(q)
            if path == "/api/episode":
                return self._api_episode(q)
            if path == "/api/scores":
                return self._json(SCORES.all())
            if path == "/api/export/pairs":
                return self._json(export_pairs())
            if path == "/api/status":
                return self._api_status()
            if path == "/api/srcabl/index":
                return self._srcabl_index()
            if path == "/api/srcabl":
                return self._srcabl(q)
            if path == "/frame":
                return self._frame(q)
            if path == "/clip":
                return self._clip(q)
            if path.startswith("/montage/"):
                return self._montage(path[len("/montage/"):])
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path.startswith("/api/") or path.startswith("/cache/"):
                return self._err(404, "알 수 없는 엔드포인트: %s" % path)
            return self._static(path.lstrip("/"))
        except BadRequest as exc:
            self._err(400, str(exc))
        except NotFound as exc:
            self._err(404, str(exc))
        except Exception as exc:
            log("GET %s 처리 실패:\n%s" % (self.path, traceback.format_exc().rstrip()))
            self._err(500, "%s: %s" % (type(exc).__name__, exc))

    # ---- body ----
    def _read_body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise BadRequest("Content-Length 가 올바르지 않습니다")
        if n <= 0:
            raise BadRequest("요청 본문이 비어 있습니다")
        if n > MAX_BODY:
            raise BadRequest("요청 본문이 너무 큽니다 (%d bytes)" % n)
        raw = self.rfile.read(n)
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise BadRequest("JSON 파싱 실패: %s" % exc)
        if not isinstance(obj, dict):
            raise BadRequest("요청 본문은 JSON 객체여야 합니다")
        return obj

    # ---- API ----
    def _api_index(self, q):
        refresh = (q.get("refresh", ["0"])[0] or "0").lower() in ("1", "true", "yes")
        idx = INDEX.get(refresh=refresh)
        self._json(idx, extra={"Cache-Control": "no-store"})

    def _api_episode(self, q):
        eid = (q.get("eid", [""])[0] or "").strip()
        run, ep = parse_eid(eid)
        refresh = (q.get("refresh", ["0"])[0] or "0").lower() in ("1", "true", "yes")
        payload = episode_payload(run, ep, use_cache=not refresh)
        self._json(payload, extra={"Cache-Control": "no-store"})

    # ---- 소스별 인과 기여도(source ablation) — dist/api/srcabl/ 정적 JSON ----
    def _srcabl_index(self):
        p = os.path.join(SRCABL_DIR, "index.json")
        if not os.path.isfile(p):
            raise NotFound("srcabl 인덱스 없음 (build_srcabl.py 를 먼저 실행하세요)")
        self._send_json_file(p)

    def _srcabl(self, q):
        eid = (q.get("eid", [""])[0] or "").strip()
        run, ep = parse_eid(eid)                       # 형식 검증 + run/ep 분해
        p = os.path.abspath(os.path.join(SRCABL_DIR, "%s_%04d.json" % (run, ep)))
        if not p.startswith(os.path.abspath(SRCABL_DIR) + os.sep):
            raise NotFound("잘못된 경로")
        if not os.path.isfile(p):
            raise NotFound("ablation 데이터 없음: %s/ep%04d" % (run, ep))
        self._send_json_file(p)

    def _send_json_file(self, path: str):
        with open(path, "rb") as f:
            data = f.read()
        self._send(200, data, "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})     # 재베이크가 바로 반영되게

    def _api_status(self):
        self._json({
            "ok": True,
            "root": ROOT,
            "data_root": DATA_ROOT,
            "runs": len(list_runs()),
            "ffmpeg": FFMPEG if os.path.exists(FFMPEG) else None,
            "metrics": MB.status(),
            "index_loaded": INDEX._idx is not None,
            "index_source": ("build_index" if INDEX._from_module else "server fallback"),
            "scores_path": SCORES_PATH,
        })

    def _post_scores(self, body):
        eid = str(body.get("eid") or "").strip()
        run, ep = parse_eid(eid)
        score = body.get("score", None)
        if score is not None:
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise BadRequest("score 는 1~5 정수 또는 null 이어야 합니다")
            score = int(score)
            if not (1 <= score <= 5):
                raise BadRequest("score 는 1~5 범위여야 합니다 (받은 값: %r)" % score)
        note = body.get("note", "")
        if note is None:
            note = ""
        if not isinstance(note, str):
            raise BadRequest("note 는 문자열이어야 합니다")
        entry = SCORES.set_score(make_eid(run, ep), score, note)
        self._json({"ok": True, "eid": make_eid(run, ep), "entry": entry})

    def _post_pairs(self, body):
        gid = str(body.get("gid") or "").strip()
        better = str(body.get("better") or "").strip()
        worse = str(body.get("worse") or "").strip()
        if not gid:
            raise BadRequest("gid 가 필요합니다")
        if not better or not worse:
            raise BadRequest("better/worse eid 가 모두 필요합니다")
        if better == worse:
            raise BadRequest("better 와 worse 가 같습니다")
        br, be = parse_eid(better)
        wr, we = parse_eid(worse)
        reason = body.get("reason", "")
        if reason is None:
            reason = ""
        if not isinstance(reason, str):
            raise BadRequest("reason 은 문자열이어야 합니다")
        rec = SCORES.add_pair(gid, make_eid(br, be), make_eid(wr, we), reason)
        self._json({"ok": True, "pair": rec,
                    "n_pairs": len(SCORES.all().get("pairs", []))})

    # ---- 프레임 ----
    def _frame(self, q):
        eid = (q.get("eid", [""])[0] or "").strip()
        run, ep = parse_eid(eid)
        cam = (q.get("cam", ["front"])[0] or "front").strip()
        if cam not in ("front", "wrist"):
            raise BadRequest("cam 은 front|wrist 여야 합니다 (받은 값: %r)" % cam)
        i_raw = q.get("i", ["0"])[0]
        try:
            i = int(i_raw)
        except (TypeError, ValueError):
            raise BadRequest("i 는 정수여야 합니다 (받은 값: %r)" % i_raw)
        if i < 0:
            raise BadRequest("i 는 0 이상이어야 합니다")
        p = os.path.join(ep_dir(run, ep), "frames", "f%05d_%s.jpg" % (i, cam))
        if not os.path.exists(p):
            raise NotFound("프레임 없음: %s/ep%04d %s #%d" % (run, ep, cam, i))
        with open(p, "rb") as f:
            data = f.read()
        self._send(200, data, "image/jpeg", {"Cache-Control": "max-age=3600"})

    # ---- 클립(Range 지원 스트리밍) ----
    def _clip(self, q):
        eid = (q.get("eid", [""])[0] or "").strip()
        run, ep = parse_eid(eid)
        cam = (q.get("cam", ["front"])[0] or "front").strip()
        if cam not in ALL_CAMS:
            raise BadRequest("cam 은 %s 중 하나여야 합니다 (받은 값: %r)" % ("|".join(ALL_CAMS), cam))
        try:
            path = ensure_clip(run, ep, cam)
        except NotFound:
            raise
        except Exception as exc:
            log("clip 생성 실패 %s/ep%04d %s: %s" % (run, ep, cam, exc))
            return self._err(500, "클립 생성 실패: %s" % exc)
        self._serve_file_range(path, "video/mp4", "max-age=3600")

    # ---- Task 몽타주(build_montage.py 산출물, dist/montage/) ----
    def _montage(self, rel: str):
        """/montage/<name>.mp4|.json — 이름은 [A-Za-z0-9_-] 만. manifest.json 이 없으면 빈 목록을 준다."""
        m = MONTAGE_RE.match(rel or "")
        if not m:
            raise NotFound("몽타주 경로가 아님: %s" % rel)
        p = os.path.abspath(os.path.join(MONTAGE_DIR, rel))
        if not p.startswith(os.path.abspath(MONTAGE_DIR) + os.sep):
            raise NotFound("잘못된 경로")
        if not os.path.isfile(p):
            if rel == "manifest.json":
                return self._json({"names": []}, 200, {"Cache-Control": "no-store"})
            raise NotFound("몽타주 없음: %s" % rel)
        if rel.endswith(".json"):
            with open(p, "rb") as f:
                data = f.read()
            return self._send(200, data, "application/json; charset=utf-8", {"Cache-Control": "no-store"})
        self._serve_file_range(p, "video/mp4", "max-age=3600")

    def _serve_file_range(self, path: str, ctype: str, cache: str = "no-store") -> None:
        size = os.path.getsize(path)
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        partial = False
        if rng:
            m = re.match(r"^bytes=(\d*)-(\d*)(?:,.*)?$", rng.strip())
            if not m or (not m.group(1) and not m.group(2)):
                self._send(416, b"", "text/plain; charset=utf-8",
                           {"Content-Range": "bytes */%d" % size})
                return
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:                                   # suffix range
                length = int(m.group(2))
                start = max(0, size - length)
                end = size - 1
            if start >= size or start > end:
                self._send(416, b"", "text/plain; charset=utf-8",
                           {"Content-Range": "bytes */%d" % size})
                return
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

    # ---- 정적 파일 ----
    def _static(self, rel: str) -> None:
        rel = rel.strip("/")
        if not rel:
            rel = "index.html"
        if ".." in rel.split("/") or rel.startswith("/") or "\\" in rel:
            raise NotFound("not found")
        cands = [os.path.join(ROOT, "static", rel), os.path.join(ROOT, rel)]
        for p in cands:
            p = os.path.abspath(p)
            if not p.startswith(ROOT + os.sep):
                continue
            if os.path.isfile(p):
                ext = os.path.splitext(p)[1].lower()
                if ext in (".mp4", ".mov"):
                    return self._serve_file_range(p, "video/mp4")
                if ext not in STATIC_TYPES:      # .py 등 소스는 서빙하지 않는다
                    break
                ctype = STATIC_TYPES[ext]
                with open(p, "rb") as f:
                    data = f.read()
                cache = "no-store" if ext in (".html", ".js", ".css") else "max-age=600"
                return self._send(200, data, ctype, {"Cache-Control": cache})
        if rel == "index.html":
            msg = (
                "<!doctype html><meta charset='utf-8'>"
                "<title>rollout_viewer</title>"
                "<body style='font:14px ui-monospace,monospace;padding:24px'>"
                "<h2>index.html 이 아직 없습니다</h2>"
                "<p>프론트엔드 담당이 <code>%s/index.html</code> 을 작성하면 여기에 표시됩니다.</p>"
                "<p>서버 API 는 동작 중입니다: "
                "<a href='/api/index'>/api/index</a> · "
                "<a href='/api/status'>/api/status</a></p></body>" % ROOT
            )
            return self._send(200, msg.encode("utf-8"), "text/html; charset=utf-8",
                              {"Cache-Control": "no-store"})
        raise NotFound("파일 없음: %s" % rel)
