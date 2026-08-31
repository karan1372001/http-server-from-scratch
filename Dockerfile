# Minimal image: this project has zero third-party runtime dependencies
# (no framework, remember?) so there's nothing to install beyond Python
# itself. pytest is a dev-only dependency, deliberately not installed here.
FROM python:3.12-slim

WORKDIR /app

COPY . .

EXPOSE 8080

# Defaults to single-threaded mode on 0.0.0.0:8080; override with e.g.
#   docker run -p 8080:8080 from-scratch-http-server --mode threaded
ENTRYPOINT ["python", "run.py", "--host", "0.0.0.0", "--port", "8080"]
