# acroscope: the player and the API over a plain bind mount of the data dir (see compose.yaml).
FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core sqlite3 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY acroscope ./acroscope
# orangebox 0.5.0's wheel declares a broken console script (`bb2csv = scripts`, no callable), so pip exits 1
# AFTER installing the package. Install it on its own, tolerate that exit code, and prove the import instead.
RUN pip install --no-cache-dir orangebox==0.5.0 || true; python -c "import orangebox" \
    && pip install --no-cache-dir --no-deps . && mkdir -p /data /cache /state && chmod 777 /cache /state
ENV ACROSCOPE_DATA=/data ACROSCOPE_CACHE=/cache ACROSCOPE_DB=/state/acroscope.db
EXPOSE 8070
# runs as whatever `user:` compose sets (the bind mount's owner); root when unset
CMD ["acroscope", "serve", "--host", "0.0.0.0", "--port", "8070"]
