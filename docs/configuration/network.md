# Network and TLS

Inverter host entries in the admin GUI and API receive a plausibility check: they must look like
a plain host name or IP address without a scheme or path. This is not a full DNS or IP validity
check; an accepted entry may still fail to resolve or connect at runtime.

The service speaks plain HTTP only. A non-loopback `BIND_ADDRESS` is refused
unless `ALLOW_NON_LOOPBACK_BIND=true` is set in the environment or `settings.env`.
This flag confirms that the operator has restricted access to the listener; it
does not enable TLS or secure cookies. Use a TLS-terminating reverse proxy in
front of any listener reachable beyond the local host. In Docker Compose, the
container listens on `0.0.0.0`, but the host publishes the port on `127.0.0.1`.
Enable the GUI option "behind reverse proxy" when requests arrive through a
TLS-terminating proxy; until then, startup logs a warning.

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

`TRUSTED_PROXIES` is the only proxy trust list: the server does not apply any
other forwarded-header handling. The administration interface takes the browser
scheme from `X-Forwarded-Proto` only when the request comes from one of these
networks; behind a TLS-terminating proxy that is not listed there, saving in the
GUI fails the same-origin check with 403.

## Interactive documentation

`/docs` and `/openapi.json` are served only with `DOCS_PUBLIC=true`; otherwise
both answer 404 `docs_not_available`, including on loopback.
