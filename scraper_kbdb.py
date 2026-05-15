#!/usr/bin/env python3
"""Scrape Belgische damclubs van KBDB/FRBJD (Google Sites)."""
from __future__ import annotations

import re
import time
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup, Tag
from urllib.parse import unquote

from bond_land import enrich_club_bond_fields
from scraper import (
    EMAIL_REGEX,
    USER_AGENT,
    collection,
    ensure_mongo,
    find_logo_on_website,
    geocode_club,
    normalize,
    normalize_website,
    upsert_club,
)

KBDB_CLUBS_NL = 'https://sites.google.com/view/frbjd-kbdb/nl/clubs'

_BE_REGION_CANON: Dict[str, str] = {
    'west-vlaanderen': 'West-Vlaanderen',
    'oost-vlaanderen': 'Oost-Vlaanderen',
    'antwerpen': 'Antwerpen',
    'limburg': 'Limburg',
    'brussel': 'Brussel',
    'luik': 'Luik',
    'henegouwen': 'Henegouwen',
    'namen': 'Namen',
    'luxemburg': 'Luxemburg',
    'waals-brabant': 'Waals-Brabant',
    'vlaams-brabant': 'Vlaams-Brabant',
}

_CLUB_NAME_RE = re.compile(r'^(Damclub|Damier|DC |WDC ).+', re.I)
_POSTCODE_LINE_RE = re.compile(r'^(\d{4})\s+(.+)$')
_STREET_LINE_RE = re.compile(r'^[A-Za-zÀ-ÿ0-9].*,\s*\d')
_DETAIL_START_RE = re.compile(r'^(Lokaal|Local)\b', re.I)
_SKIP_LINES = frozenset({'-', 'Web: -', 'Info: -', 'Web:', 'Info:'})
_KNOWN_WEBSITES = {
    'heule': 'http://www.damsport.be/',
    'juiste zet': 'http://dejuistezet.be/',
    'phenix': 'https://kortrijkse-damclub.jouwweb.be/',
}


def _canon_region(line: str) -> Optional[str]:
    return _BE_REGION_CANON.get(normalize(line).lower())


def _is_club_name(line: str) -> bool:
    return bool(_CLUB_NAME_RE.match(line.strip()))


def _fetch_html(url: str) -> str:
    r = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=60)
    r.raise_for_status()
    return r.text


def _section_first_line(sec: Tag) -> str:
    text = sec.get_text('\n', strip=True)
    for line in text.split('\n'):
        line = normalize(line)
        if line:
            return line
    return ''


def _emails_from_sections(sections: List[Tag]) -> List[str]:
    emails: List[str] = []
    for sec in sections:
        for a in sec.find_all('a', href=True):
            href = a['href'].strip()
            if not href.lower().startswith('mailto:'):
                continue
            label = normalize(a.get_text()).lower()
            if label in ('info', 'contact'):
                continue
            em = unquote(href.split(':', 1)[1].split('?', 1)[0]).strip()
            if em and EMAIL_REGEX.search(em) and em not in emails:
                emails.append(em)
    return emails


def _websites_from_sections(sections: List[Tag]) -> str:
    for sec in sections:
        for a in sec.find_all('a', href=True):
            href = (a['href'] or '').strip()
            if not href.startswith('http'):
                continue
            if any(x in href for x in (
                'sites.google.com/view/frbjd', 'google.com/policies', 'facebook.com',
            )):
                continue
            return normalize_website(href)
        for line in sec.get_text('\n', strip=True).split('\n'):
            line = line.strip()
            if line.lower().startswith('web:'):
                tail = line.split(':', 1)[-1].strip()
                if tail and tail != '-':
                    if not tail.startswith('http'):
                        tail = 'http://' + tail
                    return normalize_website(tail)
    return ''


def _parse_detail_lines(lines: List[str]) -> dict:
    clublokaal = ''
    straat = ''
    postcode = ''
    plaats = ''
    bijeenkomst = ''
    jeugd = ''

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('Lokaal:') or line.startswith('Local:'):
            clublokaal = normalize(line.split(':', 1)[1])
        elif line == 'Lokaal' or line == 'Local':
            if i + 1 < len(lines) and lines[i + 1].startswith(':'):
                clublokaal = normalize(lines[i + 1].lstrip(': '))
                i += 1
        elif line.startswith('Bijeenkomst') or line.startswith('Jour de Jeu'):
            if ':' in line:
                bijeenkomst = normalize(line.split(':', 1)[1])
            elif i + 1 < len(lines):
                bijeenkomst = normalize(lines[i + 1].lstrip(': '))
                i += 1
        elif line.startswith('Jeugdclub:'):
            jeugd = normalize(line.split(':', 1)[1])
        elif line in _SKIP_LINES:
            pass
        elif _POSTCODE_LINE_RE.match(line):
            m = _POSTCODE_LINE_RE.match(line)
            postcode = m.group(1)
            plaats = m.group(2).strip()
        elif _STREET_LINE_RE.match(line) or (',' in line and re.search(r'\d', line)):
            straat = line
        i += 1

    secretariaat = bijeenkomst
    if jeugd:
        secretariaat = f'{bijeenkomst}; jeugd: {jeugd}'.strip('; ')

    return {
        'clublokaal': clublokaal,
        'straat': straat,
        'postcode': postcode,
        'plaats': plaats,
        'secretariaat': secretariaat,
    }


def _build_club_doc(naam: str, region: str, detail_sections: List[Tag]) -> dict:
    lines = []
    for sec in detail_sections:
        for line in sec.get_text('\n', strip=True).split('\n'):
            line = normalize(line)
            if line:
                lines.append(line)

    parsed = _parse_detail_lines(lines)
    emails = _emails_from_sections(detail_sections)
    website = _websites_from_sections(detail_sections)
    if not website:
        low = naam.lower()
        for key, url in _KNOWN_WEBSITES.items():
            if key in low:
                website = url
                break

    plaats = parsed['plaats'] or region
    clublokaal = parsed['clublokaal']
    straat = parsed['straat']
    postcode = parsed['postcode']

    doc = {
        'naam': naam,
        'plaats': plaats,
        'bond_regio': region,
        'secretariaat': parsed['secretariaat'],
        'clublokaal': clublokaal or straat,
        'club_url': '',
        'bond_url': KBDB_CLUBS_NL,
        'land': 'BE',
        'bond_land': 'KBDB',
        'bron': 'kbdb-google-sites',
        'details': {
            'website': website,
            'locaties': [{
                'naam': clublokaal or naam,
                'adres': straat,
                'postcode': postcode,
                'woonplaats': parsed['plaats'] or plaats,
            }] if straat or postcode else [],
            'contactpersonen': [],
            'emails': emails,
            'telefoons': [],
        },
        'imported_at': time.strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    if straat and postcode:
        doc['straat_vestiging'] = straat
        doc['postcode_vestiging'] = postcode
    return doc


def parse_kbdb_clubs_html(html: str) -> List[dict]:
    """Parse clubs via Google Sites <section>-structuur (regio → club → Lokaal-blok)."""
    soup = BeautifulSoup(html, 'html.parser')
    region: Optional[str] = None
    current_name: Optional[str] = None
    detail_sections: List[Tag] = []
    clubs: List[dict] = []

    def finalize() -> None:
        nonlocal current_name, detail_sections
        if current_name and region:
            clubs.append(_build_club_doc(current_name, region, detail_sections))
        current_name = None
        detail_sections = []

    for sec in soup.find_all('section'):
        first = _section_first_line(sec)
        if not first:
            continue
        if first == 'Info':
            continue

        reg = _canon_region(first)
        if reg:
            finalize()
            region = reg
            continue

        if _is_club_name(first):
            finalize()
            current_name = first
            detail_sections = []
            continue

        if current_name and _DETAIL_START_RE.match(first):
            detail_sections.append(sec)

    finalize()
    return clubs


def _club_quality_score(club: dict) -> int:
    score = 0
    locs = (club.get('details') or {}).get('locaties') or []
    if locs and locs[0].get('postcode'):
        score += 10
    regio = club.get('bond_regio') or club.get('provincie')
    if club.get('plaats') and club['plaats'] != regio:
        score += 5
    score += len((club.get('details') or {}).get('emails') or [])
    if (club.get('details') or {}).get('website'):
        score += 2
    return score


def _dedupe_clubs(clubs: List[dict]) -> List[dict]:
    by_name: Dict[str, dict] = {}
    for c in clubs:
        key = c['naam'].lower()
        prev = by_name.get(key)
        if prev is None or _club_quality_score(c) > _club_quality_score(prev):
            if prev:
                for em in (prev.get('details') or {}).get('emails') or []:
                    ce = c.setdefault('details', {}).setdefault('emails', [])
                    if em not in ce:
                        ce.append(em)
            by_name[key] = c
        else:
            for em in (c.get('details') or {}).get('emails') or []:
                pe = prev.setdefault('details', {}).setdefault('emails', [])
                if em not in pe:
                    pe.append(em)
    return list(by_name.values())


def _cleanup_kbdb_stale_duplicates() -> int:
    """Verwijder KBDB-dubbelen waar plaats gelijk is aan regio (foute eerdere import)."""
    n = 0
    for doc in list(collection.find({'bond_land': 'KBDB'})):
        regio = doc.get('bond_regio') or doc.get('provincie')
        if doc.get('plaats') != regio:
            continue
        if collection.count_documents({'bond_land': 'KBDB', 'naam': doc['naam']}) > 1:
            collection.delete_one({'_id': doc['_id']})
            n += 1
    return n


def _migrate_kbdb_provincie_to_regio() -> int:
    """Eenmalig: oude BE-clubs hadden regio in `provincie`; verplaats naar `bond_regio`."""
    n = 0
    for doc in collection.find({'bond_land': 'KBDB'}):
        prov = str(doc.get('provincie') or '').strip()
        if prov in ('',) or doc.get('bond_regio'):
            continue
        collection.update_one(
            {'_id': doc['_id']},
            {'$set': {'bond_regio': prov}, '$unset': {'provincie': ''}},
        )
        n += 1
    return n


def fetch_all_kbdb_clubs() -> List[dict]:
    html = _fetch_html(KBDB_CLUBS_NL)
    return _dedupe_clubs(parse_kbdb_clubs_html(html))


def import_kbdb_clubs(*, geocode: bool = True) -> int:
    ensure_mongo()
    migrated = _migrate_kbdb_provincie_to_regio()
    if migrated:
        print(f'[KBDB] {migrated} club(s): regio verplaatst van provincie → bond_regio.', flush=True)
    removed = _cleanup_kbdb_stale_duplicates()
    if removed:
        print(f'[KBDB] {removed} verouderd(e) duplicaat(en) verwijderd.', flush=True)

    clubs = fetch_all_kbdb_clubs()
    print(f'[KBDB] {len(clubs)} club(s) gevonden op officiële clubs-pagina.', flush=True)
    n = 0
    for club in clubs:
        club = enrich_club_bond_fields(club)
        if geocode:
            geocode_club(club)
        website = (club.get('details') or {}).get('website') or ''
        club['logo'] = find_logo_on_website(website) if website else ''
        upsert_club(club)
        n += 1
        em = (club.get('details') or {}).get('emails') or []
        print(
            f"  {club.get('bond_regio')} — {club.get('naam')} ({club.get('plaats')}) "
            f"{'@' + em[0] if em else 'geen e-mail'}",
            flush=True,
        )
    print(f'[KBDB] {n} club(s) geïmporteerd naar MongoDB.', flush=True)
    return n


if __name__ == '__main__':
    import_kbdb_clubs(geocode=True)
