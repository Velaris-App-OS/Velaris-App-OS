/**
 * Location field — a map pin (Form Builder controls, 2026-10-29).
 *
 * The value is `{lat, lng}`. Latitude / longitude boxes and "Use my location"
 * always work. The map appears only when this install serves its own tiles:
 * `/api/v1/map/basemap.pmtiles` (the gateway, from `deploy/map-tiles/`). No file
 * = no map, and nothing is ever fetched from a third-party tile server. Place
 * names appear when the same directory also holds `fonts/`.
 *
 * MapLibre is loaded on demand, so a form without a location field — or an
 * install without tiles — never downloads it.
 *
 * Copyright (c) 2024-2026 Velaris Contributors
 * SPDX-License-Identifier: BSL-1.1
 */
import React, { useEffect, useRef, useState } from "react";
import { useTheme } from "@/theme/ThemeContext";
import { locationError } from "./validate";

export const MAP_BASE = "/api/v1/map";
const TILES = `${MAP_BASE}/basemap.pmtiles`;
const GLYPHS = `${MAP_BASE}/fonts/{fontstack}/{range}.pbf`;

type Pin = { lat: number; lng: number };

/** Does this install serve tiles (and fonts)? Asked once per page load. */
let probe: Promise<{ tiles: boolean; fonts: boolean }> | null = null;
export function mapAvailability(): Promise<{ tiles: boolean; fonts: boolean }> {
  const ok = (url: string) => fetch(url, { method: "HEAD" }).then(r => r.ok, () => false);
  probe ??= Promise.all([ok(TILES), ok(`${MAP_BASE}/fonts/Noto%20Sans%20Regular/0-255.pbf`)])
    .then(([tiles, fonts]) => ({ tiles, fonts: tiles && fonts }));
  return probe;
}

/** For tests: forget the answer. */
export function resetMapAvailability() { probe = null; }

const round = (n: number) => Math.round(n * 1e6) / 1e6;     // ~0.1 m

export default function LocationField({ value, onChange, readOnly, inputStyle, error }: {
  value: any;
  onChange: (v: Pin | undefined) => void;
  readOnly?: boolean;
  inputStyle: React.CSSProperties;
  error?: string;
}) {
  const pin: Pin | null = value && !locationError(value) ? value : null;
  const [lat, setLat] = useState<string>(value?.lat ?? "");
  const [lng, setLng] = useState<string>(value?.lng ?? "");
  const [locating, setLocating] = useState(false);
  const [geoError, setGeoError] = useState<string | null>(null);
  const [map, setMap] = useState<{ tiles: boolean; fonts: boolean } | null>(null);

  // Keep the boxes in step with a value set from outside (pre-fill, the map).
  useEffect(() => {
    setLat(value?.lat ?? "");
    setLng(value?.lng ?? "");
  }, [value?.lat, value?.lng]);

  useEffect(() => { let live = true; mapAvailability().then(a => live && setMap(a)); return () => { live = false; }; }, []);

  const emit = (la: string, ln: string) => {
    if (la === "" && ln === "") return onChange(undefined);
    const n = (s: string) => (s === "" || isNaN(Number(s)) ? null : Number(s));
    onChange({ lat: n(la), lng: n(ln) } as any);   // the validator names a half-filled pin
  };

  const locate = () => {
    if (!navigator.geolocation) return setGeoError("This browser cannot share its location");
    setLocating(true); setGeoError(null);
    navigator.geolocation.getCurrentPosition(
      p => { setLocating(false); onChange({ lat: round(p.coords.latitude), lng: round(p.coords.longitude) }); },
      e => { setLocating(false); setGeoError(e.code === e.PERMISSION_DENIED ? "Location permission was refused" : "Location is not available"); },
      { enableHighAccuracy: true, timeout: 15000 },
    );
  };

  const box = { ...inputStyle, width: "100%", fontFamily: "var(--font-mono)" };
  return (
    <div>
      {map?.tiles && (
        <PinMap pin={pin} readOnly={readOnly} fonts={map.fonts} onPick={p => onChange(p)} invalid={!!error} />
      )}
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: map?.tiles ? 8 : 0, flexWrap: "wrap" }}>
        <label style={{ flex: "1 1 120px", fontSize: 11, color: "var(--text-muted)" }}>
          Latitude
          <input type="number" step="any" min={-90} max={90} value={lat} disabled={readOnly} aria-label="Latitude"
            onChange={e => { setLat(e.target.value); emit(e.target.value, lng); }} style={box} placeholder="51.5074" />
        </label>
        <label style={{ flex: "1 1 120px", fontSize: 11, color: "var(--text-muted)" }}>
          Longitude
          <input type="number" step="any" min={-180} max={180} value={lng} disabled={readOnly} aria-label="Longitude"
            onChange={e => { setLng(e.target.value); emit(lat, e.target.value); }} style={box} placeholder="-0.1278" />
        </label>
        {!readOnly && (
          <div style={{ display: "flex", gap: 6, alignSelf: "flex-end" }}>
            <button type="button" onClick={locate} disabled={locating} style={smallBtn}>
              {locating ? "Locating…" : "Use my location"}
            </button>
            {value != null && <button type="button" onClick={() => onChange(undefined)} style={smallBtn}>Clear</button>}
          </div>
        )}
      </div>
      {geoError && <div style={{ fontSize: 11, color: "var(--text-muted)", marginTop: 4 }}>{geoError}</div>}
    </div>
  );
}

const smallBtn: React.CSSProperties = {
  padding: "6px 10px", fontSize: 12, borderRadius: 6, cursor: "pointer",
  border: "1px solid var(--border-default)", background: "var(--bg-surface)", color: "var(--text-primary)",
};

/** The MapLibre map: click (or drag the pin) to place it. */
function PinMap({ pin, readOnly, fonts, onPick, invalid }: {
  pin: Pin | null; readOnly?: boolean; fonts: boolean; onPick: (p: Pin) => void; invalid: boolean;
}) {
  const el = useRef<HTMLDivElement>(null);
  const mapRef = useRef<any>(null);
  const markerRef = useRef<any>(null);
  const libRef = useRef<any>(null);
  const pick = useRef(onPick);
  pick.current = onPick;
  const [failed, setFailed] = useState(false);
  const dark = useTheme().resolvedTheme === "dark";

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [maplibre, pm, basemaps] = await Promise.all([
          import("maplibre-gl"), import("pmtiles"), import("@protomaps/basemaps"),
          import("maplibre-gl/dist/maplibre-gl.css"),
        ]);
        if (cancelled || !el.current) return;
        const lib: any = (maplibre as any).default ?? maplibre;
        libRef.current = lib;
        const protocol = new pm.Protocol();
        lib.addProtocol?.("pmtiles", protocol.tile);
        const archive = new pm.PMTiles(TILES);
        protocol.add(archive);
        const h = await archive.getHeader();
        if (cancelled || !el.current) return;
        const layers = basemaps.layers("basemap", basemaps.namedFlavor(dark ? "dark" : "light"), { lang: "en" })
          .filter((l: any) => fonts || l.type !== "symbol")
          // No sprite sheet is served: keep a point's name, drop its icon.
          .map((l: any) => {
            if (!l.layout?.["icon-image"]) return l;
            const { "icon-image": _icon, ...layout } = l.layout;
            return { ...l, layout };
          });
        const m = new lib.Map({
          container: el.current,
          style: {
            version: 8,
            ...(fonts ? { glyphs: GLYPHS } : {}),
            sources: { basemap: { type: "vector", url: `pmtiles://${TILES}`, attribution: "© OpenStreetMap" } },
            layers,
          },
          center: pin ? [pin.lng, pin.lat] : [h.centerLon, h.centerLat],
          zoom: pin ? 14 : Math.max(h.centerZoom ?? 2, h.minZoom ?? 0),
          maxBounds: h.minLon < h.maxLon ? [[h.minLon, h.minLat], [h.maxLon, h.maxLat]] : undefined,
          attributionControl: { compact: true },
        });
        m.addControl(new lib.NavigationControl({ showCompass: false }), "top-right");
        if (!readOnly) {
          m.on("click", (e: any) => pick.current({ lat: round(e.lngLat.lat), lng: round(e.lngLat.lng) }));
        }
        mapRef.current = m;
        placeMarker(pin);
      } catch {
        if (!cancelled) setFailed(true);
      }
    })();
    return () => { cancelled = true; mapRef.current?.remove(); mapRef.current = null; markerRef.current = null; };
    // The map is built once per theme; the pin is moved by the effect below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dark, fonts, readOnly]);

  const placeMarker = (p: Pin | null) => {
    const m = mapRef.current, lib = libRef.current;
    if (!m || !lib) return;
    if (!p) { markerRef.current?.remove(); markerRef.current = null; return; }
    if (!markerRef.current) {
      markerRef.current = new lib.Marker({ draggable: !readOnly, color: "#2563eb" }).setLngLat([p.lng, p.lat]).addTo(m);
      markerRef.current.on("dragend", () => {
        const ll = markerRef.current.getLngLat();
        pick.current({ lat: round(ll.lat), lng: round(ll.lng) });
      });
    } else {
      markerRef.current.setLngLat([p.lng, p.lat]);
    }
    if (!m.getBounds().contains([p.lng, p.lat])) m.easeTo({ center: [p.lng, p.lat] });
  };

  useEffect(() => { placeMarker(pin); }, [pin?.lat, pin?.lng]);   // eslint-disable-line react-hooks/exhaustive-deps

  if (failed) return <div style={{ fontSize: 11, color: "var(--text-muted)" }}>The map could not be loaded — enter the coordinates below.</div>;
  return (
    <div ref={el} role="application" aria-label="Map — click to place the pin"
      style={{ height: 260, borderRadius: 8, overflow: "hidden",
        border: `1px solid ${invalid ? "var(--status-failed)" : "var(--border-default)"}` }} />
  );
}

/** The value as text, for read-only places (case data, review step). */
export function locationText(v: any): string {
  return v && !locationError(v) ? `${v.lat}, ${v.lng}` : "";
}
