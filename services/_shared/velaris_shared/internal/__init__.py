"""Internal service API — case-service routes other Velaris services call directly.

R5e step 7 (the commerce split): commerce-service asks case-service to do what only
case-service may do. `auth.require_internal_caller` guards the routes,
`client.call` makes the calls. See docs/components/backend-services.md.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
