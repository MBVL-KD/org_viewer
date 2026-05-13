"""Scholen-tab (Streamlit): zelfde UX-patroon als damclubs."""
import json
import time
from typing import Optional

import pandas as pd
import pydeck as pdk
import streamlit as st
from bson import ObjectId

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


def _apply_pydeck_school_pick(selection, filtered_reset_df):
    picked = _pydeck_first_picked_object(selection)
    if not picked or not isinstance(picked, dict):
        return
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
    prov = sb.selectbox('Provincie', ['Alle'] + sorted(df['provincie'].dropna().unique().tolist()))
    gem_vals = sorted(
        {str(v).strip() for v in df['gemeente'].dropna().unique() if str(v).strip()},
        key=lambda x: x.lower(),
    )
    gem = sb.selectbox('Gemeente', ['Alle'] + gem_vals)
    soort_f = sb.selectbox('Soort', ['Alle'] + sorted(df['soort'].dropna().unique().tolist()))
    status_f = sb.selectbox('Status', ['Alle'] + sorted(df['status'].dropna().unique().tolist()))
    sb.markdown('Postcode (eerste 4 cijfers)')
    pc_col1, pc_col2 = sb.columns(2)
    with pc_col1:
        pc_min_in = st.text_input('Van', placeholder='bv. 1000', key='school_pc_min')
    with pc_col2:
        pc_max_in = st.text_input('Tot', placeholder='bv. 1099', key='school_pc_max')
    sb.caption('Leeg laten = geen onder-/bovengrens. Ongeldige invoer wordt genegeerd.')

    search = st.text_input('Zoek school, plaats, admin.nr., e-mail …')

    filtered = df.copy()
    if prov != 'Alle':
        filtered = filtered[filtered['provincie'] == prov]
    if gem != 'Alle':
        filtered = filtered[filtered['gemeente'] == gem]
    if soort_f != 'Alle':
        filtered = filtered[filtered['soort'] == soort_f]
    if status_f != 'Alle':
        filtered = filtered[filtered['status'] == status_f]
    pc_lo = _parse_postcode_range_input(pc_min_in)
    pc_hi = _parse_postcode_range_input(pc_max_in)
    if pc_lo is not None and pc_hi is not None and pc_lo > pc_hi:
        pc_lo, pc_hi = pc_hi, pc_lo
    if pc_lo is not None or pc_hi is not None:
        m_pc = filtered['postcode4'].notna()
        if pc_lo is not None:
            m_pc &= filtered['postcode4'] >= pc_lo
        if pc_hi is not None:
            m_pc &= filtered['postcode4'] <= pc_hi
        filtered = filtered[m_pc]
    if search:
        m = filtered['text_search'].str.contains(search, case=False, na=False)
        filtered = filtered[m]

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
        map_df = filtered_reset[['naam', 'plaats', 'website', 'lat', 'lon', '_id', 'administratienummer']].dropna(
            subset=['lat', 'lon']
        )
        if map_df.empty:
            st.info('Geen coördinaten in deze selectie. Draai `python3 import_schools.py … --geocode` of vul handmatig in.')
        else:
            map_df = map_df.rename(columns={'lat': 'latitude', 'lon': 'longitude'})
            map_df['school_id'] = map_df['_id'].astype(str)
            valid = map_df[
                (map_df['latitude'] >= 50.5) & (map_df['latitude'] <= 53.7) &
                (map_df['longitude'] >= 3.0) & (map_df['longitude'] <= 7.5)
            ].copy()
            invalid_count = len(map_df) - len(valid)
            if valid.empty:
                st.warning('Geen punten binnen het NL-kaartvenster.')
            else:
                zoom_close = 12.8
                center_lat = float(valid['latitude'].median())
                center_lon = float(valid['longitude'].median())
                zoom = 7.0
                lat_span = float(valid['latitude'].max() - valid['latitude'].min())
                lon_span = float(valid['longitude'].max() - valid['longitude'].min())
                if lat_span < 0.4 and lon_span < 0.4:
                    zoom = 9.0
                elif lat_span < 0.8 and lon_span < 0.8:
                    zoom = 8.5
                elif lat_span < 1.5 and lon_span < 1.5:
                    zoom = 8.0
                elif lat_span < 2.5 and lon_span < 2.5:
                    zoom = 7.5

                sel_m = valid[valid['_id'] == sid_map] if sid_map is not None else pd.DataFrame()
                if not sel_m.empty:
                    center_lat = float(sel_m.iloc[0]['latitude'])
                    center_lon = float(sel_m.iloc[0]['longitude'])
                    zoom = zoom_close

                layer_all = pdk.Layer(
                    'ScatterplotLayer',
                    data=valid,
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
                layers = [layer_all]
                if not sel_m.empty:
                    hl = sel_m[['naam', 'plaats', 'website', 'latitude', 'longitude', 'school_id', 'administratienummer']].copy()
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
                tooltip = {
                    'html': '<b>{naam}</b><br/>{administratienummer}<br/>{plaats}<br/><a href="{website}" target="_blank">Website</a>',
                    'style': {'backgroundColor': 'black', 'color': 'white'},
                }
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
                _apply_pydeck_school_pick(sel, filtered_reset)
                st.caption('Klik op een punt om de school te selecteren.')
                if invalid_count:
                    st.warning(f'{invalid_count} punt(en) buiten het NL-venster verborgen.')

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
