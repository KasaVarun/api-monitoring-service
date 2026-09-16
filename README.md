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
- **SQLite** holds both configuration and check history. WAL mode lets the API and worker share one file. Data survives restarts via the `data/` directory or a Docker volume.

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

## Deploying to a DigitalOcean Droplet

These are instructions only. This repository does not create paid resources.

1. Create an Ubuntu Droplet and a non-root user with Docker and Docker Compose installed.
2. Open only ports 22, 80, and 443 in the cloud firewall. Do not publish port 8000 publicly.
3. Clone this repo onto the droplet. `cp .env.example .env` and keep `DATABASE_URL=sqlite+aiosqlite:////data/monitor.db`.
4. Run `docker compose up -d --build`. The `monitor-data` volume is the persistent store; back it up with `docker run --rm -v api-monitor_monitor-data:/data -v $PWD:/backup busybox tar czf /backup/monitor.tgz /data`.
5. Put Caddy or Nginx on the host (or as a compose service) to terminate HTTPS with Let’s Encrypt. You can also set `APP_USERNAME` and `APP_PASSWORD` on the containers for in-app Basic auth. A starting Caddyfile is in `deploy/Caddyfile.example`. Point the proxy at `127.0.0.1:8000` and do not expose the API port on `0.0.0.0` in production (`ports` in compose can be changed to `127.0.0.1:8000:8000`).
6. Confirm `/health` through the proxy, then register a real HTTPS endpoint from the dashboard.

Public internet exposure without Basic auth or an equivalent proxy is not acceptable.

## Tradeoffs and limitations

- **One worker.** Simple to explain and correct for the assignment. Not horizontally scalable without a distributed lock or queue.
- **SQLite.** Perfect for a single droplet or one Railway replica and a few hundred endpoints. WAL handles the API+worker pair. It is not the right store for a multi-region service.
- **Optional in-app Basic auth.** Enabled only when both `APP_USERNAME` and `APP_PASSWORD` are set. There are no user accounts or roles.
- **HEAD is not used.** Some origin servers skip GET-only health paths; GET with a truncated body is more compatible and still bounded.
- **3xx is DOWN.** That matches the written 200–299 rule. If “follow safe redirects” is needed later, each hop must pass the same IP validation and connect to the validated address.
- **Uptime is check-based, last 24 hours.** `uptime_percent = up_checks / total_checks`. It is not time-weighted. Gaps while the worker is stopped are not counted as downtime.
- **IPv6/IPv4 policy is fail-closed.** If any resolved address is blocked, the destination is rejected. That stops DNS rebinding at the cost of refusing dual-stack hosts that publish a private extra record.
- **Alerts are out of scope.** The consecutive failure count is the hook you would use for paging later.

## Interview walkthrough

1. **Register.** Dashboard `POST /api/endpoints` → FastAPI validates JSON, interval bounds, URL syntax, and resolved IPs → row inserted with `next_check_at=now` and `availability=UNKNOWN`.
2. **Schedule.** Worker tick loads `force_check OR (enabled AND next_check_at <= now)`, skips IDs already in-flight, and starts at most `CHECK_CONCURRENCY` tasks.
3. **Probe.** For that endpoint the worker takes an asyncio lock (no overlapping probe), re-reads the row, consumes `force_check`, resolves+validates, connects to the pinned IP with Host/SNI of the original hostname, times the GET with a monotonic clock, and truncates the body.
4. **Record.** Result row is appended. DOWN increments both failure counters; UP resets the streak only. `next_check_at` becomes `checked_at + interval`. Old history is pruned.
5. **Read.** List endpoints uses the denormalized last result. Details add paginated history (newest first) and 24-hour uptime. Manual check sets `force_check` and waits until a history row appears or `MANUAL_CHECK_WAIT_SECONDS` elapses (504 if the worker is down).
6. **Scale later.** Keep the API stateless. Move the worker to a lease/queue so N replicas can run without double-checking the same endpoint. Swap SQLite for Postgres when you need multiple API nodes.
