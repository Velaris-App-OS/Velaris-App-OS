import React, { useState, useEffect, useMemo } from "react";
import { useApi } from "@shared/hooks";
import {
  listCodegenPlatforms, previewApp, downloadGeneratedApp, listCaseTypes,
} from "@shared/api/client";
import { Card, Button, Spinner } from "@shared/components";
import { BRAND } from "@/branding";

/* ═══════════════════════════════════════════════════════════════════
   App Builder — generate, ship, and publish a React Native Expo app
   Tabs: Configure · Ship Update · Store Guide · Preview
   ═══════════════════════════════════════════════════════════════════ */

const TABS = ["Configure", "Ship Update", "Store Guide", "Preview"] as const;
type Tab = typeof TABS[number];

// Persisted in localStorage so "last built" survives page reloads
const LS_KEY = "helix_app_builder_last_build";

interface BuildRecord {
  builtAt: string;
  config: AppConfig;
  caseTypeVersions: Record<string, string>; // id → version
}

interface AppConfig {
  app_name: string;
  app_slug: string;
  primary_color: string;
  default_api_url: string;
  default_tenant: string;
  case_type_ids: string[];
  app_version: string;
  ios_bundle_id: string;
  android_package: string;
  app_description: string;
}

export default function AppBuilder() {
  const [tab, setTab] = useState<Tab>("Configure");
  const [config, setConfig] = useState<AppConfig>({
    app_name: `${BRAND.name} Mobile`,
    app_slug: "velaris-mobile",
    primary_color: "#4ecdc4",
    default_api_url: "http://localhost:8200",
    default_tenant: "default",
    case_type_ids: [],
    app_version: "1.0.0",
    ios_bundle_id: "com.example.velarismobile",
    android_package: "com.example.velarismobile",
    app_description: "",
  });

  const [preview, setPreview]       = useState<any>(null);
  const [loading, setLoading]       = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError]           = useState<string | null>(null);
  const [lastBuild, setLastBuild]   = useState<BuildRecord | null>(null);

  const { data: platforms }  = useApi(listCodegenPlatforms);
  const { data: ctData }     = useApi(listCaseTypes);
  const caseTypes: any[]     = ctData?.items ?? [];

  // Load last build from localStorage
  useEffect(() => {
    try {
      const saved = localStorage.getItem(LS_KEY);
      if (saved) setLastBuild(JSON.parse(saved));
    } catch {}
  }, []);

  // Detect case types that changed since last build
  const changedSinceLastBuild = useMemo(() => {
    if (!lastBuild) return [];
    return caseTypes.filter((ct: any) => {
      const prev = lastBuild.caseTypeVersions[ct.id];
      return prev && prev !== ct.version;
    });
  }, [lastBuild, caseTypes]);

  const newSinceLastBuild = useMemo(() => {
    if (!lastBuild) return [];
    const prevIds = new Set(Object.keys(lastBuild.caseTypeVersions));
    return caseTypes.filter((ct: any) => !prevIds.has(ct.id));
  }, [lastBuild, caseTypes]);

  const handleDownload = async () => {
    setDownloading(true); setError(null);
    try {
      const blob = await downloadGeneratedApp(config);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = `${config.app_slug}.zip`; a.click();
      URL.revokeObjectURL(url);
      // Record the build
      const record: BuildRecord = {
        builtAt: new Date().toISOString(),
        config: { ...config },
        caseTypeVersions: Object.fromEntries(caseTypes.map((ct: any) => [ct.id, ct.version])),
      };
      setLastBuild(record);
      localStorage.setItem(LS_KEY, JSON.stringify(record));
      setTab("Ship Update");
    } catch (e: any) {
      setError(e.message);
    } finally {
      setDownloading(false);
    }
  };

  const handlePreview = async () => {
    setLoading(true); setError(null);
    try {
      const r = await previewApp(config);
      setPreview(r);
      setTab("Preview");
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", overflow: "hidden" }}>
      {/* Tab bar — Work Center format */}
      <div style={{ padding: "var(--space-md) var(--space-2xl)", flexShrink: 0, display: "flex", alignItems: "center", gap: "var(--space-md)" }}>
        <div style={{ display: "flex", background: "var(--bg-card)", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-subtle)" }}>
          {TABS.map(t => (
            <button key={t} onClick={() => setTab(t)} style={{
              padding: "8px 16px", fontSize: 12, fontWeight: 500,
              fontFamily: "var(--font-mono)", textTransform: "uppercase", letterSpacing: "0.04em",
              border: "none", cursor: "pointer", borderRadius: "var(--radius-sm)",
              color: tab === t ? "var(--accent)" : "var(--text-muted)",
              background: tab === t ? "var(--accent-dim)" : "transparent",
              display: "flex", alignItems: "center", gap: 4,
            }}>
              {t}
              {t === "Ship Update" && (changedSinceLastBuild.length > 0 || newSinceLastBuild.length > 0) && (
                <span style={{ fontSize: 9, padding: "1px 5px", borderRadius: 8, background: "var(--status-running)", color: "#fff", fontWeight: 700 }}>
                  {changedSinceLastBuild.length + newSinceLastBuild.length}
                </span>
              )}
            </button>
          ))}
        </div>
        {/* Actions at right */}
        <div style={{ marginLeft: "auto", display: "flex", gap: "var(--space-sm)" }}>
          <Button variant="secondary" onClick={handlePreview} disabled={loading}>
            {loading ? "Generating…" : "Preview"}
          </Button>
          <Button onClick={handleDownload} disabled={downloading}>
            {downloading ? "Packaging…" : "↓ Download ZIP"}
          </Button>
        </div>
      </div>

      {/* Error banner */}
      {error && (
        <div style={{ padding: "10px var(--space-2xl)", background: "color-mix(in srgb, var(--status-failed) 10%, var(--bg-panel))", borderBottom: "1px solid color-mix(in srgb, var(--status-failed) 20%, transparent)", fontSize: 12, color: "var(--status-failed)", fontFamily: "var(--font-mono)" }}>
          {error}
        </div>
      )}

      {/* Tab content */}
      <div style={{ flex: 1, overflow: "auto" }}>
        {tab === "Configure" && <ConfigureTab config={config} setConfig={setConfig} caseTypes={caseTypes} />}
        {tab === "Ship Update" && <ShipUpdateTab config={config} lastBuild={lastBuild} changed={changedSinceLastBuild} newCts={newSinceLastBuild} caseTypes={caseTypes} onDownload={handleDownload} downloading={downloading} />}
        {tab === "Store Guide" && <StoreGuideTab config={config} />}
        {tab === "Preview"    && <PreviewTab preview={preview} loading={loading} />}
      </div>
    </div>
  );
}

/* ── Configure Tab ──────────────────────────────────────────────── */

function ConfigureTab({ config, setConfig, caseTypes }: { config: AppConfig; setConfig: (c: AppConfig) => void; caseTypes: any[] }) {
  const set = (k: keyof AppConfig) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) =>
    setConfig({ ...config, [k]: e.target.value });

  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-lg)", maxWidth: 1100 }}>
      {/* App identity */}
      <Card>
        <SectionTitle>App Identity</SectionTitle>
        <Field label="App Name"><Input value={config.app_name} onChange={set("app_name")} /></Field>
        <Field label="App Slug (npm identifier)">
          <Input value={config.app_slug} onChange={e => setConfig({ ...config, app_slug: e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-") })} />
        </Field>
        <Field label="Version">
          <Input value={config.app_version} onChange={set("app_version")} placeholder="1.0.0" />
          <Hint>Bump this when pushing a new store build (semver).</Hint>
        </Field>
        <Field label="Description (store listing)">
          <textarea value={config.app_description} onChange={set("app_description")} rows={2} placeholder={`The ${config.app_name} case management app…`} style={textareaStyle} />
        </Field>
        <Field label="Primary Color">
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <input type="color" value={config.primary_color} onChange={set("primary_color")}
              style={{ width: 46, height: 36, border: "1px solid var(--border-default)", borderRadius: 6, cursor: "pointer" }} />
            <Input value={config.primary_color} onChange={set("primary_color")} style={{ flex: 1 }} />
          </div>
        </Field>
      </Card>

      {/* Store IDs */}
      <Card>
        <SectionTitle>Store Identifiers</SectionTitle>
        <Field label="iOS Bundle ID">
          <Input value={config.ios_bundle_id} onChange={set("ios_bundle_id")} placeholder="com.yourcompany.appname" />
          <Hint>Must match your Apple Developer portal. Reverse domain notation. Once published, cannot change.</Hint>
        </Field>
        <Field label="Android Package Name">
          <Input value={config.android_package} onChange={set("android_package")} placeholder="com.yourcompany.appname" />
          <Hint>Must match your Google Play Console. Same reverse domain convention. Cannot change after publishing.</Hint>
        </Field>
        <Field label="API URL (case-service)">
          <Input value={config.default_api_url} onChange={set("default_api_url")} placeholder="https://api.yourcompany.com" />
          <Hint>Production URL. Dev: <code>http://10.0.2.2:8200</code> (Android emu) / <code>http://localhost:8200</code> (iOS sim)</Hint>
        </Field>
        <Field label="Default Tenant">
          <Input value={config.default_tenant} onChange={set("default_tenant")} />
        </Field>
      </Card>

      {/* Case types */}
      <Card style={{ gridColumn: "1 / -1" }}>
        <SectionTitle>Include Case Types</SectionTitle>
        <p style={{ fontSize: 12, color: "var(--text-secondary)", marginBottom: "var(--space-md)", lineHeight: 1.6 }}>
          Leave all unchecked to include every case type. Check specific ones to limit the app to those workflows.
          <strong style={{ color: "var(--text-primary)" }}> Note:</strong> Case type data (stages, steps, form fields) loads live from the API — no rebuild needed when you edit a case type's content.
        </p>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: 6 }}>
          {caseTypes.map((ct: any) => (
            <label key={ct.id} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, padding: "6px 10px", borderRadius: 6, border: "1px solid var(--border-subtle)", cursor: "pointer" }}>
              <input type="checkbox"
                checked={config.case_type_ids.includes(ct.id)}
                onChange={e => setConfig({
                  ...config,
                  case_type_ids: e.target.checked
                    ? [...config.case_type_ids, ct.id]
                    : config.case_type_ids.filter(id => id !== ct.id),
                })} />
              <span style={{ flex: 1 }}>{ct.name}</span>
              <span style={{ fontFamily: "var(--font-mono)", fontSize: 10, color: "var(--text-muted)" }}>v{ct.version}</span>
            </label>
          ))}
          {caseTypes.length === 0 && <span style={{ fontSize: 12, color: "var(--text-muted)" }}>No case types available</span>}
        </div>
      </Card>
    </div>
  );
}

/* ── Ship Update Tab ────────────────────────────────────────────── */

function ShipUpdateTab({ config, lastBuild, changed, newCts, caseTypes, onDownload, downloading }: {
  config: AppConfig; lastBuild: BuildRecord | null;
  changed: any[]; newCts: any[]; caseTypes: any[];
  onDownload: () => void; downloading: boolean;
}) {
  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", maxWidth: 860 }}>

      {/* Status since last build */}
      {lastBuild ? (
        <Card style={{ marginBottom: "var(--space-lg)", borderColor: (changed.length || newCts.length) ? "var(--status-running)" : "var(--status-completed)" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
            <div>
              <div style={{ fontWeight: 700, fontSize: 15, color: "var(--text-primary)", marginBottom: 4 }}>
                {changed.length === 0 && newCts.length === 0
                  ? "✓ App is up to date"
                  : `${changed.length + newCts.length} case type${changed.length + newCts.length > 1 ? "s" : ""} changed since last build`}
              </div>
              <div style={{ fontSize: 12, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                Last built: {new Date(lastBuild.builtAt).toLocaleString()} · v{lastBuild.config.app_version}
              </div>
            </div>
            {(changed.length > 0 || newCts.length > 0) && (
              <Button size="sm" onClick={onDownload} disabled={downloading}>
                {downloading ? "Packaging…" : "↓ Rebuild"}
              </Button>
            )}
          </div>
          {changed.length > 0 && (
            <div style={{ marginTop: "var(--space-md)", display: "flex", flexDirection: "column", gap: 4 }}>
              {changed.map((ct: any) => (
                <div key={ct.id} style={{ fontSize: 12, display: "flex", gap: 8, alignItems: "center" }}>
                  <span style={{ color: "#f59e0b", fontFamily: "var(--font-mono)", fontWeight: 700 }}>~</span>
                  <span style={{ color: "var(--text-primary)" }}>{ct.name}</span>
                  <span style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
                    v{lastBuild.caseTypeVersions[ct.id]} → v{ct.version}
                  </span>
                </div>
              ))}
              {newCts.map((ct: any) => (
                <div key={ct.id} style={{ fontSize: 12, display: "flex", gap: 8 }}>
                  <span style={{ color: "var(--status-completed)", fontFamily: "var(--font-mono)", fontWeight: 700 }}>+</span>
                  <span>{ct.name} v{ct.version}</span>
                  <span style={{ color: "var(--text-muted)", fontSize: 11 }}>(new)</span>
                </div>
              ))}
            </div>
          )}
        </Card>
      ) : (
        <Card style={{ marginBottom: "var(--space-lg)", borderColor: "var(--border-default)", background: "var(--bg-elevated)" }}>
          <div style={{ fontSize: 13, color: "var(--text-secondary)" }}>
            No build recorded yet. Download the ZIP to record a baseline.
          </div>
        </Card>
      )}

      {/* Strategy 1: API-driven (no rebuild) */}
      <Card style={{ marginBottom: "var(--space-lg)" }}>
        <div style={{ display: "flex", gap: 14, alignItems: "flex-start" }}>
          <div style={{ fontSize: 28, flexShrink: 0 }}>⚡</div>
          <div>
            <div style={{ fontWeight: 700, fontSize: 15, color: "var(--text-primary)", marginBottom: 4 }}>
              No rebuild needed — case data loads live from the API
            </div>
            <div style={{ fontSize: 13, color: "var(--text-secondary)", lineHeight: 1.7, marginBottom: "var(--space-md)" }}>
              When you edit a case type in Case Designer (add/remove stages, steps, change form fields, update SLA), the mobile app automatically shows those changes on the next API call.
              <strong style={{ color: "var(--text-primary)" }}> You do not need to rebuild or resubmit to the store.</strong>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
              {[
                ["✓ Add/remove stages", "Live — no action"],
                ["✓ Add/remove steps", "Live — no action"],
                ["✓ Change form fields", "Live — no action"],
                ["✓ Update SLA policies", "Live — no action"],
                ["✓ Change case type name", "Live — no action"],
                ["✓ Update descriptions", "Live — no action"],
              ].map(([what, how]) => (
                <div key={what} style={{ fontSize: 12, display: "flex", justifyContent: "space-between", gap: 8, padding: "4px 0", borderBottom: "1px solid var(--border-subtle)" }}>
                  <span style={{ color: "var(--text-primary)" }}>{what}</span>
                  <span style={{ color: "var(--status-completed)", fontFamily: "var(--font-mono)", fontSize: 11 }}>{how}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </Card>

      {/* Strategy 2: OTA update (Expo) */}
      <Card style={{ marginBottom: "var(--space-lg)" }}>
        <div style={{ display: "flex", gap: 14, alignItems: "flex-start" }}>
          <div style={{ fontSize: 28, flexShrink: 0 }}>🚀</div>
          <div>
            <div style={{ fontWeight: 700, fontSize: 15, color: "var(--text-primary)", marginBottom: 4 }}>
              Over-the-air (OTA) updates — code changes without store resubmission
            </div>
            <div style={{ fontSize: 13, color: "var(--text-secondary)", lineHeight: 1.7, marginBottom: "var(--space-md)" }}>
              For changes to the app's JavaScript code (UI tweaks, new screens, bug fixes) that don't involve native modules, use <strong style={{ color: "var(--text-primary)" }}>EAS Update</strong>. This pushes an update to all installed apps within minutes — no store review.
            </div>
            <CodeBlock>{`# One-time setup
npm install -g eas-cli
eas login
eas update:configure

# Push an OTA update (after code changes)
eas update --branch production --message "Fixed form rendering"`}</CodeBlock>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 8, lineHeight: 1.5 }}>
              Requires: <code>eas.json</code> with a production branch (already included in the downloaded ZIP) and an EAS account at <code>expo.dev</code>.
            </div>
          </div>
        </div>
      </Card>

      {/* Strategy 3: Full rebuild */}
      <Card>
        <div style={{ display: "flex", gap: 14, alignItems: "flex-start" }}>
          <div style={{ fontSize: 28, flexShrink: 0 }}>🏗️</div>
          <div>
            <div style={{ fontWeight: 700, fontSize: 15, color: "var(--text-primary)", marginBottom: 4 }}>
              Full rebuild required — when to go back to the store
            </div>
            <div style={{ fontSize: 13, color: "var(--text-secondary)", lineHeight: 1.7, marginBottom: "var(--space-md)" }}>
              Only needed for native changes. Bump the version in Configure, download a new ZIP, build with EAS, and resubmit.
            </div>
            <div style={{ fontSize: 12, display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
              {[
                ["Adding a new case type to the app", "New ZIP + rebuild"],
                ["Adding push notification support", "New ZIP + rebuild"],
                ["Adding camera / file upload", "New ZIP + rebuild"],
                ["Changing the app icon or splash", "New ZIP + rebuild"],
                ["Bumping Expo / React Native version", "New ZIP + rebuild"],
                ["Changing bundle ID / package name", "New ZIP + rebuild"],
              ].map(([what, how]) => (
                <div key={what} style={{ fontSize: 12, display: "flex", justifyContent: "space-between", gap: 8, padding: "4px 0", borderBottom: "1px solid var(--border-subtle)" }}>
                  <span style={{ color: "var(--text-primary)" }}>{what}</span>
                  <span style={{ color: "var(--status-running)", fontFamily: "var(--font-mono)", fontSize: 11 }}>{how}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </Card>
    </div>
  );
}

/* ── Store Guide Tab ────────────────────────────────────────────── */

function StoreGuideTab({ config }: { config: AppConfig }) {
  const [platform, setPlatform] = useState<"both" | "android" | "ios">("both");

  const steps = [
    {
      n: 1, title: "Prerequisites",
      content: (
        <div>
          <div style={{ fontSize: 13, color: "var(--text-secondary)", lineHeight: 1.7, marginBottom: 8 }}>
            You need accounts and tools set up before building.
          </div>
          <RequirementList items={[
            ["Node.js 18+", "nodejs.org/en/download"],
            ["EAS CLI", "npm install -g eas-cli"],
            ["Expo account", "expo.dev — free tier works for builds"],
            platform !== "ios" ? ["Google Play Console", "play.google.com/console — one-time $25 fee"] : null,
            platform !== "android" ? ["Apple Developer Program", "developer.apple.com — $99/year"] : null,
          ].filter(Boolean) as [string, string][]} />
        </div>
      ),
    },
    {
      n: 2, title: "Extract and install",
      content: (
        <CodeBlock>{`unzip ${config.app_slug}.zip
cd ${config.app_slug}
npm install
npx expo install  # sync native dependencies

# Link EAS to your Expo account
eas login
eas init          # creates the EAS project, sets extra.eas.projectId in app.json`}</CodeBlock>
      ),
    },
    {
      n: 3, title: "Configure app.json",
      content: (
        <div>
          <div style={{ fontSize: 13, color: "var(--text-secondary)", lineHeight: 1.6, marginBottom: 8 }}>
            Your <code>app.json</code> already has the bundle IDs you configured. Verify these match your store accounts:
          </div>
          <CodeBlock>{`// app.json (already generated with your values)
"ios": { "bundleIdentifier": "${config.ios_bundle_id}" }
"android": { "package": "${config.android_package}" }
"version": "${config.app_version}"   // bump this for each store release`}</CodeBlock>
          {(config.ios_bundle_id === "com.example.helixmobile" || config.android_package === "com.example.helixmobile") && (
            <div style={{ marginTop: 8, fontSize: 12, padding: "6px 12px", borderRadius: 6, background: "#fef3c7", color: "#d97706" }}>
              ⚠ You're still using the placeholder bundle IDs. Go to Configure tab and set your real reverse-domain IDs before building.
            </div>
          )}
        </div>
      ),
    },
    {
      n: 4, title: "Build for production",
      content: (
        <div>
          <div style={{ fontSize: 13, color: "var(--text-secondary)", marginBottom: 8 }}>
            EAS builds in the cloud — no Xcode or Android Studio needed on your machine.
          </div>
          <CodeBlock>{
            platform === "both"    ? `eas build --platform all --profile production` :
            platform === "android" ? `eas build --platform android --profile production\n# Produces: .aab (recommended for Play Store)\n# Or add --profile preview to get .apk for direct testing` :
                                     `eas build --platform ios --profile production\n# Produces: .ipa for App Store Connect`
          }</CodeBlock>
          <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 8, lineHeight: 1.5 }}>
            First build takes ~15 min. EAS emails you when it's done. Check progress at <code>expo.dev/accounts/[you]/projects/{config.app_slug}/builds</code>.
          </div>
        </div>
      ),
    },
    {
      n: 5, title: "Submit to stores",
      content: (
        <div>
          <CodeBlock>{
            platform === "both"    ? `eas submit --platform all --latest` :
            platform === "android" ? `eas submit --platform android --latest\n# Requires Google Play service account JSON in eas.json` :
                                     `eas submit --platform ios --latest\n# Requires Apple ID + app-specific password or API key`
          }</CodeBlock>
          <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 8 }}>
            {(platform === "both" || platform === "android") && (
              <InfoBox title="Google Play">
                Update <code>eas.json → submit.production.android.serviceAccountKeyPath</code> with your Google Play service account JSON. Create one at <em>Google Play Console → Setup → API access</em>.
              </InfoBox>
            )}
            {(platform === "both" || platform === "ios") && (
              <InfoBox title="App Store">
                Update <code>eas.json → submit.production.ios</code> with your Apple ID, App Store Connect App ID, and Team ID. Find these at <em>appstoreconnect.apple.com</em>.
              </InfoBox>
            )}
          </div>
        </div>
      ),
    },
    {
      n: 6, title: "Future updates",
      content: (
        <div>
          <div style={{ fontSize: 13, color: "var(--text-secondary)", lineHeight: 1.7, marginBottom: 10 }}>
            For code-only changes (not native), skip the store entirely:
          </div>
          <CodeBlock>{`# Bump version in app.json (not required for OTA)
# Make your code changes, then:
eas update --branch production --message "Your update description"
# Users get the update silently on next app launch`}</CodeBlock>
          <div style={{ fontSize: 13, color: "var(--text-secondary)", marginTop: 10, lineHeight: 1.7 }}>
            For native changes (new permissions, new packages with native code): bump the <code>version</code> and <code>buildNumber</code>/<code>versionCode</code>, then repeat steps 4–5.
          </div>
        </div>
      ),
    },
  ];

  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)", maxWidth: 860 }}>
      {/* Platform selector */}
      <Card style={{ marginBottom: "var(--space-lg)" }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span style={{ fontSize: 12, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>Target platform:</span>
          {(["both", "android", "ios"] as const).map(p => (
            <button key={p} onClick={() => setPlatform(p)} style={{
              padding: "5px 14px", borderRadius: 6, border: "none", cursor: "pointer", fontSize: 12, fontWeight: platform === p ? 700 : 400,
              background: platform === p ? "var(--accent)" : "var(--bg-elevated)",
              color: platform === p ? "#fff" : "var(--text-secondary)",
            }}>
              {p === "both" ? "Both" : p === "android" ? "🤖 Android" : "🍎 iOS"}
            </button>
          ))}
        </div>
      </Card>

      {steps.map((step, idx) => (
        <div key={step.n} style={{ display: "flex", gap: 16, marginBottom: "var(--space-lg)" }}>
          {/* Step number */}
          <div style={{ flexShrink: 0, display: "flex", flexDirection: "column", alignItems: "center" }}>
            <div style={{
              width: 32, height: 32, borderRadius: "50%",
              background: "var(--accent)", color: "#fff",
              display: "flex", alignItems: "center", justifyContent: "center",
              fontSize: 13, fontWeight: 700, fontFamily: "var(--font-mono)",
            }}>{step.n}</div>
            {idx < steps.length - 1 && <div style={{ width: 2, flex: 1, background: "var(--border-subtle)", marginTop: 6 }} />}
          </div>
          {/* Content */}
          <div style={{ flex: 1, paddingBottom: "var(--space-lg)" }}>
            <div style={{ fontSize: 15, fontWeight: 700, color: "var(--text-primary)", marginBottom: 10 }}>{step.title}</div>
            {step.content}
          </div>
        </div>
      ))}
    </div>
  );
}

/* ── Preview Tab ────────────────────────────────────────────────── */

function PreviewTab({ preview, loading }: { preview: any; loading: boolean }) {
  if (loading) return <div style={{ display: "flex", justifyContent: "center", padding: 60 }}><Spinner size={32} /></div>;
  if (!preview) return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "60%", color: "var(--text-muted)" }}>
      <div style={{ fontSize: 48, marginBottom: 12 }}>📱</div>
      <div style={{ fontSize: 13 }}>Click Preview to see generated files</div>
    </div>
  );
  return (
    <div style={{ padding: "var(--space-xl) var(--space-2xl)" }}>
      <div style={{ marginBottom: "var(--space-md)", fontSize: 13, color: "var(--text-muted)", fontFamily: "var(--font-mono)" }}>
        {preview.file_count} files · {(preview.total_size_bytes / 1024).toFixed(1)} KB
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {Object.entries(preview.files).map(([path, content]: any) => (
          <details key={path}>
            <summary style={{ padding: "6px 12px", background: "var(--bg-elevated)", borderRadius: 6, cursor: "pointer", fontSize: 11, fontFamily: "var(--font-mono)", color: "var(--text-primary)", listStyle: "none", display: "flex", justifyContent: "space-between" }}>
              <span>{path}</span>
              <span style={{ color: "var(--text-muted)" }}>{content.split("\n").length} lines</span>
            </summary>
            <pre style={{ background: "var(--bg-input)", padding: "var(--space-sm)", borderRadius: 6, fontSize: 10, overflow: "auto", maxHeight: 300, marginTop: 4, fontFamily: "var(--font-mono)", color: "var(--text-secondary)" }}>{content}</pre>
          </details>
        ))}
      </div>
    </div>
  );
}

/* ── Small primitives ───────────────────────────────────────────── */

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <div style={{ fontSize: 11, fontWeight: 700, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.07em", fontFamily: "var(--font-mono)", marginBottom: "var(--space-md)" }}>{children}</div>;
}
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: "var(--space-md)" }}>
      <label style={{ display: "block", fontSize: 10, fontWeight: 600, color: "var(--text-muted)", textTransform: "uppercase", fontFamily: "var(--font-mono)", marginBottom: 4, letterSpacing: "0.04em" }}>{label}</label>
      {children}
    </div>
  );
}
function Hint({ children }: { children: React.ReactNode }) {
  return <div style={{ fontSize: 10, color: "var(--text-muted)", marginTop: 4, lineHeight: 1.5 }}>{children}</div>;
}
function Input({ value, onChange, placeholder, style }: { value: string; onChange: (e: any) => void; placeholder?: string; style?: React.CSSProperties }) {
  return <input value={value} onChange={onChange} placeholder={placeholder} style={{ width: "100%", padding: "8px 12px", fontSize: 13, fontFamily: "var(--font-body)", background: "var(--bg-input)", border: "1px solid var(--border-default)", borderRadius: "var(--radius-sm)", color: "var(--text-primary)", outline: "none", boxSizing: "border-box", ...style }} />;
}
function CodeBlock({ children }: { children: React.ReactNode }) {
  return <pre style={{ background: "var(--bg-elevated)", border: "1px solid var(--border-subtle)", borderRadius: 6, padding: "10px 14px", fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--text-primary)", overflow: "auto", lineHeight: 1.6, margin: 0, whiteSpace: "pre-wrap" }}>{children}</pre>;
}
function RequirementList({ items }: { items: [string, string][] }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {items.map(([name, detail]) => (
        <div key={name} style={{ display: "flex", gap: 10, alignItems: "baseline", fontSize: 12 }}>
          <span style={{ color: "var(--status-completed)", fontWeight: 700, flexShrink: 0 }}>✓</span>
          <span style={{ fontWeight: 600, color: "var(--text-primary)", minWidth: 180 }}>{name}</span>
          <code style={{ color: "var(--text-muted)", fontFamily: "var(--font-mono)", fontSize: 11 }}>{detail}</code>
        </div>
      ))}
    </div>
  );
}
function InfoBox({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ padding: "10px 14px", borderRadius: 6, border: "1px solid var(--border-default)", background: "var(--bg-elevated)", fontSize: 12, lineHeight: 1.6 }}>
      <div style={{ fontWeight: 700, color: "var(--text-primary)", marginBottom: 4 }}>{title}</div>
      <div style={{ color: "var(--text-secondary)" }}>{children}</div>
    </div>
  );
}

const textareaStyle: React.CSSProperties = {
  width: "100%", padding: "8px 12px", fontSize: 13, fontFamily: "var(--font-body)",
  background: "var(--bg-input)", border: "1px solid var(--border-default)",
  borderRadius: "var(--radius-sm)", color: "var(--text-primary)", outline: "none",
  boxSizing: "border-box", resize: "vertical",
};
