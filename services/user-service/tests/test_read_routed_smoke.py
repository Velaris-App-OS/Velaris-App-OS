"""R5d 11b — the provisioning log runs on a read-only session; called once so a
hidden write fails here, not in production.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""


async def test_the_provisioning_log_answers_without_writing(client):
    r = await client.get("/api/v1/identity/provisioning/log")
    assert r.status_code == 200, (r.status_code, r.text[:300])   # the handler ran
    assert "readonly" not in r.text.lower()
