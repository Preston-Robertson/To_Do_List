#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this installer as root." >&2
  exit 1
fi

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if ! id luigi-web-preview >/dev/null 2>&1; then
  useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin luigi-web-preview
fi
install -d -o luigi-web-preview -g luigi-web-preview -m 0700 /opt/luigi-web-preview-data
install -o root -g root -m 0755 \
  "$ROOT/scripts/luigi_web_preview_helper.py" \
  /usr/local/sbin/luigi-web-preview
install -o root -g root -m 0644 \
  "$ROOT/luigi-web-preview.service" \
  /etc/systemd/system/luigi-web-preview.service

SUDOERS=/etc/sudoers.d/luigi-web-preview
cat > "$SUDOERS" <<'EOF'
Cmnd_Alias LUIGI_WEB_PREVIEW = /usr/local/sbin/luigi-web-preview status, /usr/local/sbin/luigi-web-preview branches, /usr/local/sbin/luigi-web-preview create *, /usr/local/sbin/luigi-web-preview update, /usr/local/sbin/luigi-web-preview restart, /usr/local/sbin/luigi-web-preview remove
luigi-web ALL=(root) NOPASSWD: LUIGI_WEB_PREVIEW
EOF
chmod 0440 "$SUDOERS"
visudo -cf "$SUDOERS"
systemctl daemon-reload

echo "Preview helper installed. Create /etc/luigi-web/preview.env and an isolated Preview PostgreSQL database before using it."