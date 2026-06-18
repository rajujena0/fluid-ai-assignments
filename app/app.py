import os, time, logging
import psycopg2
import redis
from flask import Flask, jsonify
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

app = Flask(__name__)

# Prometheus metrics
REQUEST_COUNT = Counter('app_request_total', 'Total requests', ['method', 'endpoint', 'status'])
REQUEST_LATENCY = Histogram('app_request_latency_seconds', 'Request latency')

def get_db():
    return psycopg2.connect(os.environ["DATABASE_URL"])

def get_redis():
    return redis.Redis.from_url(os.environ["REDIS_URL"])

@app.route("/")
@REQUEST_LATENCY.time()
def index():
    start = time.time()
    try:
        # Redis: increment fast counter
        r = get_redis()
        hits = r.incr("hits")

        # PostgreSQL: log the visit durably
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO visits (ts) VALUES (NOW())")
        conn.commit()
        cur.close()
        conn.close()

        REQUEST_COUNT.labels('GET', '/', '200').inc()
        log.info(f"Request served: hits={hits} latency={time.time()-start:.3f}s")
        return jsonify({"hits": int(hits), "status": "ok", "latency_ms": round((time.time()-start)*1000, 2)})
    except Exception as e:
        REQUEST_COUNT.labels('GET', '/', '500').inc()
        log.error(f"Request failed: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/health/live")
def liveness():
    return jsonify({"status": "alive"}), 200

@app.route("/health/ready")
def readiness():
    try:
        # Fail readiness if dependencies are unreachable
        get_redis().ping()
        conn = get_db()
        conn.close()
        return jsonify({"status": "ready"}), 200
    except Exception as e:
        log.warning(f"Readiness check failed: {e}")
        return jsonify({"status": "not ready", "error": str(e)}), 503

@app.route("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}

@app.route("/init-db")
def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS visits (id SERIAL PRIMARY KEY, ts TIMESTAMP)")
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "db initialized"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
