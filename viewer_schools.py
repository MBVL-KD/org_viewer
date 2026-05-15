"""Scholen-tab (Streamlit): zelfde UX-patroon als damclubs."""
import json
import time
from typing import Any, List, Optional

import pandas as pd
import pydeck as pdk
import streamlit as st
from bson import ObjectId

from nl_provincie import canonical_nl_provincie_club, normalize_nl_provincienaam

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
) -> pd.DataFrame:
    """Pas schoolfilters toe; skip_gemeente=True voor gemeenten-markers op de kaart."""
    out = df.copy()
    if prov_sel:
        out = out[out['provincie'].isin(prov_sel)]
    if gem_sel and not skip_gemeente:
        out = out[out['gemeente'].isin(gem_sel)]
    if soort_sel:
        out = out[out['soort'].isin(soort_sel)]
    if status_sel:
        out = out[out['status'].isin(status_sel)]
    if pc_lo is not None or pc_hi is not None:
        m_pc = out['postcode4'].notna()
        if pc_lo is not None:
            m_pc &= out['postcode4'] >= pc_lo
        if pc_hi is not None:
            m_pc &= out['postcode4'] <= pc_hi
        out = out[m_pc]
    if search:
        out = out[out['text_search'].str.contains(search, case=False, na=False)]
    return out


def _gemeenten_map_markers(
    df: pd.DataFrame,
    prov_sel: List[str],
    gem_sel: List[str],
    soort_sel: List[str],
    status_sel: List[str],
    pc_lo: Optional[int],
    pc_hi: Optional[int],
    search: str,
) -> pd.DataFrame:
    """Klikbare gemeentecentra (uit scholen met coördinaten), rekening houdend met alle filters behalve gemeente."""
    base = _filter_schools_df(
        df, prov_sel, [], soort_sel, status_sel, pc_lo, pc_hi, search, skip_gemeente=True,
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
            'map_tip_line': f'Gemeente · {n} school(s) in huidige filters',
            'gemeente_selected': gem in gem_sel,
        })
    return pd.DataFrame(rows)


def _apply_school_map_pick(selection, filtered_reset_df, clubs_coll):
    """Verwerk klik op scholenkaart: gemeente → filter, damclub → popup, school → selectie."""
    picked = _pydeck_first_picked_object(selection)
    if not picked or not isinstance(picked, dict):
        return
    gn = picked.get('gemeentenaam')
    if gn and str(gn).strip():
        # We mogen de key van een bestaand widget niet direct aanpassen ná
        # aanmaak; zet een pending waarde en forceer een rerun.
        gn = str(gn).strip()
        current = list(st.session_state.get('school_filter_gem', []))
        if gn not in current:
            current.append(gn)
        st.session_state['_pending_school_filter_gem'] = current
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
    if pop.get('bond') and pop.get('bond') != pop.get('provincie_label'):
        st.caption(f"Bondcode in data: **{pop['bond']}**")
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
        if not allowed_plaatsen:
            return pd.DataFrame()

    prov_wanted: Optional[set] = None
    if prov_sel:
        prov_wanted = {normalize_nl_provincienaam(str(p)) for p in prov_sel if str(p).strip()}

    out_rows = []
    for c in db['clubs'].find():
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
        if allowed_plaatsen is not None and plaats not in allowed_plaatsen:
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
    schools = list(schools_coll.find())
    if not schools:
        st.info(
            'Nog geen scholen in de database. Importeer CSV’s met:\n\n'
            '`python3 import_schools.py pad/naar/ho-Scholen.csv pad/naar/Scholen-3.csv pad/naar/Scholen-4.csv`\n\n'
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
        rows.append({
            '_id': s['_id'],
            'administratienummer': str(s.get('administratienummer', '') or ''),
            'naam': s.get('naam', ''),
            'plaats': s.get('plaats', ''),
            'gemeente': s.get('gemeente', ''),
            'postcode_vestiging': s.get('postcode_vestiging', '') or '',
            'provincie': s.get('provincie', ''),
            'soort': s.get('soort', ''),
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
    df['postcode4'] = df['postcode_vestiging'].apply(_postcode_first_four_digits)
    sb = st.sidebar
    sb.header('Filters (scholen)')
    _ms_help = 'Geen selectie = alles tonen. Meerdere waarden: elk van die waarden (OR binnen dit veld).'
    prov_opts = sorted(df['provincie'].dropna().unique().tolist())
    prov_sel = sb.multiselect('Provincie', options=prov_opts, default=[], key='school_filter_prov', help=_ms_help)
    # Eventuele pending-gemeenteselectie toepassen vóórdat de widget wordt aangemaakt.
    pending_gem = st.session_state.pop('_pending_school_filter_gem', None)
    if pending_gem is not None:
        st.session_state['school_filter_gem'] = pending_gem
    if prov_sel:
        gem_pool = df[df['provincie'].isin(prov_sel)]
    else:
        gem_pool = df
    gem_vals = sorted(
        {str(v).strip() for v in gem_pool['gemeente'].dropna().unique() if str(v).strip()},
        key=lambda x: x.lower(),
    )
    if 'school_filter_gem' not in st.session_state:
        st.session_state['school_filter_gem'] = []
    _allowed_gem = set(gem_vals)
    _cur_gem = [g for g in st.session_state.get('school_filter_gem', []) if g in _allowed_gem]
    if _cur_gem != st.session_state.get('school_filter_gem', []):
        st.session_state['school_filter_gem'] = _cur_gem
    gem_sel = sb.multiselect(
        'Gemeente',
        options=gem_vals,
        key='school_filter_gem',
        help=_ms_help + (' Alleen gemeenten uit de gekozen provincie(s).' if prov_sel else ''),
    )
    soort_opts = sorted(df['soort'].dropna().unique().tolist())
    soort_sel = sb.multiselect('Soort', options=soort_opts, default=[], key='school_filter_soort', help=_ms_help)
    status_opts = sorted(df['status'].dropna().unique().tolist())
    status_sel = sb.multiselect('Status', options=status_opts, default=[], key='school_filter_status', help=_ms_help)
    sb.markdown('Postcode (eerste 4 cijfers)')
    pc_col1, pc_col2 = sb.columns(2)
    with pc_col1:
        pc_min_in = st.text_input('Van', placeholder='bv. 1000', key='school_pc_min')
    with pc_col2:
        pc_max_in = st.text_input('Tot', placeholder='bv. 1099', key='school_pc_max')
    sb.caption('Leeg laten = geen onder-/bovengrens. Ongeldige invoer wordt genegeerd.')

    search = st.text_input('Zoek school, plaats, admin.nr., e-mail …')

    pc_lo = _parse_postcode_range_input(pc_min_in)
    pc_hi = _parse_postcode_range_input(pc_max_in)
    if pc_lo is not None and pc_hi is not None and pc_lo > pc_hi:
        pc_lo, pc_hi = pc_hi, pc_lo

    filtered = _filter_schools_df(
        df, prov_sel, gem_sel, soort_sel, status_sel, pc_lo, pc_hi, search,
    )

    filtered_reset = filtered.reset_index(drop=True)

    if 'selected_school_id' not in st.session_state:
        st.session_state.selected_school_id = None
    if st.session_state.selected_school_id is not None:
        if st.session_state.selected_school_id not in set(filtered_reset['_id'].tolist()):
            st.session_state.selected_school_id = None

    st.subheader('Schooloverzicht')
    st.write(f'Selectie: {len(filtered_reset)} · Totaal in database: {len(schools)}')

    d1, d2, _ = st.columns([1, 1, 4])
    with d1:
        st.download_button('Download alle scholen (JSON)', export_json, 'scholen_alle.json', 'application/json')
    with d2:
        st.download_button('Download alle scholen (CSV)', export_csv, 'scholen_alle.csv', 'text/csv')

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

    ev = st.dataframe(
        table_df,
        height=SCHOOL_TABLE_HEIGHT,
        use_container_width=True,
        hide_index=True,
        on_select='rerun',
        selection_mode='single-row',
        key='school_table',
        selection_default=sel_def,
    )
    st.caption(
        f'Maximaal {LIST_VISIBLE_ROWS} rijen zichtbaar; scroll voor meer ({len(filtered_reset)} in selectie).'
    )

    sr = _dataframe_selection_rows(ev)
    if sr:
        i = sr[0]
        if 0 <= i < len(filtered_reset):
            st.session_state.selected_school_id = filtered_reset.iloc[i]['_id']

    sid_map = st.session_state.selected_school_id

    col_details, col_map = st.columns(2, gap='large')

    with col_map:
        st.subheader('Kaart')
        show_clubs_on_map = st.checkbox(
            'Damclubs op deze kaart tonen',
            value=False,
            key='school_map_show_clubs',
            help=(
                'Zelfde provincie als je schoolfilter. Bij filter op gemeente, postcodebereik of zoekterm: '
                'club moet in dezelfde plaats zitten als een school in de huidige selectie. '
                'Soort/status van scholen gelden niet voor clubs. Klik op een oranje punt voor een kort '
                'gegevensvenster; lichtblauw = school (donkerblauw = geselecteerde school).'
            ),
        )
        if not show_clubs_on_map:
            st.session_state.pop('school_map_club_popup', None)

        show_gemeenten_on_map = st.checkbox(
            'Gemeenten op kaart (klik = gemeentefilter)',
            value=False,
            key='school_map_show_gemeenten',
            help=(
                'Groene labels = gemeenten met scholen in de huidige filters (provincie, soort, status, '
                'postcode, zoekterm). Elke klik voegt een gemeente toe aan het filter (meerdere mogelijk). '
                'Geen volledige gemeentegrenzen (te zwaar voor de browser); centra uit schoolcoördinaten.'
            ),
        )

        nl_lat_lo, nl_lat_hi = 50.5, 53.7
        nl_lon_lo, nl_lon_hi = 3.0, 7.5

        map_df = filtered_reset[
            ['naam', 'plaats', 'website', 'lat', 'lon', '_id', 'administratienummer']
        ].dropna(subset=['lat', 'lon'])
        valid_schools = pd.DataFrame()
        invalid_schools = 0
        if not map_df.empty:
            map_df = map_df.rename(columns={'lat': 'latitude', 'lon': 'longitude'})
            map_df['school_id'] = map_df['_id'].astype(str)
            map_df['map_tip_line'] = map_df['administratienummer'].astype(str)
            vs = map_df[
                (map_df['latitude'] >= nl_lat_lo) & (map_df['latitude'] <= nl_lat_hi) &
                (map_df['longitude'] >= nl_lon_lo) & (map_df['longitude'] <= nl_lon_hi)
            ].copy()
            invalid_schools = len(map_df) - len(vs)
            valid_schools = vs

        clubs_raw = pd.DataFrame()
        if show_clubs_on_map:
            clubs_raw = _clubs_overlay_for_school_filters(
                db, prov_sel, gem_sel, pc_lo, pc_hi, search, filtered_reset,
            )
        valid_clubs = pd.DataFrame()
        invalid_clubs = 0
        if show_clubs_on_map and not clubs_raw.empty:
            vcl = clubs_raw[
                (clubs_raw['latitude'] >= nl_lat_lo) & (clubs_raw['latitude'] <= nl_lat_hi) &
                (clubs_raw['longitude'] >= nl_lon_lo) & (clubs_raw['longitude'] <= nl_lon_hi)
            ].copy()
            invalid_clubs = len(clubs_raw) - len(vcl)
            vcl = vcl.copy()
            vcl['map_tip_line'] = 'Damclub'
            valid_clubs = vcl

        valid_gemeenten = pd.DataFrame()
        if show_gemeenten_on_map:
            gm = _gemeenten_map_markers(
                df, prov_sel, gem_sel, soort_sel, status_sel, pc_lo, pc_hi, search,
            )
            if not gm.empty:
                valid_gemeenten = gm[
                    (gm['latitude'] >= nl_lat_lo) & (gm['latitude'] <= nl_lat_hi) &
                    (gm['longitude'] >= nl_lon_lo) & (gm['longitude'] <= nl_lon_hi)
                ].copy()

        if valid_schools.empty and valid_clubs.empty and valid_gemeenten.empty:
            parts = [
                'Geen punten met coördinaten binnen het NL-kaartvenster voor deze instellingen.',
            ]
            if len(filtered_reset) and map_df.empty:
                parts.append('Scholen in de selectie hebben geen lat/lon — draai geocode of vul coördinaten handmatig in.')
            if show_clubs_on_map and clubs_raw.empty and filtered_reset.empty:
                parts.append('Lege schoolselectie: geen plaatsen om damclubs aan te koppelen.')
            if show_clubs_on_map and clubs_raw.empty and len(filtered_reset) > 0:
                parts.append('Geen damclubs voldoen aan de criteria, of clubs missen coördinaten.')
            st.info(' '.join(parts))
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
            zoom = 7.0
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
                        pickable=True,
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
            _apply_school_map_pick(sel, filtered_reset, db['clubs'])
            cap = '**Lichtblauw** = school (klik = selectie).'
            if not valid_gemeenten.empty:
                cap += f' **Groen** = gemeente ({len(valid_gemeenten)}; klik = gemeentefilter).'
            if not valid_clubs.empty:
                cap += f' **Oranje** = damclub ({len(valid_clubs)}; klik = gegevens).'
            st.caption(cap)
            warn_parts = []
            if invalid_schools:
                warn_parts.append(f'{invalid_schools} schoolpunt(en) buiten het NL-venster verborgen.')
            if invalid_clubs:
                warn_parts.append(f'{invalid_clubs} clubpunt(en) buiten het NL-venster verborgen.')
            if warn_parts:
                st.warning(' '.join(warn_parts))

    sid = st.session_state.selected_school_id
    selected = next((r['raw'] for r in rows if r['_id'] == sid), None) if sid else None

    with col_details:
        st.subheader('Schoolgegevens')
        with st.expander('Details & bewerken', expanded=selected is not None):
            if not selected:
                st.info('Kies een school in de tabel of op de kaart.')
            else:
                st.markdown(f"### {selected.get('naam', '')}")
                st.write('**Administratienummer:**', selected.get('administratienummer', ''))
                st.write('**Soort:**', selected.get('soort', ''))
                st.write('**Status:**', selected.get('status', ''))
                st.write('**Plaats:**', selected.get('plaats', ''))
                st.write('**Gemeente:**', selected.get('gemeente', ''))
                st.write('**Provincie:**', selected.get('provincie', ''))
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
