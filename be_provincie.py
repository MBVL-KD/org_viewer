"""Belgische provincie/regio uit postcode (Vlaanderen + Brussel)."""
from __future__ import annotations

import re
from typing import Optional

# Vlaamse + Brusselse postcodes → provincie (vereenvoudigd; 6xxx = Wallonië niet in VL-import)
_VL_POSTCODE_RANGES = (
    (1000, 1299, 'Brussel'),
    (1500, 1999, 'Vlaams-Brabant'),
    (2000, 2999, 'Antwerpen'),
    (3000, 3499, 'Vlaams-Brabant'),
    (3500, 3999, 'Limburg'),
    (4000, 4999, 'Luik'),  # enclave / grens — zeldzaam in VL-lijst
    (5000, 5999, 'Namen'),
    (6000, 6599, 'Henegouwen'),
    (6600, 6999, 'Luxemburg'),
    (7000, 7999, 'Henegouwen'),
    (8000, 8999, 'West-Vlaanderen'),
    (9000, 9999, 'Oost-Vlaanderen'),
)

# Alleen Vlaanderen + Brussel voor VL-bron
_VL_PROVINCES = frozenset({
    'Antwerpen', 'Limburg', 'Oost-Vlaanderen', 'West-Vlaanderen',
    'Vlaams-Brabant', 'Brussel',
})


def postcode_int(raw: str) -> Optional[int]:
    if not raw:
        return None
    m = re.search(r'\b(\d{4})\b', str(raw))
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def provincie_from_postcode(postcode: str, gemeente: str = '') -> str:
    pc = postcode_int(postcode)
    if pc is None:
        return ''
    for lo, hi, prov in _VL_POSTCODE_RANGES:
        if lo <= pc <= hi:
            if prov in _VL_PROVINCES:
                return prov
            break
    g = (gemeente or '').strip().lower()
    if 'brussel' in g or 'bruxelles' in g:
        return 'Brussel'
    return ''


def parse_adreslijn2(adreslijn2: str) -> tuple[str, str]:
    """'1000 Brussel' → (postcode, plaats)."""
    s = ' '.join((adreslijn2 or '').split())
    if not s:
        return '', ''
    m = re.match(r'^(\d{4})\s+(.+)$', s)
    if m:
        return m.group(1), m.group(2).strip()
    return '', s
