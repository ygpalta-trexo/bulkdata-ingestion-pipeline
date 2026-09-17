# Server Run Guide

This guide shows how to run the DOCDB ingestion pipeline on a long-lived server such as EC2, either as a single worker or as multiple parallel workers.

## First time setup

From the repo root:

```bash
cd /path/to/docdb_ingestion
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
mkdir -p tmp logs
```

Then configure your environment:

```bash
cp .env.example .env
# Edit .env with your PostgreSQL connection and any required secrets
```

### Database credentials from AWS Secrets Manager

On EC2 you can keep the database credentials in AWS Secrets Manager instead of `.env`:

```
USE_SECRET_MANAGER=true
DB_SECRET_NAME=<secret name or ARN>
AWS_REGION=<region the secret lives in>
POSTGRES_DB=bulk-data               # the database name still comes from .env
```

The secret must be a JSON object with `host`, `username` and `password`, and optionally `port`. The uppercase keys `DB_HOST`, `DB_USER`, `DB_PASSWORD` and `DB_PORT` also work; this is the same shape the DRM scripts read. With the flag on, `POSTGRES_HOST`, `POSTGRES_USER`, `POSTGRES_PASSWORD` and `DATABASE_URL` are ignored, and `POSTGRES_PORT` is used only if the secret has no port. Credentials are fetched once per process. If Postgres later rejects them, for example because the secret rotated during a long run, the secret is fetched again and that connection is retried once.

AWS access comes from the instance role, which needs `secretsmanager:GetSecretValue` on the secret, plus `kms:Decrypt` if the secret is encrypted with a customer-managed KMS key.

`merge_fast.py` still reads its RDS target from the `RDS_*` variables. Run it with `USE_SECRET_MANAGER=false`: with the flag on, its local source would resolve to the secret's database.

Validate the install before running the pipeline:

```bash
python -c "import docdb_ingestion"
python -m docdb_ingestion.pipeline --help
```

If you have a small sample ZIP folder, run a dry run to exercise parsing dependencies without writing to the database:

```bash
python process_folder.py /path/to/sample/zips --dry-run --limit 1
```

## Prerequisites

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

You can control batch size per run with `--batch-size` or via the `DOCDB_BATCH_SIZE` env var. Examples:

```bash
# Per-run override (sets 5k documents per staged upsert)
EPO_TEMP_DIR=./tmp/worker1 \
nohup python -m docdb_ingestion.pipeline run \
  --start-index 1 \
  --limit 100 \
  --batch-size 5000 \
  --worker-name worker1 \
  > logs/worker1.out 2>&1 &

# Or set once for the session (applies to all workers unless overridden)
export DOCDB_BATCH_SIZE=5000
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
  --batch-size 5000 \
  --worker-name worker1 \
  > logs/worker1.out 2>&1 &

EPO_TEMP_DIR=./tmp/worker2 \
nohup python -m docdb_ingestion.pipeline run \
  --start-index 51 \
  --limit 50 \
  --batch-size 5000 \
  --worker-name worker2 \
  > logs/worker2.out 2>&1 &

EPO_TEMP_DIR=./tmp/worker3 \
nohup python -m docdb_ingestion.pipeline run \
  --start-index 101 \
  --limit 50 \
  --batch-size 5000 \
  --worker-name worker3 \
  > logs/worker3.out 2>&1 &
```

Alternatively, set `DOCDB_BATCH_SIZE=5000` in the environment once and omit `--batch-size` from each command.

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

## Front-file ingestion (weekly updates)

The front-file runner (`frontfile_runner.py`) handles ongoing weekly deliveries from the EPO DOCDB front-file product. It is separate from the backfile pipeline: the backfile pipeline processes a fixed historical snapshot using `python -m docdb_ingestion.pipeline`, while the front-file runner continuously ingests new weekly publications after the backfile cutoff.

### How it works

The runner fetches all deliveries for the front-file product from the EPO API, filters to those published **after** `BACKFILE_TIME`, groups them by ISO week (`YYYY/NNN`), and processes them in the correct intra-week order:

1. **DeleteRekey** — must run first; re-keys existing primary keys
2. **CreateDelete** — creates and deletes records for the week
3. **Amend** — amends existing records

If any delivery fails the runner **halts immediately** and does not proceed to the next week. This prevents corrupting subsequent weeks with an incomplete base.

### Required `.env` variables

In addition to the standard database variables, set:

```
EPO_FRONTFILE_PRODUCT_ID=3          # product ID for the front-file (check with EPO)
BACKFILE_TIME=2026-02-24T10:50:03.000+01:00   # ISO-8601; deliveries on/before this are skipped
```

Optional overrides (all have sensible defaults):

```
EPO_API_BASE_URL=https://...        # override EPO API base URL
EPO_TEMP_DIR=./tmp                  # temp download/extraction directory
PIPELINE_LOG_FILE=/path/to/log      # explicit log file path
PIPELINE_WORKER_NAME=frontfile      # label used in default log filename
DOCDB_BATCH_SIZE=2000               # upsert batch size
```

### Dry run — preview the execution plan

Always run a dry-run first to see what weeks and deliveries would be processed, in what order, without downloading anything:

```bash
python frontfile_runner.py --mode catchup --dry-run
python frontfile_runner.py --mode latest  --dry-run
```

### Catch-up run — process all weeks after BACKFILE_TIME

Use this when running for the first time after the backfile completes, or to re-process a gap:

```bash
nohup python frontfile_runner.py --mode catchup \
  > logs/frontfile_catchup.out 2>&1 &
```

To control batch size:

```bash
nohup python frontfile_runner.py --mode catchup \
  --batch-size 5000 \
  --worker-name frontfile_catchup \
  > logs/frontfile_catchup.out 2>&1 &
```

The runner will process every week it finds, in ascending chronological order, halting on the first failure.

### Latest run — process only the most recent week (weekly cron)

Use this for the regular weekly update job. It limits processing to the single newest week found after `BACKFILE_TIME`:

```bash
python frontfile_runner.py --mode latest
```

Or as a background job:

```bash
nohup python frontfile_runner.py --mode latest \
  --worker-name frontfile_weekly \
  > logs/frontfile_weekly.out 2>&1 &
```

Typical cron entry (every Monday at 06:00, after EPO publishes the weekly delivery):

```
0 6 * * 1 cd /path/to/docdb_ingestion && source .venv/bin/activate && python frontfile_runner.py --mode latest >> logs/cron.out 2>&1
```

### Retry after a failure

If the runner halted on a failed delivery, fix the underlying issue (disk space, DB connectivity, bad ZIP), then re-run with `--retry-failed`:

```bash
# Catchup: retry all weeks that had failures
python frontfile_runner.py --mode catchup --retry-failed

# Latest: retry just the most recent week
python frontfile_runner.py --mode latest --retry-failed
```

### View logs

Shell output:

```bash
tail -f logs/frontfile_catchup.out
```

Application log (written to `logs/<date>/pipeline_frontfile.log` by default):

```bash
tail -f logs/$(date +%F)/pipeline_frontfile.log
```

Use `--log-file` or `--worker-name` to control the log destination:

```bash
python frontfile_runner.py --mode latest \
  --worker-name weekly \
  --log-file /var/log/docdb/frontfile.log
```

### Check if the runner is still going

```bash
ps -ef | grep frontfile_runner | grep -v grep
```

### Stop the runner

```bash
pkill -f frontfile_runner.py
```

---

## Notes

- The pipeline is safe to leave running for long periods.
- Logs remain on disk after the process exits.
- Checkpointing is stored in Postgres, not only in local files.
- For long backfills, prefer a few well-separated workers over launching too many at once.

## Tuning batch size

You can control how many documents are staged per bulk upsert using the `DOCDB_BATCH_SIZE` environment variable or the `--batch-size` CLI option on the pipeline and helper scripts (CLI overrides env). The default is 2000.

Quick examples:

```bash
# Use 5k document batches for this run
export DOCDB_BATCH_SIZE=5000
python -m docdb_ingestion.pipeline run --start-index 1 --limit 10 --worker-name worker1

# Or pass per-run (overrides env)
python -m docdb_ingestion.pipeline run --start-index 1 --limit 10 --batch-size 5000 --worker-name worker1

# Dry-run a single ZIP with process_folder.py
DOCDB_BATCH_SIZE=2000 python process_folder.py /path/to/sample/zips --dry-run --limit 1
```

Recommended approach:

- Increase batch size gradually (2k → 5k → 10k) and measure per-batch wall time, Postgres CPU, I/O and transaction duration.
- Target per-batch times of a few seconds when running many parallel workers; reduce batch size if commit times exceed ~20s or DB load becomes high.
- When running multiple workers, tune both batch size and worker count together — higher batch sizes improve per-worker throughput but increase transaction size and lock hold time.

Monitoring tips while tuning:

- Watch `top`/`htop` for CPU and disk iowait
- Use `pg_stat_activity` to surface long-running queries and transaction durations
- Inspect `pg_stat_progress_copy` and `pg_stat_database` for WAL/checkpoint pressure
- Ensure adequate disk space and monitor replication lag if present
