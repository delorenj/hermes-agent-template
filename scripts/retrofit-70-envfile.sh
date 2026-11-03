#!/usr/bin/env bash
# RETIRED 2026-10-11 -- refuses to run. Git history has the old behaviour.
#
# This used to rewrite every ~/.config/systemd/user/hermes-<agent>-{gateway,consumer}.service
# unit (insert EnvironmentFile=, daemon-reload, raw `systemctl restart`). There are no such
# units any more: ONE host gateway, hermes-gateway.service, serves every profile, and the
# per-profile Bloodbank consumer is retired. 70-systemd.sh retires a leftover per-agent unit
# at provision time; nothing should rewrite or restart one.
#
# To pick up a change in the host gateway, run once:
#     hermes-gateway-restart --reason "<why>"
# (never raw `systemctl --user restart|reload|stop`, `kill -USR1`, or `hermes gateway restart`).
set -euo pipefail

cat >&2 <<'MSG'
retrofit-70-envfile: RETIRED (2026-10-11) and refusing to run.
  There are no per-agent hermes-<agent>-gateway/consumer units to retrofit: one host gateway,
  hermes-gateway.service, serves every profile. Restart it only with:
      hermes-gateway-restart --reason "<why>"
MSG
exit 64
