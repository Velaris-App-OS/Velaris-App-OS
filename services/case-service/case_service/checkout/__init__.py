"""HxCheckout — commerce integration layer (marketplace app `velaris/hxcheckout`).

Bridges any external website to Velaris: an inbound basket becomes an Order
`case` plus a row in checkout_orders, with payment handled by the existing
HxConnect Stripe connector. The Python ships in-image like every official
marketplace module; the marketplace install only flips the per-tenant gate +
Studio routes (it does not provision schema — the checkout_* tables ship on the
normal migration track).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
