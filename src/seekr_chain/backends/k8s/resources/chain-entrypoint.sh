set -eu

#######################################
# Configuration
#######################################
PATH="/seekr-chain/bin:$PATH"

HB_PATH="/seekr-chain/.hb"
SHUTDOWN_PATH="/seekr-chain/.shutdown"
LOGS_FLUSHED_PATH="/seekr-chain/.logs_flushed"
LOG_FILE="/seekr-chain/logs.txt"
RC_FILE="/seekr-chain/.last_rc"

HEARTBEAT_INTERVAL=1
LOG_FLUSH_TIMEOUT="${LOG_FLUSH_TIMEOUT:-60}"

BEFORE_SCRIPT="/seekr-chain/before_script.sh"
MAIN_SCRIPT="/seekr-chain/script.sh"
AFTER_SCRIPT="/seekr-chain/after_script.sh"

#######################################
# Global state
#######################################
HB_PID=""
SCRIPT_PID=""
TEE_PID=""
CLEANED_UP=""

#######################################
# Functions
#######################################

heartbeat() {
  while :; do
    touch "$HB_PATH"
    sleep "$HEARTBEAT_INTERVAL"
  done
}

start_heartbeat() {
  heartbeat &
  HB_PID=$!
}

timestamp() {
  value=$(date -u '+%Y-%m-%dT%H:%M:%S.%3NZ')
  case "$value" in
    ????-??-??T??:??:??.[0-9][0-9][0-9]Z)
      printf '%s\n' "$value"
      return
      ;;
  esac

  while :; do
    seconds=$(date -u '+%Y-%m-%dT%H:%M:%S')
    read -r uptime _ < /proc/uptime
    [ "$seconds" = "$(date -u '+%Y-%m-%dT%H:%M:%S')" ] && break
  done
  milliseconds=$(printf '%.3s' "${uptime#*.}00")
  printf '%s.%sZ\n' "$seconds" "$milliseconds"
}

log_message() {
  printf '[%s] chain | %s\n' "$(timestamp)" "$1" | tee -a "$LOG_FILE"
}

stop_heartbeat() {
  if [ -n "${HB_PID:-}" ]; then
    kill "$HB_PID" 2>/dev/null || true
  fi
}

wait_for_log_flush() {
  elapsed=0
  while [ ! -f "$LOGS_FLUSHED_PATH" ] && [ "$elapsed" -lt "$LOG_FLUSH_TIMEOUT" ]; do
    sleep 1
    elapsed=$((elapsed + 1))
  done
}

cleanup() {
  if [ -n "$CLEANED_UP" ]; then
    return
  fi
  CLEANED_UP=1

  # Ensure final log line is flushed
  printf "\n" >> "$LOG_FILE"

  stop_heartbeat
  touch "$SHUTDOWN_PATH" 2>/dev/null || true
  wait_for_log_flush
}

terminate() {
  if [ -n "$SCRIPT_PID" ]; then
    script_pid="$SCRIPT_PID"
    log_message "Received SIGTERM; forwarding to user script PID $script_pid."
    kill -TERM "$SCRIPT_PID" 2>/dev/null || true
    wait "$SCRIPT_PID" 2>/dev/null || true
    SCRIPT_PID=""
  else
    log_message "Received SIGTERM; no user script is running."
  fi
  cleanup
  if [ -f "$LOGS_FLUSHED_PATH" ]; then
    log_message "SIGTERM shutdown: final log upload complete."
  else
    log_message "SIGTERM shutdown: final log upload did not complete before timeout."
  fi
  exit 143
}

validate_script() {
  script="$1"

  if [ ! -f "$script" ]; then
    echo "ERROR: script '$script' does not exist"
    return 1
  fi

  # Validate interpreter from shebang if present
  first_line="$(head -n 1 "$script" 2>/dev/null || true)"

  case "$first_line" in
    '#!'*)
      shebang="${first_line#\#!}"
      # Extract first word (handles "#!/usr/bin/env bash" and "#!/bin/bash" styles)
      interpreter="$(echo "$shebang" | awk '{print $1}')"

      if [ ! -x "$interpreter" ]; then
        echo "ERROR: shell not found or not executable: $interpreter"
        return 127
      fi
      ;;
  esac

  return 0
}

run_script() {
  # Run a script with output logged to LOG_FILE.
  # Returns the script's exit code (not tee's).
  # Note: We don't impose error handling on user scripts — they can add
  # `set -e` themselves if they want early termination on failure.
  # Otherwise, the exit code is that of the last command in their script.
  script="$1"
  fifo="$LOG_FILE.pipe.$$"
  mkfifo "$fifo"
  tee -a "$LOG_FILE" < "$fifo" &
  TEE_PID=$!
  "$script" > "$fifo" 2>&1 &
  SCRIPT_PID=$!
  wait "$SCRIPT_PID"
  rc=$?
  SCRIPT_PID=""
  wait "$TEE_PID"
  TEE_PID=""
  rm -f "$fifo"
  return "$rc"
}

#######################################
# Main
#######################################

main() {
  trap cleanup EXIT
  trap terminate INT TERM
  start_heartbeat

  set +e  # Manage exit codes manually

  # Validate main script before running anything
  { validate_script "$MAIN_SCRIPT"; echo $? > "$RC_FILE"; } | tee -a "$LOG_FILE"
  rc_validate="$(cat "$RC_FILE")"
  if [ "$rc_validate" -ne 0 ]; then
    exit "$rc_validate"
  fi

  # Before script
  run_script "$BEFORE_SCRIPT"
  rc_before=$?

  # Main script (only if before_script succeeded)
  rc_main=1
  if [ "$rc_before" -eq 0 ]; then
    run_script "$MAIN_SCRIPT"
    rc_main=$?
  fi

  # After script (always runs)
  run_script "$AFTER_SCRIPT"

  exit "$rc_main"
}

main "$@"
