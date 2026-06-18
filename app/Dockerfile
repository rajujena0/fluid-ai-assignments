FROM python:3.11-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

FROM python:3.11-slim
WORKDIR /app

RUN useradd -m -u 1001 appuser
COPY --from=builder /root/.local /home/appuser/.local
COPY app.py .
USER appuser
ENV PATH=/home/appuser/.local/bin:$PATH
EXPOSE 5000
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/health/live')"
CMD ["python", "app.py"]
