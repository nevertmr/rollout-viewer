# rollout_viewer 정적 배포 이미지.
# 데이터(clips/, api/)는 이미지에 굽지 않고 bind mount 로 붙인다.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIEW_PORT=8081 \
    VIEW_HOST=0.0.0.0 \
    VIEW_ROOT=/app

WORKDIR /app

# 앱 파일만 COPY (clips/ 와 api/ 는 런타임 마운트).
# index.htm[l] 은 글롭이라 파일이 아직 없어도 빌드가 깨지지 않는다.
COPY serve.py index.htm[l] /app/

EXPOSE 8081

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('VIEW_PORT','8081'), timeout=2).read().startswith(b'ok') else 1)"

CMD ["python", "/app/serve.py"]
