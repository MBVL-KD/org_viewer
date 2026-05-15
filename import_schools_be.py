#!/usr/bin/env python3
"""
Importeer Belgische scholen (Vlaanderen) naar MongoDB `schools` met land=BE.

Bron Vlaanderen:
  - Lijst: POST data-onderwijs.vlaanderen.be/.../AdresLijstPagina (niveau 1/2/3)
  - Detail: GET .../instelling?sn={instellingsnummer} (e-mail, telefoon, website, …)

Voorbeelden:
  python3 import_schools_be.py --limit 100 --niveau basis --geocode
  python3 import_schools_be.py --niveau basis --niveau secundair --geocode
  python3 import_schools_be.py --geocode-only
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import certifi
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne

from be_provincie import parse_adreslijn2, provincie_from_postcode
from be_school_soort import normalize_be_school_soort

HERE = __file__
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
for env_file in (
    os.path.join(ROOT, 'Editor', 'server', '.env'),
    os.path.join(ROOT, 'Editor', '.env'),
    os.path.join(ROOT, '.env'),
):
    if os.path.exists(env_file):
        load_dotenv(env_file)
        break

MONGO_URI = os.environ.get('MONGO_URI', 'mongodb://localhost:27017/')
MONGO_DB = os.environ.get('MONGO_DB', 'damclubs')
USER_AGENT = 'Draughts4All-schools-be/1.0'

VL_LIST_URL = (
    'https://data-onderwijs.vlaanderen.be/onderwijsaanbod/'
    'webservice/lijsten.svc/AdresLijstPagina'
)
VL_DETAIL_URL = 'https://data-onderwijs.vlaanderen.be/onderwijsaanbod/instelling'

NIVEAU_MAP = {
    'basis': 1,
    'basisonderwijs': 1,
    'secundair': 2,
    'vo': 2,
    'hoger': 3,
    'hbo': 3,
}

client = MongoClient(MONGO_URI, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=15000)
schools_coll = client[MONGO_DB]['schools']


def _norm(s: Any) -> str:
    if s is None:
        return ''
    return ' '.join(str(s).split()).strip()


def _normalize_website(url: str) -> str:
    url = _norm(url)
    if not url:
        return ''
    if url.startswith('www.'):
        return 'https://' + url
    if not re.match(r'^https?://', url, re.I):
        return 'https://' + url
    return url


def _float_or_none(v: Any) -> Optional[float]:
    if v is None or v == '':
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _mongo_ping() -> None:
    client.admin.command('ping')
    print('MongoDB verbinding OK.', flush=True)


def _list_payload(niveau: int, page: int, per_page: int) -> dict:
    return {
        'AantalRijenPerPagina': per_page,
        'PaginaNummer': page,
        'SorteerVeldNaam': 1,
        'ToonSQL': False,
        'Changed': False,
        'isKaart': False,
        'maxAantalOpKaart': 1000,
        'GeoFilter': {
            'GeoFilterSoort': 2,
            'GeoFilterWaarde': '',
            'Provincie': '',
            'GeoFilterOpties': [],
        },
        'StructuurFilter': {
            'HoofdStructuren': [],
            'InstellingsTypes': [],
            'SoortInstelling': '01',
            'IsHoofdZetel': False,
            'Niveaus': [niveau],
            'SoortenOnderwijs': [],
            'Taalstelsel': [],
            'Methode': [],
        },
        'OpleidingFilter': {
            k: []
            for k in (
                'ohs', 'studiegebied', 'opleiding', 'graad', 'onderwijsvorm',
                'typebuitengewoon', 'leerjaar', 'dkovak', 'standaardtraject',
                'methode', 'soortleerjaar',
            )
        },
        'SpecifiekeFilter': '',
    }


def fetch_vl_vestigingen(niveau: int, limit: Optional[int], per_page: int = 200) -> List[dict]:
    out: List[dict] = []
    page = 1
    total_expected: Optional[int] = None
    while True:
        r = requests.post(
            VL_LIST_URL,
            json=_list_payload(niveau, page, per_page),
            headers={'Content-Type': 'application/json', 'User-Agent': USER_AGENT},
            timeout=120,
        )
        r.raise_for_status()
        data = r.json()
        if total_expected is None:
            total_expected = int(data.get('aantal') or 0)
            print(f'  [VL niveau {niveau}] totaal in bron: {total_expected}', flush=True)
        batch = data.get('adressen') or []
        if not batch:
            break
        out.extend(batch)
        print(f'  [VL niveau {niveau}] pagina {page}: {len(out)} vestigingen', flush=True)
        if limit is not None and len(out) >= limit:
            out = out[:limit]
            break
        if len(batch) < per_page:
            break
        page += 1
        time.sleep(0.25)
    return out


def _admin_id(sn: int, postcode: str, straat: str) -> str:
    base = f'VL-{sn}-{postcode or "0000"}'
    if not straat:
        return base
    h = hashlib.md5(str(straat).lower().encode()).hexdigest()[:6]
    return f'{base}-{h}'


def vestiging_to_stub(row: dict, vl_niveau: int) -> dict:
    sn = int(row.get('instellingsnummer') or 0)
    straat = _norm(row.get('adreslijn1'))
    pc, plaats = parse_adreslijn2(_norm(row.get('adreslijn2')))
    prov = provincie_from_postcode(pc, plaats)
    lat = _float_or_none(row.get('lat'))
    lon = _float_or_none(row.get('lon'))
    return {
        'administratienummer': _admin_id(sn, pc, straat),
        'instellingsnummer': sn,
        'naam': _norm(row.get('naam')),
        'straat_vestiging': straat,
        'postcode_vestiging': pc,
        'plaats': plaats,
        'gemeente': plaats,
        'provincie': prov,
        'vl_niveau': vl_niveau,
        'soort_norm': normalize_be_school_soort(vl_niveau=vl_niveau),
        'is_hoofdzetel': bool(row.get('isHoofdzetel')),
        'lat': lat,
        'lon': lon,
    }


def _parse_instelling_html(html: str) -> dict:
    soup = BeautifulSoup(html, 'html.parser')
    detail: dict = {}
    for row in soup.select('#fichetabel .ficherij'):
        label_el = row.select_one('.fichelabel')
        data_el = row.select_one('.fichedata')
        if not label_el or not data_el:
            continue
        key = label_el.get_text(strip=True).lower()
        if key == 'e-mail':
            a = data_el.find('a', href=True)
            detail['email'] = _norm(a.get_text() if a else data_el.get_text())
        elif key == 'website':
            a = data_el.find('a', href=True)
            detail['website'] = _normalize_website(
                a['href'] if a and a.get('href') else data_el.get_text()
            )
        elif key == 'telefoon':
            detail['telefoon'] = _norm(data_el.get_text())
        elif key == 'naam':
            detail['naam_detail'] = _norm(data_el.get_text())
        elif key == 'adres':
            lines = [l.strip() for l in data_el.get_text('\n', strip=True).split('\n') if l.strip()]
            if lines:
                detail['adres_detail_straat'] = lines[0]
            if len(lines) > 1:
                pc2, pl2 = parse_adreslijn2(lines[1])
                if pc2:
                    detail['adres_detail_pc'] = pc2
                if pl2:
                    detail['adres_detail_plaats'] = pl2
        elif key == 'directeur':
            detail['directeur'] = _norm(data_el.get_text())
        elif key == 'onderwijsnet':
            detail['onderwijsnet'] = _norm(data_el.get_text())
    return detail


def _fetch_instelling_detail_http(sn: int) -> dict:
    url = f'{VL_DETAIL_URL}?sn={sn}'
    r = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=45)
    r.raise_for_status()
    return _parse_instelling_html(r.text)


def prefetch_instelling_details(
    instellingsnummers: List[int],
    *,
    workers: int = 10,
    min_interval_s: float = 0.12,
) -> Dict[int, dict]:
    """Haal alle instellingsfiches parallel op (één keer per instellingsnummer)."""
    sns = sorted({int(sn) for sn in instellingsnummers if sn})
    cache: Dict[int, dict] = {}
    if not sns:
        return cache
    lock = threading.Lock()
    last_start = [0.0]

    def _throttled_fetch(sn: int) -> Tuple[int, dict]:
        with lock:
            wait = min_interval_s - (time.time() - last_start[0])
            if wait > 0:
                time.sleep(wait)
            last_start[0] = time.time()
        try:
            return sn, _fetch_instelling_detail_http(sn)
        except Exception:
            return sn, {}

    print(f'[VL] Detailfiches ophalen: {len(sns)} unieke instellingen…', flush=True)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_throttled_fetch, sn) for sn in sns]
        for fut in as_completed(futures):
            sn, detail = fut.result()
            cache[sn] = detail
            done += 1
            if done % 200 == 0 or done == len(sns):
                print(f'  detailfiches {done}/{len(sns)}', flush=True)
    return cache


def fetch_instelling_detail(sn: int, cache: Dict[int, dict]) -> dict:
    if sn in cache:
        return cache[sn]
    detail = _fetch_instelling_detail_http(sn)
    cache[sn] = detail
    time.sleep(0.35)
    return detail


def stub_to_document(stub: dict, detail: dict) -> dict:
    email = _norm(detail.get('email'))
    website = _norm(detail.get('website'))
    telefoon = _norm(detail.get('telefoon'))
    straat = stub.get('straat_vestiging') or detail.get('adres_detail_straat') or ''
    pc = stub.get('postcode_vestiging') or detail.get('adres_detail_pc') or ''
    plaats = stub.get('plaats') or detail.get('adres_detail_plaats') or ''
    prov = stub.get('provincie') or provincie_from_postcode(pc, plaats)
    naam = stub.get('naam') or detail.get('naam_detail') or ''
    doc = {
        'land': 'BE',
        'gemeenschap': 'VL',
        'administratienummer': stub['administratienummer'],
        'instellingsnummer': stub['instellingsnummer'],
        'naam': naam,
        'plaats': plaats,
        'gemeente': plaats,
        'provincie': prov,
        'soort': f"VL niveau {stub['vl_niveau']}",
        'soort_norm': stub['soort_norm'],
        'status': 'actief',
        'straat_vestiging': straat,
        'huisnr_vestiging': '',
        'huisnr_toev_vestiging': '',
        'postcode_vestiging': pc,
        'telefoon': telefoon,
        'email': email,
        'email_bron': 'vl-instellingsfiche' if email else '',
        'website': website,
        'bron_bestand': 'data-onderwijs.vlaanderen.be',
        'imported_at': time.strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    if stub.get('lat') is not None and stub.get('lon') is not None:
        doc['lat'] = stub['lat']
        doc['lon'] = stub['lon']
    if detail.get('directeur'):
        doc['directeur'] = detail['directeur']
    if detail.get('onderwijsnet'):
        doc['onderwijsnet'] = detail['onderwijsnet']
    return doc


def _merge_preserve_email(doc: dict) -> None:
    if _norm(doc.get('email')):
        return
    existing = schools_coll.find_one(
        {'administratienummer': doc['administratienummer'], 'land': 'BE'},
        projection={'email': 1},
    )
    if existing and _norm(existing.get('email')):
        doc['email'] = _norm(existing['email'])


def _merge_preserve_coords(doc: dict) -> None:
    if doc.get('lat') is not None and doc.get('lon') is not None:
        return
    existing = schools_coll.find_one(
        {'administratienummer': doc['administratienummer'], 'land': 'BE'},
        projection={'lat': 1, 'lon': 1},
    )
    if existing and existing.get('lat') is not None and existing.get('lon') is not None:
        doc['lat'] = existing['lat']
        doc['lon'] = existing['lon']


def upsert_documents(docs: Iterable[dict]) -> int:
    ops: List[UpdateOne] = []
    n = 0
    for doc in docs:
        if not doc or not doc.get('administratienummer'):
            continue
        _merge_preserve_email(doc)
        _merge_preserve_coords(doc)
        ops.append(
            UpdateOne(
                {'administratienummer': doc['administratienummer'], 'land': 'BE'},
                {'$set': doc},
                upsert=True,
            )
        )
        if len(ops) >= 200:
            schools_coll.bulk_write(ops, ordered=False)
            n += len(ops)
            print(f'  Mongo: {n} upserts…', flush=True)
            ops.clear()
    if ops:
        schools_coll.bulk_write(ops, ordered=False)
        n += len(ops)
    return n


def _vestigings_adres_be(doc: dict) -> str:
    parts = []
    s = (doc.get('straat_vestiging') or '').strip()
    if s:
        parts.append(s)
    tail = []
    pc = (doc.get('postcode_vestiging') or '').strip()
    plaats = (doc.get('plaats') or '').strip()
    if pc:
        tail.append(pc)
    if plaats:
        tail.append(plaats)
    prov = (doc.get('provincie') or '').strip()
    if prov:
        tail.append(prov)
    if tail:
        parts.append(' '.join(tail))
    if not parts:
        return ''
    return ', '.join(parts) + ', België'


def geocode_be_schools(limit: Optional[int] = None) -> None:
    from scraper import geocode_query, is_valid_be_coords

    q = {
        'land': 'BE',
        'gemeenschap': 'VL',
        '$or': [
            {'lat': {'$exists': False}}, {'lat': None},
            {'lon': {'$exists': False}}, {'lon': None},
        ],
    }
    work = list(schools_coll.find(q))
    if limit is not None:
        work = work[:limit]
    total = len(work)
    print(f'[geo BE] {total} school(len) zonder coördinaten.', flush=True)
    ok = miss = skip = 0
    for i, doc in enumerate(work, 1):
        addr = _vestigings_adres_be(doc)
        if not addr.strip():
            skip += 1
            continue
        geom = geocode_query(addr, countrycodes='be', plaats_expected=doc.get('plaats'))
        if not geom or not is_valid_be_coords(geom['lat'], geom['lon']):
            miss += 1
            continue
        schools_coll.update_one(
            {'_id': doc['_id']},
            {'$set': {
                'lat': geom['lat'],
                'lon': geom['lon'],
                'geocode_query': addr,
                'geocoded_at': time.strftime('%Y-%m-%dT%H:%M:%SZ'),
            }},
        )
        ok += 1
        if i % 20 == 0 or i == total:
            print(f'[geo BE] {i}/{total} ok={ok} miss={miss} skip={skip}', flush=True)
    print(f'[geo BE] Klaar: {ok} gegeocodeerd.', flush=True)


def import_vl(
    niveaus: List[int],
    limit: Optional[int],
    *,
    fetch_details: bool = True,
) -> Tuple[int, int, int]:
    all_stubs: List[dict] = []
    detail_cache: Dict[int, dict] = {}
    per_niveau_limit = limit

    for nv in niveaus:
        rows = fetch_vl_vestigingen(nv, per_niveau_limit)
        for row in rows:
            all_stubs.append(vestiging_to_stub(row, nv))
        if limit is not None:
            break

    unique_sn = sorted({s['instellingsnummer'] for s in all_stubs})
    print(f'[VL] {len(all_stubs)} vestigingen, {len(unique_sn)} unieke instellingsnummers.', flush=True)

    if fetch_details:
        detail_cache = prefetch_instelling_details(unique_sn)

    docs: List[dict] = []
    emails_found = 0
    for i, stub in enumerate(all_stubs, 1):
        detail = detail_cache.get(stub['instellingsnummer'], {}) if fetch_details else {}
        doc = stub_to_document(stub, detail)
        if doc.get('email'):
            emails_found += 1
        docs.append(doc)
        if i % 500 == 0 or i == len(all_stubs):
            print(
                f'  documenten {i}/{len(all_stubs)} (emails {emails_found})',
                flush=True,
            )

    n = upsert_documents(docs)
    with_email = sum(1 for d in docs if d.get('email'))
    print(
        f'[VL] Klaar: {n} upserts, {with_email}/{len(docs)} met e-mail '
        f'({len(detail_cache)} detailfiches opgehaald).',
        flush=True,
    )
    return n, len(docs), with_email


def main() -> None:
    parser = argparse.ArgumentParser(description='Belgische scholen (Vlaanderen) → MongoDB')
    parser.add_argument(
        '--niveau',
        action='append',
        choices=list(NIVEAU_MAP.keys()),
        help='basis, secundair, hoger (meerdere keren mogelijk; default: basis)',
    )
    parser.add_argument('--limit', type=int, default=None, help='Max vestigingen (test)')
    parser.add_argument('--geocode', action='store_true', help='Geocode BE scholen zonder lat/lon')
    parser.add_argument('--geocode-only', action='store_true', help='Alleen geocoderen')
    parser.add_argument(
        '--skip-details',
        action='store_true',
        help='Geen instellingsfiches ophalen (sneller, geen e-mail)',
    )
    args = parser.parse_args()

    _mongo_ping()

    if args.geocode_only:
        geocode_be_schools(limit=args.limit)
        return

    niveau_keys = args.niveau or ['basis']
    niveaus = [NIVEAU_MAP[k] for k in niveau_keys]
    print(f'Start VL-import: niveaus={niveaus}, limit={args.limit}', flush=True)
    import_vl(niveaus, args.limit, fetch_details=not args.skip_details)

    if args.geocode:
        geocode_be_schools()


if __name__ == '__main__':
    main()
