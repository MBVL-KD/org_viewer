"""Normaliseer Nederlandse DUO-schoolsoorten naar hoofdcategorieën."""
from __future__ import annotations

import re
from typing import Optional

NL_SOORT_NORM_VALUES = (
    'Basisonderwijs',
    'VO',
    'MBO',
    'HBO',
    'WO',
    'Speciaal onderwijs',
)

_SPECiaal_SOORT_MARKERS = (
    'vsg',
    'voorbereidend speciaal',
    'speciaal onderwijs',
    'instituut voor doven',
    'doof',
    'slechthorend',
    'blind',
    'visueel beperkt',
    'orthopedagog',
    'clusterschool',
    's(b)o',
    'so ',
    'so/',
    '/so',
)

_VO_MARKERS = (
    'vbo',
    'mavo',
    'havo',
    'vwo',
    'pro',
    'brugjaar',
    'beroepscollege',
    'vo-school',
    'vo school',
)

_MBO_MARKERS = (
    'regionaal opleidingencentrum',
    'roc ',
    ' roc',
    'mbo ',
    'mbo-',
)


def _low(s: Optional[str]) -> str:
    return (s or '').strip().lower()


def _is_speciaal_soort(soort: str, bron: str) -> bool:
    text = f'{soort} {bron}'.lower()
    if 'vsg' in text:
        return True
    return any(m in text for m in _SPECiaal_SOORT_MARKERS)


def _is_vo_soort(soort: str) -> bool:
    s = _low(soort)
    if not s:
        return False
    if any(m in s for m in _VO_MARKERS):
        return True
    if '/' in s and any(p in s for p in ('vbo', 'mavo', 'havo', 'vwo', 'pro')):
        return True
    return False


def _is_mbo_soort(soort: str, bron: str) -> bool:
    text = f'{soort} {bron}'.lower()
    return any(m in text for m in _MBO_MARKERS)


def _hoofdsoort_from_soort_ho(soort_ho: str) -> Optional[str]:
    s = _low(soort_ho)
    if s == 'hbo':
        return 'HBO'
    if s == 'wo':
        return 'WO'
    if 'hogeschool' in s or s == 'hbo':
        return 'HBO'
    if 'universiteit' in s or 'wetenschapp' in s or s == 'wo':
        return 'WO'
    return None


def normalize_nl_school_soort(
    soort: Optional[str],
    bron_bestand: Optional[str] = None,
    soort_ho: Optional[str] = None,
) -> str:
    """
    Map DUO-soort + bronbestand naar hoofdcategorie.

    Volgorde: bronbestand (betrouwbaar) → SOORT HO (hbo/wo) → speciaal → overige regels.
    """
    s = _low(soort)
    bron = _low(bron_bestand)

    if _is_speciaal_soort(s, bron):
        return 'Speciaal onderwijs'

    if bron:
        if 'basisonderwijs' in bron:
            return 'Basisonderwijs'
        if 'vestigingen-vo' in bron or ('vestiging' in bron and re.search(r'\bvo\b', bron)):
            return 'VO'
        if 'mbo' in bron:
            return 'MBO'
        if 'hbo-en-wo' in bron or ('hbo' in bron and 'wo' in bron):
            from_ho = _hoofdsoort_from_soort_ho(soort_ho or soort)
            if from_ho:
                return from_ho
            if s == 'hbo':
                return 'HBO'
            if s == 'wo':
                return 'WO'

    from_ho = _hoofdsoort_from_soort_ho(soort_ho or '')
    if from_ho:
        return from_ho

    if s in ('hbo',):
        return 'HBO'
    if s in ('wo',):
        return 'WO'

    if s in ('basisschool', 'basisonderwijs'):
        return 'Basisonderwijs'

    if _is_mbo_soort(s, bron):
        return 'MBO'

    if _is_vo_soort(s):
        return 'VO'

    if s in ('mbo',):
        return 'MBO'

    return 'Overig'


def _rg_norm(v: Optional[str]) -> str:
    return (v or '').strip().lower()
