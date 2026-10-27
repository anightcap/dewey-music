FROM python:3.12-alpine
RUN apk add --no-cache tzdata ffmpeg && pip install --no-cache-dir mutagen numpy
# time zone for run names, logs and the console: docker build --build-arg TZ=America/New_York .
ARG TZ=UTC
ENV TZ=$TZ \
    PYTHONUNBUFFERED=1 \
    MD_MUSIC=/media/Music \
    MD_OUT=/out \
    MD_RULES=/rules.txt \
    MD_NDDB=/navidrome-data/navidrome.db \
    MD_QUAR=/media/_dedupe-quarantine
WORKDIR /app
# The application is baked in so the image runs on its own (docker compose). dewey.sh bind-mounts the same
# files over these, so edits take effect there without a rebuild.
COPY dewey.py runner.py webui.py /app/
COPY web /app/web
ENTRYPOINT ["python", "-u", "/app/dewey.py"]
