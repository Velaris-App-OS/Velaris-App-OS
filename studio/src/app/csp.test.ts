/**
 * Studio's security headers (2026-10-29): report-only unless told to enforce,
 * no inline script or eval, the meeting server and an operator's own origins
 * allowed to connect, framed by nobody else.
 */
import { describe, expect, it } from "vitest";
import { contentSecurityPolicy, cspMode, securityHeaders } from "../../csp.config";

const directive = (p: string, name: string) => p.split("; ").find(d => d.startsWith(`${name} `)) ?? "";

describe("Studio security headers", () => {
  it("only report when nothing is set — an update blocks nothing", () => {
    expect(cspMode(undefined)).toBe("report-only");
    expect(cspMode("nonsense")).toBe("report-only");
    const h = securityHeaders({});
    expect(Object.keys(h)).toEqual(["Content-Security-Policy-Report-Only"]);
    expect(h["Content-Security-Policy-Report-Only"]).toContain("report-uri /api/v1/csp-reports");
  });

  it("enforce sends the policy and the companion headers; off sends nothing", () => {
    const h = securityHeaders({ VELARIS_STUDIO_CSP: "enforce" });
    expect(h["Content-Security-Policy"]).toBeTruthy();
    expect(h["X-Frame-Options"]).toBe("SAMEORIGIN");
    expect(h["X-Content-Type-Options"]).toBe("nosniff");
    expect(securityHeaders({ VELARIS_STUDIO_CSP: "off" })).toEqual({});
  });

  it("allows no inline script, no eval, no plugins, and no framing by other sites", () => {
    const p = contentSecurityPolicy({});
    expect(directive(p, "script-src")).toBe("script-src 'self'");
    expect(p).not.toContain("unsafe-eval");
    expect(directive(p, "object-src")).toBe("object-src 'none'");
    expect(directive(p, "frame-ancestors")).toBe("frame-ancestors 'self'");
    expect(directive(p, "connect-src")).toBe("connect-src 'self'");
  });

  it("lets Studio reach the meeting server and an operator's own origins, nothing else", () => {
    const p = contentSecurityPolicy({ VELARIS_CASE_LIVEKIT_URL: "wss://meet.example.com:7880",
      VELARIS_STUDIO_CSP_CONNECT: "https://api.example.org/x  javascript:alert(1) not-a-url" });
    expect(directive(p, "connect-src")).toBe(
      "connect-src 'self' wss://meet.example.com:7880 https://meet.example.com:7880 https://api.example.org wss://api.example.org");
  });
});
