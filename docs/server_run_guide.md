# Server Run Guide

This guide shows how to run the DOCDB ingestion pipeline on a long-lived server such as EC2, either as a single worker or as multiple parallel workers.

## Prerequisites

From the repo root:

```bash
cd /path/to/docdb_ingestion
source .venv/bin/activate
mkdir -p tmp logs
```

Make sure:

- `.env` is configured
- Postgres is reachable
- `python -m docdb_ingestion.pipeline sync` has already been run at least once for delivery-based ingestion

## Single worker run

### Start one worker in the background

```bash
mkdir -p tmp/worker1 logs
EPO_TEMP_DIR=./tmp/worker1 \
nohup python -m docdb_ingestion.pipeline run \
  --start-index 1 \
  --limit 100 \
  --worker-name worker1 \
  > logs/worker1.out 2>&1 &
```

This does the following:

- uses `./tmp/worker1` for temporary downloads/extraction
- writes shell output to `logs/worker1.out`
- writes application logs to `logs/<date>/pipeline_worker1.log`
- keeps running after you disconnect from SSH

### Check that it started

```bash
ps -ef | grep 'docdb_ingestion.pipeline' | grep -v grep
```

### View logs later

Shell log:

```bash
tail -f logs/worker1.out
```

Application log:

```bash
tail -f logs/$(date +%F)/pipeline_worker1.log
```

If the process started on an earlier day, replace `$(date +%F)` with that date folder.

## Multiple worker run

Use different non-overlapping file ranges.

### Example: 3 workers on one server

```bash
mkdir -p tmp/worker1 tmp/worker2 tmp/worker3 logs

EPO_TEMP_DIR=./tmp/worker1 \
nohup python -m docdb_ingestion.pipeline run \
  --start-index 1 \
  --limit 50 \
  --worker-name worker1 \
  > logs/worker1.out 2>&1 &

EPO_TEMP_DIR=./tmp/worker2 \
nohup python -m docdb_ingestion.pipeline run \
  --start-index 51 \
  --limit 50 \
  --worker-name worker2 \
  > logs/worker2.out 2>&1 &

EPO_TEMP_DIR=./tmp/worker3 \
nohup python -m docdb_ingestion.pipeline run \
  --start-index 101 \
  --limit 50 \
  --worker-name worker3 \
  > logs/worker3.out 2>&1 &
```

### Why each worker needs its own temp dir

Each worker downloads and extracts files locally. Separate temp dirs prevent workers from interfering with each other’s temporary files.

Recommended pattern:

- `worker1` -> `./tmp/worker1`
- `worker2` -> `./tmp/worker2`
- `worker3` -> `./tmp/worker3`

### Why each worker needs its own log name

Each worker should have:

- its own shell log: `logs/worker1.out`, `logs/worker2.out`, `logs/worker3.out`
- its own app log: `pipeline_worker1.log`, `pipeline_worker2.log`, `pipeline_worker3.log`

This keeps troubleshooting much easier.

## Monitor workers later

The repo includes [monitor_workers.sh](/home/ygpalta/repos/bdds/docdb_ingestion/monitor_workers.sh:1).

### Run the monitor

```bash
./monitor_workers.sh
```

It auto-discovers running `python -m docdb_ingestion.pipeline run` processes and shows:

- PID and command
- CPU / memory / elapsed time
- temp dir and temp dir size
- tail of stdout file
- tail of app log file

### Useful monitor overrides

```bash
REFRESH_SECONDS=2 ./monitor_workers.sh
LINES_PER_LOG=20 ./monitor_workers.sh
DATE_DIR=2026-05-18 ./monitor_workers.sh
```

## Manual status checks

### List running pipeline processes

```bash
ps -ef | grep 'docdb_ingestion.pipeline' | grep -v grep
```

### Show process metrics

```bash
ps -p <pid> -o pid,ppid,%cpu,%mem,etime,stat,cmd
```

### Tail all shell logs together

```bash
tail -f logs/worker1.out logs/worker2.out logs/worker3.out
```

### Tail all app logs together

```bash
tail -f \
  logs/$(date +%F)/pipeline_worker1.log \
  logs/$(date +%F)/pipeline_worker2.log \
  logs/$(date +%F)/pipeline_worker3.log
```

## Stop workers

### Stop one worker by PID

```bash
kill <pid>
```

### Stop all pipeline workers by command

```bash
pkill -f "docdb_ingestion.pipeline run"
```

### Force kill if needed

```bash
kill -9 <pid>
```

Use force kill only if the process ignores normal `kill`.

## Restart after disconnect or later login

If you disconnect from SSH, the `nohup` workers keep running.

When you reconnect:

1. Check if workers are still alive:

```bash
ps -ef | grep 'docdb_ingestion.pipeline' | grep -v grep
```

2. Watch them:

```bash
./monitor_workers.sh
```

3. Or tail a specific log:

```bash
tail -f logs/worker1.out
```

## Running on multiple servers

The same pattern works across multiple EC2 instances.

The important rule is:

- do not overlap `--start-index` / `--limit` ranges across servers

Example:

Server 1:

```bash
mkdir -p tmp/server1_worker1 logs
EPO_TEMP_DIR=./tmp/server1_worker1 \
nohup python -m docdb_ingestion.pipeline run \
  --start-index 1 \
  --limit 100 \
  --worker-name server1_worker1 \
  > logs/server1_worker1.out 2>&1 &
```

Server 2:

```bash
mkdir -p tmp/server2_worker1 logs
EPO_TEMP_DIR=./tmp/server2_worker1 \
nohup python -m docdb_ingestion.pipeline run \
  --start-index 101 \
  --limit 100 \
  --worker-name server2_worker1 \
  > logs/server2_worker1.out 2>&1 &
```

## Notes

- The pipeline is safe to leave running for long periods.
- Logs remain on disk after the process exits.
- Checkpointing is stored in Postgres, not only in local files.
- For long backfills, prefer a few well-separated workers over launching too many at once.
