# rct-manager in Docker

Vendor-neutral REST gateway for RCT Power inverters. Tagged releases are published
multi-arch (`linux/amd64`, `linux/arm64`) to Docker Hub as
[`giiibates/rct-manager`](https://hub.docker.com/r/giiibates/rct-manager) by
`.github/workflows/release.yml`, with Trivy scanning, an SBOM and provenance
attestation. `docker/build.sh` below is a separate, manual local-build path
that always builds `linux/amd64` only and pushes the `:dev` tag to
`giiibates/rct-manager` on Docker Hub by default; use it for a local custom
build, not as the source for a production pull of a tagged version.

Deployment artefacts: [`compose.yaml`](compose.yaml), [`Dockerfile`](Dockerfile)
and [`build.sh`](build.sh). The operator documentation (configuration, endpoints,
security posture) is at <https://gill-bates.github.io/rct-manager/>.

## Run

```sh
cd rct-manager
docker compose -f docker/compose.yaml up -d
docker compose -f docker/compose.yaml logs rct-api
docker compose -f docker/compose.yaml run --rm rct-api validate
```

Create `settings.env` from `settings.env.example` at the project root before the
first start. Compose resolves `../settings.env` relative to `docker/compose.yaml`.
Set a stable random `HMAC_SECRET` of at least 32 characters before starting.
Generate one with `openssl rand -base64 32` and put it in `settings.env`. The read-only container never generates an
ephemeral encryption key. On first startup its logs print a boxed `FIRST START - admin login`
block with the one-time `admin` password in clear text, so it can be copy-pasted straight from the
container logs. It is also saved to `/app/data/initial-admin-password` in the `rct-data` volume
with mode 0600 as a fallback, readable with
`docker compose -f docker/compose.yaml exec rct-api cat /app/data/initial-admin-password`.
Delete the file after the change. Until the password is changed, the container logs a
warning and keeps periodic reads, heartbeat and export paused; they start without a restart
afterwards. Open the root URL, sign in, change the password, and use **API tokens**
to create PATs for clients. PAT secrets are displayed once.

Compose persists the encrypted administration database at `/app/data/rct.db` in
the `rct-data` named volume. The server runs as uid/gid 10001 (`rctapi`); the data
directory has mode 0700. Preserve the volume and `HMAC_SECRET` across upgrades
and back up both together. The database uses SQLite WAL mode, so the volume must stay writable
(`rct.db-wal` and `rct.db-shm` appear next to `rct.db`) and local; back up with the service
stopped or with `sqlite3 /app/data/rct.db ".backup <file>"`, not by copying `rct.db` alone. Catalog and initial write-policy data are packaged
inside `app/catalog`; GUI selections are stored in the database.

Compose sets `BIND_ADDRESS=0.0.0.0` and `ALLOW_NON_LOOPBACK_BIND=true` for the
container listener. Enable the GUI option "behind reverse proxy" when requests
arrive through a TLS proxy, and keep `BIND_PORT` equal to the container port of the
mapping in `compose.yaml` (default `8000`; `127.0.0.1:8000:8000`). If you change `BIND_PORT`,
change the published port as well. A loopback `BIND_ADDRESS` inside the container (including
the default `127.0.0.1`) makes the service unreachable through the port mapping; the start
then logs a WARNING and the health check fails.

`/docs` and `/openapi.json` are disabled by default and return 404. Set `DOCS_PUBLIC=true`
on the GUI **Settings** page to serve them without a token. `DOCS_PUBLIC=false`
disables both endpoints on every bind address, including loopback.

The entry point is `/entrypoint.sh` with the default command `serve`, so
`docker compose ... run --rm rct-api validate` is the configuration check in the
container.

## Startup sequence

The container starts as root; `docker/entrypoint.sh` then, in order:

1. Validates `DATA_DIR` (default `/app/data`): it must be an absolute path and not a
   system directory such as `/`, `/etc` or `/app`, because a recursive `chown` follows.
   Set `DATA_DIR` only together with a matching volume mount (and `ADMIN_DB_PATH`);
   the application itself does not read it.
2. Creates `DATA_DIR` if missing and, when its owner is not uid 10001, repairs the
   ownership recursively (children first, symlinks not followed, mount points not
   crossed). An existing volume that already has the right owner is not walked again.
   A fresh named volume inherits the owner from the image, so this normally does nothing.
3. Sets mode 0700 and checks as `rctapi` that the directory is writable; otherwise the
   start fails with the directory listing in the log.
4. Drops privileges with `exec gosu rctapi python /app/run.py serve`. The server is
   PID 1 and receives SIGTERM directly (`stop_grace_period: 1m`). Which user runs
   is decided by the real UID, never by command-line arguments.

Arguments are passed on: `serve`, `validate` and options such as `--env-file <path>`
go to `run.py`; any other command (for example `bash`) runs as `rctapi` after the same
bootstrap. Started with `--user 10001:10001`, the container skips the repair and only
checks that `DATA_DIR` is writable.

Capabilities: `compose.yaml` keeps `cap_drop: ALL` and adds back only `CHOWN` (ownership
repair), `SETUID` and `SETGID` (the `gosu` drop). The repair is ordered so that root needs
neither `DAC_OVERRIDE` nor `FOWNER`. Content owned by a different non-root uid inside the
volume (for example a bind mount from another system) is therefore not repaired: the log
shows a warning, and you fix it on the host with `chown -R 10001:10001 <dir>`. After the drop,
the server process has an empty capability set and `no-new-privileges` stays active.

## What `compose.yaml` sets

| Setting | Value | Why |
| --- | --- | --- |
| `ports` | `127.0.0.1:8000:8000` | Loopback only; TLS is terminated by a reverse proxy in front of it |
| `BIND_ADDRESS`, `ALLOW_NON_LOOPBACK_BIND` | `0.0.0.0`, `true` | Allow container networking while the host port remains loopback-only |
| `env_file` | `../settings.env` | Operator settings; never baked into the image |
| `volumes` | `rct-data:/app/data` | Persistent encrypted settings, admin credentials and PAT records |
| Protocol catalog | Packaged in `app/catalog` | Prometheus page manages exposure, Inverters page the write selection |
| `read_only` | `true` | Immutable root filesystem |
| `tmpfs` | `/tmp` (`rw,noexec,nosuid,nodev,size=1m,mode=1777`) | Temporary data; `/app/data` is a separate persistent writable volume |
| `cap_drop`, `cap_add` | `ALL`; `CHOWN`, `SETUID`, `SETGID` | Only the entrypoint's ownership repair and `gosu` drop need them; the server runs without capabilities |
| `security_opt` | `no-new-privileges:true` | No privilege escalation |
| `pids_limit`, `mem_limit`, `cpus` | `128`, `256m`, `1.0` | Blast-radius limits |
| `stop_grace_period` | `1m` | Deliberately larger than the internal shutdown grace of 20 s |
| `logging` | json-file, 50 MB, 5 files | Rotated text logs on stdout |
| `restart` | `always` | — |

The user is not set in `compose.yaml`, and the `Dockerfile` has no `USER`: the container
starts as root and the entrypoint drops to uid/gid 10001 (`rctapi`). The file declares no `replicas` either,
so the Compose default of one container per service provides the "exactly one
instance per inverter endpoint" rule that the gateway requires.

The image carries a `HEALTHCHECK` (run as the image default user, root, which is enough for a
network probe): a Python one-liner that queries `/health` on
the container's own address on `$BIND_PORT` (not loopback, so a loopback bind is not reported
as healthy). It ships neither curl nor wget. It cannot see the host-side port mapping, so a
`BIND_PORT` that differs from the mapped container port is not detected; compare it with the
mapping yourself.

## Build and publish

Tagged releases ship through `.github/workflows/release.yml` to Docker Hub
(`giiibates/rct-manager`, `linux/amd64` + `linux/arm64`), not through this
script. `docker/build.sh` has one job: build and push the `:dev` tag of a
`linux/amd64` image to `giiibates/rct-manager` on Docker Hub by default:

```sh
docker/build.sh                      # buildx build + push :dev
IMAGE=my.reg/repo docker/build.sh    # another repository
```

`<version>` (fed into the OCI labels, not into the tag) is `[project].version`
from `pyproject.toml`. The script is equivalent to

```sh
docker buildx build --platform linux/amd64 --pull -f docker/Dockerfile \
  --build-arg APP_VERSION=<version> --build-arg GIT_SHA=<sha> --build-arg BUILD_DATE=<utc> \
  -t giiibates/rct-manager:dev --push .
```

It refuses to run with an unresolved git SHA, since that would make the
`:dev` image's OCI revision label meaningless. Pushing needs
`docker login` (or `IMAGE=` pointed at a registry you can log in to).

The `Dockerfile` defaults to the Python 3.13 slim base image in both stages,
builds the dependencies into a virtual environment in a separate stage,
removes `pip` from the runtime image and carries no credentials;
`settings.env` is excluded by `.dockerignore`.
The packaged catalog covers 894 of the 895 IDs (without `wifi_password`) from
protocol v1.14. The packaged write-policy seed defines wire type limits for 893
scalar objects, rather than device safety limits. Use **Inverters** to manage
the write selection and the same page to enable write support (disabled by
default). Firmware can reject writes. The former project-root JSON files are
no longer configuration files.

> **Disclaimer:** This is an independent open-source project. It is not affiliated
> with, endorsed by or connected to RCT Power GmbH, Line-Eid-Str. 1, D-78467
> Konstanz. "RCT Power" and related names are trademarks of their respective owner
> and are used only to describe compatibility.
