# Development Setup

```sh
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

## Checks

```sh
ruff check .
python -m pytest -q
python -m app validate
```

`validate` needs `HMAC_SECRET` (copy `settings.env.example` to `settings.env`
first or pass it in the environment); devices are configured in the GUI.

Tests live in `tests/`; `pytest` runs with `asyncio_mode = "auto"`. Property
tests use `hypothesis`.

## Run locally

```sh
set -a && . ./settings.env && set +a && python -m app
```

## Build the documentation

The docs toolchain is the `docs` extra in `pyproject.toml`:

```sh
python -m pip install -e '.[docs]'
mkdocs serve -f docs/mkdocs.yml
mkdocs build -f docs/mkdocs.yml --strict -d /tmp/rct-site
```

## Continuous integration

| Workflow | Purpose |
| --- | --- |
| `.github/workflows/ci.yml` | ruff, pytest, actionlint, `docker build --check`, manifest checks |
| `.github/workflows/docs-build.yml` | strict docs build, link check, Trivy scan, deploy to GitHub Pages from `main` |

The container image is built and pushed manually with `docker/build.sh`;
there is no release workflow. Dependency updates are proposed by Renovate
(`.github/renovate.json`).
