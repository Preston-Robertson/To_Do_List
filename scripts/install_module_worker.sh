#!/usr/bin/env bash
set -euo pipefail

fail() { printf '%s\n' "$1" >&2; exit 1; }
usage() { printf '%s\n' 'Usage: bash scripts/install_module_worker.sh --install [--enable]'; }

if [[ ${1:-} == --help ]]; then
    usage
    exit 0
fi
[[ $# -ge 1 && ${1:-} == --install ]] || { usage; exit 2; }
enable=0
shift
if [[ ${1:-} == --enable ]]; then
    enable=1
    shift
fi
[[ $# == 0 ]] || { usage; exit 2; }
[[ $EUID == 0 ]] || fail 'Installation requires a deployment administrator running as root.'

source_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
environment=/etc/luigi-web/module-installer.env
command -v systemctl >/dev/null || fail 'systemd is required.'
id luigi-web >/dev/null 2>&1 || fail 'Provision the existing luigi-web service account first.'
[[ -x /opt/luigi-web/.venv/bin/luigi-web ]] || fail 'Install the compatible host CLI first.'
[[ -d /opt/luigi-web/data && ! -L /opt/luigi-web/data ]] || fail 'Provision the shared installer data directory first.'
for directory in /etc/luigi-web /etc/systemd/system; do
    [[ ! -L $directory ]] || fail 'Refusing a symbolic-link configuration directory.'
done
for target in "$environment" /etc/systemd/system/module-installer.service /etc/systemd/system/module-installer.timer; do
    [[ ! -L $target ]] || fail 'Refusing a symbolic-link configuration file.'
done
for name in module-installer.service module-installer.timer module-installer.env.example; do
    [[ -f $source_root/examples/$name ]] || fail 'Run from the reviewed checkout with its examples.'
done

if [[ ! -d /etc/luigi-web ]]; then
    install -d -o root -g luigi-web -m 0750 /etc/luigi-web
fi
if [[ ! -e $environment ]]; then
    install -o root -g luigi-web -m 0640 "$source_root/examples/module-installer.env.example" "$environment"
    printf '%s\n' 'Created the dedicated environment template; configure deployment policy before enabling.'
fi
[[ -f $environment && $(stat -c %u "$environment") == 0 && $(stat -c %a "$environment") == 640 ]] || fail 'The dedicated environment must be a root-owned regular file with mode 0640.'
has_data=0
has_policy=0
while IFS= read -r line || [[ -n $line ]]; do
    case "$line" in
        ''|'#'*) ;;
        LUIGI_WEB_DATA_DIR=/opt/luigi-web/data) has_data=1 ;;
        LUIGI_WEB_MODULE_REPOSITORY_OWNERS=*)
            value=${line#*=}
            [[ -z $value || $value =~ ^[A-Za-z0-9,-]+$ ]] || fail 'Use an unquoted comma-separated owner list.'
            [[ -z $value ]] || has_policy=1
            ;;
        LUIGI_WEB_MODULE_REPOSITORIES_FILE=*)
            value=${line#*=}
            [[ -z $value || $value =~ ^/[A-Za-z0-9_./-]+$ ]] || fail 'Use an absolute policy path without spaces.'
            [[ -z $value ]] || has_policy=1
            ;;
        LUIGI_WEB_MODULES_FILE=*)
            value=${line#*=}
            [[ -z $value || $value =~ ^/[A-Za-z0-9_./-]+$ ]] || fail 'Use an absolute selection path without spaces.'
            ;;
        *) fail 'Only data, repository policy, and optional selection paths belong in the worker environment.' ;;
    esac
done < "$environment"
[[ $has_data == 1 ]] || fail 'The example unit requires DATA_DIR=/opt/luigi-web/data.'
if [[ $enable == 1 && $has_policy != 1 ]]; then
    fail 'Set a reviewed owner or repository-file policy before enabling the timer.'
fi

install -o root -g root -m 0644 "$source_root/examples/module-installer.service" /etc/systemd/system/module-installer.service
install -o root -g root -m 0644 "$source_root/examples/module-installer.timer" /etc/systemd/system/module-installer.timer
systemctl daemon-reload
if [[ $enable == 1 ]]; then
    systemctl enable --now module-installer.timer
    printf '%s\n' 'Module installer timer enabled; the application was not restarted.'
else
    printf '%s\n' 'Units installed; timer enablement was left unchanged. No application restart.'
fi