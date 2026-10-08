# Troubleshooting

## The container is unhealthy or unreachable

The service listens on loopback. Inside a container set `BIND_ADDRESS=0.0.0.0`
and the GUI option "behind reverse proxy", and keep `BIND_PORT` equal to the container
port of the mapping. The start logs a warning for a loopback bind in a
container.

## The start is refused

Run `python -m app validate` (or `docker compose -f docker/compose.yaml run --rm rct-api validate`).
Typical causes:

- a device list saved in the GUI with more than one device without network id on
  the same endpoint;
- an invalid object registry or write allowlist.
- missing `HMAC_SECRET` in a read-only container, or a secret that differs
  from the one used to encrypt the existing database.

## First admin login

The generated `admin` password is never logged. The first start prints it in clear text in the
boxed `FIRST START - admin login` block on stdout and also saves it to `initial-admin-password`
next to the admin database (`data/initial-admin-password` by default, mode `0600`) as a fallback
for runs without a visible console. Log in, change the password, then delete the file — an absent
file is the intended end state. So a missing file means either the password has already been
changed, or this is not a first start: the administrator exists already and no new password was
generated. A restart does not replace the password. The first login requires a password change.
Keep the database and `HMAC_SECRET` together when restoring a backup.

## `/docs` answers 404

Both `/docs` and `/openapi.json` need `DOCS_PUBLIC=true`.

## Write endpoints answer 404

While write access is off (GUI **Inverters** page), the write, dispatch and Energy Manager routes
answer 404 `write_disabled`. Switching it on takes effect at once, without a restart.

## 429 from many clients behind a proxy

Set `TRUSTED_PROXIES` and `FORWARDED_HEADER`; otherwise all callers share the
proxy address. See [Network and TLS](configuration/network.md).

## 503 `device_maintenance`

The inverter signalled its bootloader. Requests resume after the cool-down once
a single read succeeds.

## Mixed or stale values

Check `foreign_access_suspected` in `GET /api/v1/readiness`: another client
(vendor app, home automation) may be using port 8899, or a second instance runs
against the same inverter. See [Operation](operation.md).
