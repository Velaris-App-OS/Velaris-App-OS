"""Stable error codes — the spine of the error catalog (roadmap doc debt).

`docs/Future/error-catalog.md` has documented codes since 2026-06-27, but
nothing emitted them: failures surfaced as a bare 500 or FastAPI's default
422 body, so the catalog described an identifier that did not exist anywhere
in the running system. A support engineer could not look up what they were
looking at.

The contract the catalog asks for is that **the same code appears in the API
response, the server log, and the catalog entry**. This module is the code
registry and the raiser; `main.py` installs the handler that puts the code in
the response body and the log line.

Response shape when a code is attached::

    {"detail": "...", "error": {"code": "VEL-DOC-STORAGE-503",
                                "docs": ".../errors#VEL-DOC-STORAGE-503"}}

`detail` is kept exactly where FastAPI already puts it, so every existing
client keeps working — the code is additive.

Adding a code: add the constant here **and** the entry in the catalog. The
`KNOWN_CODES` set exists so a typo raises at import rather than shipping a
deep link to a page that documents nothing.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException

logger = logging.getLogger(__name__)

#: Where a code deep-links. Kept relative so a self-hosted docs site works.
DOCS_BASE = "/docs/errors"

# ── Documents & Storage ───────────────────────────────────────────
DOC_STORAGE_503 = "VEL-DOC-STORAGE-503"
DOC_ENCRYPT_503 = "VEL-DOC-ENCRYPT-503"
DOC_NOTFOUND_404 = "VEL-DOC-NOTFOUND-404"
DOC_CASE_404 = "VEL-DOC-CASE-404"
DOC_UUID_422 = "VEL-DOC-UUID-422"
DOC_FILETYPE_400 = "VEL-DOC-FILETYPE-400"
DOC_PREVIEW_415 = "VEL-DOC-PREVIEW-415"
DOC_DELETE_403 = "VEL-DOC-DELETE-403"
DOC_LASTVERSION_409 = "VEL-DOC-LASTVERSION-409"
DOC_TENANT_403 = "VEL-DOC-TENANT-403"
DOC_UNREADABLE_409 = "VEL-DOC-UNREADABLE-409"

# ── Authentication ────────────────────────────────────────────────
# The failures a user actually hits and cannot self-diagnose. Deliberately
# NOT one code per raise site: `auth_real.py` alone raises 50 times, and
# most are variations of "not found" that a code would not clarify.
#
# Note what is missing on purpose — there is no code distinguishing "no such
# user" from "wrong password". Both must stay a single opaque failure, or the
# code becomes a user-enumeration oracle for anyone reading the response.
AUTH_CREDENTIALS_401 = "VEL-AUTH-CREDENTIALS-401"
AUTH_DISABLED_403 = "VEL-AUTH-DISABLED-403"
AUTH_SSO_REQUIRED_400 = "VEL-AUTH-SSO-REQUIRED-400"
AUTH_OTP_INVALID_400 = "VEL-AUTH-OTP-INVALID-400"
AUTH_RATE_LIMIT_429 = "VEL-AUTH-RATE-LIMIT-429"
AUTH_DEVICE_MISMATCH_401 = "VEL-AUTH-DEVICE-MISMATCH-401"
AUTH_SSO_UNCONFIGURED_404 = "VEL-AUTH-SSO-UNCONFIGURED-404"
AUTH_EMAIL_TAKEN_409 = "VEL-AUTH-EMAIL-TAKEN-409"
# The per-request token/account check could not reach the database. The route
# never ran, so a client may retry ANY method on this code (2026-10-19).
AUTH_CHECK_BUSY_503 = "VEL-AUTH-CHECK-BUSY-503"
# The three SSO callback refusals. Unlike the login codes above these are safe
# to tell apart: the caller has already authenticated to the identity provider
# as themselves, so none of them narrows a search over OUR user table. Each has
# a different fix, and an operator cannot guess which from a shared code.
AUTH_SSO_STATE_400 = "VEL-AUTH-SSO-STATE-400"
AUTH_SSO_PROVISION_403 = "VEL-AUTH-SSO-PROVISION-403"
AUTH_SSO_UNVERIFIED_403 = "VEL-AUTH-SSO-UNVERIFIED-403"
# SAML. Deliberately coarse where the OAuth codes above are fine-grained: the
# OAuth caller has already proven who they are to the provider before any of
# those refusals can fire, whereas anyone at all can POST bytes at the ACS. One
# code for every assertion-level refusal means a forger cannot use the response
# to learn which of signature, audience, destination, timing or replay they got
# wrong. The real reason is in `identity_provision_log` and the security event.
AUTH_SAML_ASSERTION_400 = "VEL-AUTH-SAML-ASSERTION-400"
AUTH_SAML_PROVISION_403 = "VEL-AUTH-SAML-PROVISION-403"

# ── Channels (omnichannel inbound) ────────────────────────────────
# The two refusals on the outbound path that an agent must be able to act on.
# Both are 4xx with a specific fix, and a bare 400 would leave the agent
# retrying the same message: WhatsApp's window closes on the clock, not on
# anything they did, and an opt-out is a legal instruction we may not override.
CHANNEL_OPTED_OUT_403 = "VEL-CHANNEL-OPTED-OUT-403"
CHANNEL_WINDOW_409 = "VEL-CHANNEL-WINDOW-409"
CHANNEL_NOTFOUND_404 = "VEL-CHANNEL-NOTFOUND-404"

# ── Cases ─────────────────────────────────────────────────────────
# One `NOTFOUND` code covers case, case type, step, rule, share and proposal.
# A code per entity would be noise: the entity is already named in `detail`,
# and the resolution is identical for all of them.
CASE_NOTFOUND_404 = "VEL-CASE-NOTFOUND-404"
CASE_GUARD_403 = "VEL-CASE-GUARD-403"
#: The caller names a tenant this platform does not have. Refusing beats
#: falling through to "no tenant", which stores or reads platform-wide.
CASE_TENANT_403 = "VEL-CASE-TENANT-403"
CASE_RULE_SCOPE_403 = "VEL-CASE-RULE-SCOPE-403"
CASE_RULE_DISABLED_409 = "VEL-CASE-RULE-DISABLED-409"
CASE_TYPED_VAR_400 = "VEL-CASE-TYPED-VAR-400"
CASE_VAR_FILTER_400 = "VEL-CASE-VAR-FILTER-400"
CASE_AI_ACTION_400 = "VEL-CASE-AI-ACTION-400"
CASE_PROCDEF_400 = "VEL-CASE-PROCDEF-400"
CASE_SUBPROCESS_500 = "VEL-CASE-SUBPROCESS-500"

# ── DMN interchange (market gap B5) ───────────────────────────────
# Import refuses rather than approximates: a decision table that runs and
# answers a slightly different question than the one it was written to answer
# is worse than one that would not import.
#: The bytes are not DMN — unparseable, too large, or entity-expanding.
RULE_DMN_XML_400 = "VEL-RULE-DMN-XML-400"
#: A hit policy this engine cannot honour (it returns one row, not several).
RULE_DMN_HITPOLICY_422 = "VEL-RULE-DMN-HITPOLICY-422"
#: A cell or input expression outside the FEEL unary-test subset that is read.
RULE_DMN_EXPR_422 = "VEL-RULE-DMN-EXPR-422"
#: Valid DMN, but holding no decision table at all.
RULE_DMN_EMPTY_422 = "VEL-RULE-DMN-EMPTY-422"
#: The stored rule holds something DMN's unary tests cannot express, so it can
#: be evaluated here but not written out.
RULE_DMN_EXPORT_409 = "VEL-RULE-DMN-EXPORT-409"

# ── Audit streaming to a SIEM (market gap A3) ─────────────────────
#: The endpoint is not a URL this platform will ship its audit to.
AUDIT_STREAM_URL_400 = "VEL-AUDIT-STREAM-URL-400"
#: A transport this platform does not speak.
AUDIT_STREAM_KIND_400 = "VEL-AUDIT-STREAM-KIND-400"
#: The stream is platform-wide and the caller has not said they know that.
AUDIT_STREAM_SCOPE_400 = "VEL-AUDIT-STREAM-SCOPE-400"
#: No such target.
AUDIT_STREAM_404 = "VEL-AUDIT-STREAM-404"
#: A duplicate name, or enabling a target that could not authenticate.
AUDIT_STREAM_409 = "VEL-AUDIT-STREAM-409"

# ── 198: case actions and the configured status set ───────────────
# A status write that the resolved set refuses, split three ways because
# "that did not work" is not something an operator can act on: the status is
# not in the set at all, or it exists but is no longer offered, or the
# transition graph refuses this particular move.
CASE_STATUS_UNKNOWN_400 = "VEL-CASE-STATUS-UNKNOWN-400"
CASE_STATUS_DISABLED_409 = "VEL-CASE-STATUS-DISABLED-409"
CASE_STATUS_TRANSITION_409 = "VEL-CASE-STATUS-TRANSITION-409"
#: A status a case is sitting on cannot be deleted — that strands the case.
CASE_STATUS_INUSE_409 = "VEL-CASE-STATUS-INUSE-409"
CASE_STATUS_NAME_400 = "VEL-CASE-STATUS-NAME-400"
CASE_STATUS_CATEGORY_400 = "VEL-CASE-STATUS-CATEGORY-400"

#: The action vocabulary is CLOSED. An unknown action is a refusal, never a
#: guess at what the word might mean — the HxArbiter injection lesson.
CASE_ACTION_UNKNOWN_404 = "VEL-CASE-ACTION-UNKNOWN-404"
CASE_ACTION_PARAM_400 = "VEL-CASE-ACTION-PARAM-400"
CASE_ACTION_DENIED_403 = "VEL-CASE-ACTION-DENIED-403"
#: The tenant's own configuration says this action is not offered from the
#: case's current status. Distinct from a privilege denial: the operator may
#: hold the privilege and the case still be in the wrong state.
CASE_ACTION_NOTALLOWED_409 = "VEL-CASE-ACTION-NOTALLOWED-409"
CASE_ACTION_FAILED_500 = "VEL-CASE-ACTION-FAILED-500"
CASE_ACTION_PRIV_400 = "VEL-CASE-ACTION-PRIV-400"
#: HxArbiter refused the status transition. Distinct from a privilege denial:
#: the caller may act on the case and the POLICY still refuse this move.
CASE_ACTION_POLICY_403 = "VEL-CASE-ACTION-POLICY-403"
#: The case's STAGE does not offer this action. Asked as well as the status
#: rule, never instead of it — two stages can share a status, so neither rule
#: can stand in for the other, and both can only narrow.
CASE_ACTION_STAGE_409 = "VEL-CASE-ACTION-STAGE-409"
#: The case is archived or soft-deleted, and nothing may act on it but the one
#: action that reverses that state. Checked in the dispatcher rather than in each
#: handler, so an action added later inherits the rule.
CASE_ACTION_SEALED_409 = "VEL-CASE-ACTION-SEALED-409"
#: A stage definition naming an action or a status that does not exist. Refused
#: at authoring: the action vocabulary is closed and can never grow to match a
#: typo, and a stage that sets a status nobody defined would move the case and
#: change nothing.
CASE_STAGE_CONFIG_400 = "VEL-CASE-STAGE-CONFIG-400"

#: Refused, never truncated: acting on the first N of a larger selection and
#: reporting success is the worse answer.
CASE_BULK_CEILING_400 = "VEL-CASE-BULK-CEILING-400"
CASE_BULK_EMPTY_400 = "VEL-CASE-BULK-EMPTY-400"
CASE_BULK_DESTRUCTIVE_400 = "VEL-CASE-BULK-DESTRUCTIVE-400"

#: Bulk STAGE transition (2026-09-17). A stage move fires SLAs, the stage's
#: `on_enter_status` and workflow signals per case, none of which moving the case
#: back undoes — so the run needs the server-checked confirmation a preview
#: issues, bound to the operator, the target stage and the exact set of cases.
CASE_BULK_STAGE_CONFIRM_409 = "VEL-CASE-BULK-STAGE-CONFIRM-409"
#: The caller lacks `case.actions.bulk_stage`. Checked once, before any case is
#: read: it is a question about the operator, and its answer is the same for
#: every case in the request.
CASE_BULK_STAGE_DENIED_403 = "VEL-CASE-BULK-STAGE-DENIED-403"
#: Per case: the target stage is not a stage of this case's case type.
CASE_BULK_STAGE_UNKNOWN_409 = "VEL-CASE-BULK-STAGE-UNKNOWN-409"
#: Per case: an open HxSentinel risk verification holds the case where it is.
CASE_STAGE_HELD_409 = "VEL-CASE-STAGE-HELD-409"

#: Moving a case between tenants (mig 205). Distinct codes because "fix the
#: data", "ask policy", "get a second person" and "it already moved on" are
#: four different next steps.
CASE_TENANT_TRANSFER_400 = "VEL-CASE-TENANT-TRANSFER-400"
CASE_TENANT_TRANSFER_404 = "VEL-CASE-TENANT-TRANSFER-404"
CASE_TENANT_TRANSFER_BLOCKED_409 = "VEL-CASE-TENANT-TRANSFER-BLOCKED-409"
CASE_TENANT_TRANSFER_POLICY_403 = "VEL-CASE-TENANT-TRANSFER-POLICY-403"
CASE_TENANT_TRANSFER_STATE_409 = "VEL-CASE-TENANT-TRANSFER-STATE-409"
CASE_TENANT_TRANSFER_TWO_PERSON_403 = "VEL-CASE-TENANT-TRANSFER-TWO-PERSON-403"

#: Configuration is authored per tenant. A caller resolving to no tenant would
#: be writing the PLATFORM default set, changing every tenant at once.
CASE_CONFIG_TENANT_400 = "VEL-CASE-CONFIG-TENANT-400"

#: Legal holds (mig 215). HELD is what every frozen operation answers — delete,
#: anonymize, erase, transfer — so one code means "release the hold first".
LEGALHOLD_HELD_409 = "VEL-LEGALHOLD-HELD-409"
LEGALHOLD_STATE_409 = "VEL-LEGALHOLD-STATE-409"
LEGALHOLD_INVALID_400 = "VEL-LEGALHOLD-INVALID-400"
LEGALHOLD_NOTFOUND_404 = "VEL-LEGALHOLD-NOTFOUND-404"
#: Release approval (mig 218): a second person, from the tenant's named group.
LEGALHOLD_SELF_APPROVAL_403 = "VEL-LEGALHOLD-SELF-APPROVAL-403"
LEGALHOLD_NOT_APPROVER_403 = "VEL-LEGALHOLD-NOT-APPROVER-403"
LEGALHOLD_NO_REQUEST_404 = "VEL-LEGALHOLD-NO-REQUEST-404"
LEGALHOLD_GROUP_400 = "VEL-LEGALHOLD-GROUP-400"
LEGALHOLD_SETTING_LOCKED_403 = "VEL-LEGALHOLD-SETTING-LOCKED-403"


# ── HxArbiter (`VEL-ARB-`) ────────────────────────────────────────
#
# Registered 2026-09-08, and the delay is the point: all 23 were raised by
# `api/routers/arbiter.py` and its PEPs from the day HxArbiter shipped, and all
# 23 were written up in `error-catalog.md` — but none was ever a constant, so
# `KNOWN_CODES` did not know them and `test_catalog_documents_no_code_the_app_
# cannot_raise` had been failing on them ever since. A red test nobody can close
# is a red test everybody learns to skip, which is how the next real drift would
# have gone unnoticed.
#
# Two of them, `ARB_EXPORT_DENIED_403` and `ARB_VAULT_SHRED_DENIED_403`, gained
# their `-403` on that day. They were emitted as `PermissionError` messages with
# no HTTP suffix, which no code in this registry may have — the suffix is what
# keeps `VEL-<SUBSYSTEM>-<SLUG>-<HTTP>` sortable. The new spelling is a strict
# EXTENSION of the old one, so anything matching the old identifier as a prefix
# or a substring — including this platform's own tests — still matches; only an
# exact comparison against the whole detail string would notice, and that would
# already break on any wording change. 403 is what the export refusal actually
# returns (`enterprise._policy_refusal`); the shred has no HTTP surface today,
# and 403 is what it would be given one.
ARB_ANCHOR_502 = "VEL-ARB-ANCHOR-502"
ARB_COMPILE_422 = "VEL-ARB-COMPILE-422"
ARB_DRAFT_404 = "VEL-ARB-DRAFT-404"
ARB_DRAFT_FAILED_422 = "VEL-ARB-DRAFT-FAILED-422"
ARB_ELEVATE_DENIED_403 = "VEL-ARB-ELEVATE-DENIED-403"
ARB_EXPORT_DENIED_403 = "VEL-ARB-EXPORT-DENIED-403"
ARB_IMPORT_EMPTY_422 = "VEL-ARB-IMPORT-EMPTY-422"
ARB_IMPORT_FORMAT_422 = "VEL-ARB-IMPORT-FORMAT-422"
ARB_IMPORT_REJECTED_422 = "VEL-ARB-IMPORT-REJECTED-422"
ARB_IMPORT_TOO_LARGE_422 = "VEL-ARB-IMPORT-TOO-LARGE-422"
ARB_LIFECYCLE_409 = "VEL-ARB-LIFECYCLE-409"
ARB_MOMENT_INVALID_422 = "VEL-ARB-MOMENT-INVALID-422"
ARB_NAMESPACE_404 = "VEL-ARB-NAMESPACE-404"
ARB_NO_PROVIDER_503 = "VEL-ARB-NO-PROVIDER-503"
ARB_OVERRIDE_REASON_422 = "VEL-ARB-OVERRIDE-REASON-422"
ARB_PARITY_409 = "VEL-ARB-PARITY-409"
ARB_PLATFORM_SCOPE_403 = "VEL-ARB-PLATFORM-SCOPE-403"
ARB_POLICY_404 = "VEL-ARB-POLICY-404"
ARB_RULE_INVALID_422 = "VEL-ARB-RULE-INVALID-422"
ARB_STAGE_DENIED_403 = "VEL-ARB-STAGE-DENIED-403"
ARB_TENANT_UNRESOLVED_403 = "VEL-ARB-TENANT-UNRESOLVED-403"
ARB_VAULT_SHRED_DENIED_403 = "VEL-ARB-VAULT-SHRED-DENIED-403"
ARB_VERSION_404 = "VEL-ARB-VERSION-404"


# ── Marketplace ───────────────────────────────────────────────────
MKT_NOTFOUND_404 = "VEL-MKT-NOTFOUND-404"
#: Ownership **and** role. The 2026-08-11 pass deliberately reused this rather
#: than adding a parallel ROLE code: this entry has always covered "does not own
#: the workspace or lacks the marketplace role", and two codes for one sentence
#: would make the catalog answer the same question twice.
MKT_ACCESS_403 = "VEL-MKT-ACCESS-403"
MKT_INSTALLED_409 = "VEL-MKT-INSTALLED-409"
MKT_STATE_400 = "VEL-MKT-STATE-400"
MKT_OFFICIAL_400 = "VEL-MKT-OFFICIAL-400"
MKT_VERIFY_502 = "VEL-MKT-VERIFY-502"

# Added 2026-08-11. Only conditions the six codes above could not already say.
#: The marketplace release flag is off. 404 rather than 403, the same choice
#: `VEL-DBM-DISABLED-404` makes: the flag hides the module rather than
#: advertising a locked door.
MKT_DISABLED_404 = "VEL-MKT-DISABLED-404"
#: Refused by **policy**, not by identity — a blacklisted org/source/package, or
#: an access rule denying this group. Kept apart from MKT_ACCESS_403 because the
#: fix is to change the policy, not to grant the caller a role.
MKT_BLOCKED_403 = "VEL-MKT-BLOCKED-403"
#: The conformance gate (#27) refused the package. 422, not 400: the request was
#: well-formed and the *package* failed its checks.
MKT_CONFORMANCE_422 = "VEL-MKT-CONFORMANCE-422"
#: A `runtime: wasm` package met HxSandbox switched off, or its guest module was
#: rejected at install. Deployment shape — the same class of fault as the HxMeet
#: 501s, and #15's mandate is that a module that cannot run fails at **install**
#: rather than on first use.
MKT_SANDBOX_400 = "VEL-MKT-SANDBOX-400"
#: Per-user active-workspace limit.
MKT_QUOTA_429 = "VEL-MKT-QUOTA-429"
#: A capability grant is revoked or already revoked. A revoked grant is
#: terminal by design — the package is reinstalled to ask again, so this must
#: not read as a retryable state.
MKT_GRANT_409 = "VEL-MKT-GRANT-409"
#: Accelerator-pack provisioning, upgrade or fork refused on state.
MKT_PACK_409 = "VEL-MKT-PACK-409"
#: The install itself could not be completed — execution provisioning failed, or
#: the bundle would not verify at install time. NOTE the status split this
#: preserves rather than fixes: install-time verification answers **400** while
#: the update path's identical check answers 502 (`MKT_VERIFY_502`). Left as-is
#: because changing an install endpoint's status is a client-visible break, not
#: a docs change.
MKT_INSTALL_400 = "VEL-MKT-INSTALL-400"

# ── Portal ────────────────────────────────────────────────────────
# The first codes raised on an **unauthenticated, customer-facing** surface:
# ~55 of `portal.py`'s raise sites belong to `public_router`. That changes what
# the code is for. A Studio user can click through to the catalog; a customer
# cannot, and is not shown the link. The code earns its place on the support
# call — "it says VEL-PORTAL-ACCESS-404" is diagnosable, "it says not found"
# is not.
#
# It also constrains the split. Codes are granular enough to route a support
# call and no finer, because a code that separates two deliberately merged
# failures re-opens by identifier exactly what the message closed.
PORTAL_NOTFOUND_404 = "VEL-PORTAL-NOTFOUND-404"
PORTAL_INACTIVE_403 = "VEL-PORTAL-INACTIVE-403"
PORTAL_CASETYPE_400 = "VEL-PORTAL-CASETYPE-400"
#: Case, tracking token or customer↔case link did not resolve **for this
#: caller**. Deliberately ONE code across "no such case", "wrong email",
#: "unknown tracking token" and "not linked to your account": these already
#: share a single opaque message so that an anonymous caller cannot probe which
#: case ids exist. Splitting them by code would rebuild that oracle in a field
#: clients can read just as easily.
PORTAL_ACCESS_404 = "VEL-PORTAL-ACCESS-404"
PORTAL_RATE_LIMIT_429 = "VEL-PORTAL-RATE-LIMIT-429"
#: Kept apart from the general limit because the resolution differs and this is
#: the one support actually gets called about: a 24h OTP lockout is waited out,
#: an attempt lockout is cleared by requesting a fresh code.
PORTAL_OTP_RATE_429 = "VEL-PORTAL-OTP-RATE-429"
PORTAL_OTP_INVALID_400 = "VEL-PORTAL-OTP-INVALID-400"
PORTAL_CLOSED_400 = "VEL-PORTAL-CLOSED-400"
PORTAL_UPLOAD_400 = "VEL-PORTAL-UPLOAD-400"
PORTAL_ACTION_409 = "VEL-PORTAL-ACTION-409"
#: R5e step 8: portal-service could not reach case-service's internal API.
PORTAL_UNAVAILABLE_503 = "VEL-PORTAL-UNAVAILABLE-503"
#: 2026-11-04: a tenant admin tried to change a platform default case type's
#: portal settings, which every tenant inherits. Operator only.
PORTAL_DEFAULT_403 = "VEL-PORTAL-DEFAULT-403"

# ── HxMeet ────────────────────────────────────────────────────────
# The distinguishing problem in this router is **deployment shape**: six sites
# returned an undifferentiated 501 across four different missing things, with
# four different fixes (LiveKit credentials, an egress directory, and two
# separate pip extras). A single "not installed" code would leave the reader
# parsing the message, which is what they could already do — so these stay
# four codes. That split is the main reason HxMeet was worth coding.
MEET_DRIVER_501 = "VEL-MEET-DRIVER-501"
MEET_RECORDING_501 = "VEL-MEET-RECORDING-501"
MEET_ASR_501 = "VEL-MEET-ASR-501"
MEET_KYC_501 = "VEL-MEET-KYC-501"

#: Case, session or challenge did not resolve for this caller. ONE code, for
#: the reason `_visible_session` is annotated "404 anti-oracle" in the router:
#: a malformed id, another tenant's session and a genuinely absent row already
#: answer identically, and HxGuard fail-closes an unknown action to 404 for
#: everyone including admins. A code per condition would undo all of that.
MEET_NOTFOUND_404 = "VEL-MEET-NOTFOUND-404"
#: Every guest-invite failure. The service raises "invalid invite" from six
#: separate places — consumed, expired, session dead, wrong tenant, unknown,
#: malformed — precisely so a public caller cannot tell them apart. One code.
MEET_INVITE_404 = "VEL-MEET-INVITE-404"
#: The session has no sealed recording or transcript yet. Distinct from
#: NOTFOUND: the session is right there, the evidence simply does not exist,
#: and the fix is to record and seal one rather than to check the id.
MEET_NORECORDING_404 = "VEL-MEET-NORECORDING-404"

#: The tenant has not switched on intelligence or video-KYC. An admin setting,
#: not a bug and not a missing dependency — which is why it is not a 501.
MEET_OPTIN_400 = "VEL-MEET-OPTIN-400"
#: Session or recording lifecycle: not active, already recording, no active
#: recording, challenges outside a running recording.
MEET_STATE_409 = "VEL-MEET-STATE-409"
#: Someone in the room has not consented to being recorded. Deliberately not
#: MEET_STATE_409 — this is about a person's rights, and it is the one refusal
#: here that must never read as a transient state problem to be retried past.
MEET_CONSENT_409 = "VEL-MEET-CONSENT-409"
#: A sealed recording or transcript would not unseal. Its own code for the same
#: reason VAULT_SHREDDED_410 is not VAULT_DEK_503: this is not a retry. Either
#: the ciphertext was altered or the key does not match it, and both are
#: incidents rather than hiccups.
MEET_TAMPER_409 = "VEL-MEET-TAMPER-409"
#: The upstream meeting provider or the egress service failed. 502 because the
#: fault is theirs, not the caller's.
MEET_PROVIDER_502 = "VEL-MEET-PROVIDER-502"
MEET_RATE_429 = "VEL-MEET-RATE-429"

# ── HxDBManager ───────────────────────────────────────────────────
#: The module is not enabled on this instance. **404, not 403** — the release
#: flag hides the module rather than advertising a locked door, so the code
#: must not turn round and admit it exists.
DBM_DISABLED_404 = "VEL-DBM-DISABLED-404"
DBM_ROLE_403 = "VEL-DBM-ROLE-403"

#: Table not found **or** deliberately concealed. `_require_table_access` has a
#: three-way disclosure model: `_HIDDEN_TABLES` answer 404 so their existence
#: is never confirmed, `_SUPERADMIN_ONLY_TABLES` answer 403 because admitting
#: they exist is fine, everything else 403. A concealed table therefore shares
#: this code with a genuinely absent one — a separate "hidden" code would undo
#: the concealment in a field any client can read.
DBM_TABLE_404 = "VEL-DBM-TABLE-404"
#: The table exists and this role may not touch it. Deliberately the other side
#: of the split above.
DBM_TABLE_403 = "VEL-DBM-TABLE-403"

DBM_REAUTH_401 = "VEL-DBM-REAUTH-401"
DBM_RATE_429 = "VEL-DBM-RATE-429"
DBM_TOOLARGE_413 = "VEL-DBM-TOOLARGE-413"

#: Blocked by the abuse-pattern guard. Worth its own code because it is not
#: validation: the same path calls `_fire_breach_event`, so this code is the
#: join between a user's error message and a HIGH security event in HxShield.
DBM_BLOCKED_400 = "VEL-DBM-BLOCKED-400"
#: A DML write aimed at a protected table. Also a breach event.
DBM_PROTECTED_403 = "VEL-DBM-PROTECTED-403"
#: Wrong statement type for this endpoint — DDL sent to /execute, non-DDL sent
#: to /ddl, non-DML to /dml/preview, non-SELECT to /explain or the optimiser.
#: One code: every one of them means "right SQL, wrong door", and the message
#: already names the door.
DBM_STMT_400 = "VEL-DBM-STMT-400"
#: The statement was accepted and the database refused it. The engine's own
#: message is the resolution; the code only says the fault is in the SQL rather
#: than in the platform.
DBM_QUERY_400 = "VEL-DBM-QUERY-400"
DBM_CONFIRM_400 = "VEL-DBM-CONFIRM-400"

#: The AI backend is unavailable or never answered. Split from DBM_AI_502
#: because the fixes share nothing: this one is a configuration or liveness
#: problem, and `GET /hxnexus/status` now reports `misconfigured` when a
#: provider was set without its key.
DBM_AI_503 = "VEL-DBM-AI-503"
#: The AI backend answered, and the answer was unusable — empty, not JSON, or
#: carrying no SQL. Nothing is down; the model is too small, overloaded, or
#: ignoring the format instruction.
DBM_AI_502 = "VEL-DBM-AI-502"
#: Slow-query statistics are not collected on this backend (no
#: `pg_stat_statements`). Distinct from DBM_AI_503 even though both are 503:
#: the AI is fine, the database extension is missing.
DBM_PGSTAT_503 = "VEL-DBM-PGSTAT-503"

# A DML preview cannot be confirmed. Three codes rather than one, and the
# reason is worth recording: the first cut named a single suffix-less
# `VEL-DBM-PREVIEW-STALE`, on the grounds that all three answers mean "make a
# new preview". `test_code_format_is_uniform` rejected it — the registry
# requires every code to end in its HTTP status, deliberately, so the code in a
# log line always says what the client actually received. Weakening a guard to
# fit a naming preference is the wrong trade, so the split follows the statuses.
#: Preview id unknown — evicted from the in-memory store, or never existed.
DBM_PREVIEW_404 = "VEL-DBM-PREVIEW-404"
#: The preview exists and belongs to someone else. The one of the three that is
#: NOT fixed by re-running: the caller is in the wrong session or tab.
DBM_PREVIEW_403 = "VEL-DBM-PREVIEW-403"
#: The preview is yours and timed out (5-minute limit).
DBM_PREVIEW_410 = "VEL-DBM-PREVIEW-410"

# ── Connectors (HxBridge) ─────────────────────────────────────────
CONN_NOTFOUND_404 = "VEL-CONN-NOTFOUND-404"
CONN_DISABLED_400 = "VEL-CONN-DISABLED-400"
CONN_UNKNOWN_TYPE_400 = "VEL-CONN-UNKNOWN-TYPE-400"
CONN_ROLE_403 = "VEL-CONN-ROLE-403"
CONN_FORM_LOOKUP_403 = "VEL-CONN-FORM-LOOKUP-403"

# ── LLM / HxNexus ─────────────────────────────────────────────────
LLM_NOTFOUND_404 = "VEL-LLM-NOTFOUND-404"
LLM_NOT_ASSIGNED_403 = "VEL-LLM-NOT-ASSIGNED-403"

# ── HxDraft ───────────────────────────────────────────────────────
# HxDraft generates a component from a description, then Apply re-normalises and
# re-validates it server-side — the client's copy is never trusted. So its two
# characteristic failures are not "bad request" in the ordinary sense: the draft
# was well-formed when it was generated and is refused now, either because the
# server disagrees with it (VALIDATION) or because the artifact it was drafted
# against has since changed (STALE). Both are fixed by re-drafting, and a user
# who is told only "400" will retry the same payload instead.
DRAFT_VALIDATION_400 = "VEL-DRAFT-VALIDATION-400"
DRAFT_STALE_409 = "VEL-DRAFT-STALE-409"
DRAFT_KIND_400 = "VEL-DRAFT-KIND-400"
DRAFT_ROLE_403 = "VEL-DRAFT-ROLE-403"
DRAFT_NOTFOUND_404 = "VEL-DRAFT-NOTFOUND-404"
DRAFT_BADID_400 = "VEL-DRAFT-BADID-400"
DRAFT_EXISTS_409 = "VEL-DRAFT-EXISTS-409"
DRAFT_RATE_429 = "VEL-DRAFT-RATE-429"
DRAFT_AI_503 = "VEL-DRAFT-AI-503"
DRAFT_GENERATE_400 = "VEL-DRAFT-GENERATE-400"

# ── HxBranch ──────────────────────────────────────────────────────
# A branch is a state machine with an owner, a reviewer and a remote, so almost
# every refusal answers one of four questions: what state is it in, who are you,
# is the remote usable, and did main move underneath it. Coding by *question*
# rather than per raise site is what keeps 38 sites at 11 codes — the specific
# status and role are already in `detail`.
BRANCH_NOTFOUND_404 = "VEL-BRANCH-NOTFOUND-404"
BRANCH_ARTIFACT_404 = "VEL-BRANCH-ARTIFACT-404"
BRANCH_STATE_400 = "VEL-BRANCH-STATE-400"
BRANCH_LOCKED_423 = "VEL-BRANCH-LOCKED-423"
BRANCH_ACTOR_403 = "VEL-BRANCH-ACTOR-403"
BRANCH_SOD_400 = "VEL-BRANCH-SOD-400"
BRANCH_CONFLICT_409 = "VEL-BRANCH-CONFLICT-409"
BRANCH_ENV_400 = "VEL-BRANCH-ENV-400"
BRANCH_ENVAUTH_400 = "VEL-BRANCH-ENVAUTH-400"
BRANCH_REMOTE_502 = "VEL-BRANCH-REMOTE-502"
BRANCH_UNSUPPORTED_400 = "VEL-BRANCH-UNSUPPORTED-400"

# ── Case Variables ────────────────────────────────────────────────
# Most of this router's refusals are enum and regex shape checks and stay bare.
# What is coded is the part a caller cannot deduce from the request: the reserved
# namespaces, the frozen-namespace rule, the stricter-only sensitivity rule, and
# anything the variables service itself refused — those send the reader to
# namespace grants and definitions rather than to their own request body.
VAR_NOTFOUND_404 = "VEL-VAR-NOTFOUND-404"
VAR_EXISTS_409 = "VEL-VAR-EXISTS-409"
VAR_RESERVED_400 = "VEL-VAR-RESERVED-400"
VAR_NSSTATUS_400 = "VEL-VAR-NSSTATUS-400"
VAR_SENSITIVITY_400 = "VEL-VAR-SENSITIVITY-400"
VAR_REJECTED_400 = "VEL-VAR-REJECTED-400"

# ── HxSentinel (risk) ─────────────────────────────────────────────
# Only three codes for 26 raise sites, and deliberately so. 15 of them are a
# missing entity — finding, feedback, proposal, hold, model version, pack, rule,
# case — where the entity is already in `detail` and the fix is identical, and 6
# are the risk service refusing an operation. Splitting by entity would grow the
# registry without telling a reader anything the message does not.
RISK_NOTFOUND_404 = "VEL-RISK-NOTFOUND-404"
RISK_REJECTED_400 = "VEL-RISK-REJECTED-400"
#: The community-pack SSRF guard. Its own code because the fix is nothing like
#: the other 400s here: the URL is well-formed and the manifest may be perfect —
#: the host is refused. An operator sent to check their manifest will not find it.
RISK_URL_400 = "VEL-RISK-URL-400"

# ── HxMigrate ─────────────────────────────────────────────────────
MIGRATE_NOTFOUND_404 = "VEL-MIGRATE-NOTFOUND-404"
MIGRATE_RUN_STATE_400 = "VEL-MIGRATE-RUN-STATE-400"
MIGRATE_PLATFORM_400 = "VEL-MIGRATE-PLATFORM-400"
MIGRATE_CREATOR_AUTH_400 = "VEL-MIGRATE-CREATOR-AUTH-400"
MIGRATE_TOO_LARGE_413 = "VEL-MIGRATE-TOO-LARGE-413"

# ── Vault (HxVault) ───────────────────────────────────────────────
# Both were raised deep in the crypto layer and caught nowhere, so they
# surfaced as bare 500s. A shredded DEK is UNRECOVERABLE and a 500 gives the
# operator no way to know that — they retry, escalate, and eventually restore
# a backup that cannot help either.
VAULT_SHREDDED_410 = "VEL-VAULT-SHREDDED-410"
VAULT_DEK_503 = "VEL-VAULT-DEK-503"

# ── Consent (HxConsent) ───────────────────────────────────────────
#: Raised when every recipient of a consent-gated send lacks a lawful basis.
#: A partial drop is NOT an error — the send proceeds for whoever remains, and
#: the drops are audited. This code means nobody was left to send to.
CONSENT_REFUSED_403 = "VEL-CONSENT-REFUSED-403"

# ── Database ──────────────────────────────────────────────────────
DB_UNAVAILABLE_503 = "VEL-DB-UNAVAILABLE-503"

# ── System / Startup ──────────────────────────────────────────────
SYS_BODY_413 = "VEL-SYS-BODY-413"
SYS_STORAGE_PRECHECK_000 = "VEL-SYS-STORAGE-PRECHECK-000"
#: The floor under every other handler. Anything that reaches it is a bug we
#: have not classified yet — but the client still gets JSON, because Starlette's
#: default 500 is `text/plain` and every caller in this platform parses JSON.
#: A browser hitting that default reports `JSON.parse: unexpected character at
#: line 1 column 1`, which describes the response format and says nothing about
#: the actual fault — as it did on 2026-08-05, when a dead database presented as
#: a front-end parser error.
SYS_UNHANDLED_500 = "VEL-SYS-UNHANDLED-500"
#: Workload identity (#18). The caller reached a service without coming through
#: the API gateway. Not an authentication failure — the request may carry a
#: perfectly good token — so it is deliberately NOT a 401: the problem is the
#: path it took, and saying "unauthenticated" would send an operator hunting for
#: a credential that is fine.
SYS_GATEWAY_REQUIRED_403 = "VEL-SYS-GATEWAY-REQUIRED-403"

#: Every code the catalog documents. A code raised outside this set is a bug:
#: it deep-links to a page that explains nothing.
KNOWN_CODES: frozenset[str] = frozenset({
    DOC_STORAGE_503, DOC_ENCRYPT_503, DOC_NOTFOUND_404, DOC_CASE_404,
    DOC_UUID_422, DOC_FILETYPE_400, DOC_PREVIEW_415, DOC_DELETE_403,
    DOC_LASTVERSION_409, DOC_TENANT_403, DOC_UNREADABLE_409, SYS_GATEWAY_REQUIRED_403,
    AUTH_CREDENTIALS_401, AUTH_DISABLED_403, AUTH_SSO_REQUIRED_400,
    AUTH_OTP_INVALID_400, AUTH_RATE_LIMIT_429, AUTH_DEVICE_MISMATCH_401,
    AUTH_SSO_UNCONFIGURED_404, AUTH_EMAIL_TAKEN_409, AUTH_CHECK_BUSY_503,
    AUTH_SSO_STATE_400, AUTH_SSO_PROVISION_403, AUTH_SSO_UNVERIFIED_403,
    AUTH_SAML_ASSERTION_400, AUTH_SAML_PROVISION_403,
    CHANNEL_OPTED_OUT_403, CHANNEL_WINDOW_409, CHANNEL_NOTFOUND_404,
    CASE_NOTFOUND_404, CASE_GUARD_403, CASE_TENANT_403, CASE_RULE_SCOPE_403,
    CASE_RULE_DISABLED_409, CASE_TYPED_VAR_400, CASE_VAR_FILTER_400,
    CASE_AI_ACTION_400, CASE_PROCDEF_400, CASE_SUBPROCESS_500,
    RULE_DMN_XML_400, RULE_DMN_HITPOLICY_422, RULE_DMN_EXPR_422,
    RULE_DMN_EMPTY_422, RULE_DMN_EXPORT_409,
    AUDIT_STREAM_URL_400, AUDIT_STREAM_KIND_400, AUDIT_STREAM_SCOPE_400,
    AUDIT_STREAM_404, AUDIT_STREAM_409,
    # 198 — the configured status set and the closed action vocabulary.
    CASE_STATUS_UNKNOWN_400, CASE_STATUS_DISABLED_409,
    CASE_STATUS_TRANSITION_409, CASE_STATUS_INUSE_409,
    CASE_STATUS_NAME_400, CASE_STATUS_CATEGORY_400,
    CASE_ACTION_UNKNOWN_404, CASE_ACTION_PARAM_400, CASE_ACTION_DENIED_403,
    CASE_ACTION_NOTALLOWED_409, CASE_ACTION_FAILED_500, CASE_ACTION_PRIV_400,
    CASE_ACTION_POLICY_403, CASE_ACTION_STAGE_409, CASE_STAGE_CONFIG_400,
    CASE_ACTION_SEALED_409,
    CASE_BULK_CEILING_400, CASE_BULK_EMPTY_400, CASE_BULK_DESTRUCTIVE_400,
    CASE_BULK_STAGE_CONFIRM_409, CASE_BULK_STAGE_DENIED_403,
    CASE_BULK_STAGE_UNKNOWN_409, CASE_STAGE_HELD_409,
    CASE_TENANT_TRANSFER_400, CASE_TENANT_TRANSFER_404,
    CASE_TENANT_TRANSFER_BLOCKED_409, CASE_TENANT_TRANSFER_POLICY_403,
    CASE_TENANT_TRANSFER_STATE_409, CASE_TENANT_TRANSFER_TWO_PERSON_403,
    CASE_CONFIG_TENANT_400,
    LEGALHOLD_HELD_409, LEGALHOLD_STATE_409, LEGALHOLD_INVALID_400,
    LEGALHOLD_NOTFOUND_404,
    LEGALHOLD_SELF_APPROVAL_403, LEGALHOLD_NOT_APPROVER_403, LEGALHOLD_NO_REQUEST_404,
    LEGALHOLD_GROUP_400, LEGALHOLD_SETTING_LOCKED_403,
    # HxArbiter — see the block above for the two that gained a `-403`.
    ARB_ANCHOR_502, ARB_COMPILE_422, ARB_DRAFT_404,
    ARB_DRAFT_FAILED_422, ARB_ELEVATE_DENIED_403, ARB_EXPORT_DENIED_403,
    ARB_IMPORT_EMPTY_422, ARB_IMPORT_FORMAT_422, ARB_IMPORT_REJECTED_422,
    ARB_IMPORT_TOO_LARGE_422, ARB_LIFECYCLE_409, ARB_MOMENT_INVALID_422,
    ARB_NAMESPACE_404, ARB_NO_PROVIDER_503, ARB_OVERRIDE_REASON_422,
    ARB_PARITY_409, ARB_PLATFORM_SCOPE_403, ARB_POLICY_404,
    ARB_RULE_INVALID_422, ARB_STAGE_DENIED_403, ARB_TENANT_UNRESOLVED_403,
    ARB_VAULT_SHRED_DENIED_403, ARB_VERSION_404,
    MKT_NOTFOUND_404, MKT_ACCESS_403, MKT_INSTALLED_409,
    MKT_STATE_400, MKT_OFFICIAL_400, MKT_VERIFY_502,
    MKT_DISABLED_404, MKT_BLOCKED_403, MKT_CONFORMANCE_422, MKT_SANDBOX_400,
    MKT_QUOTA_429, MKT_GRANT_409, MKT_PACK_409, MKT_INSTALL_400,
    PORTAL_NOTFOUND_404, PORTAL_INACTIVE_403, PORTAL_CASETYPE_400,
    PORTAL_ACCESS_404, PORTAL_RATE_LIMIT_429, PORTAL_OTP_RATE_429,
    PORTAL_OTP_INVALID_400, PORTAL_CLOSED_400, PORTAL_UPLOAD_400,
    PORTAL_ACTION_409, PORTAL_UNAVAILABLE_503, PORTAL_DEFAULT_403,
    MEET_DRIVER_501, MEET_RECORDING_501, MEET_ASR_501, MEET_KYC_501,
    MEET_NOTFOUND_404, MEET_INVITE_404, MEET_NORECORDING_404,
    MEET_OPTIN_400, MEET_STATE_409, MEET_CONSENT_409, MEET_TAMPER_409,
    MEET_PROVIDER_502, MEET_RATE_429,
    DBM_DISABLED_404, DBM_ROLE_403, DBM_TABLE_404, DBM_TABLE_403,
    DBM_REAUTH_401, DBM_RATE_429, DBM_TOOLARGE_413, DBM_BLOCKED_400,
    DBM_PROTECTED_403, DBM_STMT_400, DBM_QUERY_400, DBM_CONFIRM_400,
    DBM_AI_503, DBM_AI_502, DBM_PGSTAT_503,
    DBM_PREVIEW_404, DBM_PREVIEW_403, DBM_PREVIEW_410,
    CONN_NOTFOUND_404, CONN_DISABLED_400, CONN_UNKNOWN_TYPE_400, CONN_ROLE_403,
    CONN_FORM_LOOKUP_403,
    LLM_NOTFOUND_404, LLM_NOT_ASSIGNED_403,
    DRAFT_VALIDATION_400, DRAFT_STALE_409, DRAFT_KIND_400, DRAFT_ROLE_403,
    DRAFT_NOTFOUND_404, DRAFT_BADID_400, DRAFT_EXISTS_409, DRAFT_RATE_429,
    DRAFT_AI_503, DRAFT_GENERATE_400,
    BRANCH_NOTFOUND_404, BRANCH_ARTIFACT_404, BRANCH_STATE_400,
    BRANCH_LOCKED_423, BRANCH_ACTOR_403, BRANCH_SOD_400, BRANCH_CONFLICT_409,
    BRANCH_ENV_400, BRANCH_ENVAUTH_400, BRANCH_REMOTE_502,
    BRANCH_UNSUPPORTED_400,
    VAR_NOTFOUND_404, VAR_EXISTS_409, VAR_RESERVED_400, VAR_NSSTATUS_400,
    VAR_SENSITIVITY_400, VAR_REJECTED_400,
    RISK_NOTFOUND_404, RISK_REJECTED_400, RISK_URL_400,
    MIGRATE_NOTFOUND_404, MIGRATE_RUN_STATE_400, MIGRATE_PLATFORM_400,
    MIGRATE_CREATOR_AUTH_400, MIGRATE_TOO_LARGE_413,
    VAULT_SHREDDED_410, VAULT_DEK_503,
    CONSENT_REFUSED_403,
    DB_UNAVAILABLE_503,
    SYS_BODY_413, SYS_STORAGE_PRECHECK_000, SYS_UNHANDLED_500,
})


def docs_url(code: str) -> str:
    return f"{DOCS_BASE}#{code}"


class CodedHTTPException(HTTPException):
    """An HTTPException carrying a catalog code.

    Subclasses HTTPException rather than replacing it so every existing
    handler, middleware and test that expects one keeps working — the code is
    strictly additive.
    """

    def __init__(self, status_code: int, code: str, detail: str, **kwargs: Any) -> None:
        if code not in KNOWN_CODES:
            # Loud, because a code that is not in the catalog is worse than no
            # code: it promises documentation that does not exist.
            raise ValueError(
                f"{code!r} is not a documented error code — add it to "
                f"error_codes.KNOWN_CODES and to docs/Future/error-catalog.md"
            )
        super().__init__(status_code=status_code, detail=detail, **kwargs)
        self.code = code


def coded(status_code: int, code: str, detail: str, **kwargs: Any) -> CodedHTTPException:
    """Convenience raiser: `raise coded(503, DOC_STORAGE_503, "…")`."""
    return CodedHTTPException(status_code, code, detail, **kwargs)


def error_payload(code: str, detail: Any) -> dict[str, Any]:
    """The response body for a coded error."""
    return {"detail": detail, "error": {"code": code, "docs": docs_url(code)}}
