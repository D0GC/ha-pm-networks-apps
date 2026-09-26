#!/usr/bin/env bash
# Lokaler Container-Test: baut das Image (amd64), startet Fake-HA und App in einem eigenen Netz
# und prüft per HTTP von einer erlaubten und einer fremden IP.
# Optional: PIP_CA=/pfad/zur/ca.crt für Umgebungen mit TLS-Proxy (wird nur in eine Kopie des Kontexts gelegt).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CTX="$(mktemp -d)"; DATA="$(mktemp -d)"
cp -r "$ROOT/pm_klima_studio/." "$CTX/"
if [[ -n "${PIP_CA:-}" ]]; then
  cp "$PIP_CA" "$CTX/proxy-ca.crt"
  sed -i 's#^COPY requirements.txt /tmp/requirements.txt#&\nCOPY proxy-ca.crt /tmp/proxy-ca.crt\nENV PIP_CERT=/tmp/proxy-ca.crt#' "$CTX/Dockerfile"
fi
docker build --platform linux/amd64 -t local/pm_klima_studio:1.0.1 "$CTX" >/dev/null
echo '{"bericht_tag":"sonntag","bericht_uhrzeit":"18:00","bericht_notify":[],"raeume_override":[],"log_level":"info"}' > "$DATA/options.json"
docker rm -f pmks-fake pmks-app >/dev/null 2>&1 || true
docker network rm pmks-net >/dev/null 2>&1 || true
docker network create --subnet 172.31.99.0/24 pmks-net >/dev/null
docker run -d --name pmks-fake --network pmks-net --ip 172.31.99.10 -v "$ROOT/tests:/tests:ro" \
  --entrypoint python3 local/pm_klima_studio:1.0.1 /tests/fake_ha.py --host 0.0.0.0 --port 8123 >/dev/null
sleep 3
docker run -d --name pmks-app --network pmks-net --ip 172.31.99.20 -v "$DATA:/data" -e SUPERVISOR_TOKEN=test-token \
  -e PMKS_HA_API=http://172.31.99.10:8123/core/api -e PMKS_HA_WS=ws://172.31.99.10:8123/core/websocket \
  -e PMKS_ALLOWED_IPS=172.31.99.2 local/pm_klima_studio:1.0.1 >/dev/null
sleep 6
probe() { docker run --rm --network pmks-net --ip "$1" --entrypoint sh local/pm_klima_studio:1.0.1 -c "$2"; }
probe 172.31.99.2 'for p in api/health api/info api/aktuell "api/uebersicht?bereich=30d" api/heizplan/wohnzimmer "api/auswertung/kuche?bereich=24h"; do curl -s -o /dev/null -w "$p %{http_code}\n" "http://172.31.99.20:8099/$p"; done'
probe 172.31.99.3 'curl -s -o /dev/null -w "fremde IP api/info %{http_code}\n" http://172.31.99.20:8099/api/info'
docker logs pmks-app 2>&1 | grep -E "klimastudio|ERROR" | tail -5
docker stats --no-stream --format "Speicher {{.MemUsage}}" pmks-app
docker rm -f pmks-fake pmks-app >/dev/null; docker network rm pmks-net >/dev/null
rm -rf "$CTX" "$DATA"
