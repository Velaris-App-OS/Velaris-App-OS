"""HxCheckout inbound webhook mapping + HMAC verification (Phase 5).

Platforms (Shopify, WooCommerce, Magento, BigCommerce) POST an order-created event;
HxCheckout maps it to the canonical order shape ({basket, customer, shipping,
metadata}) and opens an Order case. Built-in maps need no configuration; `custom`
uses a dot-path field map.

Security (key invariant 4): the HMAC signature is verified against the
integration's shared secret BEFORE the payload is deserialized — a malformed or
unsigned request never reaches the mapping/order code.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from case_service.hxbridge.encryption import decrypt_credentials, encrypt_credentials

# Platforms that have a built-in mapper (no field_map configuration needed).
BUILTIN_PLATFORMS = {"shopify", "woocommerce", "magento", "bigcommerce"}


# ── Secret storage (reuse hxbridge credential encryption, hxv1 scheme) ────────

def encrypt_secret(secret: str) -> str:
    return json.dumps(encrypt_credentials({"v": secret}))


def decrypt_secret(stored: str | None) -> str | None:
    if not stored:
        return None
    try:
        d = json.loads(stored)
        if "_enc" in d:
            return decrypt_credentials(d).get("v")
    except Exception:
        pass
    return stored  # legacy plaintext fallback


# ── HMAC verification (runs on raw bytes, before JSON parse) ──────────────────

def verify_hmac(raw_body: bytes, signature: str | None, secret: str | None) -> bool:
    """Constant-time verify an HMAC-SHA256 signature over the raw request body.

    Accepts hex or `sha256=<hex>` form. A missing secret or signature fails closed."""
    if not secret or not signature:
        return False
    sig = signature.strip()
    if sig.startswith("sha256="):
        sig = sig[len("sha256="):]
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_cents(value: Any) -> int:
    """Normalise a platform price (decimal string/number major units) to integer
    minor units. Empty/invalid → 0."""
    if value is None or value == "":
        return 0
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return 0


def _resolve(obj: Any, path: str) -> Any:
    """Resolve a dot-path with optional `[]` list flattening, e.g.
    `line_items[].title` → list of titles. Returns None if any hop is missing."""
    parts = path.split(".")
    cur: Any = obj
    for part in parts:
        flatten = part.endswith("[]")
        key = part[:-2] if flatten else part
        if isinstance(cur, list):
            cur = [(c.get(key) if isinstance(c, dict) else None) for c in cur]
        elif isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return None
        if flatten and not isinstance(cur, list):
            cur = [cur] if cur is not None else []
    return cur


# ── Built-in platform mappers → canonical order shape ─────────────────────────

def _map_shopify(raw: dict) -> dict:
    items = []
    for li in raw.get("line_items", []) or []:
        items.append({
            "sku": li.get("sku") or str(li.get("variant_id") or li.get("id") or ""),
            "name": li.get("title") or li.get("name") or "Item",
            "quantity": int(li.get("quantity") or 1),
            "unit_price": _to_cents(li.get("price")),
            "metadata": {"variant": li.get("variant_title")} if li.get("variant_title") else {},
        })
    c = raw.get("customer") or {}
    addr = raw.get("shipping_address") or {}
    return {
        "basket": items,
        "customer": {
            "name": " ".join(x for x in [c.get("first_name"), c.get("last_name")] if x) or addr.get("name"),
            "email": raw.get("email") or c.get("email"),
            "phone": c.get("phone") or addr.get("phone"),
        },
        "shipping": {
            "address_line1": addr.get("address1"), "address_line2": addr.get("address2"),
            "city": addr.get("city"), "postcode": addr.get("zip"),
            "country": addr.get("country_code") or addr.get("country"),
        },
        "metadata": {"order_note": raw.get("note"), "platform_order_id": raw.get("id")},
    }


def _map_woocommerce(raw: dict) -> dict:
    items = []
    for li in raw.get("line_items", []) or []:
        items.append({
            "sku": li.get("sku") or str(li.get("product_id") or ""),
            "name": li.get("name") or "Item",
            "quantity": int(li.get("quantity") or 1),
            "unit_price": _to_cents(li.get("price")),
        })
    b = raw.get("billing") or {}
    s = raw.get("shipping") or {}
    return {
        "basket": items,
        "customer": {
            "name": " ".join(x for x in [b.get("first_name"), b.get("last_name")] if x),
            "email": b.get("email"), "phone": b.get("phone"),
        },
        "shipping": {
            "address_line1": s.get("address_1"), "address_line2": s.get("address_2"),
            "city": s.get("city"), "postcode": s.get("postcode"), "country": s.get("country"),
        },
        "metadata": {"order_note": raw.get("customer_note"), "platform_order_id": raw.get("id")},
    }


def _map_magento(raw: dict) -> dict:
    items = []
    for li in raw.get("items", []) or []:
        items.append({
            "sku": li.get("sku") or "",
            "name": li.get("name") or "Item",
            "quantity": int(float(li.get("qty_ordered") or li.get("qty") or 1)),
            "unit_price": _to_cents(li.get("price")),
        })
    ba = raw.get("billing_address") or {}
    street = ba.get("street") or []
    if isinstance(street, str):
        street = [street]
    return {
        "basket": items,
        "customer": {
            "name": " ".join(x for x in [ba.get("firstname"), ba.get("lastname")] if x),
            "email": raw.get("customer_email") or ba.get("email"), "phone": ba.get("telephone"),
        },
        "shipping": {
            "address_line1": street[0] if street else None,
            "address_line2": street[1] if len(street) > 1 else None,
            "city": ba.get("city"), "postcode": ba.get("postcode"), "country": ba.get("country_id"),
        },
        "metadata": {"platform_order_id": raw.get("entity_id") or raw.get("increment_id")},
    }


def _map_bigcommerce(raw: dict) -> dict:
    # BigCommerce's store/order/created webhook is THIN (id only) — a full integration
    # fetches the order via its API. This maps an already-expanded payload best-effort;
    # the API-callback enrichment is deferred post-v1.
    items = []
    for li in raw.get("products", []) or raw.get("line_items", []) or []:
        items.append({
            "sku": li.get("sku") or "",
            "name": li.get("name") or "Item",
            "quantity": int(li.get("quantity") or 1),
            "unit_price": _to_cents(li.get("price_inc_tax") or li.get("base_price") or li.get("price")),
        })
    bil = raw.get("billing_address") or {}
    return {
        "basket": items,
        "customer": {
            "name": " ".join(x for x in [bil.get("first_name"), bil.get("last_name")] if x),
            "email": bil.get("email"), "phone": bil.get("phone"),
        },
        "shipping": {
            "address_line1": bil.get("street_1"), "address_line2": bil.get("street_2"),
            "city": bil.get("city"), "postcode": bil.get("zip"), "country": bil.get("country_iso2"),
        },
        "metadata": {"platform_order_id": raw.get("id")},
    }


_BUILTIN = {
    "shopify": _map_shopify,
    "woocommerce": _map_woocommerce,
    "magento": _map_magento,
    "bigcommerce": _map_bigcommerce,
}


def _map_custom(raw: dict, field_map: dict) -> dict:
    """Custom mapper: field_map maps canonical paths → source dot-paths. Basket is
    built from `basket[].sku|name|quantity|unit_price` source paths (parallel lists)."""
    def g(key: str):
        p = field_map.get(key)
        return _resolve(raw, p) if p else None

    skus = g("basket[].sku") or []
    names = g("basket[].name") or []
    qtys = g("basket[].quantity") or []
    prices = g("basket[].unit_price") or []
    n = max(len(skus), len(names), len(qtys), len(prices))
    items = []
    for i in range(n):
        items.append({
            "sku": (skus[i] if i < len(skus) else "") or "",
            "name": (names[i] if i < len(names) else "Item") or "Item",
            "quantity": int((qtys[i] if i < len(qtys) else 1) or 1),
            "unit_price": _to_cents(prices[i] if i < len(prices) else 0),
        })
    return {
        "basket": items,
        "customer": {"name": g("customer.name"), "email": g("customer.email"), "phone": g("customer.phone")},
        "shipping": {
            "address_line1": g("shipping.address_line1"), "city": g("shipping.city"),
            "postcode": g("shipping.postcode"), "country": g("shipping.country"),
        },
        "metadata": {"order_note": g("metadata.order_note")},
    }


def map_payload(platform: str, raw: dict, field_map: dict | None) -> dict:
    """Map a raw inbound payload to the canonical order shape, by platform.

    A non-empty field_map always wins (lets a built-in platform override specific
    fields by supplying a custom map)."""
    if field_map:
        return _map_custom(raw, field_map)
    fn = _BUILTIN.get((platform or "").lower())
    if fn is None:
        raise ValueError(f"No built-in mapper for platform '{platform}' and no field_map provided")
    return fn(raw)
