#!/usr/bin/env python3
"""
Vul lat/lon in data/schooldam_hotspots.json (Nominatim via scraper.geocode_query).

    python3 geocode_schooldam_hotspots.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for env_file in (
    ROOT / 'Editor' / 'server' / '.env',
    ROOT / 'Editor' / '.env',
    ROOT / '.env',
):
    if env_file.exists():
        load_dotenv(env_file)
        break

sys.path.insert(0, str(HERE))

from schooldam_hotspots import hotspot_path, load_hotspots, save_hotspots
from scraper import geocode_query, is_valid_be_coords, is_valid_de_coords, is_valid_nl_coords


def _label(land: str) -> str:
    return {'NL': 'Nederland', 'BE': 'België', 'DE': 'Deutschland'}.get(land, 'Nederland')


def _cc(land: str) -> str:
    return {'NL': 'nl', 'BE': 'be', 'DE': 'de'}.get(land, 'nl')


def _ok(lat: float, lon: float, land: str) -> bool:
    if land == 'DE':
        return is_valid_de_coords(lat, lon)
    if land == 'BE':
        return is_valid_be_coords(lat, lon)
    return is_valid_nl_coords(lat, lon)


def main() -> None:
    p = hotspot_path()
    data = load_hotspots(p)
    entries = data.get('entries') or []
    n_up = 0
    for i, e in enumerate(entries):
        land = str(e.get('land') or 'NL').upper()
        lat, lon = e.get('lat'), e.get('lon')
        if lat is not None and lon is not None:
            try:
                if _ok(float(lat), float(lon), land):
                    continue
            except (TypeError, ValueError):
                pass
        q = f"{e['plaats']}, {e['gemeente']}, {_label(land)}"
        g = geocode_query(
            q,
            plaats_expected=str(e.get('plaats') or '').strip() or None,
            countrycodes=_cc(land),
        )
        if not g and land == 'NL':
            g = geocode_query(
                f"{e['plaats']}, Friesland, Nederland",
                plaats_expected=str(e.get('plaats') or '').strip() or None,
                countrycodes='nl',
            )
        if not g:
            print(f"[skip] geen hit: {e.get('id')} {q}", flush=True)
            continue
        la, lo = float(g['lat']), float(g['lon'])
        if not _ok(la, lo, land):
            print(f"[skip] buiten land: {e.get('id')}", flush=True)
            continue
        entries[i]['lat'] = la
        entries[i]['lon'] = lo
        entries[i]['geocoded_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        n_up += 1
        print(f"[ok] {e.get('id')} → {la:.5f},{lo:.5f}", flush=True)

    data['entries'] = entries
    save_hotspots(data, p)
    print(f'Klaar: {n_up} bijgewerkt → {p}', flush=True)


if __name__ == '__main__':
    main()
