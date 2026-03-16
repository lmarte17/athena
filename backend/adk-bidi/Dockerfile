# Stage 1: Build the gog (gogcli) binary from source.
# Cloud Build runs on linux/amd64, which matches Cloud Run — no cross-compilation needed.
FROM golang:1.25-alpine AS gog-builder
RUN apk add --no-cache git
RUN go install github.com/steipete/gogcli/cmd/gog@v0.12.0

# Stage 2: Python app image
FROM python:3.12-slim

WORKDIR /app

# Install gog binary from builder stage
COPY --from=gog-builder /go/bin/gog /usr/local/bin/gog

# Copy dependency manifest first so pip install layer is cached
# unless pyproject.toml changes.
COPY pyproject.toml .

# Copy the application package
COPY app/ ./app/

# Install slides-agent for advanced Google Slides inspection/editing, then install Athena.
RUN pip install --no-cache-dir slides-agent==0.1.0
RUN pip install --no-cache-dir .

# Startup script — initializes gog auth from Cloud Run secrets before launching uvicorn.
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

# Cloud Run injects $PORT at runtime; 8080 is the standard default.
ENV PORT=8080
EXPOSE 8080

CMD ["/app/start.sh"]
