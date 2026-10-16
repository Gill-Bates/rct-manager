# Docker

The repository ships `docker/Dockerfile`, `docker/compose.yaml` and
`docker/build.sh`. Tagged releases are published multi-arch (`linux/amd64` and
`linux/arm64`) to Docker Hub as
[`giiibates/rct-manager`](https://hub.docker.com/r/giiibates/rct-manager), with
Trivy scanning, an SBOM and provenance attestation (`.github/workflows/release.yml`).
`docker/compose.yaml` defaults to the published Docker Hub image
`giiibates/rct-manager`; set `RCT_API_TAG` to the release you want to run.
`docker/build.sh` is a separate, manual local-build path: it always builds
`linux/amd64` only and pushes the `:dev` tag to `giiibates/rct-manager` on
Docker Hub by default (set `IMAGE=` to push elsewhere).

## Run with Compose

```sh
cp settings.env.example settings.env      # then edit it
openssl rand -base64 32   # put the result in HMAC_SECRET
RCT_API_TAG=latest docker compose -f docker/compose.yaml up -d
RCT_API_TAG=latest docker compose -f docker/compose.yaml logs rct-api
RCT_API_TAG=latest docker compose -f docker/compose.yaml run --rm rct-api validate
```

This pulls the published release image from Docker Hub; `RCT_API_TAG` selects
the version (for example `latest` or `1.2.1`).

!!! note "Self-hosted mirror or local build"
    To run from a private registry mirror or a manual local build instead of
    Docker Hub, override the image repository with `RCT_API_IMAGE`, keeping the
    required `RCT_API_TAG`:

    ```sh
    RCT_API_IMAGE=docker.cirrio.de/rct-api RCT_API_TAG=latest \
      docker compose -f docker/compose.yaml up -d
    ```

Compose reads `../settings.env` relative to `docker/compose.yaml`. See
[Configuration](configuration.md) for all environment variables. The image
contains the packaged protocol catalog. GUI settings, passwords and PAT records
are persisted in the `rct-data` volume at `/app/data/rct.db`.

!!! warning "Bind address inside the container"
    Compose sets `BIND_ADDRESS=0.0.0.0` and `ALLOW_NON_LOOPBACK_BIND=true` for
    the container while publishing the host port on `127.0.0.1`. Enable the
    GUI option "behind reverse proxy" when serving through a TLS proxy. Keep
    `BIND_PORT` equal to the container side of the port mapping (default
    `8000`). A loopback bind makes the service unreachable through the
    published port; the start logs a warning and the health check fails.

!!! warning "Changing the port in the GUI"
    The GUI port setting moves only the listener inside the container. It changes
    neither the published port mapping nor the `BIND_PORT` that the health check
    uses, so the container reports unhealthy and the published port stops reaching
    the service until `BIND_PORT` and the mapping match the new port. The GUI
    shows this before it applies the change.

The shell variables `RCT_API_PORT` (host port, default `8000`), `RCT_API_TAG` (image tag, required: Compose stops
without it) and `TZ` (default `Etc/UTC`) are read by `docker/compose.yaml` itself.

## Tokens

Set `HMAC_SECRET` before starting the container. The first start prints the one-time `admin`
password in clear text in the `FIRST START` block in the container logs, so it can be copy-pasted
directly from there; it appears only on the initial bootstrap. Container logs are persisted and
centrally collected, so the password may remain in log history — a deliberately accepted tradeoff.
Read it from the container logs if needed:

```sh
docker compose -f docker/compose.yaml logs rct-api
```

Open the root page, log in as `admin` with that password, change it, and create a PAT on
**API tokens**. The container runs as uid 10001 and its database
volume is writable by that user. Keep the volume and secret across upgrades.
Devices, tokens, authentication and write support are configured in the GUI; the
former environment variables are ignored. Tokens, authentication and write
support are reported at startup with a warning, devices on INFO level.

See [Configuration](configuration.md) for details.

## Hardening in `compose.yaml`

| Setting | Value |
| --- | --- |
| `ports` | `127.0.0.1:${RCT_API_PORT:-8000}:8000` (TLS terminates at a reverse proxy) |
| `read_only` | `true`, with `/tmp` as tmpfs (`noexec,nosuid,nodev`) |
| `cap_drop`, `cap_add` | `ALL`; `CHOWN`, `SETUID`, `SETGID` (entrypoint only) |
| `security_opt` | `no-new-privileges:true` |
| `pids_limit`, `mem_limit`, `cpus` | `128`, `256m`, `1.0` |
| `stop_grace_period` | `1m` |
| `logging` | json-file, 50 MB, 5 files |

## Startup and volume ownership

The container starts as root and `/entrypoint.sh` prepares the data volume before it
drops to the unprivileged user `rctapi` (uid/gid 10001) with `gosu` and `exec`s
the server, which therefore is PID 1 and receives SIGTERM directly:

1. `DATA_DIR` (default `/app/data`) is validated: it must be an absolute path and
   not a system directory such as `/`, `/etc` or `/app`. Change it only together with a
   matching volume mount and `ADMIN_DB_PATH`; the application does not read `DATA_DIR` itself.
2. A missing directory is created. If its owner is not uid 10001, the ownership is repaired
   recursively; a fresh named volume already inherits the right owner from the image.
3. The directory gets mode 0700 and must be writable by `rctapi`, otherwise the start fails.
4. `serve` (the default), `validate` and options such as `--env-file` are passed to the
   application; any other command runs as `rctapi`. Arguments never skip the privilege drop.

The ownership repair is why `compose.yaml` adds `CHOWN`, `SETUID` and `SETGID` back after
`cap_drop: ALL`; the server process itself runs without capabilities. Neither
`DAC_OVERRIDE` nor `FOWNER` is granted, so content owned by a different non-root uid
(for example a bind mount from another system) is not repaired and logs a warning:
run `chown -R 10001:10001 <dir>` on the host.

## Health check

The image probes `GET /health` on the container's own address and `BIND_PORT`
with a Python one-liner; it contains neither curl nor wget.

## Build

Tagged releases ship through `.github/workflows/release.yml` to Docker Hub, not
through this script. `docker/build.sh` has one job: build for `linux/amd64`,
feed version, git SHA and build date into the image labels (the git SHA is also the build hash `(abc1234)` in the admin footer and the startup banner; `dev` outside an image), and push the
`:dev` tag to `giiibates/rct-manager` on Docker Hub by default (needs
`docker login`).
