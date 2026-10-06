FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The commit this image was built from, so a running container can say which code
# it is. Baked in at build time rather than passed through the ECS task definition:
# the task def is rewritten by the deploy workflow on every release, and an env var
# threaded through that JSON is one more thing to keep in sync. An image that knows
# its own SHA is correct wherever it runs -- including a local `docker run`.
ARG GIT_SHA=unknown
ENV TWAIN_GIT_SHA=$GIT_SHA
# Release version, computed once per deploy by scripts/bump-version.sh (#173).
ARG VERSION=dev
ENV TWAIN_VERSION=$VERSION

# Copy application code
COPY . .

# Expose port
EXPOSE 8000

# Run with uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
