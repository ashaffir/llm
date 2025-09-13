#!/bin/bash
# manage.sh — LLM monitoring stack manager (Prometheus + Grafana + cAdvisor)
# Controls an existing stack created from separate artifacts:
#   docker-compose.yml
#   prometheus/prometheus.yml
#   grafana/provisioning/datasources/datasource.yml
#   grafana/provisioning/dashboards/dashboards.yml
#   grafana/provisioning/dashboards/llm_overview.json

set -Eeuo pipefail

############################################
# Config (env overridable)
############################################
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="${STACK_DIR:-$SCRIPT_DIR}"
HOST_IP="${HOST_IP:-}"
GF_USER="${GF_USER:-admin}"
GF_PASS="${GF_PASS:-admin123}"

# Defaults (ports must match your compose/nginx)
PORT_NGINX=8080     # /metrics via Nginx → model-runner
PORT_PROM=9090
PORT_GRAFANA=3000
PORT_CAD=8081

############################################
# Utilities
############################################
# Colors (fallback-safe)
if command -v tput >/dev/null 2>&1 && [[ -t 1 ]]; then
  GREEN(){ tput setaf 2; printf "%s\n" "$*"; tput sgr0; }
  RED()  { tput setaf 1; printf "%s\n" "$*"; tput sgr0; }
  YEL()  { tput setaf 3; printf "%s\n" "$*"; tput sgr0; }
else
  GREEN(){ printf "%s\n" "$*"; }
  RED()  { printf "%s\n" "$*"; }
  YEL()  { printf "%s\n" "$*"; }
fi

need() { command -v "$1" >/dev/null 2>&1 || { RED "Missing dependency: $1"; exit 1; }; }

compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    echo "docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    echo "docker-compose"
  else
    RED "Docker Compose not found (need Docker Compose plugin or docker-compose)."
    exit 1
  fi
}

detect_host_ip() {
  if [[ -n "$HOST_IP" ]]; then echo "$HOST_IP"; return; fi
  # Try to detect LAN IP that matches nginx allowlist/subnet
  if command -v ip >/dev/null 2>&1; then
    ip route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}' | grep -Eo '([0-9]{1,3}\.){3}[0-9]{1,3}' || true
  fi
}

############################################
# File checks
############################################
assert_artifacts() {
  local missing=0
  local files=(
    "docker-compose.yml"
    "prometheus/prometheus.yml"
    "grafana/provisioning/datasources/datasource.yml"
    "grafana/provisioning/dashboards/dashboards.yml"
    "grafana/provisioning/dashboards/llm_overview.json"
  )
  for f in "${files[@]}"; do
    if [[ ! -f "$STACK_DIR/$f" ]]; then
      RED "Missing: $f"
      missing=1
    fi
  done
  [[ $missing -eq 0 ]] || { RED "Place the artifacts above into $STACK_DIR"; exit 1; }
}

############################################
# Core actions
############################################
up() {
  assert_artifacts
  export GF_USER GF_PASS
  local dc; dc="$(compose_cmd)"
  $dc -f "$STACK_DIR/docker-compose.yml" up -d
  GREEN "Stack starting."
}

down() {
  local dc; dc="$(compose_cmd)"
  $dc -f "$STACK_DIR/docker-compose.yml" down
  GREEN "Stack stopped."
}

restart() { down; up; }

status() {
  local dc; dc="$(compose_cmd)"
  $dc -f "$STACK_DIR/docker-compose.yml" ps
}

logs() {
  local svc="${1:-}"
  local dc; dc="$(compose_cmd)"
  if [[ -n "$svc" ]]; then
    $dc -f "$STACK_DIR/docker-compose.yml" logs -f "$svc"
  else
    $dc -f "$STACK_DIR/docker-compose.yml" logs --tail=200
  fi
}

clean() {
  local dc; dc="$(compose_cmd)"
  $dc -f "$STACK_DIR/docker-compose.yml" down -v
  GREEN "Volumes removed."
}

info() {
  local ip="${HOST_IP:-$(detect_host_ip)}"
  [[ -n "$ip" ]] || { YEL "HOST_IP not set and auto-detect failed. Export HOST_IP=..."; ip="<HOST_IP>"; }
  cat <<EOF
Endpoints:
  - Model Runner metrics: http://$ip:$PORT_NGINX/metrics
  - Prometheus UI:       http://$ip:$PORT_PROM
  - Grafana UI:          http://$ip:$PORT_GRAFANA  (user: $GF_USER)
  - cAdvisor UI:         http://$ip:$PORT_CAD
Stack dir: $STACK_DIR
EOF
}

open_urls() {
  local ip="${HOST_IP:-$(detect_host_ip)}"
  need xdg-open || { YEL "xdg-open not available; skipping."; return 0; }
  xdg-open "http://$ip:$PORT_GRAFANA" >/dev/null 2>&1 || true
  xdg-open "http://$ip:$PORT_PROM" >/dev/null 2>&1 || true
  xdg-open "http://$ip:$PORT_CAD" >/dev/null 2>&1 || true
}

############################################
# Health checks
############################################
http_code() { curl -fsS -o /dev/null -w "%{http_code}" -X GET "$1" || true; }

test_stack() {
  assert_artifacts
  local ip="${HOST_IP:-$(detect_host_ip)}"
  [[ -n "$ip" ]] || { RED "Cannot determine HOST_IP. Export HOST_IP=..."; exit 1; }

  local ok=1

  # /metrics via Nginx
  local code; code="$(http_code "http://$ip:$PORT_NGINX/metrics")"
  if [[ "$code" == "200" ]]; then
    if curl -fsS "http://$ip:$PORT_NGINX/metrics" | head -n 50 | grep -E -q '(^# HELP|llamacpp:|requests_total)'; then
      GREEN "Model Runner /metrics reachable and content OK."
    else
      YEL "Model Runner /metrics reachable but content unexpected."
    fi
  else
    RED "Model Runner /metrics NOT OK (HTTP $code)."; ok=0
  fi

  # Prometheus ready & targets
  code="$(http_code "http://$ip:$PORT_PROM/-/ready")"
  if [[ "$code" == "200" ]]; then
    GREEN "Prometheus ready."
  else
    RED "Prometheus not ready (HTTP $code)."; ok=0
  fi
  if curl -fsS "http://$ip:$PORT_PROM/api/v1/targets" | grep -q '"health":"up"'; then
    GREEN "Prometheus targets are UP."
  else
    RED "Prometheus targets not UP."; ok=0
  fi

  # Grafana
  code="$(http_code "http://$ip:$PORT_GRAFANA/login")"
  if [[ "$code" == "200" ]]; then
    GREEN "Grafana reachable."
  else
    RED "Grafana NOT reachable (HTTP $code)."; ok=0
  fi

  # cAdvisor
  code="$(http_code "http://$ip:$PORT_CAD/metrics")"
  if [[ "$code" == "200" ]]; then
    GREEN "cAdvisor metrics reachable."
  else
    RED "cAdvisor metrics NOT reachable (HTTP $code)."; ok=0
  fi

  if [[ $ok -eq 1 ]]; then
    GREEN "✅ All checks passed."
    info
  else
    RED "Some checks failed."
    exit 1
  fi
}

############################################
# Interactive menu
############################################
menu() {
  trap 'echo; echo "Exiting…"; exit 0' INT
  while true; do
    echo
    echo "=== LLM Monitoring Stack ==="
    echo "1) Up"
    echo "2) Down"
    echo "3) Restart"
    echo "4) Status"
    echo "5) Logs (choose service)"
    echo "6) Test health"
    echo "7) Clean (remove volumes)"
    echo "8) Show endpoints"
    echo "9) Open UIs (xdg-open)"
    echo "0) Exit"
    read -rp "Select [0-9]: " ans
    case "$ans" in
      1) up ;;
      2) down ;;
      3) restart ;;
      4) status ;;
      5) read -rp "Service [prometheus|grafana|cadvisor|<enter>=all]: " svc; if [[ -n "${svc:-}" ]]; then logs "$svc"; else logs; fi ;;
      6) test_stack ;;
      7) clean ;;
      8) info ;;
      9) open_urls ;;
      0) break ;;
      *) echo "Invalid selection." ;;
    esac
  done
}

############################################
# Main
############################################
need docker
need curl

case "${1:-menu}" in
  up)       up ;;
  down)     down ;;
  restart)  restart ;;
  status)   status ;;
  logs)     shift || true; logs "${1:-}" ;;
  test)     test_stack ;;
  clean)    clean ;;
  info)     info ;;
  open)     open_urls ;;
  menu|"")  menu ;;
  *)
    cat <<USAGE
Usage: $0 [command]

Commands:
  up         Start the stack
  down       Stop the stack
  restart    Restart the stack
  status     Show container status
  logs [svc] Tail logs (optionally for a single service)
  test       Run health checks against all endpoints
  clean      Stop and remove volumes
  info       Print endpoints and config
  open       Open Grafana/Prometheus/cAdvisor in browser (xdg-open)
  menu       Interactive TUI (default)
Env vars:
  STACK_DIR (default: script dir)
  HOST_IP   (auto-detected if empty)
  GF_USER   (default: $GF_USER)
  GF_PASS   (default: $GF_PASS)
USAGE
    exit 1
    ;;
esac

