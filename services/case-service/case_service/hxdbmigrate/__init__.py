"""HxDBMigrate — migrate an external source database into Velaris.

Phase 1 (Connect + Discover): a READ-ONLY connector to a customer's source DB
(postgresql | mysql | mariadb) plus a discovery report (Schema Autobiography +
data-quality scoring). Reuses the DB-SDK introspection layer against the source session.
Never writes to the source. See `docs/Future/HxDBMigrate.md`.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
