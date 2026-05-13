"""Canonieke Nederlandse provincienamen (koppeltekens, CSV vs OSM)."""
import unicodedata


def _strip_accents(value: str) -> str:
    return ''.join(
        ch for ch in unicodedata.normalize('NFD', value)
        if unicodedata.category(ch) != 'Mn'
    )


def _provincie_lookup_key(value: str) -> str:
    v = _strip_accents(' '.join(value.split())).lower()
    v = v.replace('-', ' ')
    return ' '.join(v.split())


def _prov_alias(canonical: str, *variants: str) -> dict:
    out = {}
    for v in (canonical,) + variants:
        if not (v and str(v).strip()):
            continue
        out[_provincie_lookup_key(str(v).strip())] = canonical
    return out


_NL_PROVINCIE_ALIASES = {}
for _block in (
    _prov_alias('Drenthe'),
    _prov_alias('Flevoland'),
    _prov_alias('Friesland', 'Fryslân', 'Fryslan', 'Frisia', 'Provincie Fryslân'),
    _prov_alias('Gelderland'),
    _prov_alias('Groningen'),
    _prov_alias('Limburg'),
    _prov_alias('Noord-Brabant', 'Noord Brabant', 'NoordBrabant', 'North Brabant', 'Noordbrabant'),
    _prov_alias('Noord-Holland', 'Noord Holland', 'North Holland', 'Noordholland'),
    _prov_alias('Overijssel'),
    _prov_alias('Utrecht'),
    _prov_alias('Zeeland'),
    _prov_alias('Zuid-Holland', 'Zuid Holland', 'South Holland', 'Zuidholland'),
):
    _NL_PROVINCIE_ALIASES.update(_block)


def normalize_nl_provincienaam(raw) -> str:
    """
    Canonieke Nederlandse provincienaam: koppeltekens zoals in de officiële
    benaming (Noord-Holland, Noord-Brabant). CSV/OSM-varianten worden gelijkgetrokken.
    """
    if raw is None:
        return ''
    if not isinstance(raw, str):
        raw = str(raw)
    s = ' '.join(raw.split()).strip()
    if not s:
        return ''
    k = _provincie_lookup_key(s)
    return _NL_PROVINCIE_ALIASES.get(k, s)
