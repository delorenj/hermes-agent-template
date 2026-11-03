#!/usr/bin/env python3
"""RETIRED 2026-10-11 -- refuses to run. Git history has the old generator.

This used to write ``10-versioned-runtime.conf`` for every per-agent
``hermes-<agent>-gateway.service`` unit, with ``ExecStart=... -p <profile> gateway run --replace``.
There are no per-agent gateway units any more: ONE host unit, ``hermes-gateway.service``
(``hermes gateway run``, ``HERMES_HOME=~/.hermes``), serves every profile, and a profile is
attached to it through the control socket ``~/.hermes/gateway.sock``. A per-agent unit that
runs ``gateway run --replace`` takes over the host gateway (james-brennan-pm 2026-10-20 blocked
the Bloodbank lane for 3h; tolt-pm 2026-11-02 drove ~975 host restarts), so nothing may generate
one.

To make the host gateway pick up a change, run once:

    hermes-gateway-restart --reason "<why>"

and never raw ``systemctl --user restart|reload|stop``, ``kill -USR1`` or
``hermes gateway restart``. The single-install drift check is
``scripts/hermes-single-install-check.py``.
"""
from __future__ import annotations

import sys

MESSAGE = (
    "hermes-runtime-dropin: RETIRED (2026-10-11) and refusing to run.\n"
    "  There are no per-agent gateway units to drop a runtime pin into: one host gateway,\n"
    "  hermes-gateway.service, serves every profile. Restart it only with:\n"
    '      hermes-gateway-restart --reason "<why>"\n'
    "  Drift check: scripts/hermes-single-install-check.py\n"
)


def main(argv: list[str]) -> int:
    sys.stderr.write(MESSAGE)
    return 64


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
