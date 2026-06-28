# Fluid AI DevOps Challenge — Flask + PostgreSQL + Redis on Kubernetes

## Architecture

```
                ┌──────────────────────────────┐
   GitHub  ──►  │   GitHub Actions CI/CD          │
   push         │   Job 1: build (hosted runner)  │
   (main)       │     - docker build ./app         │
                │     - push to ghcr.io            │
                │   Job 2: deploy (self-hosted)    │
                │     - inject secrets via sed      │
                │     - kubectl apply (all manifests)│
                │     - kubectl set image           │
                │     - kubectl rollout status      │
                └────────────────┬─────────────────┘
                                 │
                                 ▼
                ┌──────────────────────────────────────┐
                │  Minikube cluster                       │
                │  namespace: fluidai-demo                 │
                │                                           │
                │  Ingress: flask-ingress (host: flask-app.local)
                │       │                                    │
                │       ▼                                    │
                │  Service: flask-app (ClusterIP :80 → :5000) │
                │       │                                    │
                │       ▼                                    │
                │  Deployment: flask-app (replicas: 2)         │
                │   - readiness: GET /health/ready              │
                │   - liveness:  GET /health/live                │
                │   - /metrics (Prometheus)                       │
                │       │                  │                       │
                │       ▼                  ▼                       │
                │  Service: postgres   Service: redis                │
                │  (headless,          (ClusterIP :6379)              │
                │   ClusterIP: None)        │                          │
                │       │                  ▼                          │
                │       ▼            Deployment: redis (replicas: 1)    │
                │  StatefulSet: postgres   - PVC: redis-pvc (256Mi)       │
                │  (replicas: 1)             - AOF persistence              │
                │   - volumeClaimTemplate:                                  │
                │     postgres-data (1Gi)                                    │
                └──────────────────────────────────────────────────────────┘
```

## What's actually deployed

| Resource | File | Notes |
|---|---|---|
| Namespace | `namespace.yaml` | `fluidai-demo` |
| Secret | `secret.yaml` | `app-secrets` — placeholders templated by CI via `sed` from GitHub Secrets |
| ConfigMap | `configmap.yaml` | `app-config` — `APP_ENV`, `LOG_LEVEL`, `PORT` |
| Flask Deployment | `flask-deployment.yaml` | 2 replicas, rolling update, readiness/liveness probes, Prometheus scrape annotations |
| Flask Service | `flask-service.yaml` | ClusterIP, port 80 → 5000 |
| Ingress | `ingress.yaml` | nginx ingress class, host `flask-app.local` |
| Postgres | `postgres-statefulset.yaml` + `postgres-service.yaml` | StatefulSet with `volumeClaimTemplate` (1Gi), headless service |
| Redis | `redis-deployment.yaml` + `redis-service.yaml` | Deployment + PVC (256Mi), AOF persistence enabled |
| HPA | `hpa.yaml` | **currently empty — not yet implemented**, see Tradeoffs |

## Prerequisites
- Docker, kubectl, Minikube already installed and working
- nginx ingress controller enabled on Minikube: `minikube addons enable ingress`
- GitHub repo secrets already configured: `KUBE_CONFIG` (base64-encoded kubeconfig), `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`
- A self-hosted GitHub Actions runner with network access to the Minikube cluster (registered under Settings → Actions → Runners)

## One-time setup

```bash
minikube start
minikube addons enable ingress

# Point flask-app.local at Minikube's IP for the ingress demo
echo "$(minikube ip) flask-app.local" | sudo tee -a /etc/hosts
```

## First deploy (manual, before relying on CI/CD)

```bash
cd app
docker build -t ghcr.io/rajujena0/flask-app:latest .
minikube image load ghcr.io/rajujena0/flask-app:latest

cd ../k8s
kubectl apply -f namespace.yaml

# secret.yaml still has PLACEHOLDER_* values unless CI has run sed against it —
# for a manual local test, replace the placeholders yourself first, e.g.:
#   sed -i "s|PLACEHOLDER_DB_URL|postgresql://appuser:apppass@postgres:5432/appdb|g" secret.yaml
#   sed -i "s|PLACEHOLDER_REDIS_URL|redis://redis:6379/0|g" secret.yaml
#   sed -i "s|PLACEHOLDER_PG_USER|appuser|g" secret.yaml
#   sed -i "s|PLACEHOLDER_PG_PASS|apppass|g" secret.yaml
#   sed -i "s|PLACEHOLDER_PG_DB|appdb|g" secret.yaml
kubectl apply -f secret.yaml
kubectl apply -f configmap.yaml

kubectl apply -f postgres-service.yaml
kubectl apply -f postgres-statefulset.yaml
kubectl wait --for=condition=ready pod -l app=postgres -n fluidai-demo --timeout=120s

kubectl apply -f redis-service.yaml
kubectl apply -f redis-deployment.yaml
kubectl wait --for=condition=ready pod -l app=redis -n fluidai-demo --timeout=60s

kubectl apply -f flask-service.yaml
kubectl apply -f ingress.yaml
kubectl apply -f flask-deployment.yaml

kubectl get pods -n fluidai-demo -w
```

> **`imagePullPolicy: Always` note:** `flask-deployment.yaml` sets `imagePullPolicy: Always`,
> so even after `minikube image load`, Kubernetes will try to pull from GHCR rather than use
> the locally loaded image. For a quick local test without pushing to GHCR first, temporarily
> set this to `IfNotPresent`, or just push to GHCR once and let it pull normally.

## Verify it works

```bash
kubectl get all -n fluidai-demo
kubectl get ingress -n fluidai-demo

# IMPORTANT: initialize the database schema once before hitting / — the app does NOT
# auto-create the `visits` table on startup, only on this endpoint:
curl http://flask-app.local/init-db

curl http://flask-app.local/
curl http://flask-app.local/health/ready
curl http://flask-app.local/health/live
curl http://flask-app.local/metrics
```

If ingress/hosts isn't cooperating, fall back to port-forwarding the Service directly:
```bash
kubectl port-forward svc/flask-app 8080:80 -n fluidai-demo
curl http://localhost:8080/init-db
curl http://localhost:8080/
```

## CI/CD

`.github/workflows/ci-cd.yml` triggers on push to `main`:
1. **`build`** (hosted `ubuntu-latest` runner): builds `./app`, pushes `ghcr.io/<owner>/flask-app:latest` and `:<sha>`.
2. **`deploy`** (`self-hosted` runner, must have network access to the Minikube cluster):
   - writes `~/.kube/config` from the `KUBE_CONFIG` secret
   - uses `sed` to inject real `POSTGRES_*` and `REDIS_URL`/`DATABASE_URL` values into `k8s/secret.yaml`, replacing the placeholders
   - applies namespace, secret, configmap, Postgres, Redis, then the Flask Service/Ingress/Deployment
   - `kubectl set image` to the new commit-SHA tag, then `kubectl rollout status`
   - runs `kubectl get all` and `kubectl get ingress` as a final verification step (good to show in the Live Demo section — the Actions log itself is evidence the deploy succeeded)

**Note for the video:** because `deploy` regenerates `secret.yaml` from GitHub Secrets on every run, any manual edits you make directly to the Secret on the cluster (e.g., for the failure demo below) will be overwritten the next time CI/CD runs. Don't trigger a pipeline run between breaking and fixing the Secret during your failure demo, or the "fix" will appear to happen on its own.

## Reliability improvement: readiness + liveness probes

**Why this one:** with three backing dependencies now (Postgres, Redis, and the app itself), probes are the highest-leverage reliability mechanism available — they're what actually prevents a half-initialized or dependency-less pod from receiving traffic, and they directly drive the autohealing behavior Kubernetes is known for.

**Problem it solves:**
- `/health/ready` checks both Redis and Postgres connectivity before the pod is marked Ready — without this, a pod that starts before Redis/Postgres are reachable would receive live traffic and 500 on every request.
- `/health/live` is intentionally decoupled from dependency health (it just returns "alive") — this means a temporary Postgres or Redis blip causes the pod to fail *readiness* (pulled from the Service's load-balancing pool, no new traffic) without also failing *liveness* and getting killed. This is a deliberate design choice in your own code, worth calling out on camera: it avoids a self-inflicted restart storm if the DB has a brief hiccup.

**Tradeoff introduced:**
- Because liveness no longer reflects dependency health, a pod stuck in a *permanently* broken dependency state (e.g., a hung connection pool that can't recover even after the DB returns) will sit there marked "not ready" forever rather than getting restarted automatically. Liveness alone can't rescue you from that — you'd need a separate alert or a more sophisticated liveness check that distinguishes "dependency down" from "process actually wedged."
- The split also means a request hitting `/` directly (not gated by readiness, since Kubernetes only uses probes for routing decisions, not the app's own logic) can still throw a raw 500 with a stack-trace-shaped JSON error if a request slips through during the readiness-to-not-ready transition window — visible in your own `except Exception as e: return jsonify({"error": str(e)}), 500` handler.

## Intentional failure simulation

**Scenario:** corrupt `DATABASE_URL` in the `app-secrets` Secret to point at a nonexistent host, simulating a bad value being pushed during a deploy (e.g., a typo'd hostname or wrong port committed to the templating step).

```bash
# Break it
kubectl get secret app-secrets -n fluidai-demo -o jsonpath='{.data.DATABASE_URL}' | base64 -d
# (note the current value before breaking it, so the fix step is a clean before/after)

kubectl patch secret app-secrets -n fluidai-demo --type merge \
  -p '{"stringData":{"DATABASE_URL":"postgresql://appuser:apppass@postgres-typo:5432/appdb"}}'

# Secret changes don't propagate to already-running pods' env vars automatically —
# force a rollout to actually pick up the bad value
kubectl rollout restart deployment/flask-app -n fluidai-demo
```

**Symptoms to show on camera:**
```bash
kubectl get pods -n fluidai-demo -w
# pods go Running -> 0/1 Ready, NOT CrashLoopBackOff this time — because /health/live
# doesn't check the DB, the container itself stays alive; only readiness fails.
# This is worth narrating explicitly: it looks different from a typical crash demo,
# and that difference is the point — it shows you understand what your probes do.

kubectl describe pod <pod-name> -n fluidai-demo
# Events: "Readiness probe failed: HTTP probe failed with statuscode: 503"
# No liveness failures, no restarts — pod stays Running but is pulled out of the
# Service's endpoint list (check: kubectl get endpoints flask-app -n fluidai-demo)

kubectl logs <pod-name> -n fluidai-demo
# "Readiness check failed: could not translate host name "postgres-typo" to address"

curl http://flask-app.local/
# Likely still resolves if any of the 2 replicas haven't rolled yet, or fails entirely
# once both are unready — depending on rollout timing, good live narration moment
```

**Debugging narrative (say this out loud while running commands):**
1. `kubectl get pods` shows pods Running but not Ready (`0/1`), not crashing — first observation: this isn't a startup failure or a code crash, since the process is alive.
2. `kubectl get endpoints flask-app -n fluidai-demo` shows the Service has zero (or fewer) endpoints — confirms Kubernetes itself has already detected the problem and pulled the pod out of rotation, which is the probes doing their job correctly.
3. `kubectl describe pod` events confirm it's specifically the readiness probe hitting `/health/ready` and getting a 503 — narrows it to a dependency check failing inside the app, not a Kubernetes-level scheduling or image issue.
4. `kubectl logs` gives the exact error: DNS resolution failure on `postgres-typo`. Rules out "Postgres itself is down" (a real outage would say connection refused, not unknown host) — this is specifically a configuration/typo problem.
5. Trace to source: `kubectl get secret app-secrets -n fluidai-demo -o jsonpath='{.data.DATABASE_URL}' | base64 -d` confirms the value was changed.
6. Root cause: `DATABASE_URL` in the Secret points at a Service name (`postgres-typo`) that doesn't exist; the real Service is named `postgres`.

**Fix:**
```bash
kubectl patch secret app-secrets -n fluidai-demo --type merge \
  -p '{"stringData":{"DATABASE_URL":"postgresql://appuser:apppass@postgres:5432/appdb"}}'
kubectl rollout restart deployment/flask-app -n fluidai-demo
kubectl rollout status deployment/flask-app -n fluidai-demo
kubectl get pods -n fluidai-demo
kubectl get endpoints flask-app -n fluidai-demo
curl http://flask-app.local/health/ready
curl http://flask-app.local/
```

## Tradeoffs (state these explicitly in the video)

- **`hpa.yaml` is currently empty.** Autoscaling was scoped but not implemented — call this out directly rather than hiding it; it's an honest, defensible scope cut given the time budget, not an oversight to gloss over.
- **Single Postgres replica, no automated backups.** A `volumeClaimTemplate` gives the StatefulSet persistent storage across pod restarts, but a node failure or a deleted PVC still loses data. Production would add a managed DB or a replicated operator (CloudNativePG, Zalando) with scheduled backups.
- **Redis has no persistence guarantee beyond AOF on a single replica.** Fine as a fast counter/cache; if Redis is ever load-bearing for correctness (not just a hit counter), it needs replication too.
- **Secrets templated via `sed` in CI, not a real secrets manager.** Functional, but the rendered `secret.yaml` with real values briefly exists in the runner's working directory during the job, and the committed `secret.yaml` always has placeholder text (good practice) but the substitution mechanism itself is fragile — a wrong `sed` pattern silently leaves a placeholder in place rather than failing loudly. Production would use Sealed Secrets, SOPS, or vault/KMS-backed injection.
- **`kubectl apply` from CI rather than GitOps.** Simpler to reason about and faster to build; no drift detection if the cluster state changes outside the pipeline (as happens deliberately in the failure demo above). ArgoCD/Flux would reconcile that automatically in production.
- **No circuit breaker between the app and its dependencies.** Every request to `/` independently attempts a fresh Postgres and Redis connection; under sustained outage, this means every request pays the full connection-timeout cost rather than failing fast. Acceptable at this scale; would matter under real load.
- **Single-node Minikube.** Doesn't exercise multi-node scheduling, pod anti-affinity, or real node-failure behavior the way EKS/GKE/AKS would.
- **Ingress relies on `/etc/hosts` + Minikube's ingress addon**, which is a local-only stand-in for real DNS + a cloud load balancer + TLS via cert-manager in production.
