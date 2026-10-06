# Docker

The repository ships `docker/Dockerfile`, `docker/compose.yaml` and
`docker/build.sh`. Tagged releases are published multi-arch (`linux/amd64` and
`linux/arm64`) to Docker Hub as
[`giiibates/rct-manager`](https://hub.docker.com/r/giiibates/rct-manager), with
Trivy scanning, an SBOM and provenance attestation (`.github/workflows/release.yml`).
`docker/compose.yaml` and `docker/build.sh` default to a separate, manual path:
building and pushing `linux/amd64` only to the private registry
`docker.cirrio.de/rct-api`, for a local build or a self-hosted mirror. Point
`IMAGE=`/the `image:` key at `giiibates/rct-manager` instead to run the published
release image.

## Run with Compose

```sh
cp settings.env.example settings.env      # then edit it
openssl rand -base64 32   # put the result in HMAC_SECRET
docker compose -f docker/compose.yaml up -d
docker compose -f docker/compose.yaml logs rct-api
docker compose -f docker/compose.yaml run --rm rct-api validate
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
    `8080`). A loopback bind makes the service unreachable through the
    published port; the start logs a warning and the health check fails.

The shell variables `RCT_API_PORT` (host port, default `8080`), `RCT_API_TAG` (image tag, default
`latest`) and `TZ` (default `Etc/UTC`) are read by `docker/compose.yaml` itself.

## Tokens

Set `HMAC_SECRET` before starting the container. The first start prints the one-time `admin`
password in clear text in the `FIRST START - admin login` block in the container logs, so it can
be copy-pasted directly from there. It is also saved to `initial-admin-password` next to the
admin database as a fallback — with the volume layout above, at
`/app/data/initial-admin-password` in the `rct-data` volume. Read it from the running container
if needed:

```sh
docker compose -f docker/compose.yaml exec rct-api cat /app/data/initial-admin-password
```

Open the root page, log in as `admin` with that password, change it, delete the file
(`docker compose -f docker/compose.yaml exec rct-api rm /app/data/initial-admin-password`), and
create a PAT on **API tokens**. The container runs as uid 10001 and its database
volume is writable by that user. Keep the volume and secret across upgrades.
Devices, tokens, authentication and write support are configured in the GUI; the
former environment variables are ignored. Tokens, authentication and write
support are reported at startup with a warning, devices on INFO level.

See [Configuration](configuration.md) for details.

## Hardening in `compose.yaml`

| Setting | Value |
| --- | --- |
| `ports` | `127.0.0.1:${RCT_API_PORT:-8080}:8080` (TLS terminates at a reverse proxy) |
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
through this script. `docker/build.sh` is the manual path for a local build or a
push to a private/self-hosted registry: it builds for `linux/amd64`, feeds
version, git SHA and build date into the image labels, and pushes `:latest` and
`:<version>` to `docker.cirrio.de/rct-api` by default (needs `docker login`).
`PUSH=0 docker/build.sh` builds a local `:dev` image only.
