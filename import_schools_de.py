#!/usr/bin/env python3
"""
Importeer Duitse schooldata (JedeSchule) naar MongoDB-collectie `schools` met land=DE.

Bronnen:
  - API: https://jedeschule.codefor.de/schools/
  - CSV: https://jedeschule.codefor.de/csv-data/latest.csv

Voorbeelden:
  python3 import_schools_de.py --limit 100
  python3 import_schools_de.py --csv ~/Downloads/latest.csv --limit 500
  python3 import_schools_de.py --geocode-only
  python3 import_schools_de.py --limit 100 --geocode
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import certifi
import pandas as pd
import requests
from dotenv import load_dotenv
from pymongo import MongoClient

from de_bundesland import (
    bundesland_code_from_school_id,
    normalize_de_bundesland,
)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ENV_CANDIDATES = [ROOT / 'Editor' / 'server' / '.env', ROOT / 'Editor' / '.env', ROOT / '.env']
for env_file in ENV_CANDIDATES:
    if env_file.exists():
        load_dotenv(env_file)
        break

MONGO_URI = os.environ.get('MONGO_URI', 'mongodb://localhost:27017/')
MONGO_DB = os.environ.get('MONGO_DB', 'damclubs')
JEDESCHULE_API = 'https://jedeschule.codefor.de/schools/'
JEDESCHULE_CSV = 'https://jedeschule.codefor.de/csv-data/latest.csv'
USER_AGENT = 'Draughts4All-schools-de/1.0 (contact: org_viewer)'

client = MongoClient(MONGO_URI, tlsCAFile=certifi.where(), serverSelectionTimeoutMS=15000)
db = client[MONGO_DB]
schools_coll = db['schools']

DE_CONTACT_URL_SUFFIXES: Tuple[str, ...] = (
    '/kontakt',
    '/impressum',
    '/kontakt.html',
    '/impressum.html',
    '/de/kontakt',
    '/de/impressum',
    '/contact',
    '/contact.html',
    '/ueber-uns/kontakt',
    '/about/kontakt',
)


def _mongo_ping():
    try:
        client.admin.command('ping')
    except Exception as exc:
        print(f'MongoDB niet bereikbaar: {exc}', flush=True)
        raise SystemExit(1) from exc
    print('MongoDB verbinding OK.', flush=True)


def _norm(s: Any) -> str:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ''
    return ' '.join(str(s).split()).strip()


def _normalize_website(url: str) -> str:
    if not url:
        return ''
    url = str(url).strip()
    if url.startswith('www.'):
        return 'https://' + url
    if url.startswith('//'):
        return 'https:' + url
    if not re.match(r'^https?://', url, re.I):
        return 'https://' + url
    return url


def _float_or_none(v: Any) -> Optional[float]:
    if v is None or v == '':
        return None
    try:
        f = float(v)
        if pd.isna(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def api_record_to_document(rec: dict, bron: str) -> Optional[dict]:
    admin = _norm(rec.get('id'))
    if not admin:
        return None
    state_raw = _norm(rec.get('state'))
    bundesland = normalize_de_bundesland(state_raw, admin)
    bundesland_code = state_raw.upper() if len(state_raw) == 2 else bundesland_code_from_school_id(admin)
    straat = _norm(rec.get('address'))
    addr2 = _norm(rec.get('address2'))
    if addr2:
        straat = f'{straat}, {addr2}'.strip(', ')
    plaats = _norm(rec.get('city'))
    pc = _norm(rec.get('zip'))
    lat = _float_or_none(rec.get('latitude'))
    lon = _float_or_none(rec.get('longitude'))

    doc: dict = {
        'land': 'DE',
        'administratienummer': admin,
        'naam': _norm(rec.get('name')),
        'plaats': plaats,
        'gemeente': plaats,
        'provincie': bundesland,
        'bundesland_code': bundesland_code,
        'status': _norm(rec.get('legal_status')),
        'soort': _norm(rec.get('school_type')),
        'straat_vestiging': straat,
        'huisnr_vestiging': '',
        'huisnr_toev_vestiging': '',
        'postcode_vestiging': pc,
        'telefoon': _norm(rec.get('phone')),
        'fax': _norm(rec.get('fax')),
        'email': _norm(rec.get('email')),
        'website': _normalize_website(_norm(rec.get('website'))),
        'provider': _norm(rec.get('provider')),
        'bron_bestand': bron,
        'imported_at': time.strftime('%Y-%m-%dT%H:%M:%SZ'),
    }
    if lat is not None and lon is not None:
        doc['lat'] = lat
        doc['lon'] = lon
    ts = rec.get('update_timestamp')
    if ts:
        doc['bron_update'] = str(ts)
    return doc


def csv_row_to_document(row: pd.Series, bron: str) -> Optional[dict]:
    rec = {
        'id': row.get('id'),
        'name': row.get('name'),
        'address': row.get('address'),
        'address2': row.get('address2'),
        'city': row.get('city'),
        'zip': row.get('zip'),
        'website': row.get('website'),
        'email': row.get('email'),
        'phone': row.get('phone'),
        'fax': row.get('fax'),
        'school_type': row.get('school_type'),
        'legal_status': row.get('legal_status'),
        'provider': row.get('provider'),
        'state': row.get('state'),
        'latitude': row.get('latitude'),
        'longitude': row.get('longitude'),
        'update_timestamp': row.get('update_timestamp'),
    }
    return api_record_to_document(rec, bron)


def _vestigings_adres_de(doc: dict) -> str:
    parts = []
    s = (doc.get('straat_vestiging') or '').strip()
    if s:
        parts.append(s)
    pc = (doc.get('postcode_vestiging') or '').strip()
    plaats = (doc.get('plaats') or '').strip()
    tail = []
    if pc:
        tail.append(pc)
    if plaats:
        tail.append(plaats)
    bl = (doc.get('provincie') or '').strip()
    if bl:
        tail.append(bl)
    if tail:
        parts.append(' '.join(tail))
    if not parts:
        return ''
    return ', '.join(parts) + ', Deutschland'


def fetch_api_schools(limit: Optional[int], page_size: int = 100) -> List[dict]:
    out: List[dict] = []
    skip = 0
    while True:
        batch_limit = page_size
        if limit is not None:
            remaining = limit - len(out)
            if remaining <= 0:
                break
            batch_limit = min(batch_limit, remaining)
        params = {'limit': batch_limit, 'skip': skip}
        r = requests.get(
            JEDESCHULE_API,
            params=params,
            headers={'User-Agent': USER_AGENT},
            timeout=120,
        )
        r.raise_for_status()
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        print(f'  API: {len(out)} scholen opgehaald (skip={skip})…', flush=True)
        if len(batch) < batch_limit:
            break
        skip += len(batch)
        if limit is not None and len(out) >= limit:
            out = out[:limit]
            break
        time.sleep(0.3)
    return out


def read_csv_schools(path: Path, limit: Optional[int]) -> List[pd.Series]:
    usecols = [
        'id', 'name', 'address', 'address2', 'zip', 'city', 'website', 'email',
        'school_type', 'legal_status', 'provider', 'fax', 'phone', 'state',
        'latitude', 'longitude', 'update_timestamp',
    ]
    df = pd.read_csv(path, dtype=str, usecols=lambda c: c in usecols, encoding='utf-8-sig')
    df = df.fillna('')
    rows = []
    for _, row in df.iterrows():
        rows.append(row)
        if limit is not None and len(rows) >= limit:
            break
    return rows


def _merge_preserve_email(doc: dict) -> None:
    if _norm(doc.get('email')):
        return
    existing = schools_coll.find_one(
        {'administratienummer': doc['administratienummer'], 'land': 'DE'},
        projection={'email': 1},
    )
    if existing and _norm(existing.get('email')):
        doc['email'] = _norm(existing.get('email'))


def _merge_preserve_coords(doc: dict) -> None:
    if doc.get('lat') is not None and doc.get('lon') is not None:
        return
    existing = schools_coll.find_one(
        {'administratienummer': doc['administratienummer'], 'land': 'DE'},
        projection={'lat': 1, 'lon': 1},
    )
    if not existing:
        return
    if existing.get('lat') is not None and existing.get('lon') is not None:
        doc['lat'] = existing['lat']
        doc['lon'] = existing['lon']


def upsert_documents(docs: Iterable[dict]) -> Tuple[int, Set[str]]:
    total = 0
    ids: Set[str] = set()
    for doc in docs:
        if not doc:
            continue
        ids.add(doc['administratienummer'])
        _merge_preserve_email(doc)
        _merge_preserve_coords(doc)
        schools_coll.update_one(
            {'administratienummer': doc['administratienummer'], 'land': 'DE'},
            {'$set': doc},
            upsert=True,
        )
        total += 1
    return total, ids


def import_from_api(limit: Optional[int]) -> Tuple[int, Set[str]]:
    records = fetch_api_schools(limit=limit)
    docs = [api_record_to_document(r, 'jedeschule-api') for r in records]
    docs = [d for d in docs if d]
    n, ids = upsert_documents(docs)
    print(f'JedeSchule API: {n} scholen geïmporteerd/upsert.', flush=True)
    return n, ids


def import_from_csv(path: Path, limit: Optional[int]) -> Tuple[int, Set[str]]:
    rows = read_csv_schools(path, limit=limit)
    docs = [csv_row_to_document(row, path.name) for row in rows]
    docs = [d for d in docs if d]
    n, ids = upsert_documents(docs)
    print(f'{path.name}: {n} rijen geïmporteerd/upsert.', flush=True)
    return n, ids


def prune_de_not_in(admin_ids: Set[str]) -> int:
    if not admin_ids:
        return 0
    res = schools_coll.delete_many({
        'land': 'DE',
        'administratienummer': {'$nin': list(admin_ids)},
    })
    return int(res.deleted_count)


def geocode_de_schools_without_coords(limit: Optional[int] = None):
    from scraper import geocode_query

    q = {
        'land': 'DE',
        '$or': [
            {'lat': {'$exists': False}},
            {'lat': None},
            {'lon': {'$exists': False}},
            {'lon': None},
        ],
    }
    work = list(schools_coll.find(q))
    if limit is not None:
        work = work[:limit]
    total = len(work)
    print(f'[geo DE] Start: {total} school(len) zonder lat/lon.', flush=True)
    count = skip = miss = 0
    for i, doc in enumerate(work, 1):
        addr = _vestigings_adres_de(doc)
        if not addr.strip():
            skip += 1
            continue
        geom = geocode_query(
            addr,
            skip_cache=False,
            plaats_expected=doc.get('plaats') or None,
            countrycodes='de',
        )
        if not geom:
            miss += 1
            continue
        upd = {
            'lat': geom['lat'],
            'lon': geom['lon'],
            'geocode_query': addr,
            'geocoded_at': time.strftime('%Y-%m-%dT%H:%M:%SZ'),
        }
        if not _norm(doc.get('provincie')) and geom.get('provincie'):
            upd['provincie'] = normalize_de_bundesland(geom['provincie'])
        schools_coll.update_one({'_id': doc['_id']}, {'$set': upd})
        count += 1
        if i == 1 or i % 10 == 0:
            print(f'[geo DE] {i}/{total} — ok={count} miss={miss}', flush=True)
    print(f'[geo DE] Klaar: {count} gegeocodeerd, {miss} miss, {skip} zonder adres.', flush=True)


def run_scrape_missing_emails_de(limit: Optional[int], delay_s: float = 0.7):
    from import_schools import (
        MAX_SCRAPE_URLS_PER_SCHOOL,
        _normalize_website,
        _pick_best_email,
        scrape_email_for_document,
    )
    from scraper import EMAIL_REGEX, scrape_website_contact_info

    q = {
        'land': 'DE',
        '$or': [{'email': {'$exists': False}}, {'email': None}, {'email': ''}],
        'website': {'$exists': True, '$nin': [None, '']},
    }
    work = list(schools_coll.find(q))
    updated = 0
    for doc in work:
        if limit is not None and updated >= limit:
            break
        website = doc.get('website') or ''
        home = _normalize_website(website)
        if not home:
            continue
        urls = [home]
        root = home.rstrip('/')
        for suf in DE_CONTACT_URL_SUFFIXES:
            u = root + suf
            if u not in urls:
                urls.append(u)
        urls = urls[:MAX_SCRAPE_URLS_PER_SCHOOL]
        collected: List[str] = []
        for url in urls:
            info = scrape_website_contact_info(url, timeout=10.0)
            collected.extend(info.get('emails') or [])
            best = _pick_best_email(collected)
            if best and EMAIL_REGEX.search(best):
                schools_coll.update_one(
                    {'_id': doc['_id']},
                    {'$set': {
                        'email': best,
                        'email_scraped_at': time.strftime('%Y-%m-%dT%H:%M:%SZ'),
                    }},
                )
                updated += 1
                print(f'  scrape DE: {doc.get("naam")} -> {best}', flush=True)
                break
            time.sleep(delay_s)
    print(f'[email DE] Klaar: {updated} bijgewerkt.', flush=True)


def main():
    parser = argparse.ArgumentParser(description='Duitse scholen (JedeSchule) → MongoDB.')
    parser.add_argument('--csv', type=str, default='', help='Pad naar JedeSchule CSV (anders API)')
    parser.add_argument('--download-csv', action='store_true', help=f'Download {JEDESCHULE_CSV} naar --csv-pad')
    parser.add_argument('--csv-out', type=str, default='', help='Doelpad bij --download-csv')
    parser.add_argument('--limit', type=int, default=None, metavar='N', help='Max. aantal scholen (test)')
    parser.add_argument('--prune-not-in-upload', action='store_true', help='Verwijder DE-records niet in deze run')
    parser.add_argument('--geocode', action='store_true', help='Na import: geocode ontbrekende coördinaten (DE)')
    parser.add_argument('--geocode-only', action='store_true', help='Alleen geocoderen')
    parser.add_argument('--scrape-emails', action='store_true', help='Ontbrekende e-mails via website')
    parser.add_argument('--scrape-only', action='store_true', help='Alleen e-mailscrapen')
    args = parser.parse_args()

    _mongo_ping()
    schools_coll.create_index([('administratienummer', 1), ('land', 1)], unique=True)

    if args.scrape_only:
        run_scrape_missing_emails_de(limit=args.limit)
        return

    if args.geocode_only:
        geocode_de_schools_without_coords(limit=args.limit)
        return

    if args.download_csv:
        dest = Path(args.csv_out or Path.home() / 'Downloads' / 'jedeschule-latest.csv').expanduser()
        print(f'Download {JEDESCHULE_CSV} → {dest} …', flush=True)
        r = requests.get(JEDESCHULE_CSV, headers={'User-Agent': USER_AGENT}, timeout=600, stream=True)
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                if chunk:
                    f.write(chunk)
        print(f'Opgeslagen: {dest} ({dest.stat().st_size // (1<<20)} MB)', flush=True)
        args.csv = str(dest)

    if args.csv:
        n, ids = import_from_csv(Path(args.csv).expanduser(), limit=args.limit)
    else:
        n, ids = import_from_api(limit=args.limit)

    print(f'Totaal: {n} scholen (land=DE).', flush=True)

    if args.prune_not_in_upload:
        nd = prune_de_not_in(ids)
        print(f'Prune DE: {nd} verwijderd.', flush=True)

    if args.scrape_emails:
        run_scrape_missing_emails_de(limit=args.limit)

    if args.geocode:
        geocode_de_schools_without_coords(limit=args.limit)


if __name__ == '__main__':
    main()
