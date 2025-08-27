# Dockerfile
FROM python:3.12-slim

WORKDIR /app

# System deps (keep minimal; we use OpenAI-only pipeline)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
  && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# Default command runs the agent in production ("start") mode
CMD ["python", "main.py", "start"]
