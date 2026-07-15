/**
 * Error Catalog — single source of truth for the in-app Error Documentation modal.
 *
 * Keyed by component key (derived from the nav path, e.g. "/documents" → "documents").
 * The same data drives both the per-page default view and the global error search.
 *
 * Seeded from docs/Future/error-catalog.md. Add entries here as errors are defined.
 * Keep code/HTTP/root-cause aligned with what the backend actually returns.
 */

export interface ErrorEntry {
  /** Stable code: VEL-<SUBSYSTEM>-<SLUG>-<HTTP>. Also shown in responses/logs (future). */
  code:        string;
  /** HTTP status returned, or "—" for non-HTTP / startup errors. */
  http:        string;
  /** What the user/operator actually sees. */
  symptom:     string;
  /** Why it happens. */
  rootCause:   string;
  /** How to fix it (operator and/or user). */
  resolution:  string;
  /** Related error codes worth checking. */
  related?:    string[];
}

/** A component's section: a friendly label + its error entries. */
export interface ComponentErrors {
  /** Human label shown in the modal title and the Component column. */
  label:   string;
  entries: ErrorEntry[];
}

/**
 * The catalog. Key = component key (nav path minus leading slash).
 * Pages with no own errors can be omitted — the modal still opens for global search.
 */
export const ERROR_CATALOG: Record<string, ComponentErrors> = {
  documents: {
    label: "Documents",
    entries: [
      {
        code: "VEL-DOC-STORAGE-503",
        http: "503",
        symptom: "Document upload fails with a server error; nothing is stored.",
        rootCause:
          "The configured storage path is not writable, or the MinIO backend is unreachable. " +
          "Most common: local storage pointing at a directory the service user cannot create " +
          "(e.g. /var/lib/velaris/documents — /var/lib is root-owned), so the filesystem write raises PermissionError.",
        resolution:
          "Operator: point VELARIS_CASE_STORAGE_LOCAL_PATH at a writable, install-local path " +
          "(recommended <install-dir>/data/documents) or create the configured directory with correct ownership, " +
          "then restart case-service. For MinIO: verify endpoint, credentials, and that the bucket exists. " +
          "User: retry; if it persists, contact your administrator — the file was not saved.",
        related: ["VEL-SYS-STORAGE-PRECHECK", "VEL-SYS-BODY-413"],
      },
      {
        code: "VEL-DOC-UUID-422",
        http: "422",
        symptom: "Load or upload fails with 422 before anything happens.",
        rootCause:
          "The case identifier is not a well-formed UUID. A UUID is 36 chars (8-4-4-4-12 hex); " +
          "a fragment like the first 8-hex block fails validation before the handler runs.",
        resolution:
          "User: paste the complete 36-character case UUID (copy it from the Cases list / case URL), not a fragment.",
        related: ["VEL-DOC-CASE-404"],
      },
      {
        code: "VEL-DOC-CASE-404",
        http: "404",
        symptom: "Upload returns 404 'case not found' even though the UUID is well-formed.",
        rootCause:
          "The UUID is valid in format but no case with that id exists (wrong tenant, deleted case, or non-existent id).",
        resolution:
          "User: use a case UUID from an existing case you have access to. Operator: confirm the case exists in the expected tenant.",
        related: ["VEL-DOC-UUID-422"],
      },
      {
        code: "VEL-DOC-FILETYPE-400",
        http: "400",
        symptom: "Upload blocked with 'File rejected: …'.",
        rootCause:
          "The filename failed validation: a blocked extension (.env, .key, .py, .sh …), a blocked pattern " +
          "(secrets.*, *config.json), path traversal (../), a null byte, or a type outside the document allowlist.",
        resolution:
          "User: upload an allowed type (PDF, Office, images, common archives, txt/csv/json/xml) and avoid blocked filename patterns. " +
          "Operator: if a legitimate type is rejected, add it to ALLOWED_DOCUMENT_EXTENSIONS (never remove blocklist entries).",
        related: ["VEL-SYS-BODY-413"],
      },
      {
        code: "VEL-DOC-NOTFOUND-404",
        http: "404",
        symptom: "404 'document not found' / 'version not found'.",
        rootCause:
          "The document id doesn't exist, is soft-deleted, or the requested version number doesn't exist for that document.",
        resolution:
          "User: refresh the document list; the item may have been deleted. Operator: a soft-deleted document is intentionally hidden.",
      },
      {
        code: "VEL-DOC-PREVIEW-415",
        http: "415",
        symptom: "415 'Preview not available for this content type'.",
        rootCause:
          "Preview generation supports only PDF and common image types; anything else (zip, Office) returns no preview. This is by design.",
        resolution:
          "User: download the file to view it; previews exist only for PDFs and images.",
      },
      {
        code: "VEL-DOC-DELETE-403",
        http: "403",
        symptom: "403 'Only the case owner or an admin can delete…'.",
        rootCause:
          "The caller is neither an admin nor the owner (created_by) of the case the document belongs to.",
        resolution:
          "User: ask the case owner or an administrator to delete it. Operator: grant admin or have the owner perform the deletion.",
        related: ["VEL-DOC-LASTVERSION-409"],
      },
      {
        code: "VEL-DOC-LASTVERSION-409",
        http: "409",
        symptom: "409 'cannot delete the only remaining version; delete the document instead'.",
        rootCause:
          "A document must retain at least one version; deleting the last one is blocked to avoid an orphaned record.",
        resolution:
          "User: delete the whole document instead of the last version.",
        related: ["VEL-DOC-DELETE-403"],
      },
    ],
  },

  cases: {
    label: "Cases",
    entries: [
      {
        code: "VEL-CASE-NOTFOUND-404",
        http: "404",
        symptom: "404 'Case not found' / 'Case type not found' / 'Rule not found' / 'Step … not found'.",
        rootCause: "The referenced case, case type, rule, share, or step id does not exist (deleted, wrong tenant, or bad id).",
        resolution: "User: refresh and pick a valid item. Operator: confirm the record exists in the expected tenant.",
      },
      {
        code: "VEL-CASE-AUTHZ-403",
        http: "403",
        symptom: "403 'Not authorized: …' or 'Rule is scoped to a different case type'.",
        rootCause: "A HxGuard/decision check denied the action, or a rule is being applied to a case type it isn't scoped to.",
        resolution: "User: request access or use the correct case type. Operator: review the access group / rule scope.",
      },
      {
        code: "VEL-CASE-RULEDISABLED-409",
        http: "409",
        symptom: "409 'Rule is disabled'.",
        rootCause: "The rule exists but is disabled, so it cannot be executed.",
        resolution: "Operator: enable the rule in the Case Designer before invoking it.",
      },
      {
        code: "VEL-CASE-VARTYPED-400",
        http: "400",
        symptom: "400 '… these keys are typed variables now; write them through the owning integration…'.",
        rootCause: "Attempt to write case-data keys that are now typed/governed variables; direct writes are blocked.",
        resolution: "User: write through the owning integration, or flag the variable in the Case Designer Variables panel.",
      },
      {
        code: "VEL-CASE-CONNCONFIG-400",
        http: "400",
        symptom: "400 'service_task has no connector_id configured' / 'subprocess step has no process_definition_id'.",
        rootCause: "A case-type step is missing required wiring (connector or sub-process definition).",
        resolution: "Operator: configure the connector / process definition on the step in the Case Designer.",
      },
      {
        code: "VEL-CASE-SUBPROC-500",
        http: "500",
        symptom: "500 'Failed to start subprocess: …'.",
        rootCause: "The sub-process could not be started — bad/inactive process definition or a downstream failure.",
        resolution: "Operator: check the referenced process definition is active; inspect server logs for the cause.",
        related: ["VEL-CASE-CONNCONFIG-400"],
      },
    ],
  },

  "access-directory": {
    label: "Access & Identity",
    entries: [
      {
        code: "VEL-AUTH-BADCREDS-401",
        http: "401",
        symptom: "401 'Incorrect password. N attempt(s) left' / 'Current password is incorrect'.",
        rootCause: "Wrong password. After MAX_ATTEMPTS the account locks.",
        resolution: "User: re-enter the correct password or reset it. Operator: unlock the account if it locked out.",
      },
      {
        code: "VEL-AUTH-DISABLED-401",
        http: "401",
        symptom: "401 'Account is disabled.'",
        rootCause: "The user account has been disabled.",
        resolution: "Operator: re-enable the account in Access & Identity if access should be restored.",
      },
      {
        code: "VEL-AUTH-MFA-401",
        http: "401/400",
        symptom: "'Invalid MFA token' / 'Invalid or expired OTP' / 'No pending MFA enrolment'.",
        rootCause: "MFA/OTP code is wrong, expired, or the enrolment flow wasn't started.",
        resolution: "User: use a fresh code from your authenticator; restart enrolment if needed.",
      },
      {
        code: "VEL-AUTH-SSO-400",
        http: "400",
        symptom: "400 'This account uses SSO login. Use the SSO button instead.'",
        rootCause: "Password login attempted on an SSO-only account.",
        resolution: "User: sign in with the SSO button.",
      },
      {
        code: "VEL-AUTH-PROVIDER-400",
        http: "400",
        symptom: "400 'Unknown provider …' / 'Token exchange failed' / 'Provider did not return an email'.",
        rootCause: "OAuth/SSO provider is misconfigured or the exchange failed.",
        resolution: "Operator: verify the provider config (client id/secret, endpoints, allowed email scope).",
      },
      {
        code: "VEL-AUTH-GROUP-404",
        http: "404",
        symptom: "404 'Access group not found' / 'Access role not found' / 'Portal not found'.",
        rootCause: "Referenced access group, role, or portal does not exist.",
        resolution: "Operator: confirm the access group/role/portal exists and is in scope.",
      },
      {
        code: "VEL-AUTH-GROUPMEMBER-403",
        http: "403",
        symptom: "403 'You are not a member of this access group'.",
        rootCause: "The user isn't a member of the access group required for the action.",
        resolution: "Operator: add the user to the access group if appropriate.",
      },
    ],
  },

  tenants: {
    label: "Tenants",
    entries: [
      {
        code: "VEL-TENANT-NOTFOUND-404",
        http: "404",
        symptom: "404 'Tenant not found' / 'Membership not found'.",
        rootCause: "The tenant or the user-tenant membership does not exist.",
        resolution: "Operator: confirm the tenant/membership id.",
      },
      {
        code: "VEL-TENANT-SLUG-400",
        http: "400",
        symptom: "400 'Slug must be lowercase alphanumeric with optional hyphens'.",
        rootCause: "Invalid tenant slug format.",
        resolution: "User: use lowercase letters, digits, and hyphens only.",
      },
      {
        code: "VEL-TENANT-DUP-409",
        http: "409",
        symptom: "409 'Tenant with slug … already exists' / 'User already a member of this tenant'.",
        rootCause: "Slug collision or duplicate membership.",
        resolution: "User: choose a unique slug; the membership already exists.",
      },
      {
        code: "VEL-TENANT-DEFAULT-400",
        http: "400",
        symptom: "400 'Cannot delete default tenant' / 'Invalid role'.",
        rootCause: "The default tenant is protected from deletion; or an unrecognized role was supplied.",
        resolution: "Operator: don't delete the default tenant; use a valid role value.",
      },
    ],
  },

  hxbridge: {
    label: "Connectors (HxBridge)",
    entries: [
      {
        code: "VEL-CONN-NOTFOUND-404",
        http: "404",
        symptom: "404 'Connector not found' / 'Connector no longer exists' / 'DLQ item not found'.",
        rootCause: "The connector or dead-letter item id does not exist (or the connector was deleted).",
        resolution: "Operator: confirm the connector/DLQ id; recreate the connector if it was removed.",
      },
      {
        code: "VEL-CONN-DISABLED-400",
        http: "400",
        symptom: "400 'Connector is disabled'.",
        rootCause: "The connector exists but is disabled, so it can't run.",
        resolution: "Operator: enable the connector before invoking it.",
      },
      {
        code: "VEL-CONN-TYPE-400",
        http: "400",
        symptom: "400 \"Unknown connector_type: '…'\".",
        rootCause: "The requested connector type isn't registered.",
        resolution: "Operator: use a supported connector type; check the connector registry.",
      },
      {
        code: "VEL-CONN-AUTHZ-403",
        http: "403",
        symptom: "403 'admin or integration role required'.",
        rootCause: "The caller lacks the admin/integration role needed to manage connectors.",
        resolution: "Operator: grant the admin or integration role.",
      },
      {
        code: "VEL-CONN-DUP-409",
        http: "409",
        symptom: "409 \"Connector '…' already exists\" / 'Already resolved: …'.",
        rootCause: "Duplicate connector name, or a DLQ item already resolved.",
        resolution: "User: pick a unique connector name; the DLQ item was already handled.",
      },
    ],
  },

  marketplace: {
    label: "Marketplace",
    entries: [
      {
        code: "VEL-MKT-WSSTATE-400",
        http: "400",
        symptom: "400 \"Cannot install into workspace with status '…'\" / 'Workspace not found or not in submitted state'.",
        rootCause: "The workspace isn't in a state that permits the requested lifecycle action.",
        resolution: "User: move the workspace to the correct state (e.g. submit before review) and retry.",
      },
      {
        code: "VEL-MKT-OFFICIALSRC-400",
        http: "400",
        symptom: "400 'Official Velaris sources cannot be removed.'",
        rootCause: "The baked-in official source is protected from removal.",
        resolution: "Operator: leave the official source in place; only community/private sources are removable.",
      },
      {
        code: "VEL-MKT-VERIFY-400",
        http: "400",
        symptom: "400 'Package verification failed: …'.",
        rootCause: "The package failed checksum/manifest validation or trust-tier checks.",
        resolution: "Publisher: fix the manifest/checksum; ensure the id and tier match the registry.",
      },
      {
        code: "VEL-MKT-FLOW-400",
        http: "400",
        symptom: "400 'Only official packages use the release-request flow…' / 'must go through sandbox testing first'.",
        rootCause: "Wrong lifecycle flow for the package tier, or new outbound domains require sandbox review.",
        resolution: "Publisher: use the sandbox workflow for community packages / when domains change.",
      },
      {
        code: "VEL-MKT-AUDIT-400",
        http: "400",
        symptom: "400 'reason is required for the audit trail' / 'Decommissioning reason is required'.",
        rootCause: "An audited action was attempted without the required reason.",
        resolution: "User: supply a reason for the action.",
      },
      {
        code: "VEL-MKT-DENIED-403",
        http: "403",
        symptom: "403 'Access denied'.",
        rootCause: "The caller lacks permission for the marketplace admin action.",
        resolution: "Operator: grant the required marketplace admin role.",
      },
    ],
  },

  hxmigrate: {
    label: "HxMigrate",
    entries: [
      {
        code: "VEL-MIGRATE-TOOBIG-413",
        http: "413",
        symptom: "413 'File too large. Maximum upload size is 100 MB.'",
        rootCause: "The migration export exceeds HxMigrate's 100 MB per-file SEC-7 limit (separate from the transport cap).",
        resolution: "User: split the export or trim it under 100 MB.",
        related: ["VEL-SYS-BODY-413"],
      },
      {
        code: "VEL-MIGRATE-RUNNOTFOUND-404",
        http: "404",
        symptom: "404 'Run not found'.",
        rootCause: "The migration run id does not exist.",
        resolution: "User: re-open the run from the HxMigrate list.",
      },
      {
        code: "VEL-MIGRATE-RUNSTATE-400",
        http: "400",
        symptom: "400 'Run must be completed before applying (status: …)'.",
        rootCause: "Apply was attempted before the run finished.",
        resolution: "User: wait for the run to complete, then apply.",
      },
      {
        code: "VEL-MIGRATE-PLATFORM-400",
        http: "400",
        symptom: "400 'Unsupported platform.'",
        rootCause: "The selected source BPM platform isn't supported by the importer.",
        resolution: "User: choose a supported vendor/platform.",
      },
      {
        code: "VEL-MIGRATE-CREATORAUTH-400",
        http: "400",
        symptom: "400 'No valid auth token available for Creator…' / 'Invalid creator_auth_type'.",
        rootCause: "Creator mode lacks a usable auth token or got an invalid auth type.",
        resolution: "Operator: set HELIX_SERVICE_TOKEN or ensure the user is logged in; use a valid creator auth type.",
      },
    ],
  },

  "email-admin": {
    label: "Email",
    entries: [
      {
        code: "VEL-EMAIL-NOACCOUNT-400",
        http: "400",
        symptom: "400 'No outbound email account configured'.",
        rootCause: "No outbound email account is set up, so mail can't be sent.",
        resolution: "Operator: configure an outbound email account in Email admin.",
      },
      {
        code: "VEL-EMAIL-NOTFOUND-404",
        http: "404",
        symptom: "404 'Account not found' / 'Template not found' / 'Message not found' / 'No active accounts to poll'.",
        rootCause: "The referenced email account, template, or message doesn't exist (or no active accounts).",
        resolution: "Operator: confirm the account/template/message; activate an account to poll.",
      },
      {
        code: "VEL-EMAIL-TEMPLATE-400",
        http: "400",
        symptom: "400 'Template error: …'.",
        rootCause: "The email template failed to render (bad placeholder / syntax).",
        resolution: "User: fix the template variables/syntax.",
      },
      {
        code: "VEL-EMAIL-SCOPE-400",
        http: "400",
        symptom: "400 \"case_type_id required when scope='case_type'\".",
        rootCause: "A case-type-scoped operation was missing its case_type_id.",
        resolution: "User: supply case_type_id for case-type scope.",
      },
    ],
  },

  // System-wide errors that can surface on any page.
  system: {
    label: "System",
    entries: [
      {
        code: "VEL-SYS-BODY-413",
        http: "413",
        symptom: "413 'Request body too large. Limit is N MB.'",
        rootCause:
          "BodyLimitMiddleware enforces tiered caps (10 MB JSON, 25 MB multipart uploads, 200 MB on hxmigrate routes), " +
          "checked via both the Content-Length header and a streamed byte counter.",
        resolution:
          "User: upload a smaller file or split the payload. Operator: raise max_upload_bytes / the gateway limit only after weighing the DoS surface.",
        related: ["VEL-DOC-FILETYPE-400"],
      },
      {
        code: "VEL-SYS-STORAGE-PRECHECK",
        http: "—",
        symptom: "(Proposed) A clear boot-log error if the storage path isn't writable, instead of the first upload failing later.",
        rootCause: "Same misconfiguration as VEL-DOC-STORAGE-503, caught proactively at startup.",
        resolution: "Operator: fix the path/ownership before the service accepts traffic (see VEL-DOC-STORAGE-503).",
        related: ["VEL-DOC-STORAGE-503"],
      },
    ],
  },
};

/** All entries flattened with their owning component label — used for global search. */
export interface FlatErrorEntry extends ErrorEntry {
  componentKey:   string;
  componentLabel: string;
}

export function allErrors(): FlatErrorEntry[] {
  const out: FlatErrorEntry[] = [];
  for (const [key, comp] of Object.entries(ERROR_CATALOG)) {
    for (const e of comp.entries) {
      out.push({ ...e, componentKey: key, componentLabel: comp.label });
    }
  }
  return out;
}

/** Case-insensitive search across code, symptom, root cause, resolution, and component label. */
export function searchErrors(query: string): FlatErrorEntry[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  return allErrors().filter(e =>
    e.code.toLowerCase().includes(q) ||
    e.symptom.toLowerCase().includes(q) ||
    e.rootCause.toLowerCase().includes(q) ||
    e.resolution.toLowerCase().includes(q) ||
    e.componentLabel.toLowerCase().includes(q) ||
    e.http.toLowerCase().includes(q),
  );
}
