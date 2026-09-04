#!/usr/bin/env bash
# Stage 90: security defaults, the login profile, agent seed data, the guest
# helpers, cleanup, and smoke checks. Fetches nothing from the network.
set -euo pipefail

# Keep git publication behind a deliberate host/human step. Guest root can
# bypass this (Docker membership makes guest root part of the threat model), but
# it prevents routine autonomous pushes and accidental publication.
install -d -m 0755 /etc/git-hooks
cat > /etc/git-hooks/pre-push <<'HOOK'
#!/usr/bin/env bash
[ "${SANDBOXSH_ALLOW_PUSH:-0}" = "1" ] && exit 0
echo "sandboxsh: git push is disabled in YOLO mode." >&2
echo "Review on the host, or deliberately use SANDBOXSH_ALLOW_PUSH=1." >&2
exit 1
HOOK
chmod 0755 /etc/git-hooks/pre-push
git config --system core.hooksPath /etc/git-hooks

cat > /etc/profile.d/sandboxsh.sh <<'PROFILE'
export SANDBOXSH=1
export DEVCONTAINER=true
export PNPM_HOME="$HOME/.local/share/pnpm"
export PATH="$HOME/.local/bin:$PNPM_HOME/bin:$PATH"
# pnpm 11 stores a SQLite index beside its package content. Keep it off the
# virtiofs/9p project mount, where SQLite WAL/mmap may fail with SQLITE_IOERR.
export PNPM_CONFIG_STORE_DIR="$HOME/.local/share/pnpm/store"
export PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
export NODE_OPTIONS=--dns-result-order=ipv4first
# Firecrawl's Pi extension reads its credential from the environment. Keep the
# key out of the image: a user may add it at runtime to Pi's credential state,
# which is shared between VMs when agent_credentials is enabled and VM-local
# otherwise.
firecrawl_key_file="$HOME/.pi/firecrawl-api-key"
if [ -r "$firecrawl_key_file" ]; then
    export FIRECRAWL_API_KEY="$(cat "$firecrawl_key_file")"
fi
unset firecrawl_key_file
if [ -d /agent-creds/claude ]; then
    export CLAUDE_CONFIG_DIR=/agent-creds/claude
fi
PROFILE
chmod 0644 /etc/profile.d/sandboxsh.sh

# Seed data is copied to the shared managed volume on first use. It contains
# installed packages/settings, never authentication material from the host.
# A rebuilt finalize stage starts from the agents stage's clean seed directory,
# so remove whatever an earlier run of this stage left there.
for agent in claude pi vibe; do
    mkdir -p "/home/dev/.$agent"
    rm -rf "/opt/sandboxsh/agent-seed/$agent"
    cp -a "/home/dev/.$agent" "/opt/sandboxsh/agent-seed/$agent"
done
# Instances copy the seed once and stamp the volume with this id, so a volume
# that already carries it is left alone until the next image build.
cat /proc/sys/kernel/random/uuid > /opt/sandboxsh/agent-seed/.seed-id
chown -R dev:dev /opt/sandboxsh/agent-seed /home/dev

install -m 0755 /root/agent-init.sh /usr/local/sbin/sandboxsh-agent-init
install -m 0755 /root/instance-init.sh /usr/local/sbin/sandboxsh-instance-init

# Remove transient builder state. These remove package and shell residue from
# the golden image; cloud-init performs its own clean below.
rm -rf /tmp/* /var/tmp/* /root/.cache /root/.bash_history /home/dev/.bash_history
apt-get clean

docker compose version

# Re-enable cloud-init for the published image (10-base.sh switched it off for
# the build workers) and clear its state, so every project VM created from the
# image runs cloud-init's first boot under its own instance-id and machine-id.
rm -f /etc/cloud/cloud-init.disabled
cloud-init clean --logs --machine-id
printf 'sandboxsh golden image provisioned\n'
