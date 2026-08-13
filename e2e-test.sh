#!/usr/bin/env bash
# Destructive host integration test. Requires a built sandboxsh/base image.
set -euo pipefail

SANDBOXSH="${SANDBOXSH_BIN:-sandboxsh}"
command -v "$SANDBOXSH" >/dev/null || { echo "missing sandboxsh" >&2; exit 1; }
command -v jq >/dev/null || { echo "missing jq" >&2; exit 1; }

WORK="$(mktemp -d "${XDG_CACHE_HOME:-$HOME/.cache}/sandboxsh-e2e.XXXXXX")"
CONFIG="$WORK/.sandboxsh.json"
cleanup() {
    "$SANDBOXSH" --config "$CONFIG" destroy -y >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

cat > "$CONFIG" <<EOF
{
  "name": "sandboxsh-e2e-$$",
  "workdir": "/workspaces/$(basename "$WORK")",
  "dirs": ["."],
  "ports": [18080],
  "resources": {"cpus": 2, "memory": "2GiB", "disk": "20GiB"},
  "firewall": {"enabled": true, "allow": []},
  "agent_credentials": true
}
EOF
cat > "$WORK/compose.yaml" <<EOF
services:
  smoke:
    image: alpine:3.21
    user: "$(id -u):$(id -g)"
    command: ["sh", "-c", "echo compose-ok > /workspace/compose-result && sleep 30"]
    volumes:
      - .:/workspace
EOF
cat > "$WORK/package.json" <<'EOF'
{
  "private": true,
  "dependencies": {
    "is-number": "7.0.0"
  }
}
EOF

pass() { printf 'PASS %s\n' "$*"; }
fail() { printf 'FAIL %s\n' "$*" >&2; exit 1; }

"$SANDBOXSH" doctor
"$SANDBOXSH" image status
"$SANDBOXSH" --config "$CONFIG" up --no-shell

PLAN="$($SANDBOXSH --config "$CONFIG" plan)"
INSTANCE="$(jq -r .instance <<<"$PLAN")"
[ -n "$INSTANCE" ] || fail "instance name"

"$SANDBOXSH" --config "$CONFIG" exec -- docker compose version >/dev/null
pass "Docker Compose v2 works"

PNPM_STORE="$($SANDBOXSH --config "$CONFIG" exec -- pnpm store path)"
case "$PNPM_STORE" in
    /home/dev/.local/share/pnpm/store/*) ;;
    *) fail "pnpm store is not VM-local: $PNPM_STORE" ;;
esac
"$SANDBOXSH" --config "$CONFIG" exec -- pnpm install --reporter=silent
"$SANDBOXSH" --config "$CONFIG" exec -- pnpm exec node -e \
    'if (!require("is-number")(42)) process.exit(1)'
pass "pnpm installs from a SQLite store on the VM-local disk"

"$SANDBOXSH" --config "$CONFIG" exec -- docker compose up -d
for _ in $(seq 1 30); do
    [ -f "$WORK/compose-result" ] && break
    sleep 1
done
[ "$(cat "$WORK/compose-result" 2>/dev/null)" = compose-ok ] \
    && pass "Compose container writes through live host project mount" \
    || fail "Compose bind mount"

"$SANDBOXSH" --config "$CONFIG" exec -- sh -c \
    'test ! -S /run/incus/unix.socket && test ! -S /var/lib/incus/unix.socket'
pass "host Incus socket is absent"

"$SANDBOXSH" --config "$CONFIG" exec -- sh -c \
    'test -S /var/run/docker.sock && docker info >/dev/null'
pass "Docker socket belongs to working guest daemon"

"$SANDBOXSH" --config "$CONFIG" exec -- curl -sS --max-time 15 \
    -o /dev/null https://api.anthropic.com
pass "built-in allowlisted endpoint is reachable"

if "$SANDBOXSH" --config "$CONFIG" exec -- curl -fsS --max-time 5 \
    -o /dev/null https://example.com 2>/dev/null; then
    fail "non-allowlisted endpoint escaped ACL"
else
    pass "non-allowlisted endpoint is blocked"
fi

"$SANDBOXSH" --config "$CONFIG" exec -- sh -c \
    'nohup python3 -m http.server 18080 --bind 0.0.0.0 >/tmp/http.log 2>&1 &'
# --vm: this asserts VM ingress, so it must not follow a tailnet publication.
ADDRESS="$($SANDBOXSH --config "$CONFIG" url 18080 --vm | sed 's#http://##;s#:.*##')"
for _ in $(seq 1 20); do
    curl -fsS --max-time 2 "http://$ADDRESS:18080" >/dev/null 2>&1 && break
    sleep 1
done
curl -fsS --max-time 2 "http://$ADDRESS:18080" >/dev/null \
    && pass "declared development port is host-reachable" \
    || fail "declared port ingress"

ACL="$(jq -r '.networkAcl.description' <<<"$PLAN")"
[ "$ACL" = "Managed by sandboxsh for $INSTANCE" ] \
    && pass "host ACL policy rendered" \
    || fail "ACL policy"

"$SANDBOXSH" --config "$CONFIG" destroy -y
pass "persistent VM destroyed cleanly"
trap - EXIT
rm -rf "$WORK"
