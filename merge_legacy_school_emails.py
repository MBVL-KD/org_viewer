#!/usr/bin/env python3
"""
Zet e-mailadressen uit oude RIO/CSV-exporten terug op `schools` in MongoDB,
na een DUO-import waarbij de sleutel anders heet maar nog steeds op BRIN
gebaseerd is.

Mapping (in volgorde):
  1) Oude `Administratienummer` (4 tekens) → DUO `VESTIGINGSCODE` = code + '00'
     als die in Mongo bestaat; anders de enige Mongo-sleutel met dezelfde
     eerste 4 tekens; bij meerdere vestigingen: disambiguatie op genormaliseerde
     postcode vestiging.
  2) Als nog leeg: unieke match op genormaliseerde website-host (oude CSV
     t.o.v. Mongo), alleen als er precies één school zonder e-mail die host heeft.

Standaard: alleen rapport. Met --apply echt bijwerken.

Voorbeeld:
  python3 merge_legacy_school_emails.py \\
    ~/Downloads/ho-Scholen.csv ~/Downloads/Scholen-3.csv ~/Downloads/Scholen-4.csv
  python3 merge_legacy_school_emails.py --apply \\
    ~/Downloads/ho-Scholen.csv ~/Downloads/Scholen-3.csv ~/Downloads/Scholen-4.csv
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import certifi
import pandas as pd
from dotenv import load_dotenv
from pymongo import MongoClient

from import_schools import _rg, _row_lookup, read_school_dataframe

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
schools_coll = client[MONGO_DB]['schools']


def _norm(s) -> str:
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ''
    return ' '.join(str(s).split()).strip()


def _norm_pc(pc: str) -> str:
    return ''.join(_norm(pc).split()).upper()


def _host(url: str) -> str:
    u = _norm(url)
    if not u or u.upper() == 'NAN':
        return ''
    if not u.startswith('http'):
        u = 'https://' + u
    try:
        h = urlparse(u).netloc.lower()
        if h.startswith('www.'):
            h = h[4:]
        return h
    except Exception:
        return ''


def _mongo_ping():
    try:
        client.admin.command('ping')
    except Exception as exc:
        print(f'MongoDB niet bereikbaar: {exc}', flush=True)
        raise SystemExit(1) from exc
    print('MongoDB verbinding OK.', flush=True)


def _email_ok(raw: str) -> bool:
    from scraper import is_placeholder_email

    e = _norm(raw)
    if not e or e.upper() == 'NAN':
        return False
    return not is_placeholder_email(e)


def _sort_paths(paths: List[Path]) -> List[Path]:
    """ho-Scholen eerst, dan Scholen-3, daarna overig (o.a. Scholen-4)."""

    def key(p: Path):
        n = p.name.lower()
        if n.startswith('ho-scholen'):
            return (0, n)
        if 'scholen-3' in n:
            return (1, n)
        if 'scholen-4' in n:
            return (2, n)
        return (9, n)

    return sorted(paths, key=key)


def _collect_legacy_rows(paths: List[Path]) -> List[Tuple[str, str, str, str, str]]:
    """(admin4, email, postcode, website, bron); eerste e-mail per 4-char code wint."""
    rows: List[Tuple[str, str, str, str, str]] = []
    seen: Set[str] = set()
    for path in _sort_paths(paths):
        path = path.expanduser()
        if not path.is_file():
            print(f' overslaan (bestaat niet): {path}', flush=True)
            continue
        df = read_school_dataframe(path)
        bron = path.name
        for _, row in df.iterrows():
            lu = _row_lookup(row)
            admin4 = _rg(lu, 'Administratienummer').upper().replace(' ', '')
            if len(admin4) != 4 or admin4 in seen:
                continue
            email = _rg(lu, 'Email')
            if not _email_ok(email):
                continue
            seen.add(admin4)
            rows.append((
                admin4,
                _norm(email),
                _rg(lu, 'Postcode vestiging'),
                _rg(lu, 'Website'),
                bron,
            ))
    return rows


def _load_mongo_index() -> Tuple[Set[str], Dict[str, str], Dict[str, List[str]], Dict[str, List[str]]]:
    mongo_ids: Set[str] = set()
    admin_pc: Dict[str, str] = {}
    prefix: Dict[str, List[str]] = defaultdict(list)

    for d in schools_coll.find({}, {'administratienummer': 1, 'postcode_vestiging': 1}):
        aid = _norm(d.get('administratienummer')).upper().replace(' ', '')
        if not aid:
            continue
        mongo_ids.add(aid)
        admin_pc[aid] = _norm_pc(d.get('postcode_vestiging') or '')
        if len(aid) >= 4:
            prefix[aid[:4]].append(aid)

    q_empty = {'$or': [{'email': {'$exists': False}}, {'email': None}, {'email': ''}]}
    host_empty: Dict[str, List[str]] = defaultdict(list)
    for d in schools_coll.find(q_empty, {'administratienummer': 1, 'website': 1}):
        aid = _norm(d.get('administratienummer')).upper().replace(' ', '')
        h = _host(d.get('website') or '')
        if aid and h:
            host_empty[h].append(aid)

    return mongo_ids, admin_pc, dict(prefix), dict(host_empty)


def _map_rio_four_char_to_mongo_admin(
    rio4: str,
    postcode_vest: str,
    mongo_ids: Set[str],
    admin_pc: Dict[str, str],
    prefix: Dict[str, List[str]],
) -> Optional[str]:
    o = _norm(rio4).upper().replace(' ', '')
    if len(o) != 4 or not o.isalnum():
        return None
    pc = _norm_pc(postcode_vest)

    cand = o + '00'
    if cand in mongo_ids:
        return cand

    opts = prefix.get(o, [])
    if len(opts) == 1:
        return opts[0]
    if len(opts) > 1 and pc:
        hit = [m for m in opts if admin_pc.get(m) == pc]
        if len(hit) == 1:
            return hit[0]
    return None


def run_merge(paths: List[Path], apply: bool) -> None:
    legacy_rows = _collect_legacy_rows(paths)
    print(f'Oude CSV: {len(legacy_rows)} unieke 4-codes met bruikbare e-mail.', flush=True)

    mongo_ids, admin_pc, prefix, host_empty = _load_mongo_index()
    print(f'Mongo: {len(mongo_ids)} school-id\'s.', flush=True)

    brin_updates: Dict[str, Tuple[str, str]] = {}
    brin_skip = 0
    for admin4, email, pc, _website, bron in legacy_rows:
        mid = _map_rio_four_char_to_mongo_admin(admin4, pc, mongo_ids, admin_pc, prefix)
        if not mid:
            brin_skip += 1
            continue
        if mid not in brin_updates:
            brin_updates[mid] = (email, f'rio_csv_brin:{bron}')

    print(f'BRIN/postcode-map: {len(brin_updates)} doel-sleutels.', flush=True)
    print(f'BRIN-map: {brin_skip} oude rijen zonder Mongo-match.', flush=True)

    brin_mids = set(brin_updates.keys())
    host_updates: Dict[str, Tuple[str, str]] = {}
    host_skip_multi = 0
    host_skip_none = 0
    seen_host: Set[str] = set()

    for admin4, email, _pc, website, bron in legacy_rows:
        h = _host(website)
        if not h or h in seen_host:
            continue
        seen_host.add(h)
        mids = [m for m in host_empty.get(h, []) if m not in brin_mids]
        if len(mids) != 1:
            if len(mids) > 1:
                host_skip_multi += 1
            else:
                host_skip_none += 1
            continue
        mid = mids[0]
        if mid not in host_updates and mid not in brin_updates:
            host_updates[mid] = (email, f'rio_csv_host:{bron}')

    print(f'Website-host map: {len(host_updates)} extra (uniek).', flush=True)
    print(f'Website-host overgeslagen: {host_skip_none} geen kandidaat, {host_skip_multi} meerdere.', flush=True)

    combined: Dict[str, Tuple[str, str]] = {**host_updates, **brin_updates}
    print(f'Totaal geplande unieke updates: {len(combined)}.', flush=True)

    if not apply:
        print('Geen wijzigingen (voeg --apply toe om te schrijven).', flush=True)
        return

    now = time.strftime('%Y-%m-%dT%H:%M:%SZ')
    n = 0
    for mid, (email, src) in combined.items():
        res = schools_coll.update_one(
            {
                'administratienummer': mid,
                '$or': [{'email': {'$exists': False}}, {'email': None}, {'email': ''}],
            },
            {'$set': {
                'email': email,
                'email_herkomst': src,
                'email_gemerged_at': now,
            }},
        )
        if res.modified_count:
            n += 1
    print(f'Bijgewerkt: {n} document(en) (alleen lege e-mail).', flush=True)


def main():
    ap = argparse.ArgumentParser(description='E-mails uit oude RIO-CSV\'s mappen naar Mongo schools.')
    ap.add_argument(
        'csv_files',
        nargs='*',
        type=str,
        default=[
            str(Path.home() / 'Downloads' / 'ho-Scholen.csv'),
            str(Path.home() / 'Downloads' / 'Scholen-3.csv'),
            str(Path.home() / 'Downloads' / 'Scholen-4.csv'),
        ],
    )
    ap.add_argument('--apply', action='store_true', help='Voer Mongo-updates uit (default: alleen rapport)')
    args = ap.parse_args()

    paths = [Path(p) for p in args.csv_files if p]
    if not paths:
        print('Geen CSV-paden.', flush=True)
        sys.exit(1)

    _mongo_ping()
    run_merge(paths, apply=args.apply)


if __name__ == '__main__':
    main()
