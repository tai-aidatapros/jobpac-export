FROM python:3.12-slim

# ---------------------------------------------------------------------------
# Install Java runtime and download jt400 JDBC driver
# ---------------------------------------------------------------------------
RUN apt-get update && \
    apt-get install -y --no-install-recommends default-jre-headless curl traceroute iputils-ping iproute2 && \
    rm -rf /var/lib/apt/lists/*

# Create a stable, arch-independent JAVA_HOME symlink so JPype1 can find libjvm.so
RUN ln -s "$(dirname $(dirname $(readlink -f $(which java))))" /opt/java

RUN curl -fsSL https://repo1.maven.org/maven2/net/sf/jt400/jt400/21.0.6/jt400-21.0.6.jar \
    -o /opt/jt400.jar

# ---------------------------------------------------------------------------
# Install Python dependencies
# ---------------------------------------------------------------------------
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Copy application code
# ---------------------------------------------------------------------------
COPY src/ ./src/
COPY config/ ./config/

# ---------------------------------------------------------------------------
# Runtime configuration
# ---------------------------------------------------------------------------
# Health check — Fargate uses ECS health checks, but this is useful for local
# docker testing. The container is a one-shot batch job, so we just verify
# the Python process is alive.
HEALTHCHECK --interval=30s --timeout=5s --retries=2 \
    CMD python -c "import sys; sys.exit(0)"

ENV JAVA_HOME=/opt/java
ENV JT400_JAR=/opt/jt400.jar
# Flush Python stdout/stderr immediately so logs appear in CloudWatch without buffering
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "src.main"]
