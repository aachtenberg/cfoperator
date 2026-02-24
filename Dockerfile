FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    iputils-ping \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install kubectl for K8s tools
RUN curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
    && chmod +x kubectl \
    && mv kubectl /usr/local/bin/

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

# agent.py uses bare imports (knowledge_base, llm_fallback, etc.)
ENV PYTHONPATH="/app/agent:/app:${PYTHONPATH}"

# Expose port for HTTP/WebSocket
EXPOSE 8083

# Run the agent
CMD ["python", "-m", "agent"]
