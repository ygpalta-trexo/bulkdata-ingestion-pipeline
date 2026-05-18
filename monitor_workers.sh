#!/usr/bin/env bash

set -u

REFRESH_SECONDS="${REFRESH_SECONDS:-5}"
LINES_PER_LOG="${LINES_PER_LOG:-8}"
DATE_DIR="${DATE_DIR:-$(date +%F)}"
PROCESS_MATCH="${PROCESS_MATCH:-python[0-9.[:space:]]+-m[[:space:]]+docdb_ingestion\\.pipeline[[:space:]]+run([[:space:]]|$)}"

print_divider() {
  printf '%*s\n' "${COLUMNS:-100}" '' | tr ' ' '-'
}

read_proc_env() {
  local pid="$1"
  local key="$2"
  tr '\0' '\n' < "/proc/${pid}/environ" 2>/dev/null | awk -F= -v key="$key" '$1 == key {sub($1 "=",""); print; exit}'
}

extract_arg_value() {
  local cmd="$1"
  local flag="$2"
  awk -v flag="$flag" '
    {
      for (i = 1; i <= NF; i++) {
        if ($i == flag && i < NF) {
          print $(i + 1)
          exit
        }
      }
    }
  ' <<< "$cmd"
}

resolve_worker_name() {
  local pid="$1"
  local cmd="$2"
  local worker_name

  worker_name="$(extract_arg_value "$cmd" "--worker-name")"
  if [ -n "${worker_name}" ]; then
    echo "$worker_name"
    return
  fi

  worker_name="$(read_proc_env "$pid" "PIPELINE_WORKER_NAME")"
  if [ -n "${worker_name}" ]; then
    echo "$worker_name"
    return
  fi

  echo "pid${pid}"
}

resolve_stdout_path() {
  local pid="$1"
  local worker_name="$2"
  local stdout_path

  stdout_path="$(readlink "/proc/${pid}/fd/1" 2>/dev/null || true)"
  if [ -n "$stdout_path" ] && [ "$stdout_path" != "pipe:"* ]; then
    echo "$stdout_path"
    return
  fi

  if [ "$worker_name" != "pid${pid}" ]; then
    echo "logs/${worker_name}.out"
  else
    echo ""
  fi
}

resolve_temp_dir() {
  local pid="$1"
  local worker_name="$2"
  local temp_dir
  temp_dir="$(read_proc_env "$pid" "EPO_TEMP_DIR")"
  if [ -n "${temp_dir}" ]; then
    echo "$temp_dir"
  elif [ "$worker_name" != "pid${pid}" ]; then
    echo "./tmp/${worker_name}"
  else
    echo "./tmp_downloads"
  fi
}

resolve_log_file() {
  local pid="$1"
  local cmd="$2"
  local worker_name="$3"
  local explicit_log_file

  explicit_log_file="$(extract_arg_value "$cmd" "--log-file")"
  if [ -n "${explicit_log_file}" ]; then
    echo "$explicit_log_file"
    return
  fi

  explicit_log_file="$(read_proc_env "$pid" "PIPELINE_LOG_FILE")"
  if [ -n "${explicit_log_file}" ]; then
    echo "$explicit_log_file"
    return
  fi

  if [ "$worker_name" != "pid${pid}" ]; then
    echo "logs/${DATE_DIR}/pipeline_${worker_name}.log"
  else
    echo "logs/${DATE_DIR}/pipeline.log"
  fi
}

print_tail_block() {
  local label="$1"
  local path="$2"

  if [ -n "$path" ] && [ -f "$path" ]; then
    echo "${label}: ${path}"
    tail -n "$LINES_PER_LOG" "$path"
  else
    echo "${label}: ${path:-unknown} (missing)"
  fi
}

discover_workers() {
  ps -eo pid=,args= | awk -v pattern="$PROCESS_MATCH" '$0 ~ pattern {print}'
}

print_worker_block() {
  local pid="$1"
  local cmd="$2"
  local worker_name stdout_path temp_dir app_log

  worker_name="$(resolve_worker_name "$pid" "$cmd")"
  stdout_path="$(resolve_stdout_path "$pid" "$worker_name")"
  temp_dir="$(resolve_temp_dir "$pid" "$worker_name")"
  app_log="$(resolve_log_file "$pid" "$cmd" "$worker_name")"

  echo "[${worker_name}] pid=${pid}"

  if ps -p "$pid" > /dev/null 2>&1; then
    ps -p "$pid" -o pid=,ppid=,%cpu=,%mem=,etime=,stat=,cmd=
  else
    echo "not running"
  fi

  if [ -d "$temp_dir" ]; then
    echo "temp_dir: $temp_dir ($(du -sh "$temp_dir" 2>/dev/null | awk '{print $1}'))"
  else
    echo "temp_dir: $temp_dir"
  fi

  print_tail_block "stdout tail" "$stdout_path"
  print_tail_block "app log tail" "$app_log"
}

while true; do
  clear
  echo "DOCDB worker monitor"
  echo "refresh=${REFRESH_SECONDS}s lines_per_log=${LINES_PER_LOG} date_dir=${DATE_DIR}"
  echo "process_match=${PROCESS_MATCH}"
  echo "time=$(date '+%Y-%m-%d %H:%M:%S')"
  print_divider

  worker_lines="$(discover_workers)"
  if [ -z "$worker_lines" ]; then
    echo "No running processes matched '${PROCESS_MATCH}'."
    print_divider
    sleep "$REFRESH_SECONDS"
    continue
  fi

  while IFS= read -r line; do
    [ -z "$line" ] && continue
    pid="${line%% *}"
    cmd="${line#* }"
    print_worker_block "$pid" "$cmd"
    print_divider
  done <<< "$worker_lines"

  sleep "$REFRESH_SECONDS"
done
