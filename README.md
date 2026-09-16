# API Monitor

HTTP endpoint monitoring service built as a mock DigitalOcean coding assignment. A FastAPI process serves the REST API and dashboard. A worker process performs the probes, writes history, and updates status.

Local **Docker Compose** still runs API and worker as two services. **Railway** (and the default Docker image) uses a production launcher that starts both processes in one container.

Set `APP_USERNAME` and `APP_PASSWORD` to enable HTTP Basic authentication on public deployments. `/health` remains unauthenticated for platform health checks.

## Architecture

```
Browser  ──►  FastAPI (CRUD, history, /health, /docs, dashboard)
                 │  SQLite (WAL)
Worker   ──►  same SQLite file
                 │
                 └── HTTP GET to a pre-validated destination IP
```

- **API process** stores endpoint configuration and sets `force_check=True` for a manual probe. It does not open outbound HTTP connections.
- **Worker process** is the only component that probes URLs. Every second (configurable) it selects due or force-check endpoints, runs them under a per-endpoint asyncio lock and a global concurrency semaphore, then records the result.
- **SQLite** holds configuration, check history, and alert events. WAL mode lets the API and worker share one file. Data survives restarts via the `data/` directory or a Docker volume.
- **Alerts** create one outage event after a configurable number of consecutive failures and one recovery event when the endpoint returns. Events appear in the dashboard and can optionally be delivered to an HTTPS JSON webhook.

A **single worker instance** is required. Overlap prevention is in-process. Scaling later would mean a row lease (`UPDATE endpoints SET claimed_until=... WHERE claimed_until < now`) or an external queue (Redis/NATS) with one checker pool.

On Railway the launcher (`python -m app.launch`) is PID 1: it starts uvicorn and the worker, forwards `SIGTERM`/`SIGINT`, and exits if either child dies so the platform can restart the container.

## Data model

**Endpoint**

| Field | Meaning |
| --- | --- |
| `name`, `url`, `enabled`, `interval_seconds` | User-configured probe target. Interval defaults to 60 seconds. |
| `force_check` | Set by the API; consumed by the worker when a check starts. |
| `next_check_at` | When the next scheduled probe is allowed. |
| `last_*` | Denormalized latest result for cheap list views. |
| `total_failures` | Lifetime DOWN count. Never decreases. |
| `consecutive_failures` | Current DOWN streak. Reset to 0 on UP. |

**CheckResult**

| Field | Meaning |
| --- | --- |
| `status_code` | HTTP status if a response was received, otherwise `null`. |
| `availability` | `UP` or `DOWN`. |
| `response_time_ms` | Elapsed monotonic time when a response was received, otherwise `null`. |
| `error_message` | Short, safe message on failure. URLs and secrets are not stored here. |
| `checked_at` | UTC timestamp. |

History is pruned by retention days and a per-endpoint row cap.

**AlertEvent** records outage and recovery transitions, the failure count that triggered the event, and optional webhook delivery status. Alerts are deleted with their endpoint.

## Status semantics

- **UP**: HTTP status 200–299.
- **DOWN**: any other status, timeout, DNS failure, connection failure, TLS failure, or blocked destination.
- **UNKNOWN**: the endpoint exists but has never been checked. Shown only on the endpoint, not on history rows.
- **Latency**: measured with `time.perf_counter()` (monotonic). The dashboard shows `—` when `response_time_ms` is `null` (no HTTP response).
- **Redirects**: not followed. A 3xx response is DOWN and counts as a received response (status + latency are stored). Each redirect target would need the same SSRF checks if following were enabled.
- **Disabled endpoints**: skipped by the scheduler. An explicit “Check” still runs (the user asked).
- **Failure counts**: a recorded DOWN increments `total_failures` and `consecutive_failures`. A recorded UP sets `consecutive_failures = 0` and leaves `total_failures` unchanged. Counts do not change when a check is skipped.

## Security defaults

- Only `http://` and `https://`.
- Embedded credentials (`user:pass@host`) are rejected.
- Loopback, private, link-local, reserved, documentation, multicast, CGNAT, cloud metadata, and IPv6 equivalents are rejected, including IPv4-mapped, 6to4, Teredo, and NAT64 encodings.
- Hostnames such as `localhost`, `*.local`, `*.internal`, and `metadata.google.internal` are rejected before DNS.
- At check time the worker resolves, validates **every** address, then connects to that already-validated IP. The original hostname is used for `Host` and TLS SNI. This avoids a validate-then-resolve-again gap.
- TLS certificate verification stays on. `HTTP_PROXY` from the environment is ignored (`trust_env=False`).
- Response bodies are truncated (`MAX_RESPONSE_BYTES`, default 8 KiB).
- Logs use endpoint IDs, not URLs, so query-string secrets are not written to stdout.

Tests that need “internal” destinations use mocked DNS and `httpx.MockTransport`. They never open real sockets to private or loopback addresses.

## Local setup

Requires Python 3.12+.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
mkdir -p data
```

Terminal 1:

```bash
uvicorn app.main:app --reload --port 8000
```

Terminal 2:

```bash
python -m app.worker
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) for the dashboard, [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) for generated API docs, and [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health) for liveness.

### API examples

```bash
# Create
curl -s -X POST http://127.0.0.1:8000/api/endpoints \
  -H 'Content-Type: application/json' \
  -d '{"name":"Example","url":"https://example.com/","interval_seconds":60}'

# List
curl -s http://127.0.0.1:8000/api/endpoints

# Edit / disable
curl -s -X PATCH http://127.0.0.1:8000/api/endpoints/<id> \
  -H 'Content-Type: application/json' \
  -d '{"enabled":false}'

# Probe now (waits for the worker)
curl -s -X POST http://127.0.0.1:8000/api/endpoints/<id>/check

# History (newest first)
curl -s 'http://127.0.0.1:8000/api/endpoints/<id>/checks?page=1&page_size=20'

# Recent outage and recovery alerts
curl -s 'http://127.0.0.1:8000/api/alerts?limit=20'

# Delete
curl -s -X DELETE http://127.0.0.1:8000/api/endpoints/<id>
```

Errors use a consistent body: `{"error": "...", "message": "...", "details": ...}`.

## Docker setup

```bash
cp .env.example .env   # optional; compose already loads .env.example
docker compose up --build
```

- API: `http://127.0.0.1:8000`
- SQLite file: Docker volume `monitor-data` mounted at `/data/monitor.db`
- Worker and API share that volume

The image default command is `python -m app.launch` (API + worker together). Compose **overrides** that with separate `command:` values, so local API and worker remain two containers.

Stop with `docker compose down`. The named volume keeps history. Add `-v` only if you intend to wipe it.

## Deploying to Railway

Do not run these steps from this coding session; they are for you to apply in the Railway dashboard.

1. Create a new Railway project and deploy this repo. Railway should detect the `Dockerfile`. The default CMD starts both the API and the worker.
2. Add a **volume** mounted at `/data`. SQLite must live on that volume so checks survive restarts and deploys.
3. Set environment variables:

   | Variable | Required | Value |
   | --- | --- | --- |
   | `PORT` | Set by Railway | Leave Railway’s value. The launcher binds `0.0.0.0:${PORT:-8000}`. |
   | `DATABASE_URL` | Yes | `sqlite+aiosqlite:////data/monitor.db` |
   | `APP_USERNAME` | Recommended | Dashboard/API Basic auth username |
   | `APP_PASSWORD` | Recommended | Dashboard/API Basic auth password |
   | `ALERT_FAILURE_THRESHOLD` | Optional | Consecutive failures before an outage alert; default `3` |
   | `ALERT_WEBHOOK_URL` | Optional | HTTPS endpoint that receives outage and recovery JSON |
   | `LOG_LEVEL` | Optional | `INFO` |

   Both `APP_USERNAME` and `APP_PASSWORD` must be non-empty to enable auth. `/health` stays public so Railway can health-check the service.
4. Generate a **public domain** in Railway (Settings → Networking → Generate domain). The app is then at `https://<your-service>.up.railway.app`. `/health` should return `{"status":"ok","database":"ok"}` without credentials. The dashboard, `/api`, `/docs`, and `/redoc` prompt for Basic auth when credentials are configured.
5. Keep a **single replica**. Two replicas would run two workers against one SQLite file.

This session does not create a Railway project or deploy the image.

## Testing

```bash
ruff check .
pytest -q
```

Tests are deterministic: DNS and HTTP are mocked. They do not call the public internet.

## Configuration

All settings are environment variables (see `.env.example`):

| Variable | Default | Role |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/monitor.db` | SQLite location |
| `REQUEST_TIMEOUT_SECONDS` | `10` | Per-probe timeout |
| `MIN_CHECK_INTERVAL_SECONDS` | `10` | Lower bound for interval |
| `MAX_CHECK_INTERVAL_SECONDS` | `3600` | Upper bound |
| `DEFAULT_CHECK_INTERVAL_SECONDS` | `60` | Used when create omits interval |
| `CHECK_CONCURRENCY` | `10` | Max in-flight probes in the worker |
| `HISTORY_RETENTION_DAYS` | `7` | Age-based prune |
| `HISTORY_MAX_RECORDS_PER_ENDPOINT` | `1000` | Count-based prune |
| `WORKER_POLL_SECONDS` | `1` | Scheduler tick |
| `MANUAL_CHECK_WAIT_SECONDS` | `20` | API wait after “check now” |
| `MAX_RESPONSE_BYTES` | `8192` | Body cap |
| `LOG_LEVEL` | `INFO` | Logging |
| `APP_USERNAME` | empty | Optional Basic auth user |
| `APP_PASSWORD` | empty | Optional Basic auth password |
| `ALERT_FAILURE_THRESHOLD` | `3` | Consecutive failures before creating an outage alert |
| `ALERT_WEBHOOK_URL` | empty | Optional HTTPS JSON webhook for outage and recovery events |
| `ALERT_WEBHOOK_TIMEOUT_SECONDS` | `5` | Webhook request timeout |
