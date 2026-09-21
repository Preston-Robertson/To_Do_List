#!/bin/sh
set -eu

case "${1:-}" in
  --install) [ "$#" -eq 1 ] || exit 2 ;;
  --help)
    echo "Usage: $0 --install"
    echo "Install inactive templates only. Operator review and explicit activation are required."
    exit 0
    ;;
  *) echo "Usage: $0 --install (never enables services)" >&2; exit 2 ;;
esac

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this installer as root." >&2
  exit 1
fi

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
STATE_DIR=/var/lib/luigi-maintainer
QUEUE_DIR=/var/lib/luigi-maintainer-queue
ENV_DIR=/etc/luigi-web
ROLE_ENV_DIR=$ENV_DIR/maintainer-review
TEMPLATES=$ROOT/examples/maintainer-review

for command in systemctl getent groupadd useradd usermod install; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Required command is unavailable: $command" >&2
    exit 1
  fi
done
if ! id luigi-web >/dev/null 2>&1; then
  echo "The luigi-web service account does not exist." >&2
  exit 1
fi

for role in legacy generate publish refresh notify release preview; do
  unit=luigi-maintainer-$role.timer
  if [ "$role" = legacy ]; then unit=luigi-maintainer.timer; fi
  if systemctl cat "$unit" >/dev/null 2>&1; then
    systemctl disable --now "$unit"
  fi
done
for role in legacy generate publish refresh notify release preview gateway; do
  unit=luigi-maintainer-$role.service
  if [ "$role" = legacy ]; then unit=luigi-maintainer.service; fi
  if systemctl is-active --quiet "$unit"; then
    echo "Refusing to replace an active controller: $unit. Timers remain disabled." >&2
    exit 1
  fi
done

if ! getent group luigi-maintainer-queue >/dev/null 2>&1; then
  groupadd --system luigi-maintainer-queue
fi
for account in luigi-maintainer luigi-maintainer-notify luigi-maintainer-gateway; do
  if ! getent group "$account" >/dev/null 2>&1; then
    groupadd --system "$account"
  fi
  if ! id "$account" >/dev/null 2>&1; then
    useradd --system --gid "$account" --home-dir "/var/lib/$account" \
      --no-create-home --shell /usr/sbin/nologin "$account"
  fi
  usermod -a -G luigi-maintainer-queue "$account"
  install -d -o "$account" -g "$account" -m 0700 "/var/lib/$account"
done
usermod -a -G luigi-maintainer-queue luigi-web

install -d -o luigi-maintainer -g luigi-maintainer -m 0700 "$STATE_DIR/copilot"
install -d -o luigi-maintainer -g luigi-maintainer-queue -m 2770 "$QUEUE_DIR"
install -d -o luigi-maintainer -g luigi-maintainer-queue -m 2750 "$QUEUE_DIR/review-artifacts"
install -d -o luigi-maintainer -g luigi-maintainer-queue -m 2750 /var/lib/luigi-maintainer-preview
install -d -o luigi-maintainer -g luigi-maintainer-queue -m 2750 /var/lib/luigi-maintainer-preview/r
install -d -o luigi-maintainer -g luigi-maintainer -m 0700 /var/lib/luigi-maintainer-preview/sources
install -d -o root -g root -m 0755 "$ENV_DIR"
install -d -o root -g root -m 0700 "$ROLE_ENV_DIR"
install -o root -g root -m 0600 \
  "$ROOT/maintainer.env.example" "$ROLE_ENV_DIR/generate.env.example"
for role in publish refresh notify release preview gateway web; do
  install -o root -g root -m 0600 \
    "$TEMPLATES/$role.env.example" "$ROLE_ENV_DIR/$role.env.example"
done
install -o root -g root -m 0644 \
  "$ROOT/luigi-maintainer.service" /etc/systemd/system/luigi-maintainer.service
install -o root -g root -m 0644 \
  "$ROOT/luigi-maintainer.timer" /etc/systemd/system/luigi-maintainer.timer

for role in generate publish refresh notify release preview gateway; do
  unit=luigi-maintainer-$role.service
  install -o root -g root -m 0644 "$TEMPLATES/$unit" "/etc/systemd/system/$unit"
  install -d -o root -g root -m 0755 "/etc/systemd/system/$unit.d"
  install -o root -g root -m 0644 "$TEMPLATES/common.conf" "/etc/systemd/system/$unit.d/10-boundary.conf"
  case "$role" in
    generate|preview) ;;
    *) install -o root -g root -m 0644 "$TEMPLATES/restricted.conf" "/etc/systemd/system/$unit.d/20-restricted.conf" ;;
  esac
  if [ "$role" != gateway ]; then
    timer=luigi-maintainer-$role.timer
    install -o root -g root -m 0644 "$TEMPLATES/$timer" "/etc/systemd/system/$timer"
  fi
done
install -d -o root -g root -m 0755 /etc/systemd/system/luigi-maintainer.service.d
install -o root -g root -m 0644 "$TEMPLATES/common.conf" \
  /etc/systemd/system/luigi-maintainer.service.d/10-boundary.conf

DROPIN_DIR=/etc/systemd/system/luigi-web.service.d
install -d -o root -g root -m 0755 "$DROPIN_DIR"
install -o root -g root -m 0644 "$TEMPLATES/web-queue.conf" "$DROPIN_DIR/maintainer.conf"

systemctl daemon-reload
systemctl disable --now luigi-maintainer.timer
for role in generate publish refresh notify release preview; do
  systemctl disable --now "luigi-maintainer-$role.timer"
done
systemctl disable luigi-maintainer-gateway.service
echo "Phase 2 templates installed inactive. No services were started or restarted; actual role env files are unchanged."
echo "Prerequisites: trusted controller, separately built pinned image, rootless Podman, subuid/subgid, delegated cgroup v2."
echo "Also required: separate role credentials, protected GitHub checks/reviews, HTTPS gateway, preview CLI integration."
echo "Review docs/autonomous-maintainer.md and validate the Linux sandbox before explicit activation."
