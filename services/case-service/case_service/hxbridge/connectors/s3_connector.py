"""S3Connector — AWS S3 document upload with presigned URL generation."""
from __future__ import annotations

import hashlib
import hmac
import urllib.parse
from datetime import datetime, timezone
from typing import Any

import httpx

from case_service.hxbridge.protocol import ConnectorProtocol, register_connector

_ALGO = "AWS4-HMAC-SHA256"


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date: str, region: str, service: str) -> bytes:
    k = _sign(("AWS4" + secret).encode("utf-8"), date)
    k = _sign(k, region)
    k = _sign(k, service)
    return _sign(k, "aws4_request")


@register_connector("s3")
class S3Connector(ConnectorProtocol):
    name = "AWS S3"
    connector_type = "s3"
    schema = {
        "credentials": {"access_key_id": "str", "secret_access_key": "str"},
        "config":      {"bucket": "str", "region": "str (default us-east-1)", "key_prefix": "str (optional)"},
    }

    def __init__(self, config: dict, credentials: dict):
        self._access_key  = credentials["access_key_id"]
        self._secret_key  = credentials["secret_access_key"]
        self._bucket      = config["bucket"]
        self._region      = config.get("region", "us-east-1")
        self._key_prefix  = config.get("key_prefix", "helix/").rstrip("/") + "/"
        self._endpoint    = config.get("endpoint_url") or f"https://s3.{self._region}.amazonaws.com"

    def _presigned_put_url(self, object_key: str, content_type: str, expires: int = 3600) -> str:
        now      = datetime.now(timezone.utc)
        date_str = now.strftime("%Y%m%d")
        dt_str   = now.strftime("%Y%m%dT%H%M%SZ")

        host   = f"{self._bucket}.s3.{self._region}.amazonaws.com"
        path   = f"/{urllib.parse.quote(object_key)}"
        scope  = f"{date_str}/{self._region}/s3/aws4_request"
        cred   = f"{self._access_key}/{scope}"

        params = {
            "X-Amz-Algorithm":     _ALGO,
            "X-Amz-Credential":    cred,
            "X-Amz-Date":          dt_str,
            "X-Amz-Expires":       str(expires),
            "X-Amz-SignedHeaders": "host",
        }
        qs = "&".join(f"{urllib.parse.quote(k)}={urllib.parse.quote(v)}" for k, v in sorted(params.items()))

        canonical  = f"PUT\n{path}\n{qs}\nhost:{host}\n\nhost\nUNSIGNED-PAYLOAD"
        str_to_sign = f"{_ALGO}\n{dt_str}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
        sig = _sign(_signing_key(self._secret_key, date_str, self._region, "s3"), str_to_sign).hex()

        return f"https://{host}{path}?{qs}&X-Amz-Signature={sig}"

    async def execute(self, input_data: dict) -> dict:
        document_name = input_data["document_name"]
        content_type  = input_data.get("content_type", "application/octet-stream")
        doc_bytes     = input_data.get("document_bytes")

        object_key    = self._key_prefix + document_name
        storage_url   = f"https://{self._bucket}.s3.{self._region}.amazonaws.com/{urllib.parse.quote(object_key)}"

        if doc_bytes:
            presigned = self._presigned_put_url(object_key, content_type)
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.put(
                    presigned,
                    content=doc_bytes,
                    headers={"Content-Type": content_type},
                )
            if resp.status_code not in (200, 204):
                raise RuntimeError(f"S3 upload failed {resp.status_code}: {resp.text[:200]}")
            status = "uploaded"
        else:
            presigned = self._presigned_put_url(object_key, content_type)
            status    = "pending"

        return {
            "object_key":    object_key,
            "bucket":        self._bucket,
            "storage_url":   storage_url,
            "presigned_url": presigned,
            "status":        status,
        }

    async def test(self) -> bool:
        url = f"https://s3.{self._region}.amazonaws.com/{self._bucket}?list-type=2&max-keys=1"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
            return resp.status_code in (200, 403)
        except Exception:
            return False
