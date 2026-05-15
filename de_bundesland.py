"""Bundesland-codes (JedeSchule) naar volledige Duitse namen."""
from __future__ import annotations

from typing import Optional

_BUNDESLAND_CODE_TO_NAME = {
    'BW': 'Baden-Württemberg',
    'BY': 'Bayern',
    'BE': 'Berlin',
    'BB': 'Brandenburg',
    'HB': 'Bremen',
    'HH': 'Hamburg',
    'HE': 'Hessen',
    'MV': 'Mecklenburg-Vorpommern',
    'NI': 'Niedersachsen',
    'NW': 'Nordrhein-Westfalen',
    'RP': 'Rheinland-Pfalz',
    'SL': 'Saarland',
    'SN': 'Sachsen',
    'ST': 'Sachsen-Anhalt',
    'SH': 'Schleswig-Holstein',
    'TH': 'Thüringen',
}

_NAME_ALIASES = {
    'baden wuerttemberg': 'Baden-Württemberg',
    'baden-wurttemberg': 'Baden-Württemberg',
    'baden-württemberg': 'Baden-Württemberg',
    'nordrhein westfalen': 'Nordrhein-Westfalen',
    'nordrhein-westfalen': 'Nordrhein-Westfalen',
    'rheinland pfalz': 'Rheinland-Pfalz',
    'rheinland-pfalz': 'Rheinland-Pfalz',
    'mecklenburg vorpommern': 'Mecklenburg-Vorpommern',
    'mecklenburg-vorpommern': 'Mecklenburg-Vorpommern',
    'sachsen anhalt': 'Sachsen-Anhalt',
    'sachsen-anhalt': 'Sachsen-Anhalt',
    'schleswig holstein': 'Schleswig-Holstein',
    'schleswig-holstein': 'Schleswig-Holstein',
}


def bundesland_code_from_school_id(school_id: str) -> str:
    """JedeSchule-id is bv. ``NI-75061`` → ``NI``."""
    if not school_id:
        return ''
    part = str(school_id).strip().split('-', 1)[0].upper()
    if len(part) == 2 and part.isalpha():
        return part
    return ''


def normalize_de_bundesland(value: Optional[str], school_id: str = '') -> str:
    """Volledige Bundeslandnaam; val terug op code uit school-id."""
    raw = (value or '').strip()
    if not raw and school_id:
        raw = bundesland_code_from_school_id(school_id)
    if not raw:
        return ''
    code = raw.upper()
    if len(code) == 2 and code in _BUNDESLAND_CODE_TO_NAME:
        return _BUNDESLAND_CODE_TO_NAME[code]
    low = raw.lower().replace('ü', 'ue').replace('ö', 'oe').replace('ä', 'ae')
    low = low.replace('ß', 'ss')
    if low in _NAME_ALIASES:
        return _NAME_ALIASES[low]
    for name in _BUNDESLAND_CODE_TO_NAME.values():
        if name.lower() == raw.lower():
            return name
    return raw


def bundesland_filter_label(code_or_name: str) -> str:
    """Sidebar-label: ``BW (Baden-Württemberg)``."""
    s = (code_or_name or '').strip()
    if not s:
        return s
    if len(s) == 2 and s.upper() in _BUNDESLAND_CODE_TO_NAME:
        c = s.upper()
        return f'{c} ({_BUNDESLAND_CODE_TO_NAME[c]})'
    code = ''
    for c, name in _BUNDESLAND_CODE_TO_NAME.items():
        if name == s:
            code = c
            break
    if code:
        return f'{code} ({s})'
    return s
