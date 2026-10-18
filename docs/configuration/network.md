# Network and TLS

Inverter host entries in the admin GUI and API get a plausibility check only: a plain host name or
IP address without scheme or path. An accepted entry may still fail to resolve or connect.

The service speaks plain HTTP only. A non-loopback `BIND_ADDRESS` is refused unless
`ALLOW_NON_LOOPBACK_BIND=true` is set in the environment or `settings.env`; the flag confirms that
access to the listener is restricted and enables neither TLS nor secure cookies. Use a
TLS-terminating reverse proxy in front of any listener reachable beyond the local host (in Docker
Compose the container listens on `0.0.0.0`, the host publishes on `127.0.0.1`). Enable the GUI
option "behind reverse proxy" for such a proxy; until then startup logs a warning.

## Caller address

Behind a proxy or a container bridge, set `TRUSTED_PROXIES` and
`FORWARDED_HEADER`:

```ini
TRUSTED_PROXIES=172.18.0.0/16
FORWARDED_HEADER=X-Forwarded-For
```

!!! warning
    Without them all callers share the proxy address, and the failed logins of
    one client lock out every client.

`TRUSTED_PROXIES` is the only proxy trust list; no other forwarded-header handling exists. The
admin interface takes the browser scheme from `X-Forwarded-Proto` only from these networks;
behind an unlisted TLS proxy, saving in the GUI fails the same-origin check with 403.

## Listener and container port

`BIND_ADDRESS` is the REST API listen address (not the inverter address). Defaults: `127.0.0.1`,
`ALLOW_NON_LOOPBACK_BIND=false`, `BIND_PORT=8000`. The port can also be changed in the GUI; the
service re-binds in the background, and the value saved in the GUI takes precedence over `BIND_PORT`
after the first start.

In a container the GUI port moves only the internal listener. The published port mapping and the
health check's `BIND_PORT` stay unchanged, so the container reports unhealthy and the published port
stops reaching the service until environment, compose value and mapping match the new port. The GUI
warns before applying the change.

## Behind a reverse proxy

The GUI option **Behind reverse proxy** confirms a TLS-terminating proxy in front of the service and
sets the secure flag on session cookies. It does not trust any forwarded header; that is
`TRUSTED_PROXIES` above. A `BEHIND_REVERSE_PROXY` variable is ignored with a warning. A consented
non-loopback bind logs a warning until this option is enabled.

`TRUSTED_PROXIES` takes comma-separated reverse-proxy networks (CIDR, at most 32) and is used to
extract the client IP and to accept `X-Forwarded-Proto` for the admin interface.
`FORWARDED_HEADER` is a single header name set by the proxy (for example `X-Forwarded-For`), required
together with `TRUSTED_PROXIES`; `Forwarded` (RFC 7239) is rejected.

## Interactive documentation

`/docs` and `/openapi.json` are served only with `DOCS_PUBLIC=true`; otherwise both answer 404
`docs_not_available`, including on loopback.
