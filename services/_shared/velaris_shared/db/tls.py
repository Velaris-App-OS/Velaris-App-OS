"""TLS for a database on another server, written the way its operator already knows.

The connection URL carries the TLS settings in libpq's own words, so the SAME
URL works for the platform, for ``psql``/``pg_dump`` (start, update, backup)
and for anyone reading the .env::

    postgresql+asyncpg://velaris:…@db.example:5432/velaris?sslmode=verify-full&sslrootcert=/etc/velaris/db-ca.pem
    mysql+aiomysql://velaris:…@db.example:3306/velaris?ssl_ca=/etc/velaris/db-ca.pem

Neither async driver takes those keys as they stand: asyncpg has no
``sslrootcert`` argument (``asyncpg.connect`` raises on it) and aiomysql wants
an ``SSLContext``, not the dict SQLAlchemy builds from ``ssl_ca``. So
:func:`split_tls` takes them OUT of the URL and returns the equivalent
``connect_args``.

A URL with none of these keys comes back unchanged, string for string, with no
``connect_args`` — the bundled database's URL is never touched.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import ssl as _ssl

_PG_KEYS = ("sslmode", "ssl", "sslrootcert", "sslcert", "sslkey")
_MY_KEYS = ("ssl_ca", "ssl_cert", "ssl_key", "ssl_check_hostname")
_FALSE = {"0", "false", "no", "off"}


def _context(cafile: str | None, check_hostname: bool,
             certfile: str | None, keyfile: str | None) -> _ssl.SSLContext:
    # cafile None = the system's CAs (a database behind a public certificate).
    ctx = _ssl.create_default_context(cafile=cafile or None)
    ctx.check_hostname = check_hostname
    ctx.verify_mode = _ssl.CERT_REQUIRED
    if certfile:
        ctx.load_cert_chain(certfile, keyfile or None)
    return ctx


def split_tls(url: str) -> tuple[str, dict]:
    """``(url without its TLS keys, connect_args)`` for ``create_async_engine``."""
    if "?" not in url:
        return url, {}
    from sqlalchemy.engine import make_url

    u = make_url(url)
    # A key given twice comes back as a tuple; the last one wins, as in libpq.
    q = {k: (v[-1] if isinstance(v, tuple) else v) for k, v in u.query.items()}
    backend = u.get_backend_name()

    if backend == "postgresql":
        if not any(k in q for k in _PG_KEYS):
            return url, {}
        mode = str(q.get("sslmode") or q.get("ssl") or "").lower()
        root, cert, key = q.get("sslrootcert"), q.get("sslcert"), q.get("sslkey")
        if root == "system":            # libpq's word for "the system's CAs"
            root = None
        if mode == "disable":
            ssl_arg: object = "disable"
        elif mode in {"verify-full", "verify-ca"} or root or cert:
            # libpq: `require` WITH a root certificate behaves as verify-ca.
            ssl_arg = _context(root, mode == "verify-full", cert, key)
        else:
            # disable / allow / prefer / require: asyncpg takes these by name.
            ssl_arg = mode or "prefer"
        rest = u.difference_update_query(_PG_KEYS)
        return rest.render_as_string(hide_password=False), {"ssl": ssl_arg}

    if backend in {"mysql", "mariadb"}:
        if "ssl_ca" not in q:
            return url, {}
        check = str(q.get("ssl_check_hostname", "true")).lower() not in _FALSE
        ctx = _context(q["ssl_ca"], check, q.get("ssl_cert"), q.get("ssl_key"))
        rest = u.difference_update_query(_MY_KEYS)
        return rest.render_as_string(hide_password=False), {"ssl": ctx}

    return url, {}
