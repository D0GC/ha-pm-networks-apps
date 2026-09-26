# Offizielles Base-Image (s6-overlay v3, bashio, tzdata). Multi-Arch-Tag (amd64 + arm64/aarch64),
# der Supervisor (ab 2026.04 ohne build.yaml) baut damit für die jeweilige Plattform.
FROM ghcr.io/home-assistant/base-python:3.13-alpine3.24-2026.08.0

ARG BUILD_VERSION=1.0.1
ARG BUILD_ARCH=amd64

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt /tmp/requirements.txt
RUN pip install --only-binary=:all: -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

COPY app /opt/pm_klima_studio
COPY rootfs /
RUN chmod a+x /etc/s6-overlay/s6-rc.d/pm-klima-studio/run /etc/s6-overlay/s6-rc.d/pm-klima-studio/finish \
    && python3 -m compileall -q /opt/pm_klima_studio

WORKDIR /opt/pm_klima_studio

LABEL \
  io.hass.name="PM Klima Studio" \
  io.hass.description="Heizplan-Editor und Klima-Auswertungen für PM Klima" \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="aarch64|amd64" \
  org.opencontainers.image.title="PM Klima Studio" \
  org.opencontainers.image.licenses="MIT"
