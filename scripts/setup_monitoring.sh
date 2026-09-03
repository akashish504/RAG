#!/usr/bin/env bash
# Install + start the CloudWatch Agent with config/cloudwatch-agent.json
# (memory / swap / root-disk metrics in the MCPDev namespace).
#
#   bash scripts/setup_monitoring.sh
#
# Idempotent: safe to re-run; fetch-config restarts the agent with the
# current config. Called by reset_deployment.sh (which treats a non-zero
# exit as a WARN, not a deploy failure). Requires the instance role to
# carry CloudWatchAgentServerPolicy for metrics to actually publish.
#
# EC2/Linux only — exits 0 as a no-op on other platforms so local runs of
# reset_deployment.sh stay green.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
AGENT_CONFIG="${REPO_ROOT}/config/cloudwatch-agent.json"
AGENT_CTL="/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl"
AGENT_CONFIG_DEST="/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json"

say()  { printf '    %s\n' "$*"; }

if [ "$(uname -s)" != "Linux" ]; then
    say "not Linux — skipping CloudWatch Agent setup (no-op)"
    exit 0
fi

[ -f "${AGENT_CONFIG}" ] || { say "missing ${AGENT_CONFIG}"; exit 1; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null 2>&1 || { say "not root and no sudo — cannot install agent"; exit 1; }
    SUDO="sudo"
fi

# ------------------------------------------------------------------ install
if [ ! -x "${AGENT_CTL}" ]; then
    say "CloudWatch Agent not found — installing"
    if command -v dnf >/dev/null 2>&1; then
        ${SUDO} dnf install -y amazon-cloudwatch-agent
    elif command -v yum >/dev/null 2>&1; then
        ${SUDO} yum install -y amazon-cloudwatch-agent
    elif command -v apt-get >/dev/null 2>&1; then
        # Not in Ubuntu/Debian repos — fetch the signed package from AWS.
        ARCH="$(dpkg --print-architecture)"
        DEB="/tmp/amazon-cloudwatch-agent.deb"
        curl -fsSL "https://amazoncloudwatch-agent.s3.amazonaws.com/debian/${ARCH}/latest/amazon-cloudwatch-agent.deb" -o "${DEB}"
        ${SUDO} dpkg -i "${DEB}"
        rm -f "${DEB}"
    else
        say "no supported package manager (dnf/yum/apt-get) found"
        exit 1
    fi
fi
[ -x "${AGENT_CTL}" ] || { say "agent install did not produce ${AGENT_CTL}"; exit 1; }

# ---------------------------------------------------------------- configure
${SUDO} cp "${AGENT_CONFIG}" "${AGENT_CONFIG_DEST}"
${SUDO} "${AGENT_CTL}" -a fetch-config -m ec2 -s -c "file:${AGENT_CONFIG_DEST}"

${SUDO} "${AGENT_CTL}" -a status -m ec2 | grep -q '"status": "running"' \
    || { say "agent did not report running after fetch-config"; exit 1; }
say "CloudWatch Agent running (namespace MCPDev: mem/swap/disk)"
