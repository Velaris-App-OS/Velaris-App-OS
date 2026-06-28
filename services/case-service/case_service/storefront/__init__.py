"""HxStorefront — hosted store builder (marketplace app `velaris/hxstorefront`).

A Velaris-native storefront for clients with no existing website: branded product
catalogue, visual page/theme builder, and checkout that flows through HxCheckout to
become an Order case. Ships in-image like every official marketplace module; the
storefront_* tables ship on the normal migration track (096); install flips the
per-tenant gate (/api/v1/storefront + the public /api/v1/storefront/public routes)
and the Studio module.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
