import React, { createContext, useContext, useEffect, useState } from "react";

type Flags = Record<string, string | null>;

interface FeatureFlagsCtx {
  flags:     Flags;
  isEnabled: (featureKey: string) => boolean;
  loading:   boolean;
}

const FeatureFlagsContext = createContext<FeatureFlagsCtx>({
  flags:     {},
  isEnabled: () => false,
  loading:   true,
});

export function FeatureFlagsProvider({ children }: { children: React.ReactNode }) {
  const [flags, setFlags]     = useState<Flags>({});
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch("/api/v1/releases/flags")
      .then(r => r.ok ? r.json() : {})
      .then(data => { setFlags(data); setLoading(false); })
      .catch(() => setLoading(false));
  }, []);

  // A feature is enabled if the DB has any non-null version string for it.
  // The version value itself comes from the DB — never compared in the frontend.
  const isEnabled = (featureKey: string) => !!flags[featureKey];

  return (
    <FeatureFlagsContext.Provider value={{ flags, isEnabled, loading }}>
      {children}
    </FeatureFlagsContext.Provider>
  );
}

export function useFeatureFlags() {
  return useContext(FeatureFlagsContext);
}
