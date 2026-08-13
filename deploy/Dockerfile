# rollout_viewer 정적 배포 이미지.
# 데이터(clips/, api/)는 이미지에 굽지 않고 bind mount 로 붙인다.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIEW_PORT=8081 \
    VIEW_HOST=0.0.0.0 \
    VIEW_ROOT=/app

WORKDIR /app

# 앱 파일 COPY — serve.py + index.html + static/(뷰어 CSS/JS).
# 무거운 데이터(clips/, api/)는 .dockerignore 가 제외하고 런타임에 bind mount 한다.
# index.html/static/ 이 아직 없어도(서버만 배포) 빌드가 깨지지 않도록 통짜 COPY 를 쓴다
# (COPY stati[c] 류 글롭은 디렉터리가 없으면 빌드가 실패한다).
COPY . /app/

EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('VIEW_PORT','8081'), timeout=2).read().startswith(b'ok') else 1)"

CMD ["python", "/app/serve.py"]
