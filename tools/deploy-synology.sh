#!/usr/bin/env bash
# deploy-synology.sh — CLI deploy helper for ScaleTail Synology services
# Usage: ./tools/deploy-synology.sh <service|--list|--all> [up|down|logs|status]
set -euo pipefail

# --- Config ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICES_DIR="$PROJECT_ROOT/synology/services"
ENV_SHARED="$PROJECT_ROOT/synology/.env.shared"
DATA_ROOT="${DATA_ROOT:-/volume1/docker}"
export DATA_ROOT

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { printf "${CYAN}>>>${NC} %s\n" "$*"; }
ok()    { printf "${GREEN}>>>${NC} %s\n" "$*"; }
warn()  { printf "${YELLOW}>>>${NC} %s\n" "$*"; }
err()   { printf "${RED}>>>${NC} %s\n" "$*" >&2; }

# --- Helpers ---
list_services() {
  local count=0
  for d in "$SERVICES_DIR"/*/; do
    [ -f "$d/compose.yaml" ] || continue
    printf "  %s\n" "$(basename "$d")"
    ((count++))
  done
  info "$count services available under synology/services/"
}

validate_env() {
  if [ ! -f "$ENV_SHARED" ]; then
    err "Missing $ENV_SHARED — run from the ScaleTail project root."
    exit 1
  fi
  # Source it so downstream compose inherits the vars
  set -a
  # shellcheck disable=SC1090
  source "$ENV_SHARED"
  set +a
  if [ -z "${TS_AUTHKEY:-}" ]; then
    err "TS_AUTHKEY is empty in $ENV_SHARED"
    err "Generate a key at: https://login.tailscale.com/admin/settings/keys"
    exit 1
  fi
}

check_docker() {
  if ! command -v docker &>/dev/null; then
    err "Docker is not installed or not in PATH."
    exit 1
  fi
  if ! docker info &>/dev/null; then
    err "Docker daemon is not running."
    exit 1
  fi
}

run_compose() {
  local svc_dir="$1"
  local action="$2"
  local svc_name
  svc_name="$(basename "$svc_dir")"

  # Build the env-file flag — use per-service .env if it exists, fall back to shared
  local env_file="$ENV_SHARED"
  [ -f "$svc_dir/.env" ] && env_file="$svc_dir/.env"

  case "$action" in
    up)
      info "Deploying ${BOLD}$svc_name${NC}..."
      docker compose -f "$svc_dir/compose.yaml" --env-file "$env_file" up -d
      ok "$svc_name is up."
      ;;
    down)
      info "Stopping ${BOLD}$svc_name${NC}..."
      docker compose -f "$svc_dir/compose.yaml" --env-file "$env_file" down
      ok "$svc_name is down."
      ;;
    logs)
      info "Tailing logs for ${BOLD}$svc_name${NC} (Ctrl-C to stop)..."
      docker compose -f "$svc_dir/compose.yaml" --env-file "$env_file" logs -f
      ;;
    status)
      info "Status of ${BOLD}$svc_name${NC}:"
      docker compose -f "$svc_dir/compose.yaml" --env-file "$env_file" ps
      ;;
    *)
      err "Unknown action: $action (expected up|down|logs|status)"
      exit 1
      ;;
  esac
}

usage() {
  cat <<EOF
${BOLD}ScaleTail Synology Deploy Helper${NC}

Usage:
  $(basename "$0") <service>          Deploy a service (default: up)
  $(basename "$0") <service> <action> Run action on a service
  $(basename "$0") --list             List available services
  $(basename "$0") --all <action>     Run action on all services

Actions: up (default), down, logs, status

Environment:
  DATA_ROOT  Override data volume path (default: /volume1/docker)

Examples:
  $(basename "$0") adguardhome
  $(basename "$0") plex down
  $(basename "$0") --all status
EOF
}

# --- Main ---
if [ $# -lt 1 ]; then
  usage
  exit 1
fi

case "$1" in
  -h|--help)
    usage
    exit 0
    ;;
  --list)
    info "Available services:"
    list_services
    exit 0
    ;;
  --all)
    action="${2:-up}"
    validate_env
    check_docker
    info "Running '$action' on all services..."
    for d in "$SERVICES_DIR"/*/; do
      [ -f "$d/compose.yaml" ] || continue
      run_compose "$d" "$action"
    done
    ok "Batch '$action' complete."
    exit 0
    ;;
  -*)
    err "Unknown flag: $1"
    usage
    exit 1
    ;;
esac

# Single service mode
SERVICE="$1"
ACTION="${2:-up}"
SVC_DIR="$SERVICES_DIR/$SERVICE"

if [ ! -d "$SVC_DIR" ] || [ ! -f "$SVC_DIR/compose.yaml" ]; then
  err "Service '$SERVICE' not found under synology/services/"
  warn "Run with --list to see available services."
  exit 1
fi

validate_env
check_docker
run_compose "$SVC_DIR" "$ACTION"
