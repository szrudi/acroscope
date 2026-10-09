# acroscope player + Google Drive mount in one container (see compose.yaml and entrypoint.sh).
FROM python:3.13-slim
ARG RCLONE_VERSION=1.75.2
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fuse3 curl unzip ca-certificates \
    && curl -fsSL "https://downloads.rclone.org/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-amd64.zip" -o /tmp/rclone.zip \
    && unzip -j /tmp/rclone.zip '*/rclone' -d /usr/local/bin && chmod 755 /usr/local/bin/rclone && rm /tmp/rclone.zip \
    && sed -i 's/^#user_allow_other/user_allow_other/' /etc/fuse.conf \
    && apt-get purge -y curl unzip && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY acroscope ./acroscope
# orangebox 0.5.0's wheel declares a broken console script (`bb2csv = scripts`, no callable), so pip exits 1
# AFTER installing the package. Install it on its own, tolerate that exit code, and prove the import instead.
RUN pip install --no-cache-dir orangebox==0.5.0 || true; python -c "import orangebox" \
    && pip install --no-cache-dir --no-deps . && mkdir -p /data /cache
ENV ACROSCOPE_DATA=/data ACROSCOPE_CACHE=/cache
COPY entrypoint.sh /entrypoint.sh
EXPOSE 8070
ENTRYPOINT ["/entrypoint.sh"]
