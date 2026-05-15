"""Normaliseer Vlaamse / FWB schoolniveaus naar soort_norm (zoals NL)."""
from __future__ import annotations

from typing import Optional

VL_NIVEAU_TO_SOORT = {
    1: 'Basisonderwijs',
    2: 'VO',
    3: 'HBO',  # mix HBO/WO; detailfiche kan verfijnen
}

FWB_NIVEAU_TO_SOORT = {
    'fondamental': 'Basisonderwijs',
    'secondaire': 'VO',
    'supérieur': 'HBO',
    'superieur': 'HBO',
}


def normalize_be_school_soort(
    *,
    vl_niveau: Optional[int] = None,
    fwb_niveau: Optional[str] = None,
    type_enseignement: Optional[str] = None,
) -> str:
    if vl_niveau is not None:
        try:
            n = int(vl_niveau)
            if n in VL_NIVEAU_TO_SOORT:
                return VL_NIVEAU_TO_SOORT[n]
        except (TypeError, ValueError):
            pass
    if fwb_niveau:
        key = str(fwb_niveau).strip().lower()
        if key in FWB_NIVEAU_TO_SOORT:
            return FWB_NIVEAU_TO_SOORT[key]
    te = (type_enseignement or '').lower()
    if 'maternel' in te or 'primaire' in te or 'fondamental' in te:
        return 'Basisonderwijs'
    if 'secondaire' in te or 'cefa' in te:
        return 'VO'
    if 'supérieur' in te or 'superieur' in te or 'universit' in te:
        return 'HBO'
    if 'spécialis' in te or 'special' in te:
        return 'Speciaal onderwijs'
    return 'Overig'
