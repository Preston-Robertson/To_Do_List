#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this installer as root." >&2
  exit 1
fi

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
STATE_DIR=/var/lib/luigi-maintainer
QUEUE_DIR=/var/lib/luigi-maintainer-queue
ENV_DIR=/etc/luigi-web
ENV_FILE=$ENV_DIR/maintainer.env

for command in git gh systemctl; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Required command is unavailable: $command" >&2
    exit 1
  fi
done
if ! id luigi-web >/dev/null 2>&1; then
  echo "The luigi-web service account does not exist." >&2
  exit 1
fi

if ! getent group luigi-maintainer >/dev/null 2>&1; then
  groupadd --system luigi-maintainer
fi
if ! getent group luigi-maintainer-queue >/dev/null 2>&1; then
  groupadd --system luigi-maintainer-queue
fi
if ! id luigi-maintainer >/dev/null 2>&1; then
  useradd --system --gid luigi-maintainer --home-dir "$STATE_DIR" \
    --no-create-home --shell /usr/sbin/nologin luigi-maintainer
fi
usermod -a -G luigi-maintainer-queue luigi-maintainer
usermod -a -G luigi-maintainer-queue luigi-web

install -d -o luigi-maintainer -g luigi-maintainer -m 0700 "$STATE_DIR"
install -d -o luigi-maintainer -g luigi-maintainer-queue -m 2770 "$QUEUE_DIR"
install -d -o root -g root -m 0755 "$ENV_DIR"
install -o root -g root -m 0600 \
  "$ROOT/maintainer.env.example" "$ENV_DIR/maintainer.env.example"
install -o root -g root -m 0644 \
  "$ROOT/luigi-maintainer.service" /etc/systemd/system/luigi-maintainer.service
install -o root -g root -m 0644 \
  "$ROOT/luigi-maintainer.timer" /etc/systemd/system/luigi-maintainer.timer

DROPIN_DIR=/etc/systemd/system/luigi-web.service.d
install -d -o root -g root -m 0755 "$DROPIN_DIR"
cat > "$DROPIN_DIR/maintainer.conf" <<'EOF'
[Service]
SupplementaryGroups=luigi-maintainer-queue
Environment=LUIGI_WEB_MAINTAINER_DB=/var/lib/luigi-maintainer-queue/maintainer.db
ReadWritePaths=/var/lib/luigi-maintainer-queue
UMask=0007
EOF
chmod 0644 "$DROPIN_DIR/maintainer.conf"

systemctl daemon-reload
systemctl try-restart luigi-web.service >/dev/null 2>&1 || true
if [ -f "$ENV_FILE" ]; then
  chown root:root "$ENV_FILE"
  chmod 0600 "$ENV_FILE"
  systemctl enable --now luigi-maintainer.timer
  echo "Maintainer installed and the daily timer is active."
else
  systemctl disable --now luigi-maintainer.timer >/dev/null 2>&1 || true
  echo "Maintainer installed but inactive. Create $ENV_FILE from the root-owned example, then rerun this installer."
fi
