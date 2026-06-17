// Group J — browser-side WebAuthn helpers.
// The WebAuthn API speaks ArrayBuffers; the server speaks base64url.
// These helpers are the entire conversion layer plus the two ceremony calls.

export function b64uToBuf(s: string): ArrayBuffer {
  const pad = s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (s.length % 4)) % 4);
  const bin = atob(pad);
  const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  return buf.buffer;
}

export function bufToB64u(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function passkeysSupported(): boolean {
  return typeof window !== "undefined" && !!window.PublicKeyCredential;
}

/** Run navigator.credentials.get against server-issued request options
 *  (from /webauthn/login/options or /webauthn/stepup/options) and return
 *  the assertion serialized the way the backend expects. */
export async function getAssertion(options: any): Promise<any> {
  const publicKey: PublicKeyCredentialRequestOptions = {
    ...options,
    challenge: b64uToBuf(options.challenge),
    allowCredentials: (options.allowCredentials ?? []).map((c: any) => ({
      ...c, id: b64uToBuf(c.id),
    })),
  };
  const cred = (await navigator.credentials.get({ publicKey })) as PublicKeyCredential;
  if (!cred) throw new Error("Passkey prompt was cancelled.");
  const resp = cred.response as AuthenticatorAssertionResponse;
  return {
    id: cred.id,
    rawId: bufToB64u(cred.rawId),
    type: cred.type,
    response: {
      clientDataJSON: bufToB64u(resp.clientDataJSON),
      authenticatorData: bufToB64u(resp.authenticatorData),
      signature: bufToB64u(resp.signature),
      userHandle: resp.userHandle ? bufToB64u(resp.userHandle) : null,
    },
    clientExtensionResults: cred.getClientExtensionResults(),
  };
}
