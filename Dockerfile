# Base: Python + Node
FROM python:3.12-slim AS base
ARG PNPM_VERSION=9.15.9

# Avoid prompts from apt
ENV DEBIAN_FRONTEND=noninteractive

# Install required packages
RUN apt-get update && \
    apt-get install -y curl git build-essential nodejs npm && \
    npm install -g pnpm@${PNPM_VERSION} && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Build frontend
FROM base AS frontend

WORKDIR /app/frontend_app
COPY frontend_app /app/frontend_app

# Install and build
RUN pnpm install --no-frozen-lockfile && pnpm build

# Final stage
FROM python:3.12-slim

# Create app directory
WORKDIR /app

# Copy scripts and dependencies
COPY --from=base /usr/local /usr/local
COPY --from=frontend /app/frontend_app/dist /app/frontend
COPY main.py /app/
COPY backend_api.py /app/
COPY jupiter_quote.py /app/
COPY rsi_utils.py /app/
COPY solana_rate_limiter.py /app/

# Create shared storage volume
RUN mkdir /shared
VOLUME ["/shared"]

# Expose backend port
EXPOSE 8000

# Start the FastAPI server first, wait 5 seconds, then launch the price monitor.
CMD ["sh", "-c", "uvicorn backend_api:app --host 0.0.0.0 --port 8000 & echo 'Waiting for FastAPI to start...'; sleep 5; python3 main.py"]
