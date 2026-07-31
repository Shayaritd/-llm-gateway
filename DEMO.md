# LLM Gateway - Reviewer Demo

Everything needed to see the whole system working, with **zero real API
keys required** - the stack ships with mock providers that mimic the
OpenAI/Anthropic/Ollama APIs closely enough for every gateway feature
(routing, retries, fallback, circuit breakers, streaming, metrics) to work
against them unmodified.

## Requirements

Docker + Docker Compose. That's it.

## One command

```bash
docker compose up --build
```

Wait for all five services to report healthy (~30-60s on first build - it's
pulling/building 5 images). You'll see the gateway's log settle once
startup is complete.

## What's running

| Service | URL | Purpose |
|---|---|---|
| Gateway | http://localhost:8000 | The LLM Gateway itself |
| Mock providers | http://localhost:9100 | Stand-ins for OpenAI/Anthropic/Ollama |
| Prometheus | http://localhost:9090 | Metrics store |
| Grafana | http://localhost:3000 | Dashboards (no login needed - anonymous admin access, demo-only setting) |
| Redis | localhost:6379 | Rate limit/budget/admin state |

Demo teams are pre-seeded (see `deploy/compose/config.yaml`):

| Team | API Key | Notes |
|---|---|---|
| team-a | `sk-team-a-demo-key` | High priority, generous limits, all 3 models |
| team-b | `sk-team-b-demo-key` | Tight rate limit (rpm=5) - easy to trip |
| team-c | `sk-team-c-demo-key` | Restricted to the openai provider only |
| admin  | `sk-admin-demo-key` | Admin API (`/admin/*`) |

## 5-minute walkthrough

**1. A normal request:**
```bash
curl -s -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-team-a-demo-key" -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"hello"}]}' | python3 -m json.tool
```
Notice the response includes a disclaimer (team-a's configured policy) and
usage/cost fields.

**2. Trigger fallback live** - the mock providers fail on demand if a
message contains `FAIL:<provider>`:
```bash
curl -s -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-team-a-demo-key" -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"FAIL:openai please"}]}' | python3 -m json.tool
```
Look at the `model`/`provider` actually served vs `requested_model`, and
`fallback_attempts` showing exactly what failed first.

**3. Trip the circuit breaker** - fire the same failure repeatedly (fast
enough to exceed `failure_threshold`, default 3):
```bash
for i in 1 2 3 4; do
  curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/v1/chat/completions \
    -H "Authorization: Bearer sk-team-a-demo-key" -H "Content-Type: application/json" \
    -d '{"model":"claude-3-5-sonnet","messages":[{"role":"user","content":"FAIL:anthropic FAIL:openai"}]}'
done
curl -s http://localhost:8000/health/circuit-breakers | python3 -m json.tool
```

**4. Streaming:**
```bash
curl -N -X POST http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-team-a-demo-key" -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"stream please"}],"stream":true}'
```

**5. Load test + rate limiting** (run from the host, needs
`pip install httpx`):
```bash
python3 deploy/loadtest/load_test.py --team team-b --requests 20 --concurrency 20
```
team-b's `rpm=5` means ~5 succeed and the rest come back `429` - watch it
happen in real time.

**6. Admin API** - view/adjust limits live:
```bash
curl -s http://localhost:8000/admin/teams -H "X-Admin-Key: sk-admin-demo-key" | python3 -m json.tool
curl -s -X PATCH http://localhost:8000/admin/teams/team-b/limits \
  -H "X-Admin-Key: sk-admin-demo-key" -H "Content-Type: application/json" \
  -d '{"rate_limit": {"rpm": 100, "tpm": 100000}}'
```

**7. Grafana dashboards** - open http://localhost:3000, check
**Dashboards**: Operations, Business, and Performance are auto-provisioned.
Generate a bit of traffic first (steps 1-5 above) so panels aren't empty.

**8. Distributed tracing** - spans print to the gateway's own container
logs (`docker compose logs gateway`) since no trace collector is wired up
yet; search for `"name": "gateway.` to see the full span tree per request.

**9. Alerting (Step 16)** - Prometheus evaluates `deploy/prometheus/alert_rules.yml`
and pushes firing alerts to Alertmanager, which forwards them to Slack -
by default, a bundled mock receiver instead of a real Slack workspace, so
this works with zero setup:
```bash
# Trip the CircuitBreakerOpen alert (fires immediately once the circuit
# is open - see step 3 above to trip it), then watch it arrive:
docker compose logs -f mock-slack-receiver
```
Check http://localhost:9090/alerts to see all 4 rules (`ProviderErrorRateHigh`,
`TeamBudgetExceeded`/`TeamBudgetApproaching`, `P99LatencyHigh`,
`CircuitBreakerOpen`) and their current state. To route to a real Slack
channel instead, set `SLACK_WEBHOOK_URL` in a repo-root `.env` file (see
`.env.example`) before `docker compose up`.

## Cleanup

```bash
docker compose down -v
```

## If something doesn't come up healthy

```bash
docker compose logs gateway          # gateway crash-looping? check config/env
docker compose logs mock-providers   # mock provider issues
docker compose ps                    # see which service is unhealthy
```
