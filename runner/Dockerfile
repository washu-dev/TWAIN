# TWAIN runner service — the pipeline engine + web-UI job loop.
# BUILD CONTEXT IS THE REPO ROOT (the runner imports modules/, twain_paths.py,
# schemas/, configs/, "Intelligence Layer/"), so build with:
#     docker build -f runner/Dockerfile -t twain-runner .
#
# Uses the pixi environment (pixi.toml) so it has the same heavy scientific deps
# as the pipeline. DB + Entra/LLM credentials are supplied via the environment
# (ECS task definition + Secrets Manager); see docs/architecture/web_ui_plan.md §10.
FROM ghcr.io/prefix-dev/pixi:latest

WORKDIR /app

# Manifests first for layer caching. NOTE: pixi.lock must be refreshed (run
# `pixi install` locally) after dependency changes; we don't pass --locked so the
# image still builds if the lock is momentarily behind pixi.toml.
COPY pixi.toml pixi.lock ./
RUN pixi install

# Application code (see .dockerignore for what is excluded).
COPY . .

CMD ["pixi", "run", "python", "-m", "runner.runner"]
