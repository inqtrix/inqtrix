# Grafana dashboards

`inqtrix-observability.json` is an importable Grafana dashboard (Grafana 10+)
covering the four operational areas fed by the shared Inqtrix metric
definitions:

1. **HTTP (RED)** — request rate, error rate, and p95 latency per route
   template.
2. **LLM & Search** — LLM request rate by model/outcome, p95 call latency,
   token throughput per feature, failure outcomes, and web-search calls.
3. **Runs & Worker** — run duration per mode, queue wait, worker job
   terminations per loop, and the queue depth/active/rejection surface.
4. **Knowledge** — retrieval step latency (hybrid search, rerank), embedding
   calls, and indexed documents per outcome.

Import via *Dashboards → New → Import*, upload the JSON, and select your
Prometheus datasource when prompted (the dashboard uses a datasource
variable, nothing is hardcoded).

## Scrape targets

All series come from two kinds of endpoints (see
[docs/observability/metrics.md](../../docs/observability/metrics.md)):

- **API**: `GET /metrics` on the API process — enabled with
  `INQTRIX_METRICS_ENABLED=true`. When `INQTRIX_SERVER_API_KEY` is set the
  endpoint requires that Bearer token.
- **Worker**: each worker process serves its own registry on
  `INQTRIX_WORKER_METRICS_PORT` (default `0` = off). One target per worker
  replica; there is no pushgateway.

### Compose

Prometheus is not part of the bundled stack. Point an existing Prometheus at
the containers on the compose network, for example:

```yaml
scrape_configs:
  - job_name: inqtrix-api
    metrics_path: /metrics
    # authorization: { credentials: <INQTRIX_SERVER_API_KEY> }  # when set
    static_configs:
      - targets: ["api:5100"]          # stack-internal API port
  - job_name: inqtrix-worker
    static_configs:
      - targets: ["worker:9091"]       # INQTRIX_WORKER_METRICS_PORT
```

### Kubernetes / Helm

The chart wires both endpoints with the `metrics` values block:

```yaml
metrics:
  enabled: true        # INQTRIX_METRICS_ENABLED on api + worker processes
  podAnnotations: true # prometheus.io/{scrape,port,path} on the pods
  workerPort: 9091     # INQTRIX_WORKER_METRICS_PORT + worker pod annotations
```

Set `podAnnotations: false` when scraping through `PodMonitor` /
`ServiceMonitor` CRs instead of annotation discovery; the worker container
still exposes the `metrics` containerPort for the selector.
