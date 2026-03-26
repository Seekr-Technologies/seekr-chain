#!/bin/sh
set -eu

#######################################
# Configuration
#######################################
HB_PATH="/seekr-chain/.hb"
SHUTDOWN_PATH="/seekr-chain/.shutdown"
LOGS_FLUSHED_PATH="/seekr-chain/.logs_flushed"
FLUENTBIT_CONF="/seekr-chain/resources/fluentbit.conf"
FLUENTBIT_BIN="/fluent-bit/bin/fluent-bit"

TIMEOUT=30
STARTUP_TIMEOUT=1200
SHUTDOWN_GRACE_PERIOD=5

#######################################
# Required environment variables
#######################################
: "${SEEKR_CHAIN_LOGS:?Required}"
: "${S3_STEP_DATA_PREFIX:?Required}"

# Expand any variables embedded in S3_STEP_DATA_PREFIX
export S3_STEP_DATA_PREFIX="$(eval echo "$S3_STEP_DATA_PREFIX")"

#######################################
# Functions
#######################################

get_mtime_epoch() {
  # Returns mtime as epoch seconds, or 0 on error
  stat -c %Y -- "$1" 2>/dev/null || echo 0
}

get_file_size() {
  # Returns file size in bytes, or -1 on error
  stat -c %s -- "$1" 2>/dev/null || echo -1
}

wait_for_startup() {
  # Wait for heartbeat or shutdown signal (with timeout)
  echo "Waiting for heartbeat or shutdown (startup timeout: ${STARTUP_TIMEOUT}s)..."
  start_ts=$(date +%s)

  while [ ! -f "${HB_PATH}" ] && [ ! -f "${SHUTDOWN_PATH}" ]; do
    now=$(date +%s)
    if [ $((now - start_ts)) -ge "${STARTUP_TIMEOUT}" ]; then
      echo "Startup timeout (${STARTUP_TIMEOUT}s) exceeded waiting for heartbeat or shutdown; exiting"
      exit 1
    fi
    sleep 1
  done
}

is_heartbeat_stale() {
  hb=$(get_mtime_epoch "${HB_PATH}")
  now=$(date +%s)
  [ "$hb" -gt 0 ] && [ $((now - hb)) -ge "${TIMEOUT}" ]
}

monitor_loop() {
  # Monitor heartbeat/log activity until shutdown condition is met
  lastsz=-1
  lastts=$(date +%s)

  while :; do
    if [ -f "${SHUTDOWN_PATH}" ]; then
      echo "Shutdown signal received; exiting"
      return
    fi

    if [ -f "${HB_PATH}" ]; then
      # Heartbeat file exists; check if it's stale
      if is_heartbeat_stale; then
        echo "Heartbeat is stale; exiting"
        return
      fi
    else
      # No heartbeat; fall back to monitoring log file growth
      sz=$(get_file_size "$SEEKR_CHAIN_LOGS")
      now=$(date +%s)

      if [ "$sz" -ge 0 ] && [ "$sz" -eq "$lastsz" ] && [ $((now - lastts)) -ge "${TIMEOUT}" ]; then
        echo "Log file has not grown and heartbeat is missing; exiting"
        return
      fi

      if [ "$sz" -ne "$lastsz" ]; then
        lastsz="$sz"
        lastts="$now"
      fi
    fi

    sleep 2
  done
}

shutdown_fluentbit() {
  pid="$1"

  # Allow time for final writes
  sleep "$SHUTDOWN_GRACE_PERIOD"

  # Graceful termination
  kill -TERM "$pid" 2>/dev/null || true
  wait "$pid" || true

  # Signal to main container that logs are flushed
  touch "$LOGS_FLUSHED_PATH"
}

#######################################
# Main
#######################################

main() {
  cat "$FLUENTBIT_CONF"

  wait_for_startup

  # Inject custom S3 endpoint if provided (for hermetic/MinIO testing)
  if [ -n "${FB_S3_ENDPOINT:-}" ]; then
    CONF_TMP="$(mktemp)"
    cat "$FLUENTBIT_CONF" > "$CONF_TMP"
    printf "    endpoint    %s\n" "${FB_S3_ENDPOINT}" >> "$CONF_TMP"
    FLUENTBIT_CONF="$CONF_TMP"
  fi

  echo "Starting fluent-bit"
  "$FLUENTBIT_BIN" -c "$FLUENTBIT_CONF" &
  fb_pid=$!

  monitor_loop

  shutdown_fluentbit "$fb_pid"
}

main "$@"
