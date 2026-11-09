// R5d 11b — tells the user when figures on this page come from a read copy
// that is behind (shared/api/dataAsOf.ts). Silent while data is live; cleared
// when the user moves to another page.
//
// Copyright (c) 2024-2026 Velaris Contributors
// SPDX-License-Identifier: BSL-1.1
import React, { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { DATA_AS_OF_EVENT, STALE_NOTICE_SECONDS, type DataAsOf } from "@shared/api/dataAsOf";

export default function DataAsOfNotice() {
  const location = useLocation();
  const [oldest, setOldest] = useState<DataAsOf | null>(null);

  useEffect(() => { setOldest(null); }, [location.pathname]);

  useEffect(() => {
    const on = (e: Event) => {
      const d = (e as CustomEvent<DataAsOf>).detail;
      if (!d || d.ageSeconds < STALE_NOTICE_SECONDS) return;
      setOldest(prev => (!prev || d.ageSeconds > prev.ageSeconds ? d : prev));
    };
    window.addEventListener(DATA_AS_OF_EVENT, on);
    return () => window.removeEventListener(DATA_AS_OF_EVENT, on);
  }, []);

  if (!oldest) return null;
  const at = new Date(oldest.asOf);
  const age = Math.round(oldest.ageSeconds);
  return (
    <div role="status" style={{
      margin: "8px 16px 0", padding: "6px 10px", borderRadius: 6, fontSize: 12,
      background: "rgba(247,183,49,0.12)", color: "var(--text-secondary)",
      border: "1px solid rgba(247,183,49,0.35)",
    }}>
      Some figures on this page are from a read copy and are {age >= 120 ? `${Math.round(age / 60)} min` : `${age} s`} old
      (as of {at.toLocaleTimeString()}). Your own changes always show at once.
    </div>
  );
}
