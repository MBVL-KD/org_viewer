"""
Bekende schooldammen-locaties (handmatige lijst) voor overlay op scholenkaart.

Data: `data/schooldam_hotspots.json` (aan te vullen via Streamlit of handmatig).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Literal, Tuple

HERE = Path(__file__).resolve().parent
DEFAULT_PATH = HERE / 'data' / 'schooldam_hotspots.json'

Confidence = Literal['zeker', 'vermoedelijk']

DEFAULT_ENTRIES: List[Dict[str, Any]] = []


def _slug(s: str) -> str:
    s = ' '.join((s or '').split()).strip().lower()
    s = re.sub(r'[^a-z0-9]+', '-', s.replace('ß', 'ss'))
    return s.strip('-')[:72] or 'x'


def _expand_into(
    acc: List[Dict[str, Any]],
    line_id: str,
    land: str,
    gemeente: str,
    plaats_raw: str,
    confidence: Confidence,
    note: str = '',
) -> None:
    delims = re.split(r'[,;/]+', plaats_raw)
    parts = [' '.join(p.split()) for p in delims if p and str(p).strip()]
    if not parts:
        parts = [' '.join(gemeente.split())]
    gem = ' '.join(gemeente.split()).strip()
    for p in parts:
        pl = ' '.join(p.split()).strip()
        bid = f'{line_id}-{_slug(pl)}'
        acc.append({
            'id': bid,
            'land': land.strip().upper(),
            'gemeente': gem,
            'plaats': pl,
            'confidence': confidence,
            'note': note.strip(),
            'lat': None,
            'lon': None,
            'filter_gemeente': '',
        })


def _bootstrap_defaults() -> None:
    global DEFAULT_ENTRIES
    if DEFAULT_ENTRIES:
        return
    acc: List[Dict[str, Any]] = []

    # (line_id, gemeente_filter, plaatsen komma/slash-gescheiden)
    zeker: List[Tuple[str, str, str]] = [
        ('nl-g01', 'Den Helder', 'Den Helder, Julianadorp'),
        ('nl-g02', 'Hollands Kroon', 'Anna Paulowna'),
        ('nl-g03', 'Dijk en Waard', 'Heerhugowaard'),
        ('nl-g04', 'Schagen', 'Schagen'),
        ('nl-g05', 'Amstelveen', 'Amstelveen'),
        ('nl-g06', 'Velsen', 'IJmuiden, Santpoort-Zuid'),
        ('nl-g07', 'Purmerend', 'Purmerend'),
        ('nl-g08', 'Edam-Volendam', 'Middelie'),
        ('nl-g09', 'Zaanstad', 'Zaandam'),
        ('nl-g10', 'Lochem', 'Lochem/Laren/Harfsen/Eefde/Gorssel/Barchem/Almen'),
        ('nl-g11', 'Wageningen', 'Wageningen'),
        ('nl-g12', 'Oldebroek', 'Oldebroek'),
        ('nl-g13', 'Heerde', 'Heerde/Wapenveld/Oene'),
        ('nl-g14', 'Putten', 'Putten'),
        ('nl-g15', 'Doetinchem', 'Doetinchem'),
        ('nl-g16', 'Waddinxveen', 'Waddinxveen'),
        ('nl-g17', 'Alphen aan den Rijn', 'Boskoop'),
        ('nl-g18', 'Zuidplas', 'Moerkapelle'),
        ('nl-g19', 'Krimpenerwaard', 'Stolwijk'),
        ('nl-g20', 'Den Haag', 'Den Haag'),
        ('nl-g21', 'Gouda', 'Gouda'),
        ('nl-g22', 'Bodegraven-Reeuwijk', 'Bodegraven'),
        ('nl-g23', 'Molenlanden', 'Hoornaar'),
        ('nl-g24', 'Vijfheerenlanden', 'Meerkerk/Vianen'),
        ('nl-g25', 'Groningen', 'Groningen'),
        ('nl-g26', 'Het Hogeland', 'Warffum/Uithuizermeeden'),
        ('nl-g27', 'Westerkwartier', 'Marum'),
        ('nl-g28', 'Heerenveen', 'Aldeboarn'),
        ('nl-g29', 'Smallingerland', 'Opeinde'),
        ('nl-g30', 'Midden-Drenthe', 'Hijken/Beilen'),
        ('nl-g31', 'Hoogeveen', 'Hoogeveen'),
        ('nl-g32', 'Hardenberg', 'Gramsbergen/Dedemsvaart'),
        ('nl-g33', 'Baarn', 'Baarn'),
        ('nl-g34', 'Soest', 'Soest'),
    ]
    for lid, gem, plaats in zeker:
        _expand_into(acc, lid, 'NL', gem, plaats, 'zeker', '')

    # (line_id, gemeente, plaatsen, note)
    vermoedelijk: List[Tuple[str, str, str, str]] = [
        ('nl-u35', 'Haarlem', 'Haarlem', ''),
        ('nl-u36', 'Katwijk', 'Katwijk', ''),
        ('nl-u37', 'Zwartewaterland', 'Zwartsluis', ''),
        ('nl-u38', 'Kampen', 'Kampen', ''),
        ('nl-u39', 'Utrecht', 'Utrecht', ''),
        ('nl-u40', 'Oldambt', 'Winschoten', 'Oost-Groningen-context; nog verifiëren.'),
        ('nl-u41', 'Westerwolde', 'Bellingwolde', 'Oost-Groningen-context; vertegenwoordiger dorp.'),
        (
            'nl-u42',
            'Noardeast-Fryslân',
            'Rinsumageast',
            'Plaats in huidige Noardeast-Fryslân; grens Dantumadiel ooit verifiëren.',
        ),
    ]
    for lid, gem, plaats, note in vermoedelijk:
        _expand_into(acc, lid, 'NL', gem, plaats, 'vermoedelijk', note)

    for e in acc:
        if str(e.get('id', '')).startswith('nl-g20'):
            e['filter_gemeente'] = "'s-Gravenhage"

    DEFAULT_ENTRIES = acc


_bootstrap_defaults()

_DEFAULT_SNAPSHOT = list(DEFAULT_ENTRIES)


def hotspot_path(custom: Path | None = None) -> Path:
    return custom or DEFAULT_PATH


def ensure_default_json_file(path: Path | None = None) -> Path:
    p = hotspot_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.is_file():
        save_hotspots({'version': 3, 'entries': list(DEFAULT_ENTRIES)}, p)
    return p


def load_hotspots(path: Path | None = None) -> Dict[str, Any]:
    """Laad JSON; schrijf default bij het ontbreken van het bestand."""
    p = hotspot_path(path)
    ensure_default_json_file(p)
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def save_hotspots(payload: Dict[str, Any], path: Path | None = None) -> Path:
    p = hotspot_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write('\n')
    return p


def entries_for_land(payload: Dict[str, Any], land: str) -> List[Dict[str, Any]]:
    lc = land.strip().upper()
    return [e for e in (payload.get('entries') or []) if str(e.get('land') or '').upper() == lc]


def add_entries(
    new_rows: List[Dict[str, Any]],
    *,
    path: Path | None = None,
) -> int:
    """
    Voeg nieuwe entries toe aan JSON. Ontdubbelt op (land, gemeente, plaats).

    Retourneert aantal toegevoegde records.
    """
    p = hotspot_path(path)
    data = load_hotspots(p)
    existing = {
        (str(e['land']).upper(), _norm_key(e['gemeente']), _norm_key(e['plaats']))
        for e in data['entries']
    }
    added = 0
    for row in new_rows:
        lk = (
            str(row.get('land', '')).strip().upper(),
            _norm_key(row.get('gemeente', '')),
            _norm_key(row.get('plaats', '')),
        )
        if lk in existing or not lk[1] or not lk[2]:
            continue
        nid = row.get('id') or _make_id(lk[0], row.get('gemeente', ''), row.get('plaats', ''))
        entry = {
            'id': nid,
            'land': lk[0],
            'gemeente': row['gemeente'].strip(),
            'plaats': row['plaats'].strip(),
            'confidence': row.get('confidence') or 'zeker',
            'note': (row.get('note') or '').strip(),
            'lat': row.get('lat'),
            'lon': row.get('lon'),
            'filter_gemeente': (row.get('filter_gemeente') or '').strip(),
        }
        data.setdefault('entries', []).append(entry)
        existing.add(lk)
        added += 1
    save_hotspots(data, p)
    return added


def _norm_key(s: str) -> str:
    return ' '.join((s or '').split()).strip().lower()


def _make_id(land: str, gemeente: str, plaats: str) -> str:
    return f'{land.lower()}-{_slug(gemeente)}-{_slug(plaats)}-user'


def candidates_gemeente_voor_filter(entry: Dict[str, Any]) -> List[str]:
    """
    Kandidaten voor kaart-klik → gemeentefilter, in prioriteitsvolgorde.
    Eerst expliciete filter_gemeente (DUO/BRIN), daarna weergavenaam, daarna gangbare aliassen.
    """
    out: List[str] = []
    seen: set[str] = set()

    def add(s: str) -> None:
        t = ' '.join((s or '').split()).strip()
        if not t:
            return
        k = t.lower()
        if k not in seen:
            seen.add(k)
            out.append(t)

    add(entry.get('filter_gemeente') or '')
    add(entry.get('gemeente') or '')

    cid = str(entry.get('id') or '')
    if cid.startswith('nl-g20'):
        add("'s-Gravenhage")
        add('Den Haag')

    return out


def resolve_pick_gemeente(
    *,
    hotspot_id: str,
    gemeente: str,
    filter_gemeente: str,
    pool_lc: Dict[str, str],
) -> str:
    """Kies de eerste kandidaat die in de school-tabel (gemeente-kolom) voorkomt."""
    fake: Dict[str, Any] = {
        'id': hotspot_id,
        'gemeente': gemeente,
        'filter_gemeente': filter_gemeente,
    }
    for c in candidates_gemeente_voor_filter(fake):
        hit = pool_lc.get(c.strip().lower())
        if hit:
            return hit
    cand = candidates_gemeente_voor_filter(fake)
    return cand[0] if cand else gemeente


def migrate_reset_to_module_defaults(path: Path | None = None) -> Path:
    """Herschrijf JSON met module-ingebouwde defaults (onderhoud)."""
    return save_hotspots({'version': 3, 'entries': list(_DEFAULT_SNAPSHOT)}, path)
