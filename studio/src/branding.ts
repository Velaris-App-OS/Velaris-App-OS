/**
 * Velaris Brand Configuration — single source of truth.
 *
 * To rename the platform: change BRAND.name here.
 * To re-theme: change accent / font values here AND update
 * the matching CSS variables in global.css.
 *
 * This file is the ONLY place where brand identity is defined.
 * All UI components import from here — never hardcode strings.
 */

export const BRAND = {
  /** Platform name shown in sidebar, login, portal headers, emails. */
  name:    "Velaris",

  /** Product-line label shown next to the name (e.g. "STUDIO"). */
  studio:  "STUDIO",

  /** Short tagline used on login pages and marketing surfaces. */
  tagline: "BPM Platform",

  /** Default accent color — mirrors CSS --accent. */
  accent:  "#4ecdc4",

  /** Path to the logo image (relative to public/). Empty string = use logoChar fallback. */
  logoSrc: "/velaris.png",

  /**
   * Logo character shown in avatar slots when no image is set.
   * Defaults to first character of name; override for a custom symbol.
   */
  get logoChar() { return this.name.charAt(0); },

  /** Full display string used in document <title> tags. */
  get fullTitle() { return `${this.name} ${this.studio}`; },
} as const;

/** Theme mode — stored in localStorage under this key. */
export const THEME_STORAGE_KEY = "velaris_theme";
export type ThemeMode = "dark" | "light" | "system";
export const DEFAULT_THEME: ThemeMode = "system";
