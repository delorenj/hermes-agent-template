#!/usr/bin/env bash
# Retained only so older automation fails with a clear compatibility message.
# The former checkpoint repair belongs to a retired persistence design.
set -euo pipefail

printf '%s\n' \
  'ERROR: repair-runtime-checkpoint.sh is retired and intentionally non-operational.' \
  'Current Hermes runtimes are ignored local state; this command changed nothing.' \
  'See docs/operations.md for backup, restore, and retention policy.' >&2
exit 64
