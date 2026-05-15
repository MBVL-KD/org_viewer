"""Landelijke dambonden en onderliggende bonden (NL provinciaal, BE regio)."""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from nl_provincie import _CLUB_BOND_CODE_TO_CANONICAL_PROVINCIE, canonical_nl_provincie_club

# Landcode → landelijke bond (uitbreidbaar: België, …)
NATIONAL_BONDS: Dict[str, Dict[str, str]] = {
    'KNDB': {
        'code': 'KNDB',
        'land': 'NL',
        'label': 'KNDB (Nederland)',
        'url': 'https://www.kndb.nl/',
    },
    'KBDB': {
        'code': 'KBDB',
        'land': 'BE',
        'label': 'KBDB (België)',
        'url': 'https://sites.google.com/view/frbjd-kbdb/nl/home',
    },
}

BE_PROVINCIE_NAMES = frozenset({
    'West-Vlaanderen', 'Oost-Vlaanderen', 'Antwerpen', 'Limburg', 'Brussel',
    'Luik', 'Henegouwen', 'Namen', 'Luxemburg', 'Waals-Brabant', 'Vlaams-Brabant',
})

_LAND_TO_NATIONAL_BOND: Dict[str, str] = {
    meta['land']: code for code, meta in NATIONAL_BONDS.items()
}

NL_PROVINCIAL_BOND_CODES = frozenset(_CLUB_BOND_CODE_TO_CANONICAL_PROVINCIE.keys())

SUB_BOND_LABEL: Dict[str, str] = {
    'KNDB': 'Provinciale bond',
    'KBDB': 'Regio',
}


def national_bond_label(code: str) -> str:
    c = (code or '').strip().upper()
    if not c:
        return '—'
    meta = NATIONAL_BONDS.get(c)
    if meta:
        return meta['label']
    # Fallback als code in Mongo staat maar NATIONAL_BONDS op server nog oud is
    if c == 'KBDB':
        return 'KBDB (België)'
    if c == 'KNDB':
        return 'KNDB (Nederland)'
    return c


def bond_land_codes_in_data(clubs: Iterable[dict]) -> List[str]:
    """Unieke landelijke bondcodes uit clubdocumenten (geen lege waarden)."""
    codes: set[str] = set()
    for club in clubs:
        c = enrich_club_bond_fields(dict(club))
        bl = (c.get('bond_land') or '').strip().upper()
        if bl:
            codes.add(bl)
        elif c.get('land') == 'BE':
            codes.add('KBDB')
        else:
            codes.add('KNDB')
    return sorted(codes, key=lambda x: national_bond_label(x).lower())


def national_bond_options_for_viewer() -> List[str]:
    """Sorteer op label voor dropdown."""
    return sorted(NATIONAL_BONDS.keys(), key=lambda c: national_bond_label(c).lower())


def infer_club_land(provincie: Optional[str], land: Optional[str] = None) -> str:
    if land and str(land).strip():
        return str(land).strip().upper()
    prov = str(provincie or '').strip()
    if prov.upper() in NL_PROVINCIAL_BOND_CODES:
        return 'NL'
    if prov in BE_PROVINCIE_NAMES:
        return 'BE'
    return 'NL'


def national_bond_for_land(land: str) -> str:
    return _LAND_TO_NATIONAL_BOND.get((land or '').strip().upper(), '')


def sub_bond_filter_title(bond_land: Optional[str]) -> str:
    """Label voor filter op onderliggende bond (niet alles onder landelijke bond platsen)."""
    code = (bond_land or '').strip().upper()
    return SUB_BOND_LABEL.get(code, 'Onderliggende bond')


def club_bond_regio(club: dict) -> str:
    """Belgische regio onder KBDB (West-Vlaanderen, Brussel, …)."""
    raw = club.get('bond_regio') or ''
    if not raw and club.get('land') == 'BE':
        prov = str(club.get('provincie') or '').strip()
        if prov in BE_PROVINCIE_NAMES:
            raw = prov
    return str(raw).strip()


def club_bond_provinciaal(club: dict) -> str:
    """Nederlandse provinciale bondcode onder KNDB (PNHD, ZHDB, …)."""
    if club.get('land') == 'BE':
        return ''
    return str(club.get('provincie') or '').strip()


def club_bond_onder(club: dict) -> str:
    """Eenduidige sleutel voor filter op tweede niveau (bond-specifiek)."""
    land = infer_club_land(club.get('provincie'), club.get('land'))
    if land == 'BE':
        return club_bond_regio(club)
    return club_bond_provinciaal(club)


def club_bond_onder_label(club: dict) -> str:
    """Weergavenaam tweede niveau in viewer/export."""
    land = infer_club_land(club.get('provincie'), club.get('land'))
    if land == 'BE':
        return club_bond_regio(club)
    code = club_bond_provinciaal(club)
    if not code:
        return ''
    prov = canonical_nl_provincie_club(code)
    if prov and prov != code:
        return f'{code} ({prov})'
    return code


def enrich_club_bond_fields(club: dict) -> dict:
    """Vul land, landelijke bond en onderliggende bond-velden aan."""
    land = infer_club_land(club.get('provincie'), club.get('land'))
    bond_land = (club.get('bond_land') or '').strip().upper()
    if not bond_land:
        bond_land = national_bond_for_land(land)
    out = dict(club)
    out['land'] = land
    if bond_land:
        out['bond_land'] = bond_land
    if land == 'BE':
        regio = club_bond_regio(out)
        if regio:
            out['bond_regio'] = regio
        # Geen NL-provinciecodes op Belgische clubs
        if str(out.get('provincie') or '') in BE_PROVINCIE_NAMES:
            out.pop('provincie', None)
    out['bond_onder'] = club_bond_onder(out)
    return out


def club_matches_national_bond(club: dict, bond_land_sel: str) -> bool:
    if not bond_land_sel:
        return True
    want = bond_land_sel.strip().upper()
    got = (club.get('bond_land') or national_bond_for_land(infer_club_land(club.get('provincie'), club.get('land')))).upper()
    return got == want
