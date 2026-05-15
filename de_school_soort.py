"""Normaliseer JedeSchule `school_type` naar vaste Duitse hoofdcategorieën."""
from __future__ import annotations

import re
from typing import List, Tuple

# Hoofdcategorieën (gewenste taxonomie)
GRUNDSCHULEN = 'Grundschulen'
WEITERFUHREND = 'Weiterführende Schulen'
BERUFSBILDEND = 'Berufsschulen / Berufsbildende Schulen'
FOERDERSCHULEN = 'Förderschulen'
HOCHSCHULE = 'Hochschulen / Universitäten'
SONSTIGES = 'Sonstiges'

# Subtype binnen Weiterführende Schulen (leeg = niet van toepassing / diverse)
DET_HAUPTSCHULE = 'Hauptschule'
DET_REALSCHULE = 'Realschule'
DET_GYMNASIUM = 'Gymnasium'
DET_GESAMT = 'Gesamtschule'
DET_SONST_WEITER = 'Sonstige weiterführende Schule'

_CARRIER_NOISE = frozenset({
    'freie trägerschaft',
    'freie tragerschaft',
    'in freier trägerschaft',
    'in freier tragerschaft',
    'öffentlich',
    'oeffentlich',
    'privat',
    'staatlich anerkannte ersatzschule',
    'staatlich anerkannte',
})


def _norm_loose(s: str) -> str:
    t = ' '.join((s or '').split()).strip().lower()
    t = t.replace('ü', 'u').replace('ö', 'o').replace('ä', 'a').replace('ß', 'ss')
    return t


def _segment_raw(raw: str) -> List[str]:
    """Split op | en ; tot losse tekstfragmenten."""
    if not raw or not str(raw).strip():
        return []
    s = str(raw)
    parts = re.split(r'[|;]', s)
    out: List[str] = []
    for p in parts:
        frag = ' '.join(p.split()).strip()
        if not frag:
            continue
        lo = _norm_loose(frag)
        if lo in _CARRIER_NOISE or lo.startswith('freie trag'):
            continue
        if len(lo) <= 2 and lo.isalpha():
            continue
        out.append(frag)
    if not out:
        out = [' '.join(s.split()).strip()]
    return out


def _joined_norm(raw: str) -> str:
    return ' '.join(_norm_loose(x) for x in _segment_raw(raw))


def _pick_weiter_detail(s_norm: str) -> str:
    """Bepaal subtype binnen Weiterführend (volgorde: Gymnasium → Gesamt → Real → Haupt → sonst)."""
    if 'berufliches gymnasium' in s_norm:
        return ''
    if 'gymnasium' in s_norm or 'gymnasiale oberstufe' in s_norm or 'gymnasiale' in s_norm:
        return DET_GYMNASIUM
    if (
        'gesamtschule' in s_norm
        or 'gemeinschaftsschule' in s_norm
        or 'stadtteilschule' in s_norm
        or 'sekundarschule' in s_norm
        or 'regionalschule' in s_norm
    ):
        return DET_GESAMT
    if 'realschule' in s_norm:
        return DET_REALSCHULE
    if 'hauptschule' in s_norm or 'mittelschule' in s_norm:
        return DET_HAUPTSCHULE
    return DET_SONST_WEITER


def normalize_de_school_soort(raw: str) -> Tuple[str, str]:
    """
    JedeSchule `school_type` → (soort_norm, soort_norm_detail).

    soort_norm_detail is gevuld bij Weiterführende Schulen (subtype), anders ''.
    """
    if raw is None or not str(raw).strip():
        return (SONSTIGES, '')

    s_norm = _joined_norm(raw)
    if not s_norm:
        return (SONSTIGES, '')

    raw_lo = raw.strip().lower()

    # --- 3 Berufsbildend (voor Gymnasium/Hochschule om verwarring te voorkomen)
    beruf_markers = (
        'berufsschule',
        'berufsfachschule',
        'berufskolleg',
        'berufsoberschule',
        'berufsvorbereitung',
        'berufliches gymnasium',
        'berufliche schule',
        'berufsbildende',
        'berufsbildende schule',
        'fachoberschule',
        'fachschule',
        'berufsfachschulen',
        'beruflichen gymnasien',
        'duale berufsausbildung',
        'berufliche gymnasien',
        'bildungszentrum fur gesundheit',
        'pflegeschule',
        'krankenpflegeschule',
        'gesundheitsfachschule',
        'kaufmannische schule',
        'gewerbliche schule',
        'hauswirtschaft',
        'winzer',
        'doppeltqualifizierender',
    )
    if any(m in s_norm for m in beruf_markers):
        return (BERUFSBILDEND, '')
    if re.search(r'\bbea\b', s_norm):
        return (BERUFSBILDEND, '')

    # --- 4 Förderschulen
    if any(
        k in s_norm
        for k in (
            'forderschule',
            'förderschule',
            'sonderschule',
            'sonderpad',
            'sonderpäd',
            'geistigbehinderte',
            'korperbehinderte',
            'korperbehind',
            'horgeschadigte',
            'sehschule',
            'sprachentwicklung',
            'emotionale und soziale',
            'waldorfforder',
            'blindeninstitut',
            'schule fur kranke',
            'kranke schuler',
            'ausgleichsklassen',
        )
    ):
        return (FOERDERSCHULEN, '')

    # --- 5 Hochschule / Universität
    if 'fachhochschule' in s_norm or 'berufsakademie' in s_norm:
        return (HOCHSCHULE, '')
    if 'kunsthochschule' in s_norm or 'musikhochschule' in s_norm or 'verwaltungsfachhochschule' in s_norm:
        return (HOCHSCHULE, '')
    if re.search(r'universität|universitat', raw_lo):
        return (HOCHSCHULE, '')
    if re.search(r'\bhochschule\b', raw_lo):
        if 'berufliche schule' in s_norm:
            pass
        else:
            return (HOCHSCHULE, '')

    # --- 2 Weiterführend
    weiter_markers = (
        'hauptschule',
        'realschule',
        'gymnasium',
        'gesamtschule',
        'gemeinschaftsschule',
        'sekundarschule',
        'stadtteilschule',
        'regionalschule',
        'mittelschule',
        'schulzentrum',
        'kollegstufe',
        'gymnasiale oberstufe',
        'eigenstandige gymn',
        'abendrealschule',
        'abendgymnasium',
        'abendhauptschule',
        'oberstufenzentrum',
    )
    if any(w in s_norm for w in weiter_markers):
        return (WEITERFUHREND, _pick_weiter_detail(s_norm))

    # --- 1 Grundschule
    if any(
        g in s_norm
        for g in (
            'grundschule',
            'grundschul',
            'volksschule',
            'primarstufe',
            'vorschulklasse',
            'kitagruppe',
            'forderstufe kindergarten',
        )
    ):
        return (GRUNDSCHULEN, '')

    # VHS / Erwachsenenbildung / Verwaltung
    if any(
        x in s_norm
        for x in (
            'volkshochschule',
            'erwachsenenschule',
            'erwachsenenbildung',
            'zweiten bildungsweges',
            'allgemeine dienststelle',
            'sonstige dienststelle',
            'fiktive dienststelle',
            'zbw an vhs',
        )
    ) or re.search(r'\bvhs\b', raw_lo):
        return (SONSTIGES, '')

    # Afgevangen: alleen PO-/Trägerrest zonder erkende Schulform
    if 'trag' in s_norm and len(s_norm) < 48:
        return (SONSTIGES, '')

    return (SONSTIGES, '')


def all_soort_norm_categories() -> Tuple[str, ...]:
    """Vaste volgorde voor filters / viewer."""
    return (
        GRUNDSCHULEN,
        WEITERFUHREND,
        BERUFSBILDEND,
        FOERDERSCHULEN,
        HOCHSCHULE,
        SONSTIGES,
    )
