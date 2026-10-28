# syntax=docker/dockerfile:1

# Base images are pinned by digest; Dependabot proposes updates.
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv
FROM python:3.13-slim@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962 AS python

# ------------------------------------------------------------------ build: resolve the locked environment
FROM python AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src

# Dependencies first: this layer is rebuilt only when the lockfile changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups --extra ui --no-install-project

COPY README.md ./
COPY credit_review ./credit_review
COPY assistant ./assistant
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups --extra ui --no-editable

# ------------------------------------------------------------------ runtime: no build tools, no root
FROM python AS runtime
# pip is not needed at run time (uv built the environment), and it vendors its own copies of urllib3, msgpack and
# setuptools' pkg_resources, which the image scan flags. Removing it removes them.
RUN python -m pip uninstall --yes --no-input pip \
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app
WORKDIR /app

COPY --from=build --chown=app:app /app/.venv /app/.venv
# The policy is configuration, read at run time; documents and model caches are mounted, never baked in.
COPY --chown=app:app policy ./policy
COPY --chown=app:app config ./config
# The analyst page and the synthetic sample dossier it offers (APP_MODULE=analyst).
COPY --chown=app:app app.py ./
COPY --chown=app:app .streamlit ./.streamlit
COPY --chown=app:app case_pack/dossier ./case_pack/dossier

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CREDIT_REVIEW_CACHE_DIR=/tmp/credit_review_cache \
    CREDIT_REVIEW_DATA_DIR=/tmp/credit_review_data \
    CREDIT_REVIEW_PRICES_FILE=/app/config/model_prices.yaml \
    CREDIT_REVIEW_POLICY=/app/policy/example_bank_v3.2.yaml

USER app
EXPOSE 8000

# The default is the HTTP service (credit_review/api.py). The CLI is in the same image:
#   docker run --rm -v "$PWD/case_pack:/data:ro" -e CREDIT_REVIEW_LLM_OFFLINE=1 \
#     credit-review credit-review /data/dossier --decision-date 2026-10-12 --out /tmp/out
# APP_MODULE picks the service: credit_review.api:app (default), assistant.api:app, or analyst (the Streamlit page,
# app.py); same image, same digest, same port.
CMD ["sh", "-c", "if [ \"$APP_MODULE\" = analyst ]; then exec streamlit run app.py --server.port 8000 --server.address 0.0.0.0; else exec uvicorn \"${APP_MODULE:-credit_review.api:app}\" --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*' --no-server-header; fi"]
