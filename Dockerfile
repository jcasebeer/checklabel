# Portable single-container build. Runs identically on a laptop, a PaaS,
# or a plain VPS — the only runtime input is the ANTHROPIC_API_KEY env var.
FROM python:3.12-slim

# Pillow needs no system libs for JPEG/PNG on slim beyond what wheels bundle.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# Honor the platform's $PORT if set (Railway/Render/Fly), default 8000 for VPS.
# --forwarded-allow-ips='*': trust X-Forwarded-Proto from the reverse proxy so
# generated URLs say https. Safe here because the container publishes no ports —
# only the cloudflared sidecar on the internal network can reach it. Revisit if
# you ever expose the port directly.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
