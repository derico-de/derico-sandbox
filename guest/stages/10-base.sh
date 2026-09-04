#!/usr/bin/env bash
# Stage 10: Debian base packages for the sandboxsh golden image. Runs as root in
# a disposable build worker, once per pinned source image.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends \
    bash-completion build-essential ca-certificates curl fd-find git gnupg iproute2 jq \
    less libcairo2 libffi-dev libgdk-pixbuf-2.0-0 libldap2-dev libpango-1.0-0 \
    libpangocairo-1.0-0 libsasl2-dev locales neovim openssh-client \
    postgresql-client procps python3 python3-dev python3-pip python3-venv ripgrep \
    rsync shared-mime-info sudo tig tree unzip vim wget
rm -rf /var/lib/apt/lists/*
apt-get clean
ln -sf "$(command -v fdfind)" /usr/local/bin/fd
update-alternatives --set editor /usr/bin/nvim || true

# Cloud-init has finished its first boot by now (the host waited for it before
# this stage). Every later stage boots a copy of this worker under a fresh
# instance-id, and cloud-init would treat each as a new machine: re-create the
# default user, re-grow partitions, regenerate SSH host keys. Keep it off until
# 90-finalize.sh re-enables it for the published image.
touch /etc/cloud/cloud-init.disabled
