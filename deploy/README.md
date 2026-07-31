# Local Observability Setup (Prometheus + Grafana)

This runs Prometheus and Grafana as plain `docker run` containers against
the gateway running on your host machine. (A full `docker-compose.yml` for
the whole stack, including the gateway itself, is a separate later step -
this is just Prometheus + Grafana, per Step 15's own scope.)

## 1. Start the gateway

From the repo root, with Redis running and your `.env` filled in (see the
top-level README from Step 1):

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Confirm metrics are being produced:

```bash
curl http://localhost:8000/metrics | head -20
```

## 2. Start Prometheus

```bash
docker run -d --name llm-gateway-prometheus \
  --add-host=host.docker.internal:host-gateway \
  -p 9090:9090 \
  -v "$(pwd)/deploy/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml" \
  prom/prometheus
```

`--add-host=host.docker.internal:host-gateway` is what makes
`host.docker.internal` (used in `prometheus.yml`) resolve to your host
machine on Linux - Docker Desktop (Mac/Windows) already provides this
without the flag, but including it is harmless there too.

Open http://localhost:9090/targets and confirm the `llm-gateway` job shows
as `UP`. If it doesn't, check that the gateway is actually listening on
`0.0.0.0` (not just `127.0.0.1`) so the container can reach it.

## 3. Start Grafana

```bash
docker run -d --name llm-gateway-grafana \
  --add-host=host.docker.internal:host-gateway \
  -p 3000:3000 \
  -v "$(pwd)/deploy/grafana/provisioning:/etc/grafana/provisioning" \
  -v "$(pwd)/deploy/grafana/dashboards:/etc/grafana/provisioning/dashboards/json" \
  grafana/grafana
```

Open http://localhost:3000 (default login `admin` / `admin`, you'll be
prompted to change it). The Prometheus datasource and all three dashboards
are auto-provisioned - no manual import needed. Look under
**Dashboards** for:

- **LLM Gateway - Operations** - request rate, error rate/breakdown,
  latency percentiles, circuit breaker state, fallback rate.
- **LLM Gateway - Business** - cost per team, budget utilization %,
  token throughput, requests by model, fallback impact, total spend.
- **LLM Gateway - Performance** - RPS by model/provider, p95/p99 latency,
  token throughput, error rate by provider, admission-limited requests.

## 4. Generate some traffic

Dashboards are empty until there's data. Fire a few requests at the
gateway (see the top-level README for example `curl` commands against
`/v1/chat/completions`), then refresh - panels auto-refresh every 10s.

## Manual import (alternative to provisioning)

If you'd rather run Grafana without the provisioning volumes, import each
file under `deploy/grafana/dashboards/*.json` manually via
**Dashboards → New → Import**, and add a Prometheus datasource pointed at
`http://host.docker.internal:9090` (or `http://localhost:9090` if Grafana
isn't containerized) via **Connections → Data sources → Add data source**.

## Cleanup

```bash
docker rm -f llm-gateway-prometheus llm-gateway-grafana
```
