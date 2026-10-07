# Authentication

By default (authentication required, set in the GUI) every business request needs an
`Authorization: Bearer <token>` header. Otherwise it is rejected with 401
(`missing_token` or `invalid_token`). No token exists after the first start;
business API access stays denied until the admin creates one in the GUI.

## Administration login

The root page redirects to login. The first server startup generates a one-time password
(8 characters with exactly one special character) for the user `admin` and prints it in clear text
in the boxed `FIRST START - admin login` block on stdout, so it can be copy-pasted straight from
the console. As a fallback for operation without a visible console (a background service, a
container log that is not watched live), it is also saved to a file, `initial-admin-password` next
to the admin database, created with mode `0600` (owner read and write only) from the moment it
exists. The credential is one-time: the first login permits only password change and logout until
a new password has been set. Administrative sessions use HttpOnly cookies, CSRF protection, expiry
and login throttling. Disabling API authentication never disables GUI authentication.

The new admin password needs at least 8 characters; delete the file once it is set. No token is
issued automatically; PATs are created only by the admin in the GUI.

## Personal access tokens

A PAT has the form `pat_` followed by 40 base62 characters and a 6 character CRC32 checksum
(base62), so a mistyped token is rejected without a lookup. The former `API_TOKENS`,
`API_TOKENS_FILE` and the `tokens` CLI no longer exist. The administration API (`/admin/api/...`) accepts a session or a `read/write` PAT;
`read` tokens get 403 on every administration endpoint, reads included. Creating or
deleting tokens, and changing `auth_required`, `trusted_proxies`, `bind_address`,
`behind_reverse_proxy`, `forwarded_header`, `metrics_require_token` or
`metrics_trusted_sources`, need a signed-in session; a PAT gets 403 there.

Create named PATs with a `read` or `read/write` role and an expiry of 30 days, 90 days (the
preselected value), 1 year or never on the **API tokens** page. Copy the secret when it is shown: it cannot be retrieved
later. Revoke a token on the same page. The list shows when each token was created, last used (at most
once per minute; `Never` if unused) and expires. Records are persisted in the encrypted
SQLite administration database; raw secrets are not stored.

| Role | Permits |
| --- | --- |
| `read` | Reading values, metrics, devices, readiness |
| `read/write` | Everything above, plus writes, actions and the vendor diagnostics area |

Tokens are looked up by a keyed digest and appear in logs only as a short
non-reversible id. The business rate limit counts per token id and source
address.

## Opt-out

Disabling authentication in the GUI is an explicit operator opt-out. A request without a
token is then accepted **with the role `read/write`**; a request that sends a
token must still be valid. The server logs a `SECURITY` warning
at every start.

!!! danger
    Everyone who can reach the port may read all values and, with
    write support enabled, write every metric in the allowlist. Use the
    opt-out only on loopback or behind a reverse proxy that authenticates
    callers.
