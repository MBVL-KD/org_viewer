import os
import re
import time
import json
from urllib.parse import unquote
from pathlib import Path
from dotenv import load_dotenv
import requests
import certifi
from bs4 import BeautifulSoup
from pymongo import MongoClient

from nl_provincie import canonical_nl_provincie_club, normalize_nl_provincienaam

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
collection = db['clubs']

KNOWN_PROVINCES = {
    'DDB', 'DZHZ', 'GDB', 'PFDB', 'PGD', 'PLDB', 'PNDB', 'PNHD', 'PODB', 'PZDB', 'UPDB', 'ZHDB'
}

PROVINCIE_URLS = {
    'DDB': 'https://home.kpn.nl/prins983/dammen/ddb',
    'DZHZ': 'https://www.dambondzhz.nl/',
    'GDB': 'https://www.geldersedambond.nl/',
    'PFDB': 'https://www.pfdb.nl/',
    'PGD': 'https://home.kpn.nl/prins983/dammen/pgd',
    'PLDB': 'https://pldb.nl/cms/',
    'PNDB': 'https://www.pndb.nl/',
    'PNHD': 'https://www.pnhdb.nl/',
    'PODB': 'https://www.podb.nl/',
    'PZDB': 'https://www.adbouwens.nl/PZDB.html',
    'UPDB': 'https://www.updb-dammen.nl/',
    'ZHDB': 'https://www.zhdb.nl/'
}

GEOCODE_CACHE_PATH = HERE / 'geocode_cache.json'
USER_AGENT = 'Draughts4All Club Scraper/1.0 (+https://github.com/)'

EMAIL_REGEX = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
PHONE_REGEX = re.compile(r'\+?[0-9][0-9 \-/().]{6,}[0-9]')


def normalize(text):
    return re.sub(r'\s+', ' ', text or '').strip()


def absolute_url(url, base):
    if url.startswith('//'):
        return 'https:' + url
    if url.startswith('http'):
        return url
    if url.startswith('/'):
        return base.rstrip('/') + url
    return base.rstrip('/') + '/' + url


def get_province_code(text):
    if not text:
        return None
    for token in re.findall(r'\b[A-Z]{3,4}\b', text):
        if token in KNOWN_PROVINCES:
            return token
    return None


def normalize_website(url):
    if not url:
        return ''
    url = url.strip()
    if url.startswith('www.'):
        return 'https://' + url
    if url.startswith('//'):
        return 'https:' + url
    if not re.match(r'^https?://', url):
        return 'https://' + url
    return url


def decode_cloudflare_hex(hex_str):
    """Decode Cloudflare hex payload (same for URL #fragment and data-cfemail)."""
    hex_str = (hex_str or '').strip()
    if len(hex_str) < 4 or len(hex_str) % 2 != 0:
        return ''
    if not re.fullmatch(r'[0-9a-fA-F]+', hex_str, flags=re.I):
        return ''
    try:
        key = int(hex_str[:2], 16)
        chars = [chr(int(hex_str[i:i + 2], 16) ^ key) for i in range(2, len(hex_str), 2)]
        return ''.join(chars)
    except (ValueError, IndexError):
        return ''


def decode_cloudflare_email(href, tag=None):
    """Decode from mailto-protection URL hash and/or data-cfemail on the anchor."""
    href = (href or '').strip()
    if '#' in href:
        frag = href.split('#', 1)[1].split('?', 1)[0].strip()
        decoded = decode_cloudflare_hex(frag)
        if decoded:
            return decoded
    if tag is not None and tag.get('data-cfemail'):
        decoded = decode_cloudflare_hex(tag.get('data-cfemail'))
        if decoded:
            return decoded
    return ''


def iter_decoded_cloudflare_addresses(soup_or_tag):
    """Yield unique real emails hidden behind Cloudflare obfuscation."""
    if soup_or_tag is None:
        return
    seen = set()
    for el in soup_or_tag.select('[data-cfemail]'):
        decoded = decode_cloudflare_hex(el.get('data-cfemail', ''))
        if decoded and decoded not in seen:
            seen.add(decoded)
            yield decoded
    for a in soup_or_tag.find_all('a', href=True):
        if a.get('data-cfemail'):
            continue
        href = a['href'].strip()
        if 'email-protection' not in href.lower():
            continue
        if '#' not in href:
            continue
        decoded = decode_cloudflare_hex(href.split('#', 1)[1].split('?', 1)[0])
        if decoded and decoded not in seen:
            seen.add(decoded)
            yield decoded


def iter_mailto_addresses(soup_or_tag):
    if soup_or_tag is None:
        return
    seen = set()
    for a in soup_or_tag.find_all('a', href=True):
        href = a['href'].strip()
        if not href.startswith('mailto:'):
            continue
        email = unquote(href.split(':', 1)[1].split('?')[0].strip())
        if email and email not in seen:
            seen.add(email)
            yield email


def is_placeholder_email(value):
    if not value:
        return True
    v = re.sub(r'[\s\u00a0]+', ' ', value).strip().lower()
    if 'email protected' in v:
        return True
    if v.startswith('[') and 'protected' in v:
        return True
    if v in {'email beschermd', 'e-mail beschermd'}:
        return True
    return False


def resolve_contact_email(parsed_text, subtree_tags):
    """Prefer decoded / mailto addresses over visible [email protected] text."""
    candidates = []
    for root in subtree_tags:
        if root is None:
            continue
        candidates.extend(iter_decoded_cloudflare_addresses(root))
        candidates.extend(iter_mailto_addresses(root))
    parsed_text = normalize(parsed_text)
    if parsed_text:
        for match in EMAIL_REGEX.findall(parsed_text):
            if not is_placeholder_email(match):
                return match
        if not is_placeholder_email(parsed_text) and '@' in parsed_text:
            return parsed_text
    for addr in candidates:
        if addr and not addr.endswith('@kndb.nl'):
            m = EMAIL_REGEX.search(addr.strip())
            if m and not is_placeholder_email(m.group(0)):
                return m.group(0).strip()
    return ''


def is_valid_nl_coords(lat, lon):
    try:
        lat = float(lat)
        lon = float(lon)
    except Exception:
        return False
    return 50.5 <= lat <= 53.7 and 3.2 <= lon <= 7.2


def _nominatim_hit_is_netherlands(hit):
    """Bbox is ruim en dekt Vlaanderen; extra check op OSM-landcode."""
    addr = hit.get('address') or {}
    cc = (addr.get('country_code') or '').lower()
    if cc:
        return cc == 'nl'
    return True


def extract_field(text, label):
    if not text:
        return ''
    regex = re.compile(rf'{re.escape(label)}\s*:\s*(.*?)(?=\s+[A-Z][a-z]+:|$)', re.S)
    match = regex.search(text)
    if not match:
        return ''
    value = normalize(match.group(1))
    if value in {'Naam', 'Adres', 'Postcode', 'Woonplaats', 'Telefoon', 'Email'} or value.endswith(':'):
        return ''
    return value


def parse_club_section(text, section_name, section_roots=None):
    email_raw = extract_field(text, 'Email')
    email = resolve_contact_email(email_raw, section_roots or [])
    return {
        'naam': extract_field(text, 'Naam'),
        'adres': extract_field(text, 'Adres'),
        'postcode': extract_field(text, 'Postcode'),
        'woonplaats': extract_field(text, 'Woonplaats'),
        'telefoon': normalize_phone(extract_field(text, 'Telefoon')),
        'email': email,
        'functie': section_name,
    }


def parse_kndb_club_page(club_url):
    details = {'website': '', 'locaties': [], 'contactpersonen': [], 'emails': [], 'telefoons': []}
    try:
        response = requests.get(club_url, headers={'User-Agent': USER_AGENT}, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
    except Exception:
        return details

    main_content = soup.find('div', class_=re.compile(r'wp-site-blocks|entry-content|content', re.I)) or soup

    for p in main_content.find_all('p'):
        text = normalize(p.get_text(' ', strip=True))
        if any(skip in text.lower() for skip in ['bridgebond', 'koninklijke nederlandse dambond']):
            continue
        if text.lower().startswith('website:'):
            details['website'] = normalize_website(text.split(':', 1)[1].strip())
        details['emails'] += extract_emails(text)
        details['telefoons'] += extract_phones(text)

    for a in main_content.find_all('a', href=True):
        href = a['href'].strip()
        if 'email-protection' in href.lower():
            decoded = decode_cloudflare_email(href, a)
            if decoded:
                details['emails'].append(decoded)
        if href.startswith('mailto:'):
            email = href.split(':', 1)[1].split('?')[0].strip()
            if email:
                details['emails'].append(email)
        if href.startswith('tel:'):
            phone = normalize_phone(href.split(':', 1)[1].strip())
            if phone:
                details['telefoons'].append(phone)
        if '@' in a.get_text():
            details['emails'] += extract_emails(a.get_text())

    for header in main_content.find_all(['h2', 'h3', 'h4']):
        section_name = normalize(header.get_text())
        if section_name not in {'Clubgebouw', 'Extra locatie', 'Secretaris', 'Jeugdleider', 'Voorzitter', 'Penningmeester', 'Coördinator jeugd', 'Coördinator Teamwedstrijden'}:
            continue
        section_nodes = []
        node = header.find_next_sibling()
        while node is not None and node.name in {'p', 'div'}:
            section_nodes.append(node)
            node = node.find_next_sibling()
        section_text = normalize(' '.join(
            normalize(n.get_text(' ', strip=True)) for n in section_nodes
        ))
        if not section_text:
            continue

        if section_name in {'Clubgebouw', 'Extra locatie'}:
            location = parse_club_section(section_text, section_name, section_nodes)
            if location['naam'] or location['adres'] or location['telefoon']:
                details['locaties'].append({
                    'naam': location['naam'],
                    'adres': location['adres'],
                    'postcode': location['postcode'],
                    'woonplaats': location['woonplaats'],
                    'telefoon': location['telefoon'],
                })
            details['telefoons'] += extract_phones(section_text)
            continue

        contact = parse_club_section(section_text, section_name, section_nodes)
        if contact['naam'] or contact['telefoon'] or contact['email']:
            details['contactpersonen'].append(contact)
        if contact['email']:
            details['emails'].append(contact['email'])
        if contact['telefoon']:
            details['telefoons'].append(contact['telefoon'])

    for addr in iter_decoded_cloudflare_addresses(main_content):
        details['emails'].append(addr)

    details['emails'] = sorted(set(e for e in details['emails'] if not e.endswith('@kndb.nl')))
    details['telefoons'] = sorted(set(details['telefoons']))
    return details


def extract_emails(text):
    if not text:
        return []
    return sorted(set(EMAIL_REGEX.findall(text)))


def normalize_phone(phone):
    phone = re.sub(r'[^0-9+]', '', phone or '')
    if len(phone) < 7:
        return ''
    return phone


def extract_phones(text):
    if not text:
        return []
    matches = PHONE_REGEX.findall(text)
    phones = [normalize_phone(match) for match in matches]
    return sorted({p for p in phones if p})


def scrape_website_contact_info(url, timeout=30):
    result = {'emails': [], 'telefoons': []}
    if not url:
        return result
    try:
        response = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=timeout)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        page_text = soup.get_text(separator=' ', strip=True)

        for addr in iter_decoded_cloudflare_addresses(soup):
            result['emails'].append(addr)

        for a in soup.find_all('a', href=True):
            href = a['href'].strip()
            if href.startswith('mailto:'):
                email = unquote(href.split(':', 1)[1].split('?')[0].strip())
                if email:
                    result['emails'].append(email)
            if href.startswith('tel:'):
                phone = href.split(':', 1)[1].strip()
                normalized = normalize_phone(phone)
                if normalized:
                    result['telefoons'].append(normalized)

        result['emails'] += extract_emails(page_text)
        result['telefoons'] += extract_phones(page_text)
        result['emails'] = sorted(set(result['emails']))
        result['telefoons'] = sorted(set(result['telefoons']))
    except Exception:
        pass
    return result


def parse_kndb_clubs():
    url = 'https://www.kndb.nl/verenigingen/'
    response = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    clubs = []

    for heading in soup.find_all('h3'):
        heading_text = normalize(heading.get_text())
        province_code = get_province_code(heading_text)
        if not province_code:
            continue

        bond_url = None
        if heading.find('a') and heading.find('a').get('href'):
            bond_url = absolute_url(heading.find('a')['href'].strip(), 'https://www.kndb.nl')
        if not bond_url:
            bond_url = PROVINCIE_URLS.get(province_code, '')

        table = heading.find_next('table')
        if not table:
            continue

        for row in table.find_all('tr')[1:]:
            cells = row.find_all('td')
            if len(cells) < 4:
                continue

            club_url = ''
            name_cell = cells[1]
            anchor = name_cell.find('a', href=True)
            if anchor:
                club_url = absolute_url(anchor['href'].strip(), 'https://www.kndb.nl')

            clubs.append({
                'provincie': province_code,
                'bond_url': bond_url,
                'plaats': normalize(cells[0].get_text()),
                'naam': normalize(name_cell.get_text()),
                'secretariaat': normalize(cells[2].get_text()),
                'clublokaal': normalize(cells[3].get_text()),
                'club_url': club_url,
                'details': {'website': '', 'locaties': [], 'contactpersonen': [], 'emails': [], 'telefoons': []},
                'imported_at': time.strftime('%Y-%m-%dT%H:%M:%SZ')
            })

    return clubs


def scrape_generic_province_site(bond_url, club_names):
    details = {}
    try:
        response = requests.get(bond_url, headers={'User-Agent': USER_AGENT}, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        domain = re.sub(r'^https?://', '', bond_url).split('/')[0]

        for row in soup.find_all('tr'):
            cells = row.find_all('td')
            if len(cells) < 2:
                continue
            club_name = normalize(cells[0].get_text())
            if club_name not in club_names:
                continue

            website = ''
            for a in cells[1].find_all('a', href=True):
                href = a['href'].strip()
                if href.startswith('http') and domain not in href:
                    website = normalize_website(href)
                    break

            details[club_name] = {'website': website, 'locaties': [], 'contactpersonen': [], 'emails': [], 'telefoons': []}

        for a in soup.find_all('a', href=True):
            text = normalize(a.get_text())
            if text in club_names:
                href = a['href'].strip()
                if href.startswith('http') and domain not in href:
                    details.setdefault(text, {'website': '', 'locaties': [], 'contactpersonen': [], 'emails': [], 'telefoons': []})
                    details[text]['website'] = normalize_website(href)

        for club_name, data in details.items():
            if data.get('website'):
                website_info = scrape_website_contact_info(data['website'])
                data['emails'] = sorted(set(data.get('emails', []) + website_info.get('emails', [])))
                data['telefoons'] = sorted(set(data.get('telefoons', []) + website_info.get('telefoons', [])))

        time.sleep(1)
    except Exception:
        pass

    return details


def scrape_zhdb_details(bond_url):
    details = {}
    base = 'https://www.zhdb.nl'
    pages = [absolute_url('/over/verenigingen-a-i/', base), absolute_url('/over/verenigingen-j-z/', base)]
    visited = set()

    for page in pages:
        try:
            response = requests.get(page, headers={'User-Agent': USER_AGENT}, timeout=30)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
        except Exception:
            continue

        for a in soup.find_all('a', href=True):
            href = a['href'].strip()
            if '/over/verenigingen' not in href:
                continue
            if 'verenigingen-a-i' in href or 'verenigingen-j-z' in href:
                continue

            club_url = absolute_url(href, base)
            if club_url in visited:
                continue
            visited.add(club_url)

            try:
                response = requests.get(club_url, headers={'User-Agent': USER_AGENT}, timeout=30)
                response.raise_for_status()
                club_soup = BeautifulSoup(response.text, 'html.parser')
            except Exception:
                continue

            club_name = normalize(club_soup.find('h1').get_text() if club_soup.find('h1') else a.get_text())
            details[club_name] = parse_zhdb_club_page(club_soup)
            time.sleep(1)

    return details


def parse_zhdb_club_page(soup):
    details = {'website': '', 'locaties': [], 'contactpersonen': [], 'emails': [], 'telefoons': []}
    current_location = None
    last_key = None
    table = soup.find('table')
    if not table:
        return details

    for row in table.find_all('tr'):
        cells = row.find_all('td')
        if len(cells) < 2:
            continue
        key = normalize(cells[0].get_text())
        value_cell = cells[1]
        value = normalize(value_cell.get_text())

        if key == 'Speellokaal':
            if current_location:
                details['locaties'].append(current_location)
            current_location = {'naam': value, 'adres': '', 'telefoon': ''}
            last_key = 'Speellokaal'
            continue

        if not key and last_key == 'Speellokaal' and value.lower() != 'route':
            if current_location:
                if current_location['adres']:
                    current_location['adres'] += ', ' + value
                else:
                    current_location['adres'] = value
            continue

        if key in {'Voorzitter', 'Secretaris', 'Penningmeester', 'Coördinator jeugd', 'Coördinator Teamwedstrijden'}:
            parts = [part.strip() for part in value.split(',') if part.strip()]
            cell_emails = (
                list(iter_decoded_cloudflare_addresses(value_cell))
                + list(iter_mailto_addresses(value_cell))
                + extract_emails(value)
            )
            cell_emails = [e for e in dict.fromkeys(cell_emails) if e and not is_placeholder_email(e)]
            contact = {
                'functie': key,
                'naam': parts[0] if parts else '',
                'telefoon': parts[1] if len(parts) > 1 else '',
                'email': cell_emails[0] if cell_emails else ''
            }
            details['contactpersonen'].append(contact)
            details['emails'] += cell_emails
            details['telefoons'] += extract_phones(value)
            last_key = key
            continue

        if key == 'Secretariaat':
            emails = (
                list(iter_decoded_cloudflare_addresses(value_cell))
                + list(iter_mailto_addresses(value_cell))
                + extract_emails(value)
            )
            emails = [e for e in dict.fromkeys(emails) if e and not is_placeholder_email(e)]
            phones = extract_phones(value)
            details['contactpersonen'].append({
                'functie': 'Secretariaat',
                'naam': '',
                'adres': value,
                'telefoon': phones[0] if phones else '',
                'email': emails[0] if emails else ''
            })
            details['emails'] += emails
            details['telefoons'] += phones
            last_key = key
            continue

        if key == 'Website':
            details['website'] = normalize_website(value)
            last_key = key
            continue

    if current_location:
        details['locaties'].append(current_location)

    if details['website']:
        website_info = scrape_website_contact_info(details['website'])
        details['emails'] = sorted(set(details.get('emails', []) + website_info.get('emails', [])))
        details['telefoons'] = sorted(set(details.get('telefoons', []) + website_info.get('telefoons', [])))

    details['emails'] = sorted(set(details.get('emails', [])))
    details['telefoons'] = sorted(set(details.get('telefoons', [])))
    return details


def load_geocode_cache():
    if GEOCODE_CACHE_PATH.exists():
        try:
            with open(GEOCODE_CACHE_PATH, 'r', encoding='utf-8') as handle:
                return json.load(handle)
        except Exception:
            return {}
    return {}


def save_geocode_cache(cache):
    try:
        with open(GEOCODE_CACHE_PATH, 'w', encoding='utf-8') as handle:
            json.dump(cache, handle, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _nominatim_provincie_from_hit(hit):
    """Provincienaam uit Nominatim address (NL: vaak 'state')."""
    addr = hit.get('address') or {}
    for key in ('state', 'province'):
        v = addr.get(key)
        if isinstance(v, str) and v.strip():
            return normalize_nl_provincienaam(v.strip())
    return ''


def _nominatim_hit_haystack(hit) -> str:
    """Tekst om forward-geocode-resultaat te vergelijken met KNDB-plaats."""
    parts = []
    dn = hit.get('display_name')
    if isinstance(dn, str) and dn.strip():
        parts.append(dn.lower())
    addr = hit.get('address') or {}
    for key in (
        'village', 'town', 'city', 'municipality', 'city_district',
        'hamlet', 'suburb', 'locality', 'neighbourhood', 'county', 'state',
    ):
        v = addr.get(key)
        if isinstance(v, str) and v.strip():
            parts.append(v.lower())
    return ' | '.join(parts)


def _plaats_significant_tokens(plaats: str):
    stop = {'van', 'de', 'het', 'op', 'ten', 'aan', 'den', "'s", 's'}
    return [
        w for w in re.split(r'[\s\-]+', (plaats or '').strip().lower())
        if w and w not in stop and len(w) >= 2
    ]


def _nominatim_hit_matches_plaats(hit, plaats: str) -> bool:
    if not plaats or not (plaats := plaats.strip()):
        return True
    return _plaats_matches_reverse_haystack(plaats, _nominatim_hit_haystack(hit))


def geocode_query(query, skip_cache=False, plaats_expected=None):
    cache = load_geocode_cache()
    cache_key = query if not plaats_expected else f'{query}\0{plaats_expected.strip().lower()}'
    if not skip_cache and cache_key in cache:
        cached = cache[cache_key]
        if isinstance(cached, dict) and cached.get('provincie'):
            pn = normalize_nl_provincienaam(cached['provincie'])
            if pn != cached['provincie']:
                return {**cached, 'provincie': pn}
        return cached

    try:
        response = requests.get(
            'https://nominatim.openstreetmap.org/search',
            params={
                'q': query,
                'format': 'json',
                'limit': 8,
                'addressdetails': 1,
                'countrycodes': 'nl',
            },
            headers={'User-Agent': USER_AGENT},
            timeout=30,
        )
        response.raise_for_status()
        results = response.json()
        geometry = None
        chosen = None
        pe = (plaats_expected or '').strip() or None
        for hit in results:
            lat = float(hit['lat'])
            lon = float(hit['lon'])
            if not is_valid_nl_coords(lat, lon):
                continue
            if not _nominatim_hit_is_netherlands(hit):
                continue
            if pe and not _nominatim_hit_matches_plaats(hit, pe):
                continue
            chosen = hit
            break
        if chosen is not None:
            geometry = {'lat': float(chosen['lat']), 'lon': float(chosen['lon'])}
            prov = _nominatim_provincie_from_hit(chosen)
            if prov:
                geometry['provincie'] = prov
    except Exception:
        geometry = None

    cache[cache_key] = geometry
    save_geocode_cache(cache)
    time.sleep(1)
    return geometry


def _reverse_geocode_payload(lat, lon):
    """Volledige Nominatim reverse JSON of None; respecteert 1 s pauze."""
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return None
    try:
        response = requests.get(
            'https://nominatim.openstreetmap.org/reverse',
            params={'lat': lat, 'lon': lon, 'format': 'json', 'addressdetails': 1},
            headers={'User-Agent': USER_AGENT},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()
    except Exception:
        return None
    finally:
        time.sleep(1)


def reverse_geocode_country(lat, lon):
    """OSM country_code voor een punt (None bij fout of ontbrekend)."""
    data = _reverse_geocode_payload(lat, lon)
    if not data:
        return None
    cc = (data.get('address') or {}).get('country_code') or ''
    return cc.lower() if cc else None


def _reverse_payload_haystack_for_plaats(data):
    """Lage tekst om KNDB-plaats tegen te houden (village/town + display_name)."""
    parts = []
    dn = data.get('display_name')
    if isinstance(dn, str) and dn.strip():
        parts.append(dn.lower())
    addr = data.get('address') or {}
    for key in (
        'village', 'town', 'city', 'municipality', 'city_district',
        'hamlet', 'suburb', 'locality', 'neighbourhood', 'county',
    ):
        v = addr.get(key)
        if isinstance(v, str) and v.strip():
            parts.append(v.lower())
    return ' | '.join(parts)


def _plaats_matches_reverse_haystack(plaats, haystack):
    """Ruwe check: foutieve geocode (Oosterend vs Easterein, Bergen vs Bergen op Zoom) valt door."""
    if not plaats or not haystack:
        return True
    p = plaats.strip().lower()
    if p in haystack:
        return True
    tokens = _plaats_significant_tokens(p)
    if len(tokens) >= 2:
        return all(t in haystack for t in tokens)
    if len(p) >= 5 and p in haystack:
        return True
    if p == 'ijmuiden' and 'velsen' in haystack:
        return True
    if p in {'den haag', "'s-gravenhage", 's-gravenhage'}:
        return ("'s-gravenhage" in haystack or 's-gravenhage' in haystack or 'den haag' in haystack)
    return False


# Extra zoekterm voor Nominatim (plaatsnaam lowercase) — onder andere wijk/gemeente.
_PLAATS_GEOCODE_EXTRA = {
    'ijmuiden': 'Velsen',
    'velsen-noord': 'Velsen',
    'velsen-zuid': 'Velsen',
}

# Vestigingsplaats → echte provincie (KNDB-bondcode is géén provincie voor geocode).
_PLAATS_GEOCODE_PROVINCE_OVERRIDE = {
    'bergen op zoom': 'Noord-Brabant',
    'ossendrecht': 'Noord-Brabant',
    'halsteren': 'Noord-Brabant',
    'woensdrecht': 'Noord-Brabant',
    'hulst': 'Zeeland',
    'terneuzen': 'Zeeland',
    'goes': 'Zeeland',
    'middelburg': 'Zeeland',
    'vlissingen': 'Zeeland',
}


def _append_provincie_en_plaats_hints(parts, club):
    """Gemeente-disambiguatie + echte provincie; géén bondcode als provincie."""
    joined = ', '.join(parts).lower()
    plaats = (club.get('plaats') or '').strip().lower()
    extra = _PLAATS_GEOCODE_EXTRA.get(plaats)
    if extra and extra.lower() not in joined:
        parts.append(extra)
    prov = _PLAATS_GEOCODE_PROVINCE_OVERRIDE.get(plaats)
    if prov and prov.lower().replace('-', ' ') not in joined.replace('-', ' '):
        parts.append(prov)


def _club_plaats_centroid_fallback_address(club):
    """Laatste redmiddel: centrum van de KNDB-plaats binnen de juiste provincie (Nominatim)."""
    plaats = (club.get('plaats') or '').strip()
    if not plaats:
        return ''
    parts = [plaats]
    _append_provincie_en_plaats_hints(parts, club)
    parts.append('Nederland')
    return ', '.join(parts)


def club_coordinates_outside_netherlands(club):
    """True als er coördinaten zijn die niet in NL liggen (bbox of landcode)."""
    lat, lon = club.get('lat'), club.get('lon')
    if lat is None or lon is None:
        return False
    if not is_valid_nl_coords(lat, lon):
        return True
    cc = reverse_geocode_country(lat, lon)
    if cc is None:
        return False
    return cc != 'nl'


def club_geocode_plaats_mismatch(club, reverse_data):
    """
    True als club.plaats (lange naam) nergens in het OSM-reverse-adres voorkomt.
    Vangt bv. Oosterend (Texel) die per abuis op Easterein staat — nog steeds NL.
    """
    lat, lon = club.get('lat'), club.get('lon')
    plaats = (club.get('plaats') or '').strip()
    if lat is None or lon is None or not reverse_data or not plaats:
        return False
    if not is_valid_nl_coords(lat, lon):
        return False
    hay = _reverse_payload_haystack_for_plaats(reverse_data)
    return not _plaats_matches_reverse_haystack(plaats, hay)


def get_secretary_address(club):
    details = club.get('details', {}) or {}
    for contact in details.get('contactpersonen', []):
        functie = contact.get('functie', '').lower()
        if 'secretaris' in functie or 'secretariaat' in functie:
            adres = contact.get('adres') or ''
            if adres.strip():
                return adres
    return ''


def _maybe_append_oosterend_island(parts, club):
    """
    Nominatim geeft bij zoekterm alleen 'Oosterend' vaak Easterein (Friesland) als eerste hit.
    Oosterend bestaat op Texel (NH) en op Terschelling (FR); disambigueren op bond.
    """
    combined = ', '.join(parts).lower()
    plaats = (club.get('plaats') or '').strip().lower()
    if plaats != 'oosterend':
        return
    if 'texel' in combined or 'tersch' in combined:
        return
    prov = club.get('provincie') or ''
    if prov == 'PNHD':
        parts.append('Texel')
    elif prov == 'PFDB':
        parts.append('Terschelling')


def _geocode_address_from_location(loc, club):
    parts = []
    base = (loc.get('adres') or loc.get('naam') or '').strip()
    if base:
        parts.append(base)
    pc = (loc.get('postcode') or '').strip()
    if pc:
        parts.append(pc)
    for extra in (loc.get('woonplaats') or '', club.get('plaats') or ''):
        extra = extra.strip()
        if not extra:
            continue
        hay = ', '.join(parts).lower()
        if extra.lower() not in hay:
            parts.append(extra)
    if not parts:
        return ''
    _append_provincie_en_plaats_hints(parts, club)
    _maybe_append_oosterend_island(parts, club)
    parts.append('Nederland')
    return ', '.join(parts)


def _geocode_address_clublokaal_fallback(club):
    clublokaal = (club.get('clublokaal') or '').strip()
    plaats = (club.get('plaats') or '').strip()
    if not clublokaal:
        return ''
    parts = [clublokaal]
    if plaats:
        parts.append(plaats)
    _append_provincie_en_plaats_hints(parts, club)
    _maybe_append_oosterend_island(parts, club)
    parts.append('Nederland')
    return ', '.join(parts)


def _discard_geocode_if_plaats_mismatch(club) -> None:
    """Wis lat/lon als reverse-OSM de KNDB-plaats niet ondersteunt (bv. Leiden i.p.v. IJmuiden)."""
    plaats = (club.get('plaats') or '').strip()
    if not plaats or club.get('lat') is None or club.get('lon') is None:
        return
    data = _reverse_geocode_payload(club['lat'], club['lon'])
    if not data:
        return
    if club_geocode_plaats_mismatch(club, data):
        club.pop('lat', None)
        club.pop('lon', None)


def geocode_club(club, skip_geo_cache=False):
    if club.get('lat') and club.get('lon') and is_valid_nl_coords(club['lat'], club['lon']):
        return club
    if club.get('lat') and club.get('lon'):
        club.pop('lat', None)
        club.pop('lon', None)

    address = ''
    if club.get('details', {}).get('locaties'):
        first_location = club['details']['locaties'][0]
        address = _geocode_address_from_location(first_location, club)

    if not address and club.get('clublokaal'):
        address = _geocode_address_clublokaal_fallback(club)

    if not address:
        address = get_secretary_address(club)

    if not address.strip():
        address = _club_plaats_centroid_fallback_address(club) or ''

    if not address.strip():
        return club

    plaats_hint = (club.get('plaats') or '').strip() or None

    geometry = geocode_query(address, skip_cache=skip_geo_cache, plaats_expected=plaats_hint)
    if geometry:
        club['lat'] = geometry['lat']
        club['lon'] = geometry['lon']
        _discard_geocode_if_plaats_mismatch(club)
    else:
        secretary_address = get_secretary_address(club)
        if secretary_address and secretary_address != address:
            geometry = geocode_query(
                secretary_address, skip_cache=skip_geo_cache, plaats_expected=plaats_hint,
            )
            if geometry:
                club['lat'] = geometry['lat']
                club['lon'] = geometry['lon']
                _discard_geocode_if_plaats_mismatch(club)

    if club.get('lat') is None or club.get('lon') is None:
        fb = _club_plaats_centroid_fallback_address(club)
        if fb and fb != address:
            geometry = geocode_query(fb, skip_cache=skip_geo_cache, plaats_expected=plaats_hint)
            if geometry:
                club['lat'] = geometry['lat']
                club['lon'] = geometry['lon']
                _discard_geocode_if_plaats_mismatch(club)

    return club


def upsert_club(club):
    filter_query = {
        'naam': club['naam'],
        'plaats': club['plaats'],
        'provincie': club['provincie'],
    }
    club['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ')
    payload = {k: v for k, v in club.items() if k != '_id'}
    collection.update_one(filter_query, {'$set': payload}, upsert=True)


def find_logo_on_website(website):
    if not website:
        return ''
    try:
        response = requests.get(website, headers={'User-Agent': USER_AGENT}, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        logo = soup.find('img', {'alt': re.compile(r'logo', re.I)}) or soup.find('img', src=re.compile(r'logo', re.I))
        if logo and logo.has_attr('src'):
            return absolute_url(logo['src'], website)
    except Exception:
        pass
    return ''


def ensure_mongo():
    """Fail fast with a clear message if MongoDB is not reachable."""
    try:
        client.admin.command('ping')
    except Exception as exc:
        print(f'MongoDB niet bereikbaar (controleer MONGO_URI en netwerk): {exc}')
        raise SystemExit(1) from exc
    print('MongoDB verbinding OK.')


def import_all():
    ensure_mongo()
    collection.create_index([('naam', 1), ('plaats', 1), ('provincie', 1)], unique=True)
    clubs = parse_kndb_clubs()
    clubs_by_province = {}

    for club in clubs:
        clubs_by_province.setdefault(club['provincie'], []).append(club)

    for province_code, province_clubs in clubs_by_province.items():
        club_names = {club['naam'] for club in province_clubs}
        bond_url = province_clubs[0].get('bond_url', '')
        if province_code == 'ZHDB' and bond_url:
            details = scrape_zhdb_details(bond_url)
        else:
            details = scrape_generic_province_site(bond_url, club_names) if bond_url else {}

        for club in province_clubs:
            club_details = None
            if club.get('club_url'):
                club_details = parse_kndb_club_page(club['club_url'])
            if not club_details or not club_details.get('website'):
                club_details = club_details or {}
                fallback = details.get(club['naam'], {'website': '', 'locaties': [], 'contactpersonen': [], 'emails': [], 'telefoons': []})
                for field in ['website', 'locaties', 'contactpersonen', 'emails', 'telefoons']:
                    club_details.setdefault(field, fallback.get(field, [] if field in {'locaties', 'contactpersonen', 'emails', 'telefoons'} else ''))
            club['details'] = club_details
            if club['details'].get('website'):
                club['logo'] = find_logo_on_website(club['details']['website'])
            else:
                club['logo'] = ''
            geocode_club(club)
            upsert_club(club)
            print(f"Imported {club['provincie']} - {club['naam']}")

    print('Import klaar. Gegevens staan in MongoDB (collectie clubs).')


def recheck_geocodes(also_missing=False, also_plaats_mismatch=False):
    """
    Herbereken geocoding voor clubs met foutieve / niet-NL punten (reverse check).
    Met also_plaats_mismatch: ook clubs waar het punt wél in NL ligt maar de KNDB-plaats
    niet in het OSM-adres voorkomt (bv. Oosterend → per ongeluk Easterein).
    Veel sneller dan volledige import: alleen Nominatim + Mongo update.
    """
    ensure_mongo()
    clubs = list(collection.find())
    to_process = []
    for club in clubs:
        lat, lon = club.get('lat'), club.get('lon')
        if also_missing and (lat is None or lon is None):
            if _club_has_geocode_address(club):
                to_process.append(club)
            continue
        if lat is None or lon is None:
            continue
        if not is_valid_nl_coords(lat, lon):
            to_process.append(club)
            continue
        if also_plaats_mismatch:
            data = _reverse_geocode_payload(lat, lon)
            if data is None:
                continue
            addr = data.get('address') or {}
            cc = (addr.get('country_code') or '').lower()
            if cc and cc != 'nl':
                to_process.append(club)
            elif club_geocode_plaats_mismatch(club, data):
                to_process.append(club)
        else:
            if club_coordinates_outside_netherlands(club):
                to_process.append(club)

    print(
        f'Te herzien: {len(to_process)} club(s) '
        f'(also_missing={also_missing}, also_plaats_mismatch={also_plaats_mismatch}).'
    )
    for i, club in enumerate(to_process, 1):
        club.pop('lat', None)
        club.pop('lon', None)
        geocode_club(club, skip_geo_cache=True)
        upsert_club(club)
        print(f"[{i}/{len(to_process)}] {club.get('provincie')} - {club.get('naam')}: "
              f"lat={club.get('lat')}, lon={club.get('lon')}")
    print('Geocode-recheck klaar.')


def _club_has_geocode_address(club):
    if club.get('details', {}).get('locaties'):
        loc = club['details']['locaties'][0]
        if (loc.get('adres') or loc.get('naam') or '').strip():
            return True
    if club.get('clublokaal') and club.get('plaats'):
        return True
    return bool(get_secretary_address(club).strip())


def main():
    import sys
    if len(sys.argv) > 1 and sys.argv[1] in ('-h', '--help'):
        print('Gebruik:')
        print('  python3 scraper.py              Volledige import (KNDB + bonds + geocode)')
        print('  python3 scraper.py --fix-geo    Clubs met coördinaten buiten NL (bbox/reverse + opnieuw geocode)')
        print('  python3 scraper.py --fix-geo --also-missing   Ook clubs zonder lat/lon maar met adres vullen')
        print('  python3 scraper.py --fix-geo --also-plaats-mismatch   Ook als reverse-OSM geen KNDB-plaats toont')
        print('                              (langzamer: 1× reverse per club met punten in NL-bbox)')
        return
    if '--fix-geo' in sys.argv:
        also_missing = '--also-missing' in sys.argv
        also_plaats_mismatch = '--also-plaats-mismatch' in sys.argv
        recheck_geocodes(also_missing=also_missing, also_plaats_mismatch=also_plaats_mismatch)
        return
    import_all()


if __name__ == '__main__':
    main()
