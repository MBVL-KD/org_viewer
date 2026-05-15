"""Scholen-tab (Streamlit): zelfde UX-patroon als damclubs."""
import json
import time
from typing import Any, List, Optional

import pandas as pd
import pydeck as pdk
import streamlit as st
from bson import ObjectId

from bond_land import (
    club_bond_regio,
    club_matches_national_bond,
    enrich_club_bond_fields,
    national_bond_for_land,
    national_bond_label,
)
from de_bundesland import bundesland_filter_label
from nl_provincie import canonical_nl_provincie_club, normalize_nl_provincienaam
from scraper import is_valid_be_coords, is_valid_de_coords, is_valid_nl_coords

MAP_VIEWPORT = {
    'NL': {'center_lat': 52.2, 'center_lon': 5.3, 'zoom': 7.0, 'label': 'Nederland'},
    'DE': {'center_lat': 51.1, 'center_lon': 10.4, 'zoom': 5.8, 'label': 'Duitsland'},
    'BE': {'center_lat': 50.85, 'center_lon': 4.5, 'zoom': 7.5, 'label': 'België (Vlaanderen)'},
}

# (land_code, weergavenaam)
SCHOOL_LAND_CHOICES = (
    ('NL', 'Nederland'),
    ('DE', 'Duitsland'),
    ('BE', 'België (Vlaanderen)'),
)

LIST_VISIBLE_ROWS = 10
TABLE_ROW_PX = 36
TABLE_HEADER_PX = 52
SCHOOL_TABLE_HEIGHT = LIST_VISIBLE_ROWS * TABLE_ROW_PX + TABLE_HEADER_PX


def _postcode_first_four_digits(pc) -> Optional[int]:
    """Eerste vier cijfers van NL-postcode voor bereikfilter (None als onbruikbaar)."""
    if pc is None or (isinstance(pc, float) and pd.isna(pc)):
        return None
    s = ''.join(str(pc).split()).upper()
    digits = []
    for ch in s:
        if ch.isdigit():
            digits.append(ch)
            if len(digits) == 4:
                return int(''.join(digits))
        elif digits:
            break
    return None


def _parse_postcode_range_input(text: str) -> Optional[int]:
    """Gebruikersinvoer '1234' of '1234AB' → 1234; leeg → None; ongeldig → None."""
    if not text or not str(text).strip():
        return None
    return _postcode_first_four_digits(str(text).strip())


def _postcode_plz_five(pc) -> Optional[int]:
    """Duitse PLZ: eerste 5 cijfers."""
    if pc is None or (isinstance(pc, float) and pd.isna(pc)):
        return None
    digits = ''.join(c for c in str(pc) if c.isdigit())
    if len(digits) >= 5:
        return int(digits[:5])
    return None


def _parse_postcode_range_input_de(text: str) -> Optional[int]:
    if not text or not str(text).strip():
        return None
    return _postcode_plz_five(str(text).strip())


def _coords_ok_for_land(lat, lon, land_code: str) -> bool:
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if land_code == 'DE':
        return is_valid_de_coords(lat_f, lon_f)
    if land_code == 'BE':
        return is_valid_be_coords(lat_f, lon_f)
    return is_valid_nl_coords(lat_f, lon_f)


def _prepare_map_coords_df(
    df: pd.DataFrame,
    lat_col: str,
    lon_col: str,
    land_code: str,
) -> tuple[pd.DataFrame, int]:
    """Numerieke coördinaten; filter op land, niet op een vast kaartvenster."""
    if df.empty:
        return df.copy(), 0
    out = df.copy()
    out[lat_col] = pd.to_numeric(out[lat_col], errors='coerce')
    out[lon_col] = pd.to_numeric(out[lon_col], errors='coerce')
    out = out.dropna(subset=[lat_col, lon_col])
    if out.empty:
        return out, 0
    mask = out.apply(
        lambda r: _coords_ok_for_land(r[lat_col], r[lon_col], land_code),
        axis=1,
    )
    invalid = int((~mask).sum())
    return out.loc[mask].copy(), invalid


def _count_schools_with_coords(df: pd.DataFrame) -> int:
    if df.empty or 'lat' not in df.columns or 'lon' not in df.columns:
        return 0
    lat = pd.to_numeric(df['lat'], errors='coerce')
    lon = pd.to_numeric(df['lon'], errors='coerce')
    return int((lat.notna() & lon.notna()).sum())


def _map_empty_messages(
    land_code: str,
    land_choice: str,
    filtered_reset: pd.DataFrame,
    n_with_coords: int,
    valid_schools: pd.DataFrame,
    invalid_schools: int,
    schools_coll,
    show_clubs_on_map: bool,
    clubs_raw: pd.DataFrame,
) -> str:
    parts = [f'Geen punten om op de kaart te tonen voor {land_choice}.']
    if len(filtered_reset) and n_with_coords == 0:
        n = len(filtered_reset)
        parts.append(
            f'{n} school(s) in de selectie zonder bruikbare lat/lon. '
            'Draai geocode of vul coördinaten handmatig in.'
        )
    elif n_with_coords and valid_schools.empty and invalid_schools:
        parts.append(
            f'{invalid_schools} school(s) hebben coördinaten die niet bij dit land passen.'
        )
    if land_code != 'BE':
        be_n = schools_coll.count_documents({
            'land': 'BE', 'lat': {'$ne': None}, 'lon': {'$ne': None},
        })
        if be_n:
            parts.append(
                f'Tip: kies **België (Vlaanderen)** bij Land — er staan {be_n} BE-scholen met coördinaten.'
            )
    if land_code == 'NL':
        other = schools_coll.count_documents({
            'land': 'DE', 'lat': {'$ne': None}, 'lon': {'$ne': None},
        })
        if other:
            parts.append(
                f'Tip: kies **Duitsland** bij Land — er staan {other} Duitse scholen met coördinaten.'
            )
    elif land_code == 'DE':
        other = schools_coll.count_documents({
            '$or': [{'land': 'NL'}, {'land': {'$exists': False}}],
            'lat': {'$ne': None},
            'lon': {'$ne': None},
        })
        if other:
            parts.append(
                f'Tip: kies **Nederland** bij Land — er staan {other} NL-scholen met coördinaten.'
            )
    elif land_code == 'BE':
        other = schools_coll.count_documents({
            '$or': [{'land': 'NL'}, {'land': {'$exists': False}}],
            'lat': {'$ne': None},
            'lon': {'$ne': None},
        })
        if other:
            parts.append(
                f'Tip: kies **Nederland** bij Land — er staan {other} NL-scholen met coördinaten.'
            )
    if show_clubs_on_map and clubs_raw.empty and len(filtered_reset) > 0:
        parts.append('Geen damclubs voldoen aan de criteria, of clubs missen coördinaten.')
    return ' '.join(parts)


def _mongo_school_land_query(land_code: str) -> dict:
    if land_code == 'DE':
        return {'land': 'DE'}
    if land_code == 'BE':
        return {'$or': [{'land': 'BE'}, {'gemeenschap': 'VL'}]}
    return {'$or': [{'land': 'NL'}, {'land': {'$exists': False}}, {'land': None}, {'land': ''}]}


def _school_land_counts(schools_coll) -> dict:
    return {
        code: schools_coll.count_documents(_mongo_school_land_query(code))
        for code, _ in SCHOOL_LAND_CHOICES
    }


def _school_land_selectbox(schools_coll, *, container, key: str = 'school_filter_land_v2') -> str:
    """Landkeuze NL / DE / BE — altijd alle drie tonen met aantallen."""
    counts = _school_land_counts(schools_coll)
    codes = [code for code, _ in SCHOOL_LAND_CHOICES]
    labels = {code: f'{name} ({counts[code]})' for code, name in SCHOOL_LAND_CHOICES}
    return container.selectbox(
        'Land',
        options=codes,
        format_func=lambda c: labels[c],
        key=key,
        help='Kies het land van de schooldata.',
    )


def _provincie_filter_label(value: str, land_code: str) -> str:
    if land_code == 'DE':
        return bundesland_filter_label(value)
    return str(value)


def _serialize_mongo_doc(doc):
    return json.loads(json.dumps(doc, default=str))


def _dataframe_selection_rows(df_event):
    if df_event is None:
        return []
    sel = getattr(df_event, 'selection', None)
    if sel is None and isinstance(df_event, dict):
        sel = df_event.get('selection')
    if sel is None:
        return []
    rows = getattr(sel, 'rows', None)
    if rows is not None:
        return list(rows)
    if isinstance(sel, dict):
        return list(sel.get('rows', []))
    return []


def _pydeck_first_picked_object(selection):
    if selection is None:
        return None
    inner = None
    if hasattr(selection, 'selection'):
        inner = selection.selection
    elif isinstance(selection, dict) and 'selection' in selection:
        inner = selection['selection']
    if inner is None and isinstance(selection, dict):
        legacy_sel = selection.get('selected', selection.get('selection', selection))
        if isinstance(legacy_sel, list) and legacy_sel:
            return legacy_sel[0]
        if isinstance(legacy_sel, dict) and legacy_sel.get('school_id') is not None:
            return legacy_sel
        return None
    objs = getattr(inner, 'objects', None)
    if objs is None and isinstance(inner, dict):
        objs = inner.get('objects') or {}
    if not objs:
        return None
    for _layer_id, rows in objs.items():
        if rows:
            return rows[0]
    return None


def _club_doc_to_map_popup(doc: dict) -> dict:
    """Compacte dict voor popup na klik op damclub op de scholenkaart."""
    det = doc.get('details') if isinstance(doc.get('details'), dict) else {}
    eml = det.get('emails') or []
    first_email = ''
    for e in eml:
        if e and str(e).strip():
            first_email = str(e).strip()
            break
    bond = str(doc.get('provincie') or '').strip()
    prov_label = canonical_nl_provincie_club(doc.get('provincie'))
    return {
        'naam': doc.get('naam') or '',
        'plaats': doc.get('plaats') or '',
        'secretariaat': str(doc.get('secretariaat') or '').strip(),
        'bond': bond,
        'provincie_label': prov_label,
        'website': str(det.get('website') or '').strip(),
        'bond_url': str(doc.get('bond_url') or '').strip(),
        'club_url': str(doc.get('club_url') or '').strip(),
        'email': first_email,
    }


def _filter_schools_df(
    df: pd.DataFrame,
    prov_sel: List[str],
    gem_sel: List[str],
    soort_sel: List[str],
    status_sel: List[str],
    pc_lo: Optional[int],
    pc_hi: Optional[int],
    search: str,
    *,
    skip_gemeente: bool = False,
    postcode_col: str = 'postcode4',
    soort_col: str = 'soort',
) -> pd.DataFrame:
    """Pas schoolfilters toe; skip_gemeente=True voor gemeenten-markers op de kaart."""
    out = df.copy()
    if prov_sel:
        out = out[out['provincie'].isin(prov_sel)]
    if gem_sel and not skip_gemeente:
        out = out[out['gemeente'].isin(gem_sel)]
    if soort_sel and soort_col in out.columns:
        out = out[out[soort_col].isin(soort_sel)]
    if status_sel:
        out = out[out['status'].isin(status_sel)]
    if pc_lo is not None or pc_hi is not None:
        m_pc = out[postcode_col].notna()
        if pc_lo is not None:
            m_pc &= out[postcode_col] >= pc_lo
        if pc_hi is not None:
            m_pc &= out[postcode_col] <= pc_hi
        out = out[m_pc]
    if search:
        out = out[out['text_search'].str.contains(search, case=False, na=False)]
    return out


def _apply_email_filter(df: pd.DataFrame, email_choice: str) -> pd.DataFrame:
    if email_choice == 'Met e-mail':
        return df[df['has_email']].copy()
    if email_choice == 'Zonder e-mail':
        return df[~df['has_email']].copy()
    return df


def _gemeenten_map_markers(
    df: pd.DataFrame,
    prov_sel: List[str],
    gem_sel: List[str],
    soort_sel: List[str],
    status_sel: List[str],
    pc_lo: Optional[int],
    pc_hi: Optional[int],
    search: str,
    *,
    postcode_col: str = 'postcode4',
    place_label: str = 'Gemeente',
    soort_col: str = 'soort',
) -> pd.DataFrame:
    """Klikbare plaatscentra (uit scholen met coördinaten), rekening houdend met alle filters behalve gemeente."""
    base = _filter_schools_df(
        df, prov_sel, [], soort_sel, status_sel, pc_lo, pc_hi, search,
        skip_gemeente=True, postcode_col=postcode_col, soort_col=soort_col,
    )
    sub = base[base['gemeente'].astype(str).str.strip().astype(bool)].copy()
    sub = sub.dropna(subset=['lat', 'lon'])
    if sub.empty:
        return pd.DataFrame()
    rows = []
    sub['_gem_key'] = sub['gemeente'].astype(str).str.strip()
    for gem, grp in sub.groupby('_gem_key', sort=True):
        if not gem:
            continue
        lat = pd.to_numeric(grp['lat'], errors='coerce').dropna()
        lon = pd.to_numeric(grp['lon'], errors='coerce').dropna()
        if lat.empty or lon.empty:
            continue
        n = len(grp)
        prov_s = grp['provincie'].dropna()
        prov = str(prov_s.iloc[0]) if len(prov_s) else ''
        rows.append({
            'gemeentenaam': gem,
            'naam': gem,
            'plaats': prov,
            'website': '',
            'latitude': float(lat.median()),
            'longitude': float(lon.median()),
            'school_count': n,
            'map_tip_line': f'{place_label} · {n} school(s) in huidige filters',
            'gemeente_selected': gem in gem_sel,
        })
    return pd.DataFrame(rows)


def _map_pick_fingerprint(picked: dict) -> str:
    """Unieke sleutel voor Pydeck-selectie (voorkomt herhaalde verwerking na checkbox/rerun)."""
    parts = []
    for key in ('gemeentenaam', 'club_id', 'school_id', 'administratienummer'):
        val = picked.get(key)
        if val is not None and str(val).strip():
            parts.append(f'{key}={val}')
    naam = picked.get('naam')
    if naam and not parts:
        parts.append(f'naam={naam}')
    return '|'.join(parts)


def _apply_school_map_pick(selection, filtered_reset_df, clubs_coll, gem_key: str, pending_gem_key: str):
    """Verwerk klik op scholenkaart: gemeente → filter, damclub → popup, school → selectie."""
    picked = _pydeck_first_picked_object(selection)
    if not picked or not isinstance(picked, dict):
        return
    fp = _map_pick_fingerprint(picked)
    if fp and fp == st.session_state.get('_school_map_pick_fp'):
        return
    if fp:
        st.session_state['_school_map_pick_fp'] = fp
    gn = picked.get('gemeentenaam')
    if gn and str(gn).strip():
        gn = str(gn).strip()
        current = list(st.session_state.get(gem_key, []))
        # Pydeck houdt de klik-selectie vast na rerun: niet opnieuw rerunnen als al in filter.
        if gn not in current:
            current.append(gn)
            st.session_state[pending_gem_key] = current
            st.session_state.pop('school_map_club_popup', None)
            st.rerun()
        return
    cid = picked.get('club_id')
    if cid:
        try:
            oid = ObjectId(str(cid))
        except Exception:
            return
        doc = clubs_coll.find_one({'_id': oid})
        if doc:
            st.session_state['school_map_club_popup'] = _club_doc_to_map_popup(doc)
        else:
            st.session_state.pop('school_map_club_popup', None)
        return
    st.session_state.pop('school_map_club_popup', None)
    sid = picked.get('school_id')
    if sid:
        try:
            oid = ObjectId(str(sid))
            if oid in set(filtered_reset_df['_id'].tolist()):
                st.session_state.selected_school_id = oid
        except Exception:
            return
        return
    naam = picked.get('naam') or picked.get('name')
    admin = picked.get('administratienummer')
    if admin:
        match = filtered_reset_df[filtered_reset_df['administratienummer'].astype(str) == str(admin)]
        if len(match) == 1:
            st.session_state.selected_school_id = match.iloc[0]['_id']


@st.dialog('Damclub')
def _school_map_club_popup_dialog():
    pop = st.session_state.get('school_map_club_popup')
    if not pop:
        return
    st.markdown(f"### {pop.get('naam') or '—'}")
    st.caption(f"{pop.get('plaats') or '—'} · {pop.get('provincie_label') or '—'}")
    if pop.get('bond_land_label'):
        st.write('**Landelijke bond:**', pop['bond_land_label'])
    if pop.get('bond') and pop.get('bond') != pop.get('provincie_label'):
        bond_lbl = 'Regio' if pop.get('bond_land_label', '').startswith('KBDB') else 'Provinciale bond'
        st.write(f'**{bond_lbl}:**', pop['bond'])
    if pop.get('secretariaat'):
        st.write('**Secretariaat:**', pop['secretariaat'])
    if pop.get('website'):
        st.write('**Website:**', pop['website'])
    if pop.get('email'):
        st.write('**E-mail:**', pop['email'])
    if pop.get('bond_url'):
        st.write('**Bond:**', pop['bond_url'])
    if pop.get('club_url'):
        st.write('**KNDB:**', pop['club_url'])
    if st.button('Sluiten', use_container_width=True, key='school_map_club_popup_close'):
        st.session_state.pop('school_map_club_popup', None)
        st.rerun()


def _clubs_overlay_for_school_filters(
    db: Any,
    prov_sel: List[str],
    gem_sel: List[str],
    pc_lo: Optional[int],
    pc_hi: Optional[int],
    search: str,
    filtered_schools: pd.DataFrame,
    *,
    bond_land: str = 'KNDB',
) -> pd.DataFrame:
    """Damclubs met lat/lon die bij de schoolfilters horen (provincie; plaats bij ruimtelijke/zoekfilter)."""
    narrow_plaats = bool(gem_sel) or (pc_lo is not None) or (pc_hi is not None) or bool((search or '').strip())
    allowed_plaatsen: Optional[set] = None
    if narrow_plaats:
        if filtered_schools.empty:
            return pd.DataFrame()
        allowed_plaatsen = {
            str(p).strip()
            for p in filtered_schools['plaats'].dropna().unique()
            if str(p).strip()
        }
        for g in filtered_schools['gemeente'].dropna().unique():
            s = str(g).strip()
            if s:
                allowed_plaatsen.add(s)
        if not allowed_plaatsen:
            return pd.DataFrame()
        allowed_plaatsen_lc = {p.lower() for p in allowed_plaatsen}
    else:
        allowed_plaatsen_lc = None

    prov_wanted: Optional[set] = None
    if prov_sel:
        prov_wanted = {normalize_nl_provincienaam(str(p)) for p in prov_sel if str(p).strip()}

    out_rows = []
    for c in db['clubs'].find():
        if bond_land and not club_matches_national_bond(c, bond_land):
            continue
        lat, lon = c.get('lat'), c.get('lon')
        if lat is None or lon is None:
            continue
        try:
            latf = float(lat)
            lonf = float(lon)
        except (TypeError, ValueError):
            continue
        prov = str(c.get('provincie') or '').strip()
        plaats = str(c.get('plaats') or '').strip()
        club_prov = canonical_nl_provincie_club(c.get('provincie'))
        if prov_wanted is not None and club_prov not in prov_wanted:
            continue
        if allowed_plaatsen is not None and plaats.lower() not in allowed_plaatsen_lc:
            continue
        det = c.get('details') if isinstance(c.get('details'), dict) else {}
        website = str(det.get('website') or '').strip()
        out_rows.append({
            'naam': c.get('naam') or '',
            'plaats': plaats,
            'provincie': prov,
            'website': website,
            'latitude': latf,
            'longitude': lonf,
            'club_id': str(c['_id']),
        })
    return pd.DataFrame(out_rows)


def render_schools(db, map_height: int):
    schools_coll = db['schools']
    sb = st.sidebar

    land_code = _school_land_selectbox(schools_coll, container=st)
    land_choice = MAP_VIEWPORT[land_code]['label']
    vp = MAP_VIEWPORT[land_code]

    sb.header('Filters (scholen)')
    sb.caption(f'Land: **{land_choice}**')

    email_sel = sb.radio(
        'E-mail',
        ['Alle', 'Met e-mail', 'Zonder e-mail'],
        horizontal=True,
        key=f'school_filter_email_{land_code}',
        help='Filter op aanwezigheid van een e-mailadres in de database.',
    )

    schools = list(schools_coll.find(_mongo_school_land_query(land_code)))
    if not schools:
        if land_code == 'DE':
            st.info(
                'Nog geen Duitse scholen in de database. Testimport:\n\n'
                '`python3 import_schools_de.py --limit 100 --geocode`'
            )
        elif land_code == 'BE':
            st.info(
                'Nog geen Belgische (Vlaamse) scholen in de database. Testimport:\n\n'
                '`python3 import_schools_be.py --limit 100 --niveau basis --geocode`'
            )
        else:
            st.info(
                'Nog geen scholen in de database. Importeer CSV’s met:\n\n'
                '`python3 import_schools.py pad/naar/ho-Scholen.csv …`\n\n'
                'Optioneel: `--geocode` voor coördinaten (duurt langer).'
            )
        return

    export_json = json.dumps([_serialize_mongo_doc(s) for s in schools], ensure_ascii=False, indent=2).encode('utf-8')
    df_exp = pd.json_normalize([_serialize_mongo_doc(s) for s in schools], sep='_')
    for col in df_exp.columns:
        if df_exp[col].dtype == object:
            df_exp[col] = df_exp[col].apply(
                lambda x: json.dumps(x, ensure_ascii=False) if isinstance(x, (dict, list)) else x
            )
    export_csv = df_exp.to_csv(index=False).encode('utf-8-sig')

    rows = []
    for s in schools:
        em = s.get('email') or ''
        em_ok = bool(str(em).strip())
        rows.append({
            '_id': s['_id'],
            'has_email': em_ok,
            'administratienummer': str(s.get('administratienummer', '') or ''),
            'naam': s.get('naam', ''),
            'plaats': s.get('plaats', ''),
            'gemeente': s.get('gemeente', ''),
            'postcode_vestiging': s.get('postcode_vestiging', '') or '',
            'provincie': s.get('provincie', ''),
            'soort': s.get('soort', ''),
            'soort_norm': s.get('soort_norm', '') or s.get('soort', ''),
            'status': s.get('status', ''),
            'website': s.get('website', ''),
            'telefoon': s.get('telefoon', ''),
            'email': em,
            'lat': s.get('lat'),
            'lon': s.get('lon'),
            'bron_bestand': s.get('bron_bestand', ''),
            'imported_at': s.get('imported_at', ''),
            'text_search': ' '.join(
                str(x) for x in [
                    s.get('naam'), s.get('plaats'), s.get('gemeente'), s.get('provincie'),
                    s.get('postcode_vestiging'),
                    s.get('soort'), s.get('website'), em, s.get('telefoon'),
                    s.get('administratienummer'),
                ] if x
            ),
            'raw': s,
        })

    df = pd.DataFrame(rows)
    if land_code == 'DE':
        df['postcode_filter'] = df['postcode_vestiging'].apply(_postcode_plz_five)
        postcode_col = 'postcode_filter'
    else:
        df['postcode4'] = df['postcode_vestiging'].apply(_postcode_first_four_digits)
        postcode_col = 'postcode4'

    _ms_help = 'Geen selectie = alles tonen. Meerdere waarden: elk van die waarden (OR binnen dit veld).'
    if land_code == 'DE':
        prov_label = 'Bundesland'
    elif land_code == 'BE':
        prov_label = 'Provincie / regio'
    else:
        prov_label = 'Provincie'
    prov_opts = sorted(df['provincie'].dropna().unique().tolist())
    prov_sel = sb.multiselect(
        prov_label,
        options=prov_opts,
        default=[],
        key=f'school_filter_prov_{land_code}',
        format_func=lambda v: _provincie_filter_label(v, land_code),
        help=_ms_help,
    )
    gem_key = f'school_filter_gem_{land_code}'
    pending_gem_key = f'_pending_school_filter_gem_{land_code}'
    pending_gem = st.session_state.pop(pending_gem_key, None)
    if pending_gem is not None:
        st.session_state[gem_key] = pending_gem
    if prov_sel:
        gem_pool = df[df['provincie'].isin(prov_sel)]
    else:
        gem_pool = df
    gem_vals = sorted(
        {str(v).strip() for v in gem_pool['gemeente'].dropna().unique() if str(v).strip()},
        key=lambda x: x.lower(),
    )
    if gem_key not in st.session_state:
        st.session_state[gem_key] = []
    _allowed_gem = set(gem_vals)
    _cur_gem = [g for g in st.session_state.get(gem_key, []) if g in _allowed_gem]
    if _cur_gem != st.session_state.get(gem_key, []):
        st.session_state[gem_key] = _cur_gem
    gem_label = 'Stadt / Gemeinde' if land_code == 'DE' else 'Gemeente'
    gem_sel = sb.multiselect(
        gem_label,
        options=gem_vals,
        key=gem_key,
        help=_ms_help + (
            f' Alleen uit gekozen {prov_label.lower()}(en).' if prov_sel else ''
        ),
    )
    if land_code in ('NL', 'BE'):
        soort_col = 'soort_norm'
        soort_label = 'Onderwijstype'
        soort_opts = [x for x in (
            'Basisonderwijs', 'VO', 'MBO', 'HBO', 'WO', 'Speciaal onderwijs', 'Overig',
        ) if x in set(df['soort_norm'].dropna().unique())]
    else:
        soort_col = 'soort'
        soort_label = 'Soort'
        soort_opts = sorted(df['soort'].dropna().unique().tolist())
    soort_sel = sb.multiselect(
        soort_label, options=soort_opts, default=[], key=f'school_filter_soort_{land_code}', help=_ms_help,
    )
    status_opts = sorted(df['status'].dropna().unique().tolist())
    status_sel = sb.multiselect(
        'Status', options=status_opts, default=[], key=f'school_filter_status_{land_code}', help=_ms_help,
    )
    if land_code == 'DE':
        sb.markdown('PLZ (5 cijfers)')
        pc_ph_lo, pc_ph_hi = '10115', '10999'
    else:
        sb.markdown('Postcode (eerste 4 cijfers)')
        pc_ph_lo, pc_ph_hi = '1000', '1099'
    pc_col1, pc_col2 = sb.columns(2)
    with pc_col1:
        pc_min_in = st.text_input('Van', placeholder=pc_ph_lo, key=f'school_pc_min_{land_code}')
    with pc_col2:
        pc_max_in = st.text_input('Tot', placeholder=pc_ph_hi, key=f'school_pc_max_{land_code}')
    sb.caption('Leeg laten = geen onder-/bovengrens. Ongeldige invoer wordt genegeerd.')
    sb.markdown('**Kaartlagen**')
    show_gemeenten_on_map = sb.checkbox(
        'Gemeenten op kaart' if land_code in ('NL', 'BE') else 'Städte op kaart',
        value=False,
        key=f'school_map_show_gemeenten_{land_code}',
        help=(
            'Groene punten: klik voegt plaats/gemeente toe aan het filter. '
            'Centra uit schoolcoördinaten (geen officiële grenzen).'
        ),
    )
    show_clubs_on_map = False
    if land_code == 'NL':
        show_clubs_on_map = sb.checkbox(
            'Damclubs op kaart',
            value=False,
            key='school_map_show_clubs',
            help='Oranje punten; zelfde provincie als schoolfilter. Klik voor korte clubinfo.',
        )
    if not show_clubs_on_map:
        st.session_state.pop('school_map_club_popup', None)

    search = st.text_input('Zoek school, plaats, admin.nr., e-mail …', key=f'school_search_{land_code}')

    if land_code == 'DE':
        pc_lo = _parse_postcode_range_input_de(pc_min_in)
        pc_hi = _parse_postcode_range_input_de(pc_max_in)
    else:
        pc_lo = _parse_postcode_range_input(pc_min_in)
        pc_hi = _parse_postcode_range_input(pc_max_in)
    if pc_lo is not None and pc_hi is not None and pc_lo > pc_hi:
        pc_lo, pc_hi = pc_hi, pc_lo

    filtered = _filter_schools_df(
        df, prov_sel, gem_sel, soort_sel, status_sel, pc_lo, pc_hi, search,
        postcode_col=postcode_col, soort_col=soort_col,
    )
    filtered = _apply_email_filter(filtered, email_sel)

    filtered_reset = filtered.reset_index(drop=True)

    if 'selected_school_id' not in st.session_state:
        st.session_state.selected_school_id = None
    if st.session_state.selected_school_id is not None:
        if st.session_state.selected_school_id not in set(filtered_reset['_id'].tolist()):
            st.session_state.selected_school_id = None

    st.subheader(f'Schooloverzicht — {land_choice}')
    st.write(f'Selectie: {len(filtered_reset)} · Totaal in database ({land_code}): {len(schools)}')

    d1, d2, _ = st.columns([1, 1, 4])
    with d1:
        st.download_button('Download alle scholen (JSON)', export_json, 'scholen_alle.json', 'application/json')
    with d2:
        st.download_button('Download alle scholen (CSV)', export_csv, 'scholen_alle.csv', 'text/csv')

    if land_code in ('NL', 'BE'):
        display_cols = [
            'administratienummer', 'provincie', 'gemeente', 'postcode_vestiging', 'plaats', 'naam',
            'soort_norm', 'status', 'website', 'lat', 'lon',
        ]
    else:
        display_cols = [
            'administratienummer', 'provincie', 'gemeente', 'postcode_vestiging', 'plaats', 'naam', 'soort', 'status',
            'website', 'lat', 'lon',
        ]
    table_df = filtered_reset[display_cols] if len(filtered_reset) else pd.DataFrame(columns=display_cols)

    pre = st.session_state.get('selected_school_id')
    sel_def = None
    if pre is not None and len(filtered_reset):
        hits = filtered_reset.index[filtered_reset['_id'] == pre].tolist()
        if hits:
            sel_def = {'selection': {'rows': [int(hits[0])]}}

    col_table, col_map = st.columns(2, gap='large')

    with col_table:
        ev = st.dataframe(
            table_df,
            height=map_height,
            use_container_width=True,
            hide_index=True,
            on_select='rerun',
            selection_mode='single-row',
            key='school_table',
            selection_default=sel_def,
        )
        st.caption(
            f'Scroll in de tabel voor meer rijen ({len(filtered_reset)} in selectie).'
        )

    sr = _dataframe_selection_rows(ev)
    if sr:
        i = sr[0]
        if 0 <= i < len(filtered_reset):
            st.session_state.selected_school_id = filtered_reset.iloc[i]['_id']

    sid_map = st.session_state.selected_school_id

    with col_map:
        map_df = filtered_reset[
            ['naam', 'plaats', 'website', 'lat', 'lon', '_id', 'administratienummer']
        ].copy()
        valid_schools = pd.DataFrame()
        invalid_schools = 0
        if not map_df.empty:
            map_df = map_df.rename(columns={'lat': 'latitude', 'lon': 'longitude'})
            map_df['school_id'] = map_df['_id'].astype(str)
            map_df['map_tip_line'] = map_df['administratienummer'].astype(str)
            valid_schools, invalid_schools = _prepare_map_coords_df(
                map_df, 'latitude', 'longitude', land_code,
            )

        clubs_raw = pd.DataFrame()
        if show_clubs_on_map:
            nl_bond = national_bond_for_land('NL') or 'KNDB'
            clubs_raw = _clubs_overlay_for_school_filters(
                db, prov_sel, gem_sel, pc_lo, pc_hi, search, filtered_reset,
                bond_land=nl_bond,
            )
        valid_clubs = pd.DataFrame()
        invalid_clubs = 0
        if show_clubs_on_map and not clubs_raw.empty:
            valid_clubs, invalid_clubs = _prepare_map_coords_df(
                clubs_raw, 'latitude', 'longitude', 'NL',
            )
            if not valid_clubs.empty:
                valid_clubs = valid_clubs.copy()
                valid_clubs['map_tip_line'] = 'Damclub'

        valid_gemeenten = pd.DataFrame()
        if show_gemeenten_on_map:
            place_lbl = 'Stadt' if land_code == 'DE' else 'Gemeente'
            gm = _gemeenten_map_markers(
                df, prov_sel, gem_sel, soort_sel, status_sel, pc_lo, pc_hi, search,
                postcode_col=postcode_col, place_label=place_lbl, soort_col=soort_col,
            )
            if not gm.empty:
                valid_gemeenten, _ = _prepare_map_coords_df(
                    gm, 'latitude', 'longitude', land_code,
                )

        if valid_schools.empty and valid_clubs.empty and valid_gemeenten.empty:
            st.info(_map_empty_messages(
                land_code,
                land_choice,
                filtered_reset,
                _count_schools_with_coords(filtered_reset),
                valid_schools,
                invalid_schools,
                schools_coll,
                show_clubs_on_map,
                clubs_raw,
            ))
        else:
            lats: List[float] = []
            lons: List[float] = []
            if not valid_schools.empty:
                lats.extend(valid_schools['latitude'].astype(float).tolist())
                lons.extend(valid_schools['longitude'].astype(float).tolist())
            if not valid_clubs.empty:
                lats.extend(valid_clubs['latitude'].astype(float).tolist())
                lons.extend(valid_clubs['longitude'].astype(float).tolist())
            if not valid_gemeenten.empty:
                lats.extend(valid_gemeenten['latitude'].astype(float).tolist())
                lons.extend(valid_gemeenten['longitude'].astype(float).tolist())

            zoom_close = 12.8
            center_lat = float(pd.Series(lats).median())
            center_lon = float(pd.Series(lons).median())
            zoom = float(vp['zoom'])
            lat_span = float(max(lats) - min(lats))
            lon_span = float(max(lons) - min(lons))
            if lat_span < 0.4 and lon_span < 0.4:
                zoom = 9.0
            elif lat_span < 0.8 and lon_span < 0.8:
                zoom = 8.5
            elif lat_span < 1.5 and lon_span < 1.5:
                zoom = 8.0
            elif lat_span < 2.5 and lon_span < 2.5:
                zoom = 7.5

            sel_m = (
                valid_schools[valid_schools['_id'] == sid_map]
                if sid_map is not None and not valid_schools.empty
                else pd.DataFrame()
            )
            if not sel_m.empty:
                center_lat = float(sel_m.iloc[0]['latitude'])
                center_lon = float(sel_m.iloc[0]['longitude'])
                zoom = zoom_close

            def _website_link_col(series):
                return series.apply(
                    lambda w: f'<br/><a href="{w}" target="_blank">Website</a>' if w else ''
                )

            if not valid_schools.empty:
                valid_schools = valid_schools.copy()
                valid_schools['website_link'] = _website_link_col(valid_schools['website'])
            if not valid_clubs.empty:
                valid_clubs = valid_clubs.copy()
                valid_clubs['website_link'] = _website_link_col(valid_clubs['website'])
            if not valid_gemeenten.empty:
                valid_gemeenten = valid_gemeenten.copy()
                valid_gemeenten['website_link'] = ''

            tooltip = {
                'html': '<b>{naam}</b><br/>{map_tip_line}<br/>{plaats}{website_link}',
                'style': {'backgroundColor': 'black', 'color': 'white'},
            }

            layers = []
            if not valid_schools.empty:
                layers.append(
                    pdk.Layer(
                        'ScatterplotLayer',
                        data=valid_schools,
                        id='school_layer',
                        pickable=True,
                        opacity=0.85,
                        stroked=True,
                        filled=True,
                        radius_min_pixels=8,
                        radius_max_pixels=36,
                        get_position='[longitude, latitude]',
                        get_fill_color='[160, 180, 240, 170]',
                        get_line_color='[255, 255, 255]',
                        get_radius=2000,
                        auto_highlight=True,
                    )
                )
            if not valid_clubs.empty:
                layers.append(
                    pdk.Layer(
                        'ScatterplotLayer',
                        data=valid_clubs,
                        id='school_map_clubs',
                        pickable=True,
                        opacity=0.78,
                        stroked=True,
                        filled=True,
                        radius_min_pixels=6,
                        radius_max_pixels=24,
                        get_position='[longitude, latitude]',
                        get_fill_color='[235, 115, 35, 200]',
                        get_line_color='[90, 40, 10]',
                        get_radius=1700,
                        auto_highlight=True,
                    )
                )
            if not valid_gemeenten.empty:
                gm_plain = valid_gemeenten[~valid_gemeenten['gemeente_selected']].copy()
                gm_sel = valid_gemeenten[valid_gemeenten['gemeente_selected']].copy()
                if not gm_plain.empty:
                    layers.append(
                        pdk.Layer(
                            'ScatterplotLayer',
                            data=gm_plain,
                            id='school_map_gemeente',
                            pickable=True,
                            opacity=0.55,
                            stroked=True,
                            filled=True,
                            radius_min_pixels=10,
                            radius_max_pixels=22,
                            get_position='[longitude, latitude]',
                            get_fill_color='[72, 140, 72, 140]',
                            get_line_color='[30, 70, 30]',
                            get_radius=2800,
                            auto_highlight=True,
                        )
                    )
                if not gm_sel.empty:
                    layers.append(
                        pdk.Layer(
                            'ScatterplotLayer',
                            data=gm_sel,
                            id='school_map_gemeente_selected',
                            pickable=True,
                            opacity=0.85,
                            stroked=True,
                            filled=True,
                            radius_min_pixels=12,
                            radius_max_pixels=28,
                            get_position='[longitude, latitude]',
                            get_fill_color='[20, 120, 40, 220]',
                            get_line_color='[255, 255, 255]',
                            get_radius=3600,
                            auto_highlight=True,
                        )
                    )
                layers.append(
                    pdk.Layer(
                        'TextLayer',
                        data=valid_gemeenten,
                        id='school_map_gemeente_labels',
                        pickable=False,
                        get_position='[longitude, latitude]',
                        get_text='gemeentenaam',
                        get_size=13,
                        get_color='[25, 70, 25, 230]',
                        get_text_anchor='"middle"',
                        get_alignment_baseline='"bottom"',
                    )
                )
            if not sel_m.empty:
                hl = sel_m[
                    ['naam', 'plaats', 'website', 'latitude', 'longitude', 'school_id', 'administratienummer', 'map_tip_line']
                ].copy()
                hl['website_link'] = hl['website'].apply(
                    lambda w: f'<br/><a href="{w}" target="_blank">Website</a>' if w else ''
                )
                layers.append(
                    pdk.Layer(
                        'ScatterplotLayer',
                        data=hl,
                        id='school_selected',
                        pickable=True,
                        opacity=1.0,
                        stroked=True,
                        filled=True,
                        radius_min_pixels=14,
                        radius_max_pixels=48,
                        get_position='[longitude, latitude]',
                        get_fill_color='[30, 80, 220, 230]',
                        get_line_color='[255, 255, 255]',
                        get_radius=4200,
                        auto_highlight=True,
                    )
                )
            deck = pdk.Deck(
                map_style='light',
                initial_view_state=pdk.ViewState(latitude=center_lat, longitude=center_lon, zoom=zoom, pitch=0),
                layers=layers,
                tooltip=tooltip,
                height=map_height,
            )
            sel = st.pydeck_chart(
                deck,
                use_container_width=True,
                selection_mode='single-object',
                on_select='rerun',
                key='school_map',
            )
            _apply_school_map_pick(sel, filtered_reset, db['clubs'], gem_key, pending_gem_key)
            cap = '**Lichtblauw** = school (klik = selectie).'
            if not valid_gemeenten.empty:
                cap += f' **Groen** = gemeente ({len(valid_gemeenten)}; klik = gemeentefilter).'
            if not valid_clubs.empty:
                cap += f' **Oranje** = damclub ({len(valid_clubs)}; klik = gegevens).'
            st.caption(cap)
            warn_parts = []
            if invalid_schools:
                warn_parts.append(
                    f'{invalid_schools} schoolpunt(en) met ongeldige coördinaten voor {land_choice} niet getoond.'
                )
            if invalid_clubs:
                warn_parts.append(
                    f'{invalid_clubs} clubpunt(en) met ongeldige coördinaten niet getoond.'
                )
            if warn_parts:
                st.warning(' '.join(warn_parts))

    sid = st.session_state.selected_school_id
    selected = next((r['raw'] for r in rows if r['_id'] == sid), None) if sid else None

    st.subheader('Schoolgegevens')
    with st.expander('Details & bewerken', expanded=selected is not None):
        if not selected:
            st.info('Kies een school in de tabel of op de kaart.')
        else:
            st.markdown(f"### {selected.get('naam', '')}")
            st.write('**Administratienummer:**', selected.get('administratienummer', ''))
            if land_code == 'NL':
                st.write('**Onderwijstype:**', selected.get('soort_norm', ''))
                st.write('**DUO-soort:**', selected.get('soort', ''))
            else:
                st.write('**Soort:**', selected.get('soort', ''))
            st.write('**Status:**', selected.get('status', ''))
            st.write('**Plaats:**', selected.get('plaats', ''))
            st.write('**Gemeente:**', selected.get('gemeente', ''))
            prov_key = 'Bundesland' if land_code == 'DE' else 'Provincie'
            st.write(f'**{prov_key}:**', selected.get('provincie', ''))
            if land_code == 'DE' and selected.get('bundesland_code'):
                st.write('**Bundesland-code:**', selected.get('bundesland_code', ''))
            st.write('**Adres vestiging:**', ' '.join(filter(None, [
                selected.get('straat_vestiging'),
                selected.get('huisnr_vestiging'),
                selected.get('huisnr_toev_vestiging'),
            ])).strip())
            st.write('**Postcode:**', selected.get('postcode_vestiging', ''))
            st.write('**Telefoon:**', selected.get('telefoon', ''))
            st.write('**E-mail:**', selected.get('email', ''))
            st.write('**Website:**', selected.get('website', ''))
            st.write('**Bronbestand:**', selected.get('bron_bestand', ''))
            corr = selected.get('correspondentie') or {}
            if any(corr.values()):
                st.subheader('Correspondentie-adres')
                st.json(corr)
            with st.expander('Volledige JSON'):
                st.json(_serialize_mongo_doc(selected))

            st.subheader('Handmatig bijwerken')
            with st.form(f"school_edit_{selected['_id']}"):
                website = st.text_input('Website', selected.get('website', '') or '')
                email = st.text_input('E-mail', selected.get('email', '') or '')
                lat = st.text_input('Latitude', str(selected.get('lat') or ''))
                lon = st.text_input('Longitude', str(selected.get('lon') or ''))
                sub = st.form_submit_button('Opslaan')
                if sub:
                    try:
                        upd = {
                            'website': website.strip(),
                            'email': email.strip(),
                            'lat': float(lat) if lat.strip() else None,
                            'lon': float(lon) if lon.strip() else None,
                            'updated_at_manual': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                        }
                        schools_coll.update_one({'_id': selected['_id']}, {'$set': upd})
                        st.success('Opgeslagen.')
                        st.rerun()
                    except Exception as exc:
                        st.error(f'Opslaan mislukt: {exc}')

    if st.session_state.get('school_map_club_popup'):
        _school_map_club_popup_dialog()
