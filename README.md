# Soundings

External HTTP endpoint prober with consecutive-failure alerting via ntfy.

Probes service URLs from outside the Docker network to catch port-binding failures, reverse-proxy misroutes, and DNS issues that internal Docker healthchecks miss.

## Why

Internal Docker healthchecks confirm "the app process responds to localhost inside the container." They cannot detect broken port bindings, reverse-proxy misroutes, or DNS issues. Soundings probes from the host network — the same perspective a user has.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `ENDPOINTS` | (required) | Comma-separated `url:label` pairs |
| `PROBE_INTERVAL` | `60` | Seconds between probe cycles |
| `FAILURE_THRESHOLD` | `3` | Consecutive failures before alerting |
| `NTFY_URL` | `http://127.0.0.1:8888` | ntfy server URL |
| `NTFY_TOPIC` | `vela` | ntfy topic for alerts |
| `API_PORT` | `8591` | Port for the JSON status API |
| `PROBE_TIMEOUT` | `10` | HTTP request timeout in seconds |
| `RECOVERY_NOTIFY` | `true` | Send notification when endpoint recovers |

## API

- `GET /api/status` — JSON status of all endpoints
- `GET /health` — healthcheck endpoint

## Docker

```bash
docker compose up -d
```

Uses `network_mode: host` so probes hit services via `127.0.0.1` — the same path a user or reverse proxy would take.
