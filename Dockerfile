FROM python:3.13-slim

# System deps (psycopg2 needs libpq)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cached unless requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

EXPOSE 8000

# --loop uvloop: 2-4x throughput vs asyncio selector; dramatically fewer
# event-loop stalls under the orderbook-WS + broadcaster + arb-compute mix.
# Falls back to stock asyncio automatically if uvloop isn't installed.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--loop", "uvloop"]
