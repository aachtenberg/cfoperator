FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY agent/ ./agent/
COPY web_server.py ./
COPY observability/ ./observability/
COPY tools/ ./tools/
COPY skills/ ./skills/
COPY ui/ ./ui/
COPY config.yaml.example ./config.yaml.example

# Expose port for HTTP/WebSocket
EXPOSE 8083

# Run the agent
CMD ["python", "-m", "agent"]
