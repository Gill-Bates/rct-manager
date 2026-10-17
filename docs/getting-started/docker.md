# Docker

The repository ships `docker/Dockerfile`, `docker/compose.yaml` and `docker/build.sh`. Tagged
releases are published multi-arch (`linux/amd64`, `linux/arm64`) to Docker Hub as
[`giiibates/rct-manager`](https://hub.docker.com/r/giiibates/rct-manager), with Trivy scanning, an
SBOM and provenance attestation (`.github/workflows/release.yml`). `docker/compose.yaml` defaults
to that image; set `RCT_API_TAG` to the release to run.

## Run with Compose

```sh
cp settings.env.example settings.env      # then edit it
openssl rand -base64 32   # put the result in HMAC_SECRET
RCT_API_TAG=latest docker compose -f docker/compose.yaml up -d
RCT_API_TAG=latest docker compose -f docker/compose.yaml logs rct-api
RCT_API_TAG=latest docker compose -f docker/compose.yaml run --rm rct-api validate
```

`RCT_API_TAG` selects the version (for example `latest` or `1.2.1`).

!!! note "Self-hosted mirror or local build"
    Override the image repository with `RCT_API_IMAGE` (keep `RCT_API_TAG`):

    ```sh
    RCT_API_IMAGE=docker.cirrio.de/rct-api RCT_API_TAG=latest \
      docker compose -f docker/compose.yaml up -d
    ```

Compose reads `../settings.env` relative to `docker/compose.yaml` (see
[Configuration](configuration.md)). GUI settings, passwords and PAT records persist in the
`rct-data` volume at `/app/data/rct.db`.

!!! warning "Bind address inside the container"
    Compose sets `BIND_ADDRESS=0.0.0.0` and `ALLOW_NON_LOOPBACK_BIND=true` while publishing the
    host port on `127.0.0.1`. Enable the GUI option "behind reverse proxy" when serving through a
    TLS proxy. Keep `BIND_PORT` equal to the container side of the port mapping (default `8000`).
    A loopback bind makes the service unreachable through the published port; the start logs a
    warning and the health check fails.

!!! warning "Changing the port in the GUI"
    The GUI port setting moves only the listener inside the container, not the published port
    mapping or the health check's `BIND_PORT`. The container then reports unhealthy and the
    published port stops reaching the service until `BIND_PORT` and the mapping match the new
    port. The GUI warns before applying the change.

The shell variables `RCT_API_PORT` (host port, default `8000`), `RCT_API_TAG` (image tag, required: Compose stops
without it) and `TZ` (default `Etc/UTC`) are read by `docker/compose.yaml` itself.

## Tokens

Set `HMAC_SECRET` before starting the container. The first start prints the one-time `admin`
password in the `FIRST START` block of the container logs (see
[Authentication](../configuration/authentication.md#administration-login)):

```sh
docker compose -f docker/compose.yaml logs rct-api
```

Log in as `admin`, change the password and create a PAT on **API tokens**. The container runs as
uid 10001 and the volume is writable by that user. Keep volume and secret across upgrades. Devices,
tokens, authentication and write support are GUI settings; the former environment variables are
ignored (warning at startup, devices on INFO). See [Configuration](configuration.md).

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

The container starts as root; `/entrypoint.sh` prepares the data volume, drops to the user
`rctapi` (uid/gid 10001) with `gosu` and `exec`s the server (PID 1, receives SIGTERM directly):

1. `DATA_DIR` (default `/app/data`) is validated: it must be an absolute path and
   not a system directory such as `/`, `/etc` or `/app`. Change it only together with a
   matching volume mount and `ADMIN_DB_PATH`; the application does not read `DATA_DIR` itself.
2. A missing directory is created. If its owner is not uid 10001, the ownership is repaired
   recursively; a fresh named volume already inherits the right owner from the image.
3. The directory gets mode 0700 and must be writable by `rctapi`, otherwise the start fails.
4. `serve` (the default), `validate` and options such as `--env-file` are passed to the
   application; any other command runs as `rctapi`. Arguments never skip the privilege drop.

The ownership repair is why `compose.yaml` adds `CHOWN`, `SETUID` and `SETGID` back after
`cap_drop: ALL`; the server runs without capabilities. Without `DAC_OVERRIDE` and `FOWNER`,
content owned by another non-root uid (for example a foreign bind mount) is not repaired and logs
a warning: run `chown -R 10001:10001 <dir>` on the host.

## Health check

The image probes `GET /health` on the container's own address and `BIND_PORT`
with a Python one-liner; it contains neither curl nor wget.

## Build

Releases ship through `.github/workflows/release.yml`, not this script. `docker/build.sh` is the
manual local-build path: it builds `linux/amd64` only, feeds version, git SHA and build date into
the image labels (the SHA is the build hash `(abc1234)` in the admin footer and startup banner;
`dev` outside an image) and pushes the `:dev` tag to `giiibates/rct-manager` on Docker Hub by
default (needs `docker login`; set `IMAGE=` to push elsewhere).
