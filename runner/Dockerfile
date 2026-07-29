# TWAIN runner service — the pipeline engine + web-UI job loop.
# BUILD CONTEXT IS THE REPO ROOT (the runner imports modules/, twain_paths.py,
# schemas/, configs/), so build with:
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
# The calculators (GPAW/DFTB+) live in the isolated 'sim' env, which a plain
# `pixi install` does NOT materialize. The state machine resolves the sim
# interpreter by path (.pixi/envs/sim/bin/python), so without this layer EXECUTE
# silently degrades to skipped_missing_dependency and the runner is planning-only.
RUN pixi install -e sim

# Application code (see .dockerignore for what is excluded).
COPY . .

# DFTB+ ships only the binary; a real run needs Slater-Koster (.skf) parameter
# files, found via DFTB_PREFIX (wired to /app/slako by pixi's [activation.env]).
# Fetch them into the image so a DFTB+ calculation runs without a runtime network
# dependency; without this a DFTB+ job aborts with "SK file ... not found". curl
# isn't guaranteed in the base image, so install it for this layer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && bash runner/fetch_slako.sh

CMD ["pixi", "run", "python", "-m", "runner.runner"]
