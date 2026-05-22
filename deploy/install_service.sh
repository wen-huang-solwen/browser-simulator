#!/usr/bin/env bash
# Install the Reels Scraper as a systemd service with auto-restart.
#
# Run as root (or with sudo) on the Linux server:
#   sudo bash deploy/install_service.sh
#
# Override the Python interpreter (e.g. a virtualenv) with:
#   sudo PYTHON_BIN=/path/to/venv/bin/python bash deploy/install_service.sh
set -euo pipefail

SERVICE_NAME="reels-scraper"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || command -v python || true)}"
UNIT_TEMPLATE="${PROJECT_DIR}/deploy/${SERVICE_NAME}.service"
UNIT_DST="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ -z "$PYTHON_BIN" ]]; then
  echo "ERROR: python not found. Re-run with PYTHON_BIN=/path/to/python" >&2
  exit 1
fi
if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: must run as root (use sudo)." >&2
  exit 1
fi

echo "Project dir: $PROJECT_DIR"
echo "Python:      $PYTHON_BIN"
echo "Installing:  $UNIT_DST"

sed -e "s#@PROJECT_DIR@#${PROJECT_DIR}#g" \
    -e "s#@PYTHON_BIN@#${PYTHON_BIN}#g" \
    "$UNIT_TEMPLATE" > "$UNIT_DST"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo
echo "Done. The service now starts on boot and restarts automatically if it dies."
echo "  Status:  systemctl status ${SERVICE_NAME}"
echo "  Logs:    journalctl -u ${SERVICE_NAME} -f"
echo "  Restart: systemctl restart ${SERVICE_NAME}"
