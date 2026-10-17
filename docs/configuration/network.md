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

## Interactive documentation

`/docs` and `/openapi.json` are served only with `DOCS_PUBLIC=true`; otherwise both answer 404
`docs_not_available`, including on loopback.
