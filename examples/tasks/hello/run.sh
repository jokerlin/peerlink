#!/bin/sh
# Echo the params back; PEERLINK_* variables come from peerlink.
sleep "$PEERLINK_PARAM_SLEEP"
printf 'Got: %s (from %s, run %s, %s)\n' "$PEERLINK_PARAM_TEXT" "$PEERLINK_CALLER" \
  "$PEERLINK_RUN_ID" "$(date '+%F %T')" > "$PEERLINK_RUN_DIR/result.md"
cat "$PEERLINK_RUN_DIR/result.md"
