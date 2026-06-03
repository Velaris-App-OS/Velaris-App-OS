import React, { createContext, useContext, useEffect, useState } from "react";
import { DEFAULT_THEME, THEME_STORAGE_KEY, type ThemeMode } from "@/branding";

function getSystemTheme(): "dark" | "light" {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function resolveTheme(mode: ThemeMode): "dark" | "light" {
  return mode === "system" ? getSystemTheme() : mode;
}

interface ThemeCtx {
  theme: ThemeMode;
  resolvedTheme: "dark" | "light";
  toggle: () => void;
  setTheme: (t: ThemeMode) => void;
}

const Ctx = createContext<ThemeCtx>({
  theme: DEFAULT_THEME,
  resolvedTheme: "dark",
  toggle: () => {},
  setTheme: () => {},
});

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, setThemeState] = useState<ThemeMode>(() => {
    const stored = localStorage.getItem(THEME_STORAGE_KEY) as ThemeMode | null;
    return stored ?? DEFAULT_THEME;
  });
  const [resolvedTheme, setResolvedTheme] = useState<"dark" | "light">(() => resolveTheme(
    (localStorage.getItem(THEME_STORAGE_KEY) as ThemeMode | null) ?? DEFAULT_THEME
  ));

  useEffect(() => {
    const resolved = resolveTheme(theme);
    setResolvedTheme(resolved);
    document.documentElement.setAttribute("data-theme", resolved);
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  }, [theme]);

  // Sync when another part of the app (e.g. login page) writes to the same key.
  // The native storage event only fires for cross-tab writes, so LoginPage
  // manually dispatches a StorageEvent for same-tab updates.
  useEffect(() => {
    const handler = (e: StorageEvent) => {
      if (e.key === THEME_STORAGE_KEY && e.newValue) {
        setThemeState(e.newValue as ThemeMode);
      }
    };
    window.addEventListener("storage", handler);
    return () => window.removeEventListener("storage", handler);
  }, []);

  // React to OS preference changes when in "system" mode
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const handler = () => {
      if (theme === "system") {
        const resolved = getSystemTheme();
        setResolvedTheme(resolved);
        document.documentElement.setAttribute("data-theme", resolved);
      }
    };
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, [theme]);

  const setTheme = (t: ThemeMode) => setThemeState(t);
  const toggle = () =>
    setThemeState(prev =>
      prev === "dark" ? "light" : prev === "light" ? "system" : "dark"
    );

  return (
    <Ctx.Provider value={{ theme, resolvedTheme, toggle, setTheme }}>
      {children}
    </Ctx.Provider>
  );
}

export function useTheme() { return useContext(Ctx); }
