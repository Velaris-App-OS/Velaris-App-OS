/**
 * Marketplace — Velaris App Store
 * Browse, install, and manage platform extensions.
 * Tabs: Browse · Installed · Workspaces · Review Queue
 */
import React, { useState, useMemo, useCallback } from "react";
import { useAuth } from "@/auth";

// ── Types ─────────────────────────────────────────────────────────────────────

type PackageType = string;   // dynamic — loaded from types.json via API
type PublisherTier = "official" | "community";
type PriceType = "free" | "paid";
type MainTab = "browse" | "installed" | "workspaces" | "review";

interface MktType { id: string; label: string; color: string; description: string; }
interface MktTypeCategory { label: string; types: MktType[]; }

interface MktPackage {
  id: string;
  name: string;
  description: string;
  long_description: string;
  type: PackageType;
  category: string;
  publisher: string;
  publisher_tier: PublisherTier;
  version: string;
  price: PriceType;
  price_label?: string;
  contact_url?: string;
  rating: number;
  installs: number;
  tags: string[];
  outbound_domains: string[];
  min_platform_version: string;
  updated_at: string;
  icon_color: string;
  icon_letter: string;
}

// ── API ───────────────────────────────────────────────────────────────────────

const API = "/api/v1/marketplace";

function _authHdr(): Record<string, string> {
  const t = localStorage.getItem("helix_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}

async function apiFetch(path: string, opts: RequestInit = {}) {
  return fetch(`${API}${path}`, {
    ...opts,
    headers: { "Content-Type": "application/json", ..._authHdr(), ...(opts.headers ?? {}) },
  });
}

// ── Fallback mock data (used when API is unreachable) ─────────────────────────

const MOCK_PACKAGES: MktPackage[] = [
  {
    id: "velaris/stripe-connector",
    name: "Stripe Connector",
    description: "Accept payments, issue refunds, and retrieve payment status directly from workflow steps.",
    long_description: "Full Stripe integration for Velaris case workflows. Supports payment_request steps (one-time charges), subscription billing, refund initiation, dispute webhooks, and real-time payment status tracking. Credentials are stored encrypted in HxBridge. Works with all Stripe-supported currencies.\n\nSetup: add your Stripe secret key in HxBridge after install. No additional configuration required.",
    type: "connector", category: "Payments",
    publisher: "Velaris", publisher_tier: "official",
    version: "2.1.0", price: "free",
    rating: 4.9, installs: 3240,
    tags: ["payments", "stripe", "billing", "fintech"],
    outbound_domains: ["api.stripe.com", "hooks.stripe.com"],
    min_platform_version: "1.0.0",
    updated_at: "2026-05-20",
    icon_color: "#635bff", icon_letter: "S",
  },
  {
    id: "velaris/salesforce-sync",
    name: "Salesforce CRM Sync",
    description: "Bi-directional sync of case data with Salesforce Leads, Contacts, Opportunities, and Cases.",
    long_description: "Keeps your Velaris case data in sync with Salesforce automatically. On case creation, a corresponding Salesforce record is created or matched. On case resolution, the Salesforce record is updated. Supports custom field mapping, Salesforce Sandbox for testing, and OAuth 2.0 authentication.\n\nRequires a Salesforce Connected App with API access enabled.",
    type: "connector", category: "CRM",
    publisher: "Velaris", publisher_tier: "official",
    version: "1.4.0", price: "free",
    rating: 4.7, installs: 1890,
    tags: ["crm", "salesforce", "sync", "enterprise"],
    outbound_domains: ["*.salesforce.com", "*.force.com"],
    min_platform_version: "1.0.0",
    updated_at: "2026-05-15",
    icon_color: "#00a1e0", icon_letter: "SF",
  },
  {
    id: "velaris/docusign-esign",
    name: "DocuSign E-Sign",
    description: "Send documents for electronic signature from any esign_send step. Track signature status in real time.",
    long_description: "Fully integrated DocuSign e-signature for Velaris workflows. Drop an esign_send step into any stage, configure the template and recipient fields, and Velaris handles the entire signature lifecycle — sending, reminders, completion, and void. Signed documents are automatically attached to the case.\n\nRequires a DocuSign Developer or production account. Supports DocuSign templates and dynamic envelope generation.",
    type: "connector", category: "E-Signature",
    publisher: "Velaris", publisher_tier: "official",
    version: "1.2.0", price: "free",
    rating: 4.8, installs: 2100,
    tags: ["esign", "docusign", "documents", "legal"],
    outbound_domains: ["api.docusign.com", "demo.docusign.net"],
    min_platform_version: "1.0.0",
    updated_at: "2026-04-28",
    icon_color: "#ffcc00", icon_letter: "DS",
  },
  {
    id: "velaris/hr-onboarding-template",
    name: "HR Employee Onboarding",
    description: "Complete employee onboarding case type: offer acceptance, document collection, IT provisioning, and day-1 checklist.",
    long_description: "A production-ready HR onboarding workflow built on Velaris. Covers the full employee lifecycle from offer letter acceptance to day-one readiness:\n\n• Stage 1: Offer & contract e-sign (DocuSign)\n• Stage 2: Personal data and right-to-work document collection\n• Stage 3: IT provisioning request (integrates with your helpdesk)\n• Stage 4: Equipment and access checklist\n• Stage 5: Day-1 confirmation and buddy assignment\n\nIncludes 12 pre-built forms, SLA policies (5-day total, per-stage), and a manager approval gate before the final confirmation.",
    type: "case_template", category: "HR",
    publisher: "Velaris", publisher_tier: "official",
    version: "1.0.0", price: "free",
    rating: 4.6, installs: 980,
    tags: ["hr", "onboarding", "employees", "template"],
    outbound_domains: [],
    min_platform_version: "1.0.0",
    updated_at: "2026-03-10",
    icon_color: "#22c55e", icon_letter: "HR",
  },
  {
    id: "acme/insurance-claims-template",
    name: "Insurance Claims Handler",
    description: "Multi-stage claims processing for P&C insurance: FNOL, assessment, reserve, settlement, and closure.",
    long_description: "A community-contributed case type template for property and casualty insurance claims. Covers the full claims lifecycle:\n\n• FNOL (First Notice of Loss) intake form with geo-location\n• Auto-triage by claim type and estimated value\n• Adjuster assignment with territory rules\n• Reserve management and approval\n• Payment disbursement via integrated payment step\n• Closure with customer satisfaction survey\n\nBuilt by ACME Insurance Tech and used in production by 3 UK insurers. Compatible with Stripe Connector for disbursements.\n\nNote: SLA policies are pre-configured for UK regulatory timelines (FCA DISP rules) — adjust for your jurisdiction.",
    type: "case_template", category: "Insurance",
    publisher: "ACME Insurance Tech", publisher_tier: "community",
    version: "2.0.1", price: "free",
    rating: 4.5, installs: 340,
    tags: ["insurance", "claims", "p&c", "fnol", "fintech"],
    outbound_domains: [],
    min_platform_version: "1.0.0",
    updated_at: "2026-05-01",
    icon_color: "#f97316", icon_letter: "IC",
  },
  {
    id: "fintech-labs/kyc-bundle",
    name: "KYC Onboarding Bundle",
    description: "Complete KYC/AML workflow bundle: identity verification, PEP screening, document check, and risk scoring.",
    long_description: "A full customer onboarding bundle for financial services regulatory compliance. Includes:\n\n• KYC Case Type: multi-stage identity verification workflow\n• Onfido Identity Connector: automated document + liveness check\n• PEP/Sanctions Screening Step: integrates with ComplyAdvantage\n• Risk Scoring Form: configurable risk appetite questionnaire\n• AML Alert Case Type: auto-created on high-risk triggers\n• Compliance Report Template: FCA-ready evidence pack\n\nUsed by 12+ EU/UK regulated entities. Includes audit chain integration for GDPR Article 30 records.\n\nPricing: €299/month per tenant. Contact seller for volume discounts and on-premise licensing.",
    type: "bundle", category: "Compliance",
    publisher: "FinTech Labs Ltd", publisher_tier: "community",
    version: "3.1.0", price: "paid", price_label: "€299/mo",
    contact_url: "https://fintechlabs.example.com/marketplace/kyc",
    rating: 4.8, installs: 89,
    tags: ["kyc", "aml", "compliance", "identity", "fca", "gdpr"],
    outbound_domains: ["api.onfido.com", "api.complyadvantage.com"],
    min_platform_version: "1.2.0",
    updated_at: "2026-05-10",
    icon_color: "#8b5cf6", icon_letter: "KY",
  },
  {
    id: "designstudio/midnight-portal",
    name: "Midnight Portal Theme",
    description: "Deep navy customer portal theme with teal accents, custom logo placement, and mobile-optimised layout.",
    long_description: "A premium dark portal theme for the Velaris Customer Portal. Features:\n\n• Deep navy (#0d1b2a) base with teal (#0d9488) accent\n• Custom logo placement in header and login page\n• Mobile-first responsive grid (works on iOS Safari and Android Chrome)\n• Accessible: WCAG 2.1 AA contrast ratios throughout\n• Customisable hero text and CTA button\n• Inter font stack (no Google Fonts dependency)\n\nPricing: $99 one-time licence per tenant. Includes 12 months of updates.",
    type: "portal_theme", category: "Branding",
    publisher: "Design Studio Co.", publisher_tier: "community",
    version: "1.1.0", price: "paid", price_label: "$99",
    contact_url: "https://designstudio.example.com/velaris-themes",
    rating: 4.3, installs: 52,
    tags: ["theme", "portal", "dark", "branding", "mobile"],
    outbound_domains: [],
    min_platform_version: "1.0.0",
    updated_at: "2026-04-15",
    icon_color: "#0d1b2a", icon_letter: "MP",
  },
  {
    id: "nlp-labs/financial-nlp-pack",
    name: "Financial Services NLP Pack",
    description: "50+ pre-built HxNexus prompt templates for banking, insurance, and wealth management operations.",
    long_description: "A curated pack of HxNexus query templates optimised for financial services terminology and workflows. Includes:\n\n• 20 case queue analysis prompts (claims backlog, SLA breach prediction)\n• 15 regulatory reporting prompts (FCA, PRA, MiFID II language)\n• 10 customer communication templates\n• 5 fraud pattern analysis queries\n• Glossary injection for UK/EU financial regulatory terms\n\nPrompts are version-controlled and can be customised per tenant. Includes a test harness for prompt regression testing.\n\nContributed by NLP Labs UK — open sourced under MIT licence.",
    type: "nlp_pack", category: "AI & NLP",
    publisher: "NLP Labs UK", publisher_tier: "community",
    version: "1.3.0", price: "free",
    rating: 4.6, installs: 210,
    tags: ["nlp", "prompts", "finance", "banking", "ai", "hxnexus"],
    outbound_domains: [],
    min_platform_version: "1.1.0",
    updated_at: "2026-05-05",
    icon_color: "#0d9488", icon_letter: "NLP",
  },
  {
    id: "connectors-io/iban-validator",
    name: "IBAN Validator & BIC Lookup",
    description: "Real-time IBAN validation and BIC/SWIFT lookup for payment form fields. Supports 77 countries.",
    long_description: "A lightweight connector for validating IBANs and looking up BIC/SWIFT codes in payment forms. Integrates as a connector-backed dropdown or validation rule in any form field.\n\nFeatures:\n• Real-time IBAN format and checksum validation (ISO 13616)\n• BIC/SWIFT lookup by bank and branch code\n• 77-country coverage (all SEPA + major non-SEPA countries)\n• Offline fallback: local IBAN structure database for format validation without an API call\n\nPricing: $49/month for up to 50,000 lookups. Additional usage at $0.001/lookup. Contact seller for unlimited tier.",
    type: "connector", category: "Payments",
    publisher: "Connectors.io", publisher_tier: "community",
    version: "1.0.3", price: "paid", price_label: "$49/mo",
    contact_url: "https://connectors.io/iban-validator",
    rating: 4.4, installs: 120,
    tags: ["iban", "bic", "swift", "payments", "validation", "sepa"],
    outbound_domains: ["api.connectors.io"],
    min_platform_version: "1.0.0",
    updated_at: "2026-04-20",
    icon_color: "#3b82f6", icon_letter: "IB",
  },
];

// ── Helpers ───────────────────────────────────────────────────────────────────

// Fallback type registry used before API responds.
// Overridden by data from GET /marketplace/types (types.json in platform).
const FALLBACK_TYPE_META: Record<string, { label: string; color: string }> = {
  connector:      { label: "Connector",      color: "#3b82f6" },
  case_template:  { label: "Case Template",  color: "#22c55e" },
  module:         { label: "Module",         color: "#8b5cf6" },
  nlp_pack:       { label: "NLP Pack",       color: "#0d9488" },
  portal_theme:   { label: "Portal Theme",   color: "#f97316" },
  bundle:         { label: "Bundle",         color: "#ec4899" },
};

// Runtime type map — populated from API, falls back to FALLBACK_TYPE_META
let _typeMeta: Record<string, { label: string; color: string }> = { ...FALLBACK_TYPE_META };

function fmtInstalls(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

function Stars({ rating }: { rating: number }) {
  return (
    <span style={{ fontSize: 11, color: "#f59e0b", letterSpacing: 1 }}>
      {"★".repeat(Math.round(rating))}{"☆".repeat(5 - Math.round(rating))}
      <span style={{ color: "var(--text-muted)", fontWeight: 400, marginLeft: 4 }}>{rating.toFixed(1)}</span>
    </span>
  );
}

function TypeBadge({ type }: { type: PackageType }) {
  const m = _typeMeta[type] ?? { label: type, color: "#94a3b8" };
  return (
    <span style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.06em",
      color: m.color, background: `${m.color}1a`, padding: "2px 7px", borderRadius: 4, fontFamily: "var(--font-mono)" }}>
      {m.label}
    </span>
  );
}

function TierBadge({ tier }: { tier: PublisherTier }) {
  return tier === "official"
    ? <span style={{ fontSize: 10, fontWeight: 700, color: "#0d9488", background: "#0d94881a",
        padding: "2px 7px", borderRadius: 4, display: "inline-flex", alignItems: "center", gap: 3 }}>
        <svg width="9" height="9" viewBox="0 0 9 9"><path d="M1.5 4.5L3.5 6.5L7.5 2.5" stroke="#0d9488" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" fill="none"/></svg>
        Official
      </span>
    : <span style={{ fontSize: 10, fontWeight: 600, color: "var(--text-muted)", background: "var(--bg-subtle)",
        padding: "2px 7px", borderRadius: 4 }}>Community</span>;
}

function PkgIcon({ pkg, size = 56 }: { pkg: MktPackage; size?: number }) {
  const textSize = size < 40 ? 11 : size < 56 ? 14 : 18;
  return (
    <div style={{ width: size, height: size, borderRadius: size * 0.22, background: pkg.icon_color,
      display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0,
      boxShadow: "0 2px 8px rgba(0,0,0,.25)" }}>
      <span style={{ fontSize: textSize, fontWeight: 700, color: "#fff", letterSpacing: "-0.02em",
        fontFamily: "var(--font-mono)", userSelect: "none" }}>
        {pkg.icon_letter}
      </span>
    </div>
  );
}

// ── Styles ────────────────────────────────────────────────────────────────────

const S: Record<string, React.CSSProperties> = {
  page:      { display: "flex", flexDirection: "column", height: "100%", overflow: "hidden", background: "var(--bg-root)" },
  tabBar:    { display: "flex", gap: 2, borderBottom: "1px solid var(--border-subtle)", padding: "0 var(--space-2xl)", flexShrink: 0 },
  tab:       { padding: "10px 16px", fontSize: 12, fontWeight: 500, fontFamily: "var(--font-mono)",
                textTransform: "uppercase" as const, letterSpacing: "0.04em", border: "none", cursor: "pointer",
                color: "var(--text-muted)", background: "transparent", borderBottom: "2px solid transparent", marginBottom: -1 },
  tabA:      { color: "var(--accent)", borderBottomColor: "var(--accent)" },
  body:      { flex: 1, overflow: "auto", display: "flex" },
  main:      { flex: 1, overflow: "auto", padding: "var(--space-xl) var(--space-2xl)" },
  searchRow: { display: "flex", gap: 10, marginBottom: "var(--space-md)", alignItems: "center" },
  search:    { flex: 1, padding: "9px 14px", border: "1px solid var(--border-default)",
                borderRadius: "var(--radius-md)", fontSize: 13, background: "var(--bg-input)",
                color: "var(--text-primary)", outline: "none" },
  filterRow: { display: "flex", gap: 6, marginBottom: "var(--space-lg)", flexWrap: "wrap" as const },
  chip:      { padding: "5px 14px", fontSize: 12, fontWeight: 500, border: "1px solid var(--border-default)",
                borderRadius: 20, cursor: "pointer", background: "transparent", color: "var(--text-secondary)" },
  chipA:     { background: "var(--accent)", borderColor: "var(--accent)", color: "#fff" },
  sectionHd: { fontSize: 11, fontWeight: 700, textTransform: "uppercase" as const, letterSpacing: "0.08em",
                color: "var(--text-muted)", marginBottom: "var(--space-md)", fontFamily: "var(--font-mono)" },
  grid:      { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))", gap: 16, marginBottom: "var(--space-2xl)" },
  card:      { background: "var(--bg-card)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-lg)",
                padding: 20, cursor: "pointer", transition: "border-color .15s, box-shadow .15s", display: "flex", flexDirection: "column", gap: 12 },
  cardHd:    { display: "flex", gap: 14, alignItems: "flex-start" },
  cardMeta:  { flex: 1, minWidth: 0 },
  cardName:  { fontSize: 14, fontWeight: 700, color: "var(--text-primary)", marginBottom: 2, lineHeight: 1.3,
                overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" as const },
  cardPub:   { fontSize: 11, color: "var(--text-muted)", marginBottom: 6 },
  cardDesc:  { fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.5,
                display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical" as const, overflow: "hidden" },
  cardFoot:  { display: "flex", alignItems: "center", justifyContent: "space-between", marginTop: "auto", gap: 8 },
  btn:       { padding: "7px 16px", border: "none", borderRadius: "var(--radius-sm)", fontSize: 12,
                fontWeight: 700, cursor: "pointer", background: "var(--accent)", color: "#fff", flexShrink: 0 },
  btnGhost:  { background: "transparent", border: "1px solid var(--border-default)", color: "var(--text-secondary)" },
  // Detail panel
  panel:     { width: 420, flexShrink: 0, borderLeft: "1px solid var(--border-subtle)", display: "flex",
                flexDirection: "column", overflow: "hidden", background: "var(--bg-panel)" },
  panelHd:   { padding: "20px 24px 16px", borderBottom: "1px solid var(--border-subtle)", flexShrink: 0 },
  panelBody: { flex: 1, overflow: "auto", padding: "24px" },
  panelFoot: { padding: "16px 24px", borderTop: "1px solid var(--border-subtle)", flexShrink: 0 },
  closeBtn:  { padding: "4px 10px", border: "1px solid var(--border-subtle)", borderRadius: 6,
                background: "transparent", color: "var(--text-muted)", cursor: "pointer", fontSize: 18, lineHeight: 1 },
  tag:       { fontSize: 10, padding: "3px 8px", borderRadius: 4, background: "var(--bg-subtle)",
                color: "var(--text-muted)", fontFamily: "var(--font-mono)" },
  domain:    { fontSize: 11, padding: "4px 10px", borderRadius: 4, background: "#3b82f61a",
                color: "#3b82f6", fontFamily: "var(--font-mono)", border: "1px solid #3b82f630" },
  priceBig:  { fontSize: 22, fontWeight: 800, color: "var(--text-primary)" },
  infoRow:   { display: "flex", justifyContent: "space-between", borderBottom: "1px solid var(--border-subtle)", padding: "8px 0", fontSize: 12 },
  // Empty / placeholder
  empty:     { textAlign: "center" as const, padding: "80px 40px", color: "var(--text-muted)" },
  placeholderCard: { background: "var(--bg-card)", border: "2px dashed var(--border-subtle)", borderRadius: "var(--radius-lg)",
                      padding: 32, textAlign: "center" as const, color: "var(--text-muted)" },
};

// ── PackageCard ───────────────────────────────────────────────────────────────

function PackageCard({ pkg, onSelect, onInstall, canInstall, installed, requested, selected }:
  { pkg: MktPackage; onSelect: () => void; onInstall: (pkg: MktPackage) => void;
    canInstall: boolean; installed: boolean; requested: boolean; selected: boolean }) {
  const doneBtn = (color: string): React.CSSProperties => ({
    ...S.btn, width: "100%", padding: "8px 0", background: "var(--bg-subtle)",
    color, cursor: "default",
  });
  return (
    <div
      style={{ ...S.card, borderColor: selected ? "var(--accent)" : "var(--border-subtle)",
        boxShadow: selected ? "0 0 0 2px var(--accent)30" : undefined }}
      onClick={onSelect}
    >
      <div style={S.cardHd}>
        <PkgIcon pkg={pkg} size={52} />
        <div style={S.cardMeta}>
          <div style={S.cardName}>{pkg.name}</div>
          <div style={S.cardPub}>{pkg.publisher}</div>
          <TierBadge tier={pkg.publisher_tier} />
        </div>
      </div>

      <div style={S.cardDesc}>{pkg.description}</div>

      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        <TypeBadge type={pkg.type} />
        {pkg.price === "paid"
          ? <span style={{ fontSize: 10, fontWeight: 700, color: "#f59e0b", background: "#f59e0b1a",
              padding: "2px 7px", borderRadius: 4 }}>{pkg.price_label}</span>
          : <span style={{ fontSize: 10, fontWeight: 700, color: "#22c55e", background: "#22c55e1a",
              padding: "2px 7px", borderRadius: 4 }}>FREE</span>
        }
      </div>

      <div style={{ marginTop: "auto", display: "flex", flexDirection: "column", gap: 8 }}>
        {/* Rating + installs row */}
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <Stars rating={pkg.rating} />
          <span style={{ fontSize: 11, color: "var(--text-muted)" }}>{fmtInstalls(pkg.installs)} installs</span>
        </div>
        {/* Action button — full width so any label length fits */}
        {canInstall && pkg.publisher_tier === "official" && (
          installed
            ? <button disabled style={doneBtn("var(--text-muted)")}>✓ Installed</button>
            : requested
              ? <button disabled style={doneBtn("#0d9488")}>✓ Requested for Release</button>
              : <button style={{ ...S.btn, background: "#0d9488", width: "100%", padding: "8px 0" }}
                  onClick={e => { e.stopPropagation(); onInstall(pkg); }}>
                  Request Release
                </button>
        )}
        {canInstall && pkg.publisher_tier !== "official" && (
          installed
            ? <button disabled style={doneBtn("var(--text-muted)")}>✓ Installed</button>
            : <button style={{ ...S.btn, width: "100%", padding: "8px 0" }}
                onClick={e => { e.stopPropagation(); onInstall(pkg); }}>
                {pkg.price === "paid" ? "Get" : "Install"}
              </button>
        )}
      </div>
    </div>
  );
}

// ── DetailPanel ───────────────────────────────────────────────────────────────

function DetailPanel({ pkg, onClose, onInstall, canInstall, installed, requested, isManager }:
  { pkg: MktPackage; onClose: () => void; onInstall: (pkg: MktPackage) => void;
    canInstall: boolean; installed: boolean; requested: boolean; isManager: boolean }) {
  const doneFootBtn = (color: string): React.CSSProperties => ({
    ...S.btn, width: "100%", padding: "11px 0", fontSize: 13,
    background: "var(--bg-subtle)", color, cursor: "default",
  });
  return (
    <div style={S.panel}>
      {/* Header */}
      <div style={S.panelHd}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
          <div style={{ display: "flex", gap: 16, alignItems: "center" }}>
            <PkgIcon pkg={pkg} size={64} />
            <div>
              <div style={{ fontSize: 17, fontWeight: 800, color: "var(--text-primary)", marginBottom: 4 }}>{pkg.name}</div>
              <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 6 }}>{pkg.publisher} · v{pkg.version}</div>
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                <TierBadge tier={pkg.publisher_tier} />
                <TypeBadge type={pkg.type} />
              </div>
            </div>
          </div>
          <button style={S.closeBtn} onClick={onClose}>×</button>
        </div>
      </div>

      {/* Body */}
      <div style={S.panelBody}>
        {/* Ratings row */}
        <div style={{ display: "flex", gap: 24, marginBottom: 20 }}>
          <div style={{ textAlign: "center" }}>
            <div style={{ fontSize: 22, fontWeight: 800, color: "var(--text-primary)" }}>{pkg.rating.toFixed(1)}</div>
            <Stars rating={pkg.rating} />
            <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 2 }}>Rating</div>
          </div>
          <div style={{ textAlign: "center" }}>
            <div style={{ fontSize: 22, fontWeight: 800, color: "var(--text-primary)" }}>{fmtInstalls(pkg.installs)}</div>
            <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 2 }}>Installs</div>
          </div>
          <div style={{ textAlign: "center" }}>
            <div style={{ fontSize: 22, fontWeight: 800, color: "var(--text-primary)" }}>
              {pkg.price === "free" ? "Free" : pkg.price_label}
            </div>
            <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 2 }}>Price</div>
          </div>
        </div>

        {/* Description */}
        <div style={{ fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.7, marginBottom: 20,
          whiteSpace: "pre-line" }}>
          {pkg.long_description}
        </div>

        {/* Meta info */}
        <div style={{ marginBottom: 20 }}>
          <div style={S.infoRow}>
            <span style={{ color: "var(--text-muted)" }}>Category</span>
            <span style={{ color: "var(--text-primary)", fontWeight: 600 }}>{pkg.category}</span>
          </div>
          <div style={S.infoRow}>
            <span style={{ color: "var(--text-muted)" }}>Min platform version</span>
            <span style={{ color: "var(--text-primary)", fontFamily: "var(--font-mono)", fontSize: 11 }}>{pkg.min_platform_version}</span>
          </div>
          <div style={S.infoRow}>
            <span style={{ color: "var(--text-muted)" }}>Updated</span>
            <span style={{ color: "var(--text-primary)" }}>{pkg.updated_at}</span>
          </div>
        </div>

        {/* Tags */}
        {pkg.tags.length > 0 && (
          <div style={{ marginBottom: 20 }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>Tags</div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {pkg.tags.map(t => <span key={t} style={S.tag}>{t}</span>)}
            </div>
          </div>
        )}

        {/* Security disclosure — outbound domains */}
        <div style={{ marginBottom: 20 }}>
          <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", marginBottom: 8, textTransform: "uppercase", letterSpacing: "0.06em" }}>
            Outbound Domains
          </div>
          {pkg.outbound_domains.length === 0
            ? <span style={{ fontSize: 12, color: "var(--text-muted)" }}>None — this package makes no external calls.</span>
            : <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                {pkg.outbound_domains.map(d => <span key={d} style={S.domain}>{d}</span>)}
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
                  All outbound calls are blocked in sandbox until admin-approved per domain.
                </div>
              </div>
          }
        </div>

        {/* Paid — contact seller */}
        {pkg.price === "paid" && pkg.contact_url && (
          <div style={{ background: "#f59e0b12", border: "1px solid #f59e0b30", borderRadius: "var(--radius-md)", padding: 14, marginBottom: 16 }}>
            <div style={{ fontSize: 12, fontWeight: 600, color: "#f59e0b", marginBottom: 4 }}>Paid Package</div>
            <div style={{ fontSize: 12, color: "var(--text-secondary)", lineHeight: 1.5 }}>
              Contact the seller to purchase a licence key, then enter it during install.
            </div>
            <a href={pkg.contact_url} target="_blank" rel="noreferrer"
              style={{ display: "inline-block", marginTop: 10, fontSize: 12, color: "var(--accent)", fontWeight: 600 }}>
              Contact seller →
            </a>
          </div>
        )}

        {/* Manager read-only notice */}
        {isManager && (
          <div style={{ background: "var(--bg-subtle)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md)", padding: 14, fontSize: 12, color: "var(--text-secondary)" }}>
            Ask a Developer or Admin to install this package into a sandbox workspace.
          </div>
        )}
      </div>

      {/* Footer — install button */}
      {canInstall && (
        <div style={S.panelFoot}>
          {pkg.publisher_tier === "official"
            ? <>
                {installed
                  ? <button disabled style={doneFootBtn("var(--text-muted)")}>✓ Installed</button>
                  : requested
                    ? <button disabled style={doneFootBtn("#0d9488")}>✓ Requested for Release</button>
                    : <button style={{ ...S.btn, width: "100%", padding: "11px 0", fontSize: 13, background: "#0d9488" }}
                        onClick={() => onInstall(pkg)}>
                        Request for Next Release
                      </button>
                }
                <div style={{ fontSize: 11, color: "var(--text-muted)", textAlign: "center", marginTop: 8 }}>
                  {requested && !installed
                    ? "Pending admin approval in the Review Queue. Once approved it is installed for your tenant."
                    : "Official packages are flagged for the next release cycle. An admin approves them in the Review Queue, which installs them for this tenant."}
                </div>
              </>
            : <>
                {installed
                  ? <button disabled style={doneFootBtn("var(--text-muted)")}>✓ Installed</button>
                  : <button style={{ ...S.btn, width: "100%", padding: "11px 0", fontSize: 13 }}
                      onClick={() => onInstall(pkg)}>
                      {pkg.price === "paid" ? "Enter Licence Key & Install to Sandbox" : "Install to Sandbox"}
                    </button>
                }
                <div style={{ fontSize: 11, color: "var(--text-muted)", textAlign: "center", marginTop: 8 }}>
                  Installs into an isolated sandbox workspace. Admin approval required before production.
                </div>
              </>
          }
        </div>
      )}
    </div>
  );
}

// ── InstallModal ──────────────────────────────────────────────────────────────

function InstallModal({ pkg, onClose, onConfirm }:
  { pkg: MktPackage; onClose: () => void; onConfirm: (workspaceName: string, licenceKey?: string) => void }) {
  const [licenceKey, setLicenceKey] = useState("");
  const [workspaceName, setWorkspaceName] = useState(`${pkg.name} Sandbox`);

  return (
    <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.55)", zIndex: 1000,
      display: "flex", alignItems: "center", justifyContent: "center" }}>
      <div style={{ background: "var(--bg-panel)", border: "1px solid var(--border-default)",
        borderRadius: "var(--radius-lg)", padding: 28, width: 440, maxWidth: "90vw" }}>
        <div style={{ display: "flex", gap: 14, alignItems: "center", marginBottom: 20 }}>
          <PkgIcon pkg={pkg} size={44} />
          <div>
            <div style={{ fontSize: 15, fontWeight: 700, color: "var(--text-primary)" }}>Install to Sandbox</div>
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>{pkg.name} · v{pkg.version}</div>
          </div>
        </div>

        <div style={{ marginBottom: 14 }}>
          <label style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", display: "block",
            textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 4 }}>Workspace Name</label>
          <input style={{ ...S.search, width: "100%", boxSizing: "border-box" as const }}
            value={workspaceName} onChange={e => setWorkspaceName(e.target.value)} />
        </div>

        {pkg.price === "paid" && (
          <div style={{ marginBottom: 14 }}>
            <label style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", display: "block",
              textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 4 }}>Licence Key</label>
            <input style={{ ...S.search, width: "100%", boxSizing: "border-box" as const, fontFamily: "var(--font-mono)", fontSize: 12 }}
              placeholder="Paste your licence key here…"
              value={licenceKey} onChange={e => setLicenceKey(e.target.value)} />
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>
              Obtain from the seller. Velaris stores this key but does not validate it externally.
            </div>
          </div>
        )}

        <div style={{ background: "#f59e0b12", border: "1px solid #f59e0b30", borderRadius: "var(--radius-sm)",
          padding: "10px 14px", marginBottom: 20, fontSize: 12, color: "var(--text-secondary)" }}>
          The package will be installed into an isolated sandbox container with no access to production data.
          Admin approval is required before it can be promoted to production.
        </div>

        <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
          <button style={{ ...S.btn, ...S.btnGhost }} onClick={onClose}>Cancel</button>
          <button style={S.btn}
            disabled={pkg.price === "paid" && !licenceKey.trim()}
            onClick={() => onConfirm(workspaceName, licenceKey || undefined)}>
            Create Sandbox & Install
          </button>
        </div>
      </div>
    </div>
  );
}

// ── BrowseTab ─────────────────────────────────────────────────────────────────

const PAGE_SIZE = 12;

type TagCategory = { label: string; tags: string[] };

// Smart search — scores each package against query, returns sorted by relevance
function scorePackage(pkg: MktPackage, q: string): number {
  if (!q) return 1;
  const ql = q.toLowerCase();
  let score = 0;
  const name = pkg.name.toLowerCase();
  if (name === ql)               score += 100;
  else if (name.startsWith(ql))  score += 60;
  else if (name.includes(ql))    score += 40;
  if (pkg.tags.some(t => t === ql))          score += 50;
  else if (pkg.tags.some(t => t.includes(ql))) score += 25;
  if (pkg.publisher.toLowerCase().includes(ql)) score += 15;
  if (pkg.category.toLowerCase().includes(ql))  score += 10;
  if (pkg.description.toLowerCase().includes(ql)) score += 8;
  return score;
}

function BrowseTab({ canInstall, isManager }: { canInstall: boolean; isManager: boolean }) {
  const [query,        setQuery]        = useState("");
  const [typeFilter,   setTypeFilter]   = useState<PackageType | "all">("all");
  const [tierFilter,   setTierFilter]   = useState<PublisherTier | "all">("all");
  const [activeTags,   setActiveTags]   = useState<Set<string>>(new Set());
  const [expandedCats, setExpandedCats] = useState<Set<string>>(new Set());
  const [page,         setPage]         = useState(1);
  const [selected,     setSelected]     = useState<MktPackage | null>(null);
  const [installing,   setInstalling]   = useState<MktPackage | null>(null);
  const [installedIds, setInstalledIds] = useState<Set<string>>(new Set());
  const [requestedIds, setRequestedIds] = useState<Set<string>>(new Set());
  const [packages,      setPackages]      = useState<MktPackage[]>(MOCK_PACKAGES);
  const [tagCategories, setTagCategories] = useState<TagCategory[]>([]);
  const [typeCategories,setTypeCategories]= useState<MktTypeCategory[]>([]);
  const [typeFilters,   setTypeFilters]   = useState<{ key: string; label: string }[]>(
    [{ key: "all", label: "All" }, ...Object.entries(FALLBACK_TYPE_META).map(([k, v]) => ({ key: k, label: v.label }))]
  );
  const [loading,       setLoading]       = useState(true);
  const [apiError,      setApiError]      = useState(false);

  React.useEffect(() => {
    // Fetch packages, tag taxonomy, and type registry in parallel
    Promise.all([
      apiFetch("/packages").then(r => r.json()).catch(() => null),
      apiFetch("/tags").then(r => r.json()).catch(() => null),
      apiFetch("/types").then(r => r.json()).catch(() => null),
      // Seed button state so already-requested / already-installed packages persist
      // across a page refresh (both endpoints are admin-only → null for other roles).
      apiFetch("/release-requests").then(r => r.ok ? r.json() : null).catch(() => null),
      apiFetch("/installs").then(r => r.ok ? r.json() : null).catch(() => null),
    ]).then(([pkgData, tagData, typeData, rrData, instData]) => {
      if (pkgData?.packages?.length) setPackages(pkgData.packages);
      else setPackages(MOCK_PACKAGES);
      setApiError(!pkgData);

      if (rrData?.requests?.length)
        setRequestedIds(new Set(rrData.requests.map((r: any) => r.package_id)));
      if (instData?.installs?.length)
        setInstalledIds(prev => {
          const next = new Set(prev);
          instData.installs.forEach((i: any) => next.add(i.package_id));
          return next;
        });

      if (tagData?.categories) {
        const cats: TagCategory[] = Object.entries(tagData.categories).map(
          ([label, cat]: [string, any]) => ({ label, tags: cat.tags ?? [] })
        );
        setTagCategories(cats);
      }

      if (typeData?.categories?.length) {
        const cats: MktTypeCategory[] = typeData.categories;
        setTypeCategories(cats);
        // Update global type meta lookup
        const newMeta: Record<string, { label: string; color: string }> = {};
        cats.forEach(cat => cat.types.forEach(t => { newMeta[t.id] = { label: t.label, color: t.color }; }));
        _typeMeta = { ...FALLBACK_TYPE_META, ...newMeta };
        // Build flat filter list: All + each type across all categories
        const allTypes = cats.flatMap(c => c.types);
        setTypeFilters([
          { key: "all", label: "All" },
          ...allTypes.map(t => ({ key: t.id, label: t.label })),
        ]);
      }
    }).finally(() => setLoading(false));
  }, []);

  const toggleTag = useCallback((tag: string) => {
    setActiveTags(prev => {
      const next = new Set(prev);
      next.has(tag) ? next.delete(tag) : next.add(tag);
      return next;
    });
    setPage(1);
  }, []);

  const toggleCat = useCallback((cat: string) => {
    setExpandedCats(prev => {
      const next = new Set(prev);
      next.has(cat) ? next.delete(cat) : next.add(cat);
      return next;
    });
  }, []);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    // Filter
    let result = packages.filter(p => {
      if (typeFilter !== "all" && p.type !== typeFilter) return false;
      if (tierFilter !== "all" && p.publisher_tier !== tierFilter) return false;
      if (activeTags.size > 0 && !p.tags.some(t => activeTags.has(t))) return false;
      return true;
    });
    // Smart search with scoring
    if (q) {
      result = result
        .map(p => ({ p, score: scorePackage(p, q) }))
        .filter(({ score }) => score > 0)
        .sort((a, b) => b.score - a.score)
        .map(({ p }) => p);
    }
    return result;
  }, [query, typeFilter, tierFilter, activeTags, packages]);

  // Reset page when filters change
  React.useEffect(() => { setPage(1); }, [query, typeFilter, tierFilter, activeTags]);

  const totalPages = Math.ceil(filtered.length / PAGE_SIZE);
  const paginated  = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  const official   = paginated.filter(p => p.publisher_tier === "official");
  const community  = paginated.filter(p => p.publisher_tier === "community");

  const handleInstallConfirm = async (workspaceName: string, licenceKey?: string) => {
    if (!installing) return;
    try {
      if (installing.publisher_tier === "official") {
        // Official packages: flag for next release cycle — no sandbox
        await apiFetch(`/packages/${encodeURIComponent(installing.id)}/request-release`, { method: "POST" });
        setRequestedIds(prev => new Set(prev).add(installing.id));
      } else {
        // Community packages: create sandbox workspace + install
        const wsRes = await apiFetch("/workspaces", {
          method: "POST",
          body: JSON.stringify({ name: workspaceName }),
        });
        if (!wsRes.ok) {
          const err = await wsRes.json().catch(() => ({}));
          throw new Error(err.detail ?? "Failed to create workspace");
        }
        const ws = await wsRes.json();
        const installRes = await apiFetch(`/workspaces/${ws.id}/install`, {
          method: "POST",
          body: JSON.stringify({ package_id: installing.id, licence_key: licenceKey || null }),
        });
        if (!installRes.ok) throw new Error("Failed to install package");
        setInstalledIds(prev => new Set(prev).add(installing.id));
      }
    } catch (err: any) {
      alert(err?.message ?? "Install failed. Check workspace limit or try again.");
    } finally {
      setInstalling(null);
    }
  };

  const activeTagList = [...activeTags];

  return (
    <div style={{ ...S.body, flexDirection: "column" }}>

      {/* ── Main area: sidebar + (search+content) ── */}
      <div style={{ display: "flex", flex: 1, overflow: "hidden" }}>

        {/* Sidebar */}
        <div style={{ width: 220, flexShrink: 0, borderRight: "1px solid var(--border-subtle)", overflow: "auto", padding: "var(--space-lg) var(--space-md)" }}>

          {/* Type — grouped by category from types.json */}
          <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.08em", color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginBottom: 8 }}>Type</div>
          {/* All option */}
          <button onClick={() => { setTypeFilter("all"); setPage(1); }}
            style={{ display: "flex", alignItems: "center", gap: 8, width: "100%", padding: "6px 8px", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 12, fontWeight: typeFilter === "all" ? 700 : 400, background: typeFilter === "all" ? "var(--accent)15" : "transparent", color: typeFilter === "all" ? "var(--accent)" : "var(--text-secondary)", textAlign: "left" as const, marginBottom: 4 }}>
            {typeFilter === "all" && <span style={{ width: 4, height: 4, borderRadius: "50%", background: "var(--accent)", flexShrink: 0 }} />}
            All
          </button>
          {/* Grouped by category */}
          {typeCategories.length > 0
            ? typeCategories.map(cat => (
                <div key={cat.label} style={{ marginBottom: 8 }}>
                  <div style={{ fontSize: 9, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.07em", color: "var(--text-muted)", fontFamily: "var(--font-mono)", padding: "4px 8px 2px" }}>{cat.label}</div>
                  {cat.types.map(t => (
                    <button key={t.id}
                      onClick={() => { setTypeFilter(t.id); setPage(1); }}
                      style={{ display: "flex", alignItems: "center", gap: 8, width: "100%", padding: "5px 8px", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 11, fontWeight: typeFilter === t.id ? 700 : 400, background: typeFilter === t.id ? `${t.color}18` : "transparent", color: typeFilter === t.id ? t.color : "var(--text-secondary)", textAlign: "left" as const, marginBottom: 1 }}>
                      <span style={{ width: 6, height: 6, borderRadius: 2, background: t.color, flexShrink: 0, opacity: typeFilter === t.id ? 1 : 0.4 }} />
                      {t.label}
                    </button>
                  ))}
                </div>
              ))
            : /* Fallback flat list while loading */
              typeFilters.filter(f => f.key !== "all").map(f => (
                <button key={f.key}
                  onClick={() => { setTypeFilter(f.key); setPage(1); }}
                  style={{ display: "flex", alignItems: "center", gap: 8, width: "100%", padding: "6px 8px", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 12, fontWeight: typeFilter === f.key ? 700 : 400, background: typeFilter === f.key ? "var(--accent)15" : "transparent", color: typeFilter === f.key ? "var(--accent)" : "var(--text-secondary)", textAlign: "left" as const, marginBottom: 2 }}>
                  {typeFilter === f.key && <span style={{ width: 4, height: 4, borderRadius: "50%", background: "var(--accent)", flexShrink: 0 }} />}
                  {f.label}
                </button>
              ))
          }

          {/* Publisher */}
          <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.08em", color: "var(--text-muted)", fontFamily: "var(--font-mono)", margin: "16px 0 8px" }}>Publisher</div>
          {(["all", "official", "community"] as const).map(tier => (
            <button key={tier}
              onClick={() => { setTierFilter(tier); setPage(1); }}
              style={{ display: "flex", alignItems: "center", gap: 8, width: "100%", padding: "6px 8px", border: "none", borderRadius: 6, cursor: "pointer", fontSize: 12, fontWeight: tierFilter === tier ? 700 : 400, background: tierFilter === tier ? "var(--accent)15" : "transparent", color: tierFilter === tier ? "var(--accent)" : "var(--text-secondary)", textAlign: "left" as const, marginBottom: 2, textTransform: "capitalize" as const }}>
              {tierFilter === tier && <span style={{ width: 4, height: 4, borderRadius: "50%", background: "var(--accent)", flexShrink: 0 }} />}
              {tier === "all" ? "All" : tier === "official" ? "✓ Official" : "Community"}
            </button>
          ))}

          {/* Tag categories — from tags.json via API */}
          {tagCategories.length > 0 && (
            <>
              <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.08em", color: "var(--text-muted)", fontFamily: "var(--font-mono)", margin: "16px 0 8px" }}>Tags</div>
              {tagCategories.map(cat => {
                const isOpen = expandedCats.has(cat.label);
                const activeCatTags = cat.tags.filter(t => activeTags.has(t));
                return (
                  <div key={cat.label} style={{ marginBottom: 4 }}>
                    <button onClick={() => toggleCat(cat.label)}
                      style={{ display: "flex", alignItems: "center", justifyContent: "space-between", width: "100%", padding: "5px 8px", border: "none", background: "transparent", cursor: "pointer", fontSize: 11, fontWeight: 600, color: activeCatTags.length > 0 ? "var(--accent)" : "var(--text-secondary)", textAlign: "left" as const, borderRadius: 4 }}>
                      <span>{cat.label} {activeCatTags.length > 0 && `(${activeCatTags.length})`}</span>
                      <span style={{ fontSize: 9, color: "var(--text-muted)" }}>{isOpen ? "▲" : "▼"}</span>
                    </button>
                    {isOpen && (
                      <div style={{ paddingLeft: 8, paddingTop: 4 }}>
                        {cat.tags.map(tag => (
                          <button key={tag} onClick={() => toggleTag(tag)}
                            style={{ display: "block", width: "100%", padding: "3px 6px", border: "none", borderRadius: 4, cursor: "pointer", fontSize: 11, textAlign: "left" as const, marginBottom: 1, background: activeTags.has(tag) ? "var(--accent)" : "transparent", color: activeTags.has(tag) ? "#fff" : "var(--text-muted)", fontWeight: activeTags.has(tag) ? 600 : 400 }}>
                            {tag}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </>
          )}
        </div>

        {/* Content column: search row + results */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>

          {/* ── Search + Publish CTA row ── */}
          <div style={{ display: "flex", gap: 16, alignItems: "flex-start", padding: "var(--space-lg) var(--space-xl) 0", flexShrink: 0 }}>

            {/* Search */}
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ position: "relative" }}>
                <svg style={{ position: "absolute", left: 12, top: "50%", transform: "translateY(-50%)", pointerEvents: "none" }}
                  width="14" height="14" viewBox="0 0 16 16" fill="none">
                  <circle cx="7" cy="7" r="5" stroke="var(--text-muted)" strokeWidth="1.5"/>
                  <path d="M11 11L14 14" stroke="var(--text-muted)" strokeWidth="1.5" strokeLinecap="round"/>
                </svg>
                <input
                  style={{ ...S.search, paddingLeft: 34, fontSize: 14, width: "100%", boxSizing: "border-box" as const }}
                  placeholder="Search by name, tag, publisher, description…"
                  value={query}
                  onChange={e => { setQuery(e.target.value); setPage(1); }}
                />
                {query && (
                  <button onClick={() => setQuery("")}
                    style={{ position: "absolute", right: 10, top: "50%", transform: "translateY(-50%)", background: "none", border: "none", cursor: "pointer", color: "var(--text-muted)", fontSize: 16, lineHeight: 1 }}>
                    ×
                  </button>
                )}
              </div>
              {/* Active tag pills */}
              {activeTagList.length > 0 && (
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 8 }}>
                  {activeTagList.map(t => (
                    <span key={t} style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11, padding: "3px 8px", borderRadius: 4, background: "var(--accent)", color: "#fff", fontWeight: 600 }}>
                      {t}
                      <button onClick={() => toggleTag(t)} style={{ background: "none", border: "none", cursor: "pointer", color: "#fff", fontSize: 13, lineHeight: 1, padding: 0 }}>×</button>
                    </span>
                  ))}
                  <button onClick={() => setActiveTags(new Set())}
                    style={{ fontSize: 11, color: "var(--text-muted)", background: "none", border: "1px solid var(--border-default)", borderRadius: 4, padding: "3px 8px", cursor: "pointer" }}>
                    Clear all
                  </button>
                </div>
              )}
            </div>

            {/* Compact Publish CTA — same row as search, fixed width */}
            <div style={{ width: 240, flexShrink: 0, background: "var(--bg-card)", border: "1px solid var(--border-subtle)", borderRadius: "var(--radius-md)", padding: "12px 16px" }}>
              <div style={{ fontSize: 13, fontWeight: 700, color: "var(--text-primary)", marginBottom: 4 }}>📦 Publish a Package</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)", lineHeight: 1.5, marginBottom: 8 }}>
                Host in your own repo, submit one URL to the index.
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                <a href="https://github.com/Velaris-App-OS/Marketplace/tree/main/community" target="_blank" rel="noreferrer"
                  style={{ fontSize: 11, color: "var(--accent)", fontWeight: 600, textDecoration: "none" }}>
                  Submit your source URL →
                </a>
                <a href="https://github.com/Velaris-App-OS/Marketplace/blob/main/community/APP_TEMPLATE.md" target="_blank" rel="noreferrer"
                  style={{ fontSize: 11, color: "var(--text-muted)", textDecoration: "none" }}>
                  Publishing guide
                </a>
              </div>
            </div>
          </div>

          {/* Content area */}
          <div style={{ flex: 1, overflow: "auto", padding: "var(--space-md) var(--space-xl)" }}>
          {/* Results count */}
          <div style={{ marginBottom: "var(--space-md)" }}>
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
              {loading ? "Loading…" : `${filtered.length} package${filtered.length !== 1 ? "s" : ""}${query || activeTags.size > 0 ? " found" : ""}`}
            </div>
          </div>

          {loading && <div style={S.empty}><div style={{ fontSize: 13, color: "var(--text-muted)" }}>Loading marketplace…</div></div>}

          {!loading && apiError && (
            <div style={{ background: "#f59e0b12", border: "1px solid #f59e0b30", borderRadius: "var(--radius-md)", padding: "10px 16px", marginBottom: "var(--space-md)", fontSize: 12, color: "var(--text-secondary)" }}>
              Could not reach the marketplace registry. Showing cached packages.
            </div>
          )}

          {!loading && filtered.length === 0 && (
            <div style={S.empty}>
              <div style={{ fontSize: 32, marginBottom: 12 }}>🔍</div>
              <div style={{ fontWeight: 600, marginBottom: 4 }}>No packages found</div>
              <div style={{ fontSize: 13 }}>Try adjusting your search, type filter, or tag selection.</div>
            </div>
          )}

          {/* When searching/filtering: flat sorted list. Default: Official → Community sections. */}
          {!loading && paginated.length > 0 && (() => {
            const isFiltered = query || activeTags.size > 0 || typeFilter !== "all" || tierFilter !== "all";
            if (isFiltered) {
              return (
                <div style={S.grid}>
                  {paginated.map(pkg => (
                    <PackageCard key={pkg.id} pkg={pkg}
                      selected={selected?.id === pkg.id}
                      onSelect={() => setSelected(s => s?.id === pkg.id ? null : pkg)}
                      onInstall={setInstalling}
                      canInstall={canInstall}
                      installed={installedIds.has(pkg.id)}
                      requested={requestedIds.has(pkg.id)} />
                  ))}
                </div>
              );
            }
            return (
              <>
                {official.length > 0 && (
                  <>
                    <div style={S.sectionHd}>✓ Official by Velaris · Free &amp; Open-Source</div>
                    <div style={S.grid}>
                      {official.map(pkg => (
                        <PackageCard key={pkg.id} pkg={pkg}
                          selected={selected?.id === pkg.id}
                          onSelect={() => setSelected(s => s?.id === pkg.id ? null : pkg)}
                          onInstall={setInstalling}
                          canInstall={canInstall}
                          installed={installedIds.has(pkg.id)}
                          requested={requestedIds.has(pkg.id)} />
                      ))}
                    </div>
                  </>
                )}
                {community.length > 0 && (
                  <>
                    <div style={S.sectionHd}>Community Packages</div>
                    <div style={S.grid}>
                      {community.map(pkg => (
                        <PackageCard key={pkg.id} pkg={pkg}
                          selected={selected?.id === pkg.id}
                          onSelect={() => setSelected(s => s?.id === pkg.id ? null : pkg)}
                          onInstall={setInstalling}
                          canInstall={canInstall}
                          installed={installedIds.has(pkg.id)}
                          requested={requestedIds.has(pkg.id)} />
                      ))}
                    </div>
                  </>
                )}
              </>
            );
          })()}

          {/* Pagination */}
          {totalPages > 1 && (
            <div style={{ display: "flex", justifyContent: "center", alignItems: "center", gap: 6, padding: "var(--space-lg) 0", flexWrap: "wrap" }}>
              <button
                disabled={page === 1}
                onClick={() => setPage(p => p - 1)}
                style={{ padding: "6px 12px", border: "1px solid var(--border-default)", borderRadius: 6, background: "transparent", cursor: page === 1 ? "default" : "pointer", color: page === 1 ? "var(--text-muted)" : "var(--text-secondary)", fontSize: 12 }}>
                ← Prev
              </button>
              {Array.from({ length: totalPages }, (_, i) => i + 1)
                .filter(n => n === 1 || n === totalPages || Math.abs(n - page) <= 2)
                .reduce<(number | "…")[]>((acc, n, i, arr) => {
                  if (i > 0 && n - (arr[i - 1] as number) > 1) acc.push("…");
                  acc.push(n);
                  return acc;
                }, [])
                .map((n, i) => n === "…"
                  ? <span key={`ellipsis-${i}`} style={{ fontSize: 12, color: "var(--text-muted)", padding: "0 4px" }}>…</span>
                  : <button key={n} onClick={() => setPage(n as number)}
                      style={{ padding: "6px 10px", border: "1px solid", borderRadius: 6, fontSize: 12, cursor: "pointer", minWidth: 34, fontWeight: page === n ? 700 : 400, background: page === n ? "var(--accent)" : "transparent", color: page === n ? "#fff" : "var(--text-secondary)", borderColor: page === n ? "var(--accent)" : "var(--border-default)" }}>
                      {n}
                    </button>
                )
              }
              <button
                disabled={page === totalPages}
                onClick={() => setPage(p => p + 1)}
                style={{ padding: "6px 12px", border: "1px solid var(--border-default)", borderRadius: 6, background: "transparent", cursor: page === totalPages ? "default" : "pointer", color: page === totalPages ? "var(--text-muted)" : "var(--text-secondary)", fontSize: 12 }}>
                Next →
              </button>
            </div>
          )}

          </div>{/* end content area */}
        </div>{/* end content column */}

        {/* Detail panel */}
        {selected && (
          <DetailPanel pkg={selected} onClose={() => setSelected(null)}
            onInstall={setInstalling} canInstall={canInstall}
            installed={installedIds.has(selected.id)} requested={requestedIds.has(selected.id)}
            isManager={isManager} />
        )}
      </div>

      {/* Install modal */}
      {installing && (
        <InstallModal pkg={installing} onClose={() => setInstalling(null)}
          onConfirm={handleInstallConfirm} />
      )}
    </div>
  );
}

// ── WorkspacesTab ─────────────────────────────────────────────────────────────

const WS_STATUS_COLOR: Record<string, string> = {
  active: "#3b82f6", submitted: "#f59e0b", approved: "#22c55e",
  rejected: "#ef4444", expired: "#94a3b8", destroyed: "#94a3b8",
};

function daysUntil(iso: string) {
  return Math.ceil((new Date(iso).getTime() - Date.now()) / 86_400_000);
}

function WorkspacesTab({ wsLimit = 2 }: { wsLimit?: number }) {
  const [workspaces, setWorkspaces] = useState<any[]>([]);
  const [loading,    setLoading]    = useState(true);
  const [expanded,   setExpanded]   = useState<string | null>(null);
  const [netLogs,    setNetLogs]    = useState<Record<string, any[]>>({});
  const [creating,   setCreating]   = useState(false);
  const [newName,    setNewName]    = useState("");
  const [submitting, setSubmitting] = useState<string | null>(null);
  const [submitNote, setSubmitNote] = useState("");
  const [showSubmit, setShowSubmit] = useState<string | null>(null);
  const [wlReq,      setWlReq]     = useState<{ wsId: string; domain: string; pkg: string } | null>(null);
  const [wlNote,     setWlNote]    = useState("");

  const load = () => {
    setLoading(true);
    apiFetch("/workspaces").then(r => r.json())
      .then(d => { setWorkspaces(d.workspaces ?? []); setLoading(false); })
      .catch(() => setLoading(false));
  };
  React.useEffect(load, []);

  const loadNetLog = async (wsId: string) => {
    if (netLogs[wsId]) return;
    const r = await apiFetch(`/workspaces/${wsId}/network-log`);
    if (r.ok) { const d = await r.json(); setNetLogs(p => ({ ...p, [wsId]: d.logs ?? [] })); }
  };

  const toggle = (id: string) => {
    const next = expanded === id ? null : id;
    setExpanded(next);
    if (next) loadNetLog(next);
  };

  const createWorkspace = async () => {
    if (!newName.trim()) return;
    await apiFetch("/workspaces", { method: "POST", body: JSON.stringify({ name: newName }) });
    setCreating(false); setNewName(""); load();
  };

  const deleteWs = async (id: string) => {
    await apiFetch(`/workspaces/${id}`, { method: "DELETE" }); load();
  };

  const submitForReview = async (id: string) => {
    setSubmitting(id);
    await apiFetch(`/workspaces/${id}/submit`, { method: "POST", body: JSON.stringify({ notes: submitNote }) });
    setSubmitting(null); setShowSubmit(null); setSubmitNote(""); load();
  };

  const requestWhitelist = async () => {
    if (!wlReq) return;
    await apiFetch(`/workspaces/${wlReq.wsId}/whitelist`, {
      method: "POST",
      body: JSON.stringify({ domain: wlReq.domain, package_id: wlReq.pkg, justification: wlNote }),
    });
    setWlReq(null); setWlNote("");
  };

  const enterSandbox = (id: string) => {
    localStorage.setItem("velaris_sandbox_workspace_id", id);
    window.dispatchEvent(new Event("storage"));
    window.location.reload();
  };

  return (
    <div style={{ flex: 1, padding: "var(--space-xl) var(--space-2xl)", overflow: "auto" }}>
      {/* Header */}
      {/* Workspace limit banner */}
      {(() => {
        const activeCount = workspaces.filter(w => w.status === "active" || w.status === "submitted").length;
        const atLimit = activeCount >= wsLimit;
        return atLimit ? (
          <div style={{ background: "#f59e0b12", border: "1px solid #f59e0b40", borderRadius: "var(--radius-md)",
            padding: "10px 16px", marginBottom: "var(--space-md)", fontSize: 12, color: "#f59e0b", fontWeight: 600 }}>
            ⚠ Workspace limit reached ({wsLimit} active). Delete or get one approved before creating a new one.
            Admins can raise this limit via <code>HELIX_CASE_MARKETPLACE_MAX_WORKSPACES_PER_USER</code>.
          </div>
        ) : null;
      })()}

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "var(--space-lg)" }}>
        <div>
          <div style={{ fontSize: 15, fontWeight: 700, color: "var(--text-primary)" }}>
            Sandbox Workspaces
            <span style={{ fontSize: 11, color: "var(--text-muted)", fontWeight: 400, marginLeft: 8 }}>
              {workspaces.filter(w => ["active","submitted"].includes(w.status)).length} / {wsLimit} active
            </span>
          </div>
          <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>
            Each workspace is an isolated container — zero production DB access, synthetic data only.
          </div>
        </div>
        <button style={{
          ...S.btn,
          opacity: workspaces.filter(w => ["active","submitted"].includes(w.status)).length >= wsLimit ? 0.4 : 1,
          cursor: workspaces.filter(w => ["active","submitted"].includes(w.status)).length >= wsLimit ? "not-allowed" : "pointer",
        }}
          onClick={() => {
            if (workspaces.filter(w => ["active","submitted"].includes(w.status)).length >= wsLimit) return;
            setCreating(v => !v);
          }}>
          + New Workspace
        </button>
      </div>

      {/* Create workspace form */}
      {creating && (
        <div style={{ ...S.card, marginBottom: "var(--space-lg)", border: "1px solid var(--accent)" }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", marginBottom: 10 }}>Create Sandbox Workspace</div>
          <input style={{ ...S.search, marginBottom: 10 }} placeholder="Workspace name…"
            value={newName} onChange={e => setNewName(e.target.value)}
            onKeyDown={e => e.key === "Enter" && createWorkspace()} />
          <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 10 }}>
            An isolated container will be provisioned with synthetic data. Auto-expires in 30 days.
          </div>
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button style={{ ...S.btn, ...S.btnGhost }} onClick={() => setCreating(false)}>Cancel</button>
            <button style={S.btn} onClick={createWorkspace} disabled={!newName.trim()}>Create</button>
          </div>
        </div>
      )}

      {loading && <div style={S.empty}><div style={{ fontSize: 13, color: "var(--text-muted)" }}>Loading workspaces…</div></div>}

      {!loading && workspaces.length === 0 && (
        <div style={S.empty}>
          <div style={{ fontSize: 32, marginBottom: 12 }}>🔲</div>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>No workspaces yet</div>
          <div style={{ fontSize: 13 }}>Install a package from Browse — it will ask you to create a workspace.</div>
        </div>
      )}

      {workspaces.map(ws => {
        const days = daysUntil(ws.expires_at);
        const expiryWarn = days <= 7;
        const logs = netLogs[ws.id] ?? [];
        const violations = logs.filter((l: any) => !l.is_declared);
        const isExpanded = expanded === ws.id;

        return (
          <div key={ws.id} style={{ ...S.card, marginBottom: 12 }}>
            {/* Card header */}
            <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
              <div style={{ flex: 1 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4, flexWrap: "wrap" }}>
                  <span style={{ fontSize: 14, fontWeight: 700, color: "var(--text-primary)" }}>{ws.name}</span>
                  <span style={{
                    fontSize: 10, fontWeight: 700, padding: "2px 8px", borderRadius: 4,
                    color: WS_STATUS_COLOR[ws.status] ?? "#94a3b8",
                    background: `${WS_STATUS_COLOR[ws.status] ?? "#94a3b8"}1a`,
                    fontFamily: "var(--font-mono)", textTransform: "uppercase",
                  }}>{ws.status}</span>
                  {violations.length > 0 && (
                    <span style={{ fontSize: 10, fontWeight: 700, padding: "2px 8px", borderRadius: 4,
                      color: "#ef4444", background: "#ef44441a", fontFamily: "var(--font-mono)" }}>
                      ⚠ SECURITY VIOLATION
                    </span>
                  )}
                </div>

                <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6 }}>
                  {ws.items.length} package{ws.items.length !== 1 ? "s" : ""} · {" "}
                  <span style={{ color: expiryWarn ? "#f59e0b" : "var(--text-muted)" }}>
                    {days > 0 ? `expires in ${days}d` : "expired"}
                  </span>
                  {ws.submitted_at && ` · submitted ${new Date(ws.submitted_at).toLocaleDateString()}`}
                </div>

                {/* Package badges */}
                <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
                  {ws.items.map((item: any) => (
                    <span key={item.package_id} style={{
                      fontSize: 10, padding: "2px 8px", borderRadius: 4,
                      background: "var(--bg-subtle)", color: "var(--text-muted)",
                      fontFamily: "var(--font-mono)",
                    }}>
                      {item.package_id.split("/").pop()}
                      {item.status === "approved" && " ✓"}
                    </span>
                  ))}
                </div>
              </div>

              {/* Actions */}
              <div style={{ display: "flex", gap: 6, flexShrink: 0, flexWrap: "wrap", justifyContent: "flex-end" }}>
                {ws.status === "active" && (
                  <button style={{ ...S.btn, fontSize: 11 }} onClick={() => enterSandbox(ws.id)}>
                    Enter Sandbox
                  </button>
                )}
                {ws.status === "active" && ws.items.length > 0 && (
                  <button style={{ ...S.btn, ...S.btnGhost, fontSize: 11 }}
                    onClick={() => setShowSubmit(ws.id)}>
                    Submit for Review
                  </button>
                )}
                <button style={{ ...S.btn, ...S.btnGhost, fontSize: 11 }} onClick={() => toggle(ws.id)}>
                  {isExpanded ? "Collapse" : "Details"}
                </button>
                {ws.status === "active" && (
                  <button style={{ ...S.btn, background: "#ef44441a", color: "#ef4444",
                    border: "1px solid #ef444430", fontSize: 11 }}
                    onClick={() => deleteWs(ws.id)}>
                    Delete
                  </button>
                )}
              </div>
            </div>

            {/* Submit for review panel */}
            {showSubmit === ws.id && (
              <div style={{ marginTop: 12, padding: 14, background: "var(--bg-subtle)", borderRadius: "var(--radius-sm)" }}>
                <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-primary)", marginBottom: 6 }}>
                  Submit workspace for admin review
                </div>
                <textarea
                  style={{ ...S.search, height: 60, resize: "none", display: "block", marginBottom: 8 }}
                  placeholder="Test notes for the admin (optional)…"
                  value={submitNote} onChange={e => setSubmitNote(e.target.value)} />
                <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                  <button style={{ ...S.btn, ...S.btnGhost, fontSize: 11 }} onClick={() => setShowSubmit(null)}>Cancel</button>
                  <button style={{ ...S.btn, fontSize: 11 }} disabled={submitting === ws.id}
                    onClick={() => submitForReview(ws.id)}>
                    {submitting === ws.id ? "Submitting…" : "Submit"}
                  </button>
                </div>
              </div>
            )}

            {/* Expanded detail */}
            {isExpanded && (
              <div style={{ marginTop: 14, borderTop: "1px solid var(--border-subtle)", paddingTop: 14 }}>
                {/* Network log */}
                <div style={{ fontSize: 11, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase",
                  letterSpacing: "0.06em", marginBottom: 8 }}>
                  Network Log — {logs.length} events
                </div>
                {logs.length === 0
                  ? <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 12 }}>No outbound calls recorded yet.</div>
                  : (
                    <div style={{ overflowX: "auto", marginBottom: 14 }}>
                      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11, fontFamily: "var(--font-mono)" }}>
                        <thead>
                          <tr style={{ borderBottom: "1px solid var(--border-subtle)" }}>
                            {["Time", "Package", "Domain", "Method", "Bytes↑", "Bytes↓", "Status", "Declared"].map(h => (
                              <th key={h} style={{ textAlign: "left", padding: "4px 8px", color: "var(--text-muted)", fontWeight: 600, fontSize: 10 }}>{h}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {logs.map((l: any) => (
                            <tr key={l.id} style={{ borderBottom: "1px solid var(--border-subtle)",
                              background: !l.is_declared ? "#ef44440a" : undefined }}>
                              <td style={{ padding: "4px 8px", color: "var(--text-muted)" }}>{new Date(l.created_at).toLocaleTimeString()}</td>
                              <td style={{ padding: "4px 8px", color: "var(--text-secondary)" }}>{l.package_id.split("/").pop()}</td>
                              <td style={{ padding: "4px 8px", color: "var(--text-primary)" }}>{l.destination_url.replace(/^https?:\/\//, "").slice(0, 40)}</td>
                              <td style={{ padding: "4px 8px", color: "var(--text-muted)" }}>{l.http_method ?? "-"}</td>
                              <td style={{ padding: "4px 8px", color: "var(--text-muted)" }}>{l.bytes_sent}</td>
                              <td style={{ padding: "4px 8px", color: "var(--text-muted)" }}>{l.bytes_received}</td>
                              <td style={{ padding: "4px 8px" }}>
                                <span style={{ color: l.status === "blocked" ? "#ef4444" : "#22c55e", fontWeight: 700 }}>
                                  {l.status}
                                </span>
                              </td>
                              <td style={{ padding: "4px 8px" }}>
                                {l.is_declared
                                  ? <span style={{ color: "#22c55e" }}>✓</span>
                                  : <span style={{ color: "#ef4444", fontWeight: 700 }}>✗ UNDECLARED</span>}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )
                }

                {/* Whitelist request button — only for blocked calls */}
                {logs.some((l: any) => l.status === "blocked") && (
                  <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                    {[...new Set<string>(logs.filter((l: any) => l.status === "blocked").map((l: any) =>
                      JSON.stringify({ domain: new URL(l.destination_url).hostname, pkg: l.package_id })
                    ))].map((s: string) => {
                      const { domain, pkg } = JSON.parse(s);
                      return (
                        <button key={`${pkg}-${domain}`}
                          style={{ ...S.btn, ...S.btnGhost, fontSize: 11 }}
                          onClick={() => setWlReq({ wsId: ws.id, domain, pkg })}>
                          Request whitelist: {domain}
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}

      {/* Whitelist request modal */}
      {wlReq && (
        <div style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.55)", zIndex: 1000,
          display: "flex", alignItems: "center", justifyContent: "center" }}>
          <div style={{ background: "var(--bg-panel)", border: "1px solid var(--border-default)",
            borderRadius: "var(--radius-lg)", padding: 28, width: 400 }}>
            <div style={{ fontSize: 14, fontWeight: 700, color: "var(--text-primary)", marginBottom: 4 }}>
              Request Whitelist Access
            </div>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 14 }}>
              Domain: <code style={{ fontFamily: "var(--font-mono)" }}>{wlReq.domain}</code> · Package: {wlReq.pkg.split("/").pop()}
            </div>
            <textarea style={{ ...S.search, height: 72, resize: "none", display: "block", marginBottom: 10 }}
              placeholder="Why does this package need to call this domain? (shown to admin)"
              value={wlNote} onChange={e => setWlNote(e.target.value)} />
            <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 16 }}>
              Admin approval required. Access will be blocked until approved.
            </div>
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button style={{ ...S.btn, ...S.btnGhost }} onClick={() => { setWlReq(null); setWlNote(""); }}>Cancel</button>
              <button style={S.btn} onClick={requestWhitelist}>Send Request</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ── SourcesTab — manage publisher source URLs (admin only) ───────────────────

function SourcesTab() {
  const [sources, setSources] = React.useState<any[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [showAdd, setShowAdd] = React.useState(false);
  const [name, setName] = React.useState("");
  const [url, setUrl] = React.useState("");
  const [token, setToken] = React.useState("");
  const [tier, setTier] = React.useState("community");
  const [pollHours, setPollHours] = React.useState(6);
  const [syncing, setSyncing] = React.useState<string | null>(null);

  const load = () => {
    setLoading(true);
    apiFetch("/sources").then(r => r.json()).then(d => { setSources(d.sources ?? []); setLoading(false); }).catch(() => setLoading(false));
  };
  React.useEffect(load, []);

  const addSource = async () => {
    if (!name.trim() || !url.trim()) return;
    await apiFetch("/sources", { method: "POST", body: JSON.stringify({ name, url, tier, token: token || null, poll_interval_hours: pollHours }) });
    setShowAdd(false); setName(""); setUrl(""); setToken(""); load();
  };

  const removeSource = async (id: string) => {
    await apiFetch(`/sources/${id}`, { method: "DELETE" }); load();
  };

  const syncSource = async (id: string) => {
    setSyncing(id);
    await apiFetch(`/sources/${id}/sync`, { method: "POST" });
    setSyncing(null); load();
  };

  const syncAll = async () => {
    setSyncing("all");
    await apiFetch("/sources/sync-all", { method: "POST" });
    setSyncing(null); load();
  };

  const TIER_COLOR: Record<string, string> = { official: "#0d9488", community: "#3b82f6", private: "#8b5cf6" };

  return (
    <div style={{ flex: 1, padding: "var(--space-xl) var(--space-2xl)", overflow: "auto" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "var(--space-lg)" }}>
        <div>
          <div style={{ fontSize: 15, fontWeight: 700, color: "var(--text-primary)" }}>Package Sources</div>
          <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>
            Each source is a publisher's <code>velaris.json</code> URL. Velaris polls these on schedule and caches the package catalogue.
          </div>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button style={{ ...S.btn, ...S.btnGhost, fontSize: 12 }} onClick={syncAll} disabled={syncing === "all"}>
            {syncing === "all" ? "Syncing…" : "Sync All"}
          </button>
          <button style={{ ...S.btn, fontSize: 12 }} onClick={() => setShowAdd(v => !v)}>+ Add Source</button>
        </div>
      </div>

      {showAdd && (
        <div style={{ ...S.card, marginBottom: "var(--space-lg)", border: "1px solid var(--accent)" }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", marginBottom: 12 }}>Add Package Source</div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 2fr", gap: 10, marginBottom: 10 }}>
            <div>
              <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.06em" }}>Name</div>
              <input style={S.search} value={name} onChange={e => setName(e.target.value)} placeholder="ACME Corp" />
            </div>
            <div>
              <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.06em" }}>velaris.json URL</div>
              <input style={S.search} value={url} onChange={e => setUrl(e.target.value)} placeholder="https://raw.githubusercontent.com/acme/connector/main/velaris.json" />
            </div>
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 80px", gap: 10, marginBottom: 12 }}>
            <div>
              <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.06em" }}>Personal Access Token (private repos)</div>
              <input style={{ ...S.search, fontFamily: "var(--font-mono)" }} value={token} onChange={e => setToken(e.target.value)} placeholder="ghp_… (optional)" type="password" />
            </div>
            <div>
              <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.06em" }}>Trust Tier</div>
              <select style={S.search} value={tier} onChange={e => setTier(e.target.value)}>
                <option value="community">Community</option>
                <option value="private">Private (admin-vouched)</option>
              </select>
            </div>
            <div>
              <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", marginBottom: 4, textTransform: "uppercase", letterSpacing: "0.06em" }}>Poll (hrs)</div>
              <input style={S.search} type="number" min={1} max={168} value={pollHours} onChange={e => setPollHours(Number(e.target.value))} />
            </div>
          </div>
          <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
            <button style={{ ...S.btn, ...S.btnGhost }} onClick={() => setShowAdd(false)}>Cancel</button>
            <button style={S.btn} onClick={addSource} disabled={!name.trim() || !url.trim()}>Add & Sync</button>
          </div>
        </div>
      )}

      {loading
        ? <div style={S.empty}><div style={{ fontSize: 13, color: "var(--text-muted)" }}>Loading sources…</div></div>
        : sources.length === 0
          ? <div style={S.empty}><div style={{ fontSize: 28, marginBottom: 10 }}>🔗</div><div>No sources registered yet.</div></div>
          : sources.map(s => (
              <div key={s.id} style={{ ...S.card, marginBottom: 10 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
                  <div style={{ flex: 1 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                      <span style={{ fontSize: 14, fontWeight: 700, color: "var(--text-primary)" }}>{s.name}</span>
                      <span style={{ fontSize: 10, fontWeight: 700, padding: "2px 7px", borderRadius: 4,
                        color: TIER_COLOR[s.tier] ?? "#94a3b8", background: `${TIER_COLOR[s.tier] ?? "#94a3b8"}1a` }}>
                        {s.tier}
                      </span>
                      {s.has_token && <span style={{ fontSize: 10, color: "var(--text-muted)" }}>🔒 token</span>}
                      {!s.enabled && <span style={{ fontSize: 10, color: "#ef4444" }}>disabled</span>}
                    </div>
                    <div style={{ fontSize: 11, color: "var(--text-muted)", fontFamily: "var(--font-mono)", marginBottom: 4, wordBreak: "break-all" }}>{s.url}</div>
                    <div style={{ fontSize: 11, color: "var(--text-secondary)" }}>
                      {s.package_count} packages · polls every {s.poll_interval_hours}h
                      {s.last_polled_at && ` · last synced ${new Date(s.last_polled_at).toLocaleString()}`}
                    </div>
                    {s.last_error && (
                      <div style={{ fontSize: 11, color: "#ef4444", marginTop: 4 }}>⚠ {s.last_error}</div>
                    )}
                  </div>
                  <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
                    <button style={{ ...S.btn, ...S.btnGhost, fontSize: 11 }}
                      onClick={() => syncSource(s.id)} disabled={syncing === s.id}>
                      {syncing === s.id ? "Syncing…" : "Sync"}
                    </button>
                    {s.tier !== "official" && (
                      <button style={{ ...S.btn, background: "#ef44441a", color: "#ef4444", border: "1px solid #ef444430", fontSize: 11 }}
                        onClick={() => removeSource(s.id)}>Remove</button>
                    )}
                  </div>
                </div>
              </div>
            ))
      }

      <div style={{ marginTop: "var(--space-xl)", padding: 16, background: "var(--bg-subtle)", borderRadius: "var(--radius-md)", fontSize: 12, color: "var(--text-muted)", lineHeight: 1.6 }}>
        <strong style={{ color: "var(--text-secondary)" }}>How sources work:</strong><br/>
        Publishers host a <code>velaris.json</code> in their own repo — no code ever touches the Velaris repository.
        Adding a source URL here is the only step needed. Velaris polls each source on its schedule and updates the package catalogue automatically.
        When a new version is detected for an installed package, it appears in the Updates tab for admin review.
      </div>
    </div>
  );
}

// ── ReviewQueueTab ────────────────────────────────────────────────────────────

function ReviewQueueTab() {
  const [queue,      setQueue]      = useState<any[]>([]);
  const [loading,    setLoading]    = useState(true);
  const [expanded,   setExpanded]   = useState<string | null>(null);
  const [rejecting,  setRejecting]  = useState<string | null>(null);
  const [rejectNote, setRejectNote] = useState("");
  const [updates,    setUpdates]    = useState<any[]>([]);
  const [releaseReqs,setReleaseReqs]= useState<any[]>([]);

  const load = () => {
    setLoading(true);
    // Per-fetch catch: one failing endpoint must not blank the whole tab
    // (e.g. /release-requests erroring should still let submissions render, and
    //  vice-versa). Mirrors BrowseTab's resilient loading.
    Promise.all([
      apiFetch("/review-queue").then(r => r.ok ? r.json() : null).catch(() => null),
      apiFetch("/updates").then(r => r.ok ? r.json() : null).catch(() => null),
      apiFetch("/release-requests").then(r => r.ok ? r.json() : null).catch(() => null),
    ]).then(([q, u, rr]) => {
      setQueue(q?.queue ?? []);
      setUpdates(u?.updates ?? []);
      setReleaseReqs(rr?.requests ?? []);
      setLoading(false);
    }).catch(() => setLoading(false));
  };
  React.useEffect(load, []);

  const approveRelease = async (id: string) => {
    await apiFetch(`/release-requests/${id}/approve`, { method: "POST" });
    load();
  };

  const rejectRelease = async (id: string) => {
    await apiFetch(`/release-requests/${id}/reject`, { method: "POST" });
    load();
  };

  const approve = async (wsId: string, packageIds?: string[]) => {
    await apiFetch(`/review-queue/${wsId}/approve`, {
      method: "POST",
      body: JSON.stringify({ package_ids: packageIds ?? null }),
    });
    load();
  };

  const reject = async (wsId: string) => {
    await apiFetch(`/review-queue/${wsId}/reject`, {
      method: "POST",
      body: JSON.stringify({ reason: rejectNote }),
    });
    setRejecting(null); setRejectNote(""); load();
  };

  const approveUpdate = async (id: string) => {
    await apiFetch(`/updates/${id}/approve`, { method: "POST" }); load();
  };

  const dismissUpdate = async (id: string) => {
    await apiFetch(`/updates/${id}/dismiss`, { method: "POST" }); load();
  };

  const approveWhitelist = async (wlId: string, decision: string) => {
    await apiFetch(`/whitelist/${wlId}`, {
      method: "PATCH",
      body: JSON.stringify({ decision }),
    }); load();
  };

  if (loading) return <div style={S.empty}><div style={{ fontSize: 13, color: "var(--text-muted)" }}>Loading review queue…</div></div>;

  return (
    <div style={{ flex: 1, padding: "var(--space-xl) var(--space-2xl)", overflow: "auto" }}>

      {/* Update notifications */}
      {updates.length > 0 && (
        <>
          <div style={S.sectionHd}>Package Updates Available — {updates.length}</div>
          {updates.map(upd => (
            <div key={upd.id} style={{ ...S.card, marginBottom: 10,
              borderColor: upd.fast_track ? "var(--border-subtle)" : "#f59e0b55" }}>
              <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: 13, fontWeight: 700, color: "var(--text-primary)", marginBottom: 2 }}>
                    {upd.package_id.split("/").pop()} {upd.installed_version} → {upd.available_version}
                  </div>
                  {upd.release_notes && (
                    <div style={{ fontSize: 11, color: "var(--text-secondary)", marginBottom: 4 }}>{upd.release_notes}</div>
                  )}
                  {upd.fast_track
                    ? <span style={{ fontSize: 10, color: "#22c55e", fontWeight: 700 }}>✓ Fast-track — no new outbound domains</span>
                    : (
                      <span style={{ fontSize: 10, color: "#f59e0b", fontWeight: 700 }}>
                        ⚠ New domains: {upd.new_outbound_domains.join(", ")} — sandbox testing required
                      </span>
                    )
                  }
                </div>
                <div style={{ display: "flex", gap: 6 }}>
                  {upd.fast_track && (
                    <button style={{ ...S.btn, fontSize: 11 }} onClick={() => approveUpdate(upd.id)}>
                      Approve & Install
                    </button>
                  )}
                  <button style={{ ...S.btn, ...S.btnGhost, fontSize: 11 }} onClick={() => dismissUpdate(upd.id)}>
                    Dismiss
                  </button>
                </div>
              </div>
            </div>
          ))}
        </>
      )}

      {/* Official package release requests */}
      {releaseReqs.length > 0 && (
        <>
          <div style={S.sectionHd}>Official Package Release Requests — {releaseReqs.length}</div>
          {releaseReqs.map(rr => (
            <div key={rr.id} style={{ ...S.card, marginBottom: 10 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                <div style={{ flex: 1 }}>
                  <div style={{ fontSize: 13, fontWeight: 700, color: "var(--text-primary)", marginBottom: 2 }}>
                    {rr.package_id.split("/").pop()}{" "}
                    <span style={{ color: "var(--text-muted)", fontWeight: 400 }}>v{rr.package_version}</span>
                  </div>
                  <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
                    Requested by {rr.requested_by}
                    {rr.created_at && ` · ${new Date(rr.created_at).toLocaleDateString()}`}
                  </div>
                </div>
                <div style={{ display: "flex", gap: 6 }}>
                  <button style={{ ...S.btn, fontSize: 11, background: "#0d9488" }}
                    onClick={() => approveRelease(rr.id)}>
                    Approve &amp; Install
                  </button>
                  <button style={{ ...S.btn, background: "#ef44441a", color: "#ef4444",
                    border: "1px solid #ef444430", fontSize: 11 }}
                    onClick={() => rejectRelease(rr.id)}>
                    Reject
                  </button>
                </div>
              </div>
            </div>
          ))}
        </>
      )}

      {/* Workspace review queue */}
      <div style={S.sectionHd}>Workspace Submissions — {queue.length}</div>

      {queue.length === 0 && (
        <div style={S.empty}>
          <div style={{ fontSize: 28, marginBottom: 10 }}>✅</div>
          <div style={{ fontWeight: 600, marginBottom: 4 }}>Queue is empty</div>
          <div style={{ fontSize: 13 }}>No workspaces pending review.</div>
        </div>
      )}

      {queue.map(ws => {
        const isExpanded = expanded === ws.id;
        return (
          <div key={ws.id} style={{ ...S.card, marginBottom: 12,
            borderColor: ws.security_violation ? "#ef4444" : "var(--border-subtle)" }}>

            {/* Violation banner */}
            {ws.security_violation && (
              <div style={{ background: "#ef44441a", border: "1px solid #ef444430", borderRadius: "var(--radius-sm)",
                padding: "8px 12px", marginBottom: 10, fontSize: 12, color: "#ef4444", fontWeight: 600 }}>
                ⚠ SECURITY VIOLATION — This workspace made calls to undeclared domains. Review carefully before approving.
              </div>
            )}

            <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
              <div style={{ flex: 1 }}>
                <div style={{ fontSize: 14, fontWeight: 700, color: "var(--text-primary)", marginBottom: 4 }}>{ws.name}</div>
                <div style={{ fontSize: 11, color: "var(--text-muted)", marginBottom: 6 }}>
                  By {ws.created_by} · Submitted {ws.submitted_at ? new Date(ws.submitted_at).toLocaleDateString() : "-"}
                  · {ws.network_log_count} network events ({ws.blocked_calls} blocked)
                </div>
                {ws.review_note && (
                  <div style={{ fontSize: 12, color: "var(--text-secondary)", fontStyle: "italic", marginBottom: 6 }}>
                    "{ws.review_note}"
                  </div>
                )}
                {/* Package list */}
                <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  {ws.items.map((item: any) => (
                    <div key={item.package_id} style={{ display: "flex", gap: 4, alignItems: "center" }}>
                      <span style={{ fontSize: 10, padding: "2px 8px", borderRadius: 4,
                        background: "var(--bg-subtle)", color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                        {item.package_id.split("/").pop()} v{item.package_version}
                      </span>
                      <button style={{ fontSize: 10, padding: "1px 6px", borderRadius: 4,
                        background: "#22c55e1a", color: "#22c55e", border: "1px solid #22c55e30", cursor: "pointer" }}
                        onClick={() => approve(ws.workspace_id, [item.package_id])}>
                        ✓
                      </button>
                    </div>
                  ))}
                </div>
              </div>

              <div style={{ display: "flex", gap: 6, flexShrink: 0, flexDirection: "column", alignItems: "flex-end" }}>
                <div style={{ display: "flex", gap: 6 }}>
                  <button style={{ ...S.btn, fontSize: 11 }} onClick={() => approve(ws.workspace_id)}>
                    Approve All
                  </button>
                  <button style={{ ...S.btn, background: "#ef44441a", color: "#ef4444",
                    border: "1px solid #ef444430", fontSize: 11 }}
                    onClick={() => setRejecting(ws.workspace_id)}>
                    Reject
                  </button>
                </div>
                <button style={{ ...S.btn, ...S.btnGhost, fontSize: 11 }}
                  onClick={() => setExpanded(isExpanded ? null : ws.workspace_id)}>
                  {isExpanded ? "Hide" : "Network Log"}
                </button>
              </div>
            </div>

            {/* Reject form */}
            {rejecting === ws.workspace_id && (
              <div style={{ marginTop: 10, padding: 12, background: "var(--bg-subtle)", borderRadius: "var(--radius-sm)" }}>
                <textarea style={{ ...S.search, height: 56, resize: "none", display: "block", marginBottom: 8 }}
                  placeholder="Reason for rejection (shown to developer)…"
                  value={rejectNote} onChange={e => setRejectNote(e.target.value)} />
                <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                  <button style={{ ...S.btn, ...S.btnGhost, fontSize: 11 }} onClick={() => setRejecting(null)}>Cancel</button>
                  <button style={{ ...S.btn, background: "#ef4444", fontSize: 11 }} onClick={() => reject(ws.workspace_id)}>
                    Confirm Reject
                  </button>
                </div>
              </div>
            )}

            {/* Network log */}
            {isExpanded && ws.network_log_count > 0 && (
              <div style={{ marginTop: 12, borderTop: "1px solid var(--border-subtle)", paddingTop: 12 }}>
                <div style={{ fontSize: 11, fontWeight: 600, color: "var(--text-muted)", marginBottom: 6, textTransform: "uppercase", letterSpacing: "0.06em" }}>
                  Network Activity Summary
                </div>
                <div style={{ fontSize: 12, color: "var(--text-secondary)" }}>
                  {ws.network_log_count} total calls · {ws.blocked_calls} blocked
                  {ws.security_violation && " · ⚠ undeclared domains detected"}
                </div>
                <div style={{ marginTop: 8, fontSize: 11, color: "var(--text-muted)" }}>
                  Full log is available in the workspace network log viewer.
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── InstalledTab ──────────────────────────────────────────────────────────────

function InstalledTab({ isAdmin = false }: { isAdmin?: boolean }) {
  const [installs, setInstalls] = useState<any[]>([]);
  const [packages, setPackages] = useState<Record<string, any>>({});
  const [loading,  setLoading]  = useState(true);
  const [revoking, setRevoking] = useState<string | null>(null);

  React.useEffect(() => {
    Promise.all([
      apiFetch("/installs").then(r => r.json()),
      apiFetch("/packages").then(r => r.json()),
    ]).then(([inst, pkgs]) => {
      setInstalls(inst.installs ?? []);
      const map: Record<string, any> = {};
      (pkgs.packages ?? []).forEach((p: any) => { map[p.id] = p; });
      setPackages(map);
      setLoading(false);
    }).catch(() => setLoading(false));
  }, []);

  const revoke = async (id: string, pkgId: string) => {
    if (!confirm(`Uninstall ${pkgId.split("/").pop()}?\n\nThis will immediately disable the package in production. Active case workflows using this connector will lose their integration. This action is logged and cannot be undone here.`)) return;
    setRevoking(id);
    await apiFetch(`/installs/${id}`, { method: "DELETE" });
    setInstalls(p => p.filter(i => i.id !== id));
    setRevoking(null);
  };

  if (loading) return <div style={S.empty}><div style={{ fontSize: 13, color: "var(--text-muted)" }}>Loading…</div></div>;

  if (installs.length === 0) return (
    <div style={S.empty}>
      <div style={{ fontSize: 32, marginBottom: 12 }}>📥</div>
      <div style={{ fontWeight: 600, marginBottom: 4 }}>No packages installed</div>
      <div style={{ fontSize: 13 }}>Install a package from Browse → test in a workspace → admin approves → appears here.</div>
    </div>
  );

  return (
    <div style={{ flex: 1, padding: "var(--space-xl) var(--space-2xl)", overflow: "auto" }}>
      <div style={{ marginBottom: "var(--space-lg)" }}>
        <div style={{ fontSize: 15, fontWeight: 700, color: "var(--text-primary)" }}>
          Installed Packages — {installs.length}
        </div>
        <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 2 }}>
          Production-approved packages for this tenant.
        </div>
      </div>

      <div style={S.grid}>
        {installs.map(inst => {
          const pkg = packages[inst.package_id];
          const licenceExpired = inst.licence_expires && new Date(inst.licence_expires) < new Date();
          return (
            <div key={inst.id} style={S.card}>
              <div style={S.cardHd}>
                {pkg ? <PkgIcon pkg={pkg} size={44} /> : (
                  <div style={{ width: 44, height: 44, borderRadius: 10, background: "var(--bg-subtle)" }} />
                )}
                <div style={S.cardMeta}>
                  <div style={S.cardName}>{pkg?.name ?? inst.package_id}</div>
                  <div style={S.cardPub}>v{inst.package_version}</div>
                  {pkg && <TypeBadge type={pkg.type} />}
                </div>
              </div>

              {licenceExpired && (
                <div style={{ fontSize: 11, color: "#f59e0b", fontWeight: 600 }}>
                  ⚠ Licence may have expired ({inst.licence_expires}) — contact publisher.
                </div>
              )}

              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>
                Approved {new Date(inst.installed_at).toLocaleDateString()}
              </div>

              {isAdmin && (
                <button
                  disabled={revoking === inst.id}
                  style={{ ...S.btn, background: "#ef44441a", color: "#ef4444",
                    border: "1px solid #ef444430", fontSize: 11, marginTop: 4 }}
                  onClick={() => revoke(inst.id, inst.package_id)}>
                  {revoking === inst.id ? "Uninstalling…" : "Uninstall"}
                </button>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Marketplace ───────────────────────────────────────────────────────────────

export default function Marketplace() {
  const { user } = useAuth();
  const [tab,       setTab]       = useState<MainTab>("browse");
  const [devOnly,   setDevOnly]   = useState(false);
  const [wsLimit,   setWsLimit]   = useState(2);

  const isAdmin     = user?.is_admin ?? false;
  const isDeveloper = isAdmin || (user?.roles ?? []).includes("developer");
  const isManager   = !isDeveloper && (user?.roles ?? []).includes("manager");
  const canInstall  = isDeveloper;

  // Fetch server config to enforce dev-only flag and workspace limit in UI
  React.useEffect(() => {
    apiFetch("/config").then(r => r.ok ? r.json() : null).then(d => {
      if (!d) return;
      if (d.dev_only) setDevOnly(true);
      if (d.max_workspaces_per_user) setWsLimit(d.max_workspaces_per_user);
    }).catch(() => {});
  }, []);

  // Dev-only gate — show blocked screen if marketplace is restricted to dev environments
  if (devOnly && import.meta.env.VITE_ENV && import.meta.env.VITE_ENV !== "dev") {
    return (
      <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", flexDirection: "column", gap: 12, color: "var(--text-muted)", padding: 40 }}>
        <div style={{ fontSize: 36 }}>🔒</div>
        <div style={{ fontSize: 15, fontWeight: 700, color: "var(--text-primary)" }}>Marketplace — Dev Environment Only</div>
        <div style={{ fontSize: 13, textAlign: "center", maxWidth: 400, lineHeight: 1.6 }}>
          The Marketplace is restricted to development environments. To install packages in this environment,
          they must be included in a deployment release via HxDeploy.
        </div>
      </div>
    );
  }

  const tabs: { key: MainTab; label: string; adminOnly?: boolean }[] = [
    { key: "browse",     label: "Browse" },
    { key: "installed",  label: "Installed" },
    { key: "workspaces", label: "Workspaces" },
    { key: "review",     label: "Review Queue", adminOnly: true },
  ];

  return (
    <div style={S.page}>
      <div style={S.tabBar}>
        {tabs.map(t => {
          if (t.adminOnly && !isAdmin) return null;
          if (t.key === "workspaces" && isManager) return null;
          return (
            <button key={t.key}
              style={{ ...S.tab, ...(tab === t.key ? S.tabA : {}) }}
              onClick={() => setTab(t.key)}>
              {t.label}
            </button>
          );
        })}
      </div>

      {tab === "browse"     && <BrowseTab canInstall={canInstall} isManager={isManager} />}
      {tab === "installed"  && <InstalledTab isAdmin={isAdmin} />}
      {tab === "workspaces" && <WorkspacesTab wsLimit={wsLimit} />}
      {tab === "review"     && <ReviewQueueTab />}
    </div>
  );
}
