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
# `pixi install` does NOT materialize. Only a runner that executes calculations
# IN the container needs it (TWAIN_EXECUTE_LOCALLY: the state machine resolves
# .pixi/envs/sim/bin/python, and without it local EXECUTE degrades to
# skipped_missing_dependency). The event-driven worker (P2, #171) runs every
# calculation -- and its smoke test -- on RIS through Slurm, so it skips this:
# the sim env and the DFTB+ data were most of a 4.2 GB image that every Fargate
# task pulls at start. Build with --build-arg WITH_SIM=1 to restore them.
ARG WITH_SIM=0
RUN if [ "$WITH_SIM" = "1" ]; then pixi install -e sim; fi

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
    && if [ "$WITH_SIM" = "1" ]; then bash runner/fetch_slako.sh; fi

# The commit this image was built from (CI passes it): enables the RIS
# provenance line in each S3-staged job's log (TWAIN_EXPECTED_SHA).
ARG GIT_SHA=
ENV TWAIN_GIT_SHA=$GIT_SHA

# Without a TTY, print() is block-buffered: the pipeline's and the cluster
# monitor's progress lines would reach CloudWatch late, or never.
ENV PYTHONUNBUFFERED=1

# The SQS worker (TWAIN_DISPATCH=sqs). The polling runner is `python -m runner.runner`.
CMD ["pixi", "run", "python", "-m", "runner.worker"]
