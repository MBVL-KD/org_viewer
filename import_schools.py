#!/usr/bin/env python3
"""
Importeer schooldata (DUO/RIO: CSV of XLSX) naar MongoDB-collectie `schools`.

Ondersteunde bronnen o.a. oude RIO-CSV's en recente DUO-exporten (VO, PO, MBO,
HBO/WO) met verschillende kolomnamen.

Voorbeeld (nieuwe DUO-xlsx in Downloads):
  python3 import_schools.py \\
    ~/Downloads/02.-alle-vestigingen-vo.xlsx \\
    ~/Downloads/02.-alle-schoolvestigingen-basisonderwijs-2.xlsx \\
    ~/Downloads/01.-adressen-mbo-instellingen.xlsx \\
    ~/Downloads/01.-instellingen-hbo-en-wo.xlsx \\
    --prune-not-in-upload

--prune-not-in-upload: verwijdert alle scholen waarvan het administratienummer
niet in de gecombineerde set van de opgegeven bestanden voorkomt (alleen
gebruiken als deze bestanden samen de volledige actuele bron vormen).

E-mail: als de nieuwe bron geen e-mail heeft, blijft een bestaande e-mail in
Mongo behouden.

Optioneel: na import ontbrekende e-mails proberen te vullen via website(s) en
gangbare contact-URL's; voor basisscholen ook een tweede site (bevoegd gezag /
koepel) als die in de data staat.

  python3 import_schools.py --scrape-only

Alleen geocoderen (Nominatim, vestigingsadres uit Mongo):
  python3 import_schools.py --geocode-only

Geocode (Nominatim):
  python3 import_schools.py ... --geocode
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import unquote, urljoin, urlparse

import certifi
import pandas as pd
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pymongo import MongoClient

from nl_provincie import normalize_nl_provincienaam

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ENV_CANDIDATES = [ROOT / 'Editor' / 'server' / '.env', ROOT / 'Editor' / '.env', ROOT / '.env']
for env_file in ENV_CANDIDATES:
    if env_file.exists():
        load_dotenv(env_file)
        break

MONGO_URI = os.environ.get('MONGO_URI', 'mongodb://localhost:27017/')
MONGO_DB = os.environ.get('MONGO_DB', 'damclubs')
client = MongoClient(MONGO_URI, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=15000)
db = client[MONGO_DB]
schools_coll = db['schools']

# Meest voorkomende contact-URL's (bewust kort: elke URL heeft een HTTP-timeout).
CONTACT_URL_SUFFIXES: Tuple[str, ...] = (
    '/contact',
    '/nl/contact',
    '/contact-en-route',
    '/contact-en-route/',
    '/nl/contact-en-route',
    '/contactgegevens',
    '/over-ons/contact',
)

# Maximaal aantal pagina's per school (homepage + ontdekte links + vaste paden + evt. bevoegd gezag).
MAX_SCRAPE_URLS_PER_SCHOOL = 22


def _mongo_ping():
    try:
        client.admin.command('ping')
    except Exception as exc:
        print(f'MongoDB niet bereikbaar: {exc}', flush=True)
        raise SystemExit(1) from exc
    print('MongoDB verbinding OK.', flush=True)


def _normalize_website(url: str) -> str:
    if not url:
        return ''
    url = str(url).strip()
    if url.startswith('www.'):
        return 'https://' + url
    if url.startswith('//'):
        return 'https:' + url
    if not re.match(r'^https?://', url):
        return 'https://' + url
    return url


def _norm(s: Any) -> str:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ''
    return ' '.join(str(s).split()).strip()


def _canon_header(name: Any) -> str:
    s = re.sub(r'\s+', ' ', str(name).strip())
    return s.lower()


def _row_lookup(row: pd.Series) -> Dict[str, Any]:
    return {_canon_header(c): row[c] for c in row.index}


def _rg(lookup: Dict[str, Any], *keys: str) -> str:
    for k in keys:
        kk = _canon_header(k)
        if kk in lookup:
            v = lookup[kk]
            t = _norm(v)
            if t:
                return t
    return ''


def _split_huisnummer_toevoeging(combined: str) -> Tuple[str, str]:
    c = _norm(combined)
    if not c:
        return '', ''
    m = re.match(r'^(\d+)\s*[-]?\s*(.*)$', c)
    if m:
        num, rest = m.group(1), _norm(m.group(2))
        return num, rest
    return c, ''


def _infer_soort_from_filename(name: str) -> str:
    low = name.lower()
    if 'basisonderwijs' in low or ('basis' in low and 'school' in low):
        return 'basisschool'
    if 'vestigingen-vo' in low or re.search(r'\bvo\b', low) and 'vestiging' in low:
        return 'VO-school'
    if 'mbo' in low:
        return 'MBO'
    if 'hbo' in low or 'wo' in low or 'hoger' in low:
        return 'HBO/WO'
    return ''


def resolve_administratienummer(lookup: Dict[str, Any]) -> str:
    """Unieke sleutel: expliciet veld of BRIN/vestiging (DUO)."""
    for k in (
        'administratienummer',
        'administratie nummer',
        'brin',
        'brin nummer',
        'brin_nummer',
        'vestigingsnummer',
        'vestigingsnr',
    ):
        kk = _canon_header(k)
        if kk in lookup:
            t = _norm(lookup[kk])
            if t:
                return t.upper().replace(' ', '')

    inst = _rg(lookup, 'INSTELLINGSCODE', 'Instellingscode')
    vest = _rg(lookup, 'VESTIGINGSCODE', 'Vestigingscode')

    if vest:
        v = vest.upper().replace(' ', '')
        if len(v) >= 5:
            return v
        if inst:
            i = inst.upper().replace(' ', '')
            if len(vest) <= 2 and vest.strip().isdigit():
                return f"{i}{vest.strip().zfill(2)}".upper()
            return f"{i}{v}".upper()

    if inst:
        return inst.upper().replace(' ', '')

    bg = _rg(lookup, 'BEVOEGD GEZAG NUMMER', 'Bevoegd gezag nummer')
    if bg and inst:
        return f"{bg}{inst}".upper().replace(' ', '')

    return ''


def read_school_dataframe(path: Path) -> pd.DataFrame:
    path = Path(path)
    suf = path.suffix.lower()
    if suf == '.xlsx':
        df = pd.read_excel(path, dtype=str, engine='openpyxl')
    elif suf == '.xls':
        try:
            df = pd.read_excel(path, dtype=str, engine='xlrd')
        except Exception:
            df = pd.read_excel(path, dtype=str)
    else:
        raw = path.read_text(encoding='utf-8-sig', errors='replace')
        head = raw[:4096]
        sep = ';' if head.count(';') > head.count(',') else ','
        df = pd.read_csv(path, sep=sep, dtype=str, encoding='utf-8-sig')
    df.columns = [str(c).strip() for c in df.columns]
    return df.fillna('')


def row_to_document(row: pd.Series, bron: str) -> Optional[dict]:
    lu = _row_lookup(row)
    admin = resolve_administratienummer(lu)
    if not admin:
        return None

    straat = _rg(
        lu,
        'Straat vestiging',
        'STRAATNAAM',
        'Straatnaam',
        'ADRES',
    )
    hn_raw = _rg(lu, 'Huisnr vestiging', 'HUISNUMMER', 'Huisnummer')
    hnt_raw = _rg(lu, 'HuisnrToev vestiging', 'HUISNUMMER-TOEVOEGING', 'Huisnummer-toevoeging')
    if hn_raw:
        hn, hnt = _norm(hn_raw), _norm(hnt_raw)
    else:
        hn, hnt = _split_huisnummer_toevoeging(hnt_raw)

    plaats = _rg(lu, 'Plaats vestiging', 'PLAATSNAAM', 'Plaatsnaam')
    gemeente = _rg(lu, 'Gemeente', 'GEMEENTENAAM', 'Gemeentenaam')
    pc = _rg(lu, 'Postcode vestiging', 'POSTCODE', 'Postcode')

    naam = _rg(
        lu,
        'NaamKort',
        'VESTIGINGSNAAM',
        'Vestigingsnaam',
        'INSTELLINGSNAAM',
        'Instellingsnaam',
        'NAAM',
    )
    soort = _rg(
        lu,
        'SoortNaam',
        'Soort naam',
        'ONDERWIJSSTRUCTUUR',
        'Onderwijsstructuur',
        'SOORT HO',
        'MBO INSTELLINGSSOORT - NAAM',
        'SOORT',
    )
    if not soort:
        soort = _infer_soort_from_filename(bron)

    doc: dict = {
        'land': 'NL',
        'administratienummer': admin,
        'naam': naam,
        'plaats': plaats,
        'gemeente': gemeente,
        'provincie': normalize_nl_provincienaam(_rg(lu, 'Provincie', 'PROVINCIE')),
        'status': _rg(lu, 'Status', 'STATUS'),
        'soort': soort,
        'straat_vestiging': straat,
        'huisnr_vestiging': hn,
        'huisnr_toev_vestiging': hnt,
        'postcode_vestiging': pc,
        'telefoon': _rg(lu, 'Telefoon', 'TELEFOONNUMMER', 'Telefoonnummer'),
        'fax': _rg(lu, 'Fax', 'FAX'),
        'email': _rg(
            lu,
            'Email',
            'E-mail',
            'E-MAILADRES',
            'E-mailadres',
            'EMAILADRES',
            'EMAIL',
        ),
        'website': _normalize_website(_rg(lu, 'Website', 'INTERNETADRES', 'Internetadres')),
        'bron_bestand': bron,
        'imported_at': time.strftime('%Y-%m-%dT%H:%M:%SZ'),
    }

    w_bg = _normalize_website(
        _rg(
            lu,
            'INTERNETADRES BEVOEGD GEZAG',
            'Internetadres bevoegd gezag',
            'WEBSITE BEVOEGD GEZAG',
            'Website bevoegd gezag',
        )
    )
    if w_bg:
        doc['website_bevoegd_gezag'] = w_bg

    bg_num = _rg(lu, 'BEVOEGD GEZAG NUMMER', 'Bevoegd gezag nummer')
    if bg_num:
        doc['bevoegd_gezag_nummer'] = bg_num

    denom = _rg(lu, 'DENOMINATIE', 'Denominatie', 'GRONDSLAG', 'Grondslag')
    if denom:
        doc['denominatie'] = denom

    corr = {
        'plaats': _rg(lu, 'Plaats correspondentie', 'PLAATSNAAM CORRESPONDENTIEADRES', 'Plaatsnaam correspondentieadres'),
        'straat': _rg(lu, 'Straat correspondentie', 'STRAATNAAM CORRESPONDENTIEADRES'),
        'huisnr': _rg(lu, 'Huisnr correspondentie', 'HUISNUMMER CORRESPONDENTIEADRES'),
        'huisnr_toev': _rg(lu, 'HuisnrToev correspondentie', 'HUISNUMMER-TOEVOEGING CORRESPONDENTIEADRES'),
        'postcode': _rg(lu, 'Postcode correspondentie', 'POSTCODE CORRESPONDENTIEADRES'),
    }
    if any(_norm(v) for v in corr.values()):
        doc['correspondentie'] = corr

    return doc


def _vestigings_adres(doc) -> str:
    parts = []
    s = doc.get('straat_vestiging') or ''
    hn = doc.get('huisnr_vestiging') or ''
    tv = doc.get('huisnr_toev_vestiging') or ''
    if s:
        line = s
        if hn:
            line += f' {hn}'
        if tv:
            line += f' {tv}'
        parts.append(line)
    pc = (doc.get('postcode_vestiging') or '').strip()
    plaats = (doc.get('plaats') or '').strip()
    gemeente = (doc.get('gemeente') or '').strip()
    if not plaats and gemeente:
        plaats = gemeente
        gemeente = ''
    tail = []
    if pc:
        tail.append(pc)
    if plaats:
        tail.append(plaats)
    low = ' '.join(tail).lower()
    if gemeente and gemeente.lower() not in low:
        tail.append(gemeente)
        low = ' '.join(tail).lower()
    prov = (doc.get('provincie') or '').lower()
    if 'oosterend' in low and 'texel' not in low and 'tersch' not in low:
        if 'noord-holland' in prov:
            tail.append('Texel')
        elif 'friesland' in prov:
            tail.append('Terschelling')
    if tail:
        parts.append(' '.join(tail))
    if not parts:
        return ''
    return ', '.join(parts) + ', Nederland'


def _merge_preserve_email(doc: dict) -> None:
    """Vul doc['email'] vanuit Mongo als de import geen e-mail heeft."""
    if _norm(doc.get('email')):
        return
    existing = schools_coll.find_one(
        {'administratienummer': doc['administratienummer'], 'land': 'NL'},
        projection={'email': 1},
    )
    if existing and _norm(existing.get('email')):
        doc['email'] = _norm(existing.get('email'))


def collect_ids_and_import(paths: Iterable[Path]) -> Tuple[int, Set[str]]:
    total = 0
    all_ids: Set[str] = set()
    for path in paths:
        path = Path(path).expanduser()
        if not path.is_file():
            print(f' overslaan (bestaat niet): {path}', flush=True)
            continue
        df = read_school_dataframe(path)
        bron = path.name
        n = 0
        for _, row in df.iterrows():
            doc = row_to_document(row, bron)
            if not doc:
                continue
            all_ids.add(doc['administratienummer'])
            _merge_preserve_email(doc)
            schools_coll.update_one(
                {'administratienummer': doc['administratienummer'], 'land': 'NL'},
                {'$set': doc},
                upsert=True,
            )
            n += 1
        print(f'{bron}: {n} rijen geïmporteerd/upsert.', flush=True)
        total += n
    return total, all_ids


def prune_schools_not_in(admin_ids: Set[str]) -> int:
    if not admin_ids:
        print('Geen administratienummers verzameld: overslaan prune.', flush=True)
        return 0
    res = schools_coll.delete_many({
        '$or': [{'land': 'NL'}, {'land': {'$exists': False}}, {'land': None}, {'land': ''}],
        'administratienummer': {'$nin': list(admin_ids)},
    })
    return int(res.deleted_count)


def _discover_contact_urls_from_homepage(home: str, http_timeout: float) -> List[str]:
    """
    Zoek op de homepage <a href>-links naar contact-/route-pagina's (ook andere
    subdomeinen, bv. ares.nl → aeresvmbo.nl/contact-en-route).
    """
    from scraper import USER_AGENT

    home = _normalize_website(home)
    if not home:
        return []
    try:
        r = requests.get(home, headers={'User-Agent': USER_AGENT}, timeout=http_timeout)
        r.raise_for_status()
    except Exception:
        return []
    soup = BeautifulSoup(r.text, 'html.parser')
    path_keys = ('contact', 'bereik', 'route', 'secretariaat', 'locatie', 'mailto:')
    text_keys = ('contact', 'route', 'bereikbaar', 'bezoek')
    host_keys = ('aeresvmbo', 'aeres-mbo', 'aeresmbo')  # DUO-site vs landingsdomein
    out: List[str] = []
    seen: Set[str] = set()
    home_norm = home.rstrip('/')
    for a in soup.find_all('a', href=True):
        href = (a.get('href') or '').strip()
        if not href or href.startswith('#') or 'javascript:' in href.lower():
            continue
        full = urljoin(home, href).split('#', 1)[0].strip()
        low = full.lower()
        if not low.startswith('http'):
            continue
        path = urlparse(full).path.lower()
        host = urlparse(full).netloc.lower()
        label = ' '.join(a.get_text().split()).lower()
        host_hit = any(k in host for k in host_keys)
        if (
            any(k in path for k in path_keys)
            or any(k in label for k in text_keys)
            or host_hit
        ):
            if full.rstrip('/') != home_norm and full not in seen:
                seen.add(full)
                out.append(full)
    return out[:15]


def _urls_for_school_scrape(
    website: str,
    website_bg: str,
    is_basis: bool,
    discovered: Optional[List[str]] = None,
) -> List[str]:
    """Homepage, optioneel ontdekte contact-URL's, vaste paden; basisscholen ook bevoegd-gezag-site."""
    seen: Set[str] = set()
    out: List[str] = []

    def add(u: str) -> None:
        u = _normalize_website(u)
        if not u or u in seen:
            return
        seen.add(u)
        out.append(u)

    base = _normalize_website(website)
    if base:
        add(base)
        for u in discovered or []:
            add(u)
        root = base.rstrip('/')
        for suf in CONTACT_URL_SUFFIXES:
            add(root + suf)

    if is_basis and website_bg:
        add(website_bg)
        root2 = _normalize_website(website_bg).rstrip('/')
        for suf in CONTACT_URL_SUFFIXES:
            add(root2 + suf)

    return out


def _pick_best_email(emails: List[str]) -> str:
    from scraper import EMAIL_REGEX, is_placeholder_email

    candidates: List[str] = []
    for raw in emails:
        if not raw:
            continue
        u = unquote(str(raw).strip())
        for m in EMAIL_REGEX.finditer(u):
            addr = m.group(0).strip()
            if addr and not is_placeholder_email(addr):
                if '?' not in addr and '&' not in addr:
                    candidates.append(addr)
    if not candidates:
        return ''
    # uniek, volgorde behouden
    seen: Set[str] = set()
    clean = []
    for c in candidates:
        k = c.lower()
        if k not in seen:
            seen.add(k)
            clean.append(c)
    low_prio = ('noreply', 'no-reply', 'webmaster', 'postmaster', 'privacy', 'abuse')

    def score(e: str) -> Tuple[int, int]:
        el = e.lower()
        bonus = 0
        if any(k in el for k in ('info@', 'directie@', 'contact@', 'school@', 'secretariaat@')):
            bonus += 10
        if any(p in el for p in low_prio):
            bonus -= 5
        return (bonus, len(el))

    clean.sort(key=score, reverse=True)
    return clean[0]


def scrape_email_for_document(doc: dict, delay_s: float, http_timeout: float = 10.0) -> str:
    """Haal eerste bruikbare e-mail van school- en (basis) bevoegd-gezag-URL's."""
    from scraper import EMAIL_REGEX, scrape_website_contact_info

    if _norm(doc.get('email')):
        return ''

    website = doc.get('website') or ''
    w_bg = doc.get('website_bevoegd_gezag') or ''
    soort = (doc.get('soort') or '').lower()
    bron = (doc.get('bron_bestand') or '').lower()
    is_basis = 'basis' in soort or 'basisonderwijs' in bron

    home = _normalize_website(website)
    discovered = _discover_contact_urls_from_homepage(home, http_timeout) if home else []
    urls = _urls_for_school_scrape(website, w_bg, is_basis, discovered=discovered)[:MAX_SCRAPE_URLS_PER_SCHOOL]
    collected: List[str] = []
    for url in urls:
        info = scrape_website_contact_info(url, timeout=http_timeout)
        collected.extend(info.get('emails') or [])
        best = _pick_best_email(collected)
        if best and EMAIL_REGEX.search(best):
            return best
        time.sleep(delay_s)

    return _pick_best_email(collected)


def run_scrape_missing_emails(
    limit: Optional[int],
    delay_s: float,
    http_timeout: float = 10.0,
) -> int:
    from scraper import EMAIL_REGEX

    q = {
        '$or': [{'email': {'$exists': False}}, {'email': None}, {'email': ''}],
        'website': {'$exists': True, '$nin': [None, '']},
    }
    # Hele queue in één keer laden: bij trage HTTP-requests verloopt een Mongo-cursor
    # anders (CursorNotFound) voordat de job klaar is.
    work = list(schools_coll.find(q))
    total = len(work)
    print(
        f'[email] Start: {total} school(len) zonder e-mail mét website '
        f'(limiet={"geen" if limit is None else limit}; queue in geheugen geladen).',
        flush=True,
    )
    updated = 0
    seen = 0
    for doc in work:
        seen += 1
        if limit is not None and updated >= limit:
            break
        em = scrape_email_for_document(doc, delay_s=delay_s, http_timeout=http_timeout)
        found = bool(em and EMAIL_REGEX.search(em))
        if found:
            schools_coll.update_one(
                {'_id': doc['_id']},
                {'$set': {
                    'email': em,
                    'email_scraped_at': time.strftime('%Y-%m-%dT%H:%M:%SZ'),
                }},
            )
            updated += 1
            print(f'  scrape: {doc.get("naam")} -> {em}', flush=True)
        if seen == 1 or seen % 10 == 0:
            pct = (100.0 * seen / total) if total else 0.0
            last = f'OK {em}' if found else 'geen e-mail gevonden'
            print(
                f'[email] {time.strftime("%H:%M:%S")} voortgang: {seen}/{total} '
                f'({pct:.1f}%) klaar, {updated} e-mail(s) toegevoegd | laatste: {last} | {doc.get("naam")}',
                flush=True,
            )
    print(f'[email] Klaar: {updated} bijgewerkt van {seen} bekeken (queue was {total}).', flush=True)
    return updated


def geocode_schools_without_coords():
    from scraper import geocode_query

    q = {
        '$or': [{'land': 'NL'}, {'land': {'$exists': False}}, {'land': None}, {'land': ''}],
        '$and': [{
            '$or': [
                {'lat': {'$exists': False}},
                {'lat': None},
                {'lon': {'$exists': False}},
                {'lon': None},
            ],
        }],
    }
    work = list(schools_coll.find(q))
    total = len(work)
    print(
        f'[geo] Start: {total} school(len) zonder volledige lat/lon (queue in geheugen).',
        flush=True,
    )
    count = 0
    seen = 0
    skip_no_addr = 0
    miss = 0
    for doc in work:
        seen += 1
        if seen == 1 or seen % 15 == 0:
            pct = (100.0 * seen / total) if total else 0.0
            print(
                f'[geo] {time.strftime("%H:%M:%S")} voortgang: {seen}/{total} ({pct:.1f}%) '
                f'— ok={count} geen_adres={skip_no_addr} nominatim_miss={miss}',
                flush=True,
            )
        addr = _vestigings_adres(doc)
        if not addr.strip():
            skip_no_addr += 1
            continue
        geom = geocode_query(addr, skip_cache=False)
        if not geom:
            miss += 1
            continue
        update_fields = {
            'lat': geom['lat'],
            'lon': geom['lon'],
            'geocode_query': addr,
            'geocoded_at': time.strftime('%Y-%m-%dT%H:%M:%SZ'),
        }
        if not _norm(doc.get('provincie')) and geom.get('provincie'):
            update_fields['provincie'] = normalize_nl_provincienaam(geom['provincie'])
        schools_coll.update_one(
            {'_id': doc['_id']},
            {'$set': update_fields},
        )
        count += 1
        print(f'  geocode {count}: {doc.get("naam")} -> {geom["lat"]}, {geom["lon"]}', flush=True)
    print(
        f'[geo] Klaar: {count} gegeocodeerd, {skip_no_addr} zonder bruikbaar adres, '
        f'{miss} geen Nominatim-treffer (van {seen} documenten in queue).',
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(
        description='School-CSV/XLSX naar MongoDB (collectie schools), sync en e-mailscraping.',
    )
    parser.add_argument(
        'data_files',
        nargs='*',
        type=str,
        default=[
            str(Path.home() / 'Downloads' / '02.-alle-vestigingen-vo.xlsx'),
            str(Path.home() / 'Downloads' / '02.-alle-schoolvestigingen-basisonderwijs-2.xlsx'),
            str(Path.home() / 'Downloads' / '01.-adressen-mbo-instellingen.xlsx'),
            str(Path.home() / 'Downloads' / '01.-instellingen-hbo-en-wo.xlsx'),
        ],
        help='Paden naar CSV- of XLSX-bestanden',
    )
    parser.add_argument(
        '--prune-not-in-upload',
        action='store_true',
        help='Verwijder Mongo-records waarvan administratienummer niet in deze bestanden voorkomt',
    )
    parser.add_argument('--geocode', action='store_true', help='Na import: geocode ontbrekende coördinaten')
    parser.add_argument(
        '--scrape-emails',
        action='store_true',
        help='Na import: voor scholen zonder e-mail maar met website URL’s proberen te scrapen',
    )
    parser.add_argument(
        '--scrape-email-delay',
        type=float,
        default=0.7,
        metavar='SEC',
        help='Pauze tussen HTTP-requests bij e-mailscrapen (default 0.7)',
    )
    parser.add_argument(
        '--scrape-email-limit',
        type=int,
        default=None,
        metavar='N',
        help='Maximaal aantal scholen bij te werken (testen)',
    )
    parser.add_argument(
        '--scrape-only',
        action='store_true',
        help='Geen import: alleen ontbrekende e-mails scrapen (website / contact-URL’s)',
    )
    parser.add_argument(
        '--scrape-http-timeout',
        type=float,
        default=10.0,
        metavar='SEC',
        help='HTTP-timeout per URL bij e-mailscrapen (default 10)',
    )
    parser.add_argument(
        '--geocode-only',
        action='store_true',
        help='Geen import: alleen scholen zonder lat/lon geocoderen (Nominatim)',
    )
    args = parser.parse_args()

    _mongo_ping()
    schools_coll.create_index([('administratienummer', 1), ('land', 1)], unique=True)

    if args.scrape_only:
        print('Start e-mailscraping (--scrape-only, geen import)...', flush=True)
        run_scrape_missing_emails(
            limit=args.scrape_email_limit,
            delay_s=args.scrape_email_delay,
            http_timeout=args.scrape_http_timeout,
        )
        if args.geocode:
            print('Start geocoding...', flush=True)
            geocode_schools_without_coords()
        return

    if args.geocode_only:
        print('Start geocoding (--geocode-only, geen import)...', flush=True)
        geocode_schools_without_coords()
        return

    paths = [Path(p).expanduser() for p in args.data_files if p]
    if not paths:
        print('Geen bestandspaden opgegeven.', flush=True)
        sys.exit(1)

    n, ids_union = collect_ids_and_import(paths)
    print(f'Totaal verwerkte rijen: {n}', flush=True)

    if args.prune_not_in_upload:
        nd = prune_schools_not_in(ids_union)
        print(f'Prune: {nd} school(len) verwijderd (niet in geüploade set).', flush=True)

    if args.scrape_emails:
        print('Start e-mailscraping (kan lang duren; respecteer servers)...', flush=True)
        run_scrape_missing_emails(
            limit=args.scrape_email_limit,
            delay_s=args.scrape_email_delay,
            http_timeout=args.scrape_http_timeout,
        )

    if args.geocode:
        print('Start geocoding (kan lang duren i.v.m. Nominatim rate limit)...', flush=True)
        geocode_schools_without_coords()


if __name__ == '__main__':
    main()
