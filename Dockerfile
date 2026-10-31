# Imagen del Conversor para la arquitectura en la que se construye (tarea 24.3, MVP).
# FFmpeg sale del paquete de Debian (con GPL). No incluye whisper.cpp ni el modelo.
# La variante multiarquitectura queda para después.

FROM python:3.12-slim-trixie AS python-deps

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/
WORKDIR /opt/converter
ENV UV_PROJECT_ENVIRONMENT=/opt/converter/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim-trixie

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libreoffice-writer-nogui \
        fonts-dejavu-core \
        fonts-liberation \
        fonts-crosextra-carlito \
        fonts-crosextra-caladea \
        libmp3lame0 \
        libvorbisenc2 \
        libopus0 \
        libvpx9 \
        libopenh264-8 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libgdk-pixbuf-2.0-0 \
        libcairo2 \
        shared-mime-info \
        fontconfig \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin converter \
    && mkdir -p \
        /var/lib/converter/storage \
        /opt/converter/fonts/default \
        /opt/converter/fontconfig/cache \
        /opt/converter/libreoffice \
    && cp /usr/share/fonts/truetype/dejavu/DejaVu*.ttf /opt/converter/fonts/default/

COPY --from=python-deps /opt/converter/venv /opt/converter/venv
COPY src /opt/converter/src
COPY docker/fontconfig/default-only.conf /opt/converter/fontconfig/default-only.conf
COPY docker/libreoffice/profile /opt/converter/libreoffice/profile

ENV PYTHONPATH=/opt/converter/src \
    PATH=/opt/converter/venv/bin:${PATH}

RUN FONTCONFIG_FILE=/opt/converter/fontconfig/default-only.conf fc-cache -f \
    && chown -R converter:converter /var/lib/converter /opt/converter

USER converter
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=8s --start-period=60s --start-interval=2s --retries=3 \
    CMD ["/opt/converter/venv/bin/python", "-m", "converter.healthcheck"]
ENTRYPOINT ["/opt/converter/venv/bin/python", "-m", "converter"]
