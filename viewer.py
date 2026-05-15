import os
import json
import hmac
import certifi
from pathlib import Path
from dotenv import load_dotenv
import streamlit as st
from bson import ObjectId
from pymongo import MongoClient
import pandas as pd
import pydeck as pdk

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


def _viewer_password_from_config() -> str:
    """Optionele toegangscode: eerst omgevingsvariabele (.env), anders Streamlit Secrets."""
    p = (os.environ.get('VIEWER_PASSWORD') or '').strip()
    if p:
        return p
    try:
        sec = getattr(st, 'secrets', None)
        if sec:
            p2 = sec.get('VIEWER_PASSWORD') or sec.get('STREAMLIT_VIEWER_PASSWORD')
            return str(p2 or '').strip()
    except Exception:
        pass
    return ''


def _viewer_password_matches(entered: str, correct: str) -> bool:
    if len(entered) != len(correct):
        return False
    return hmac.compare_digest(entered.encode('utf-8'), correct.encode('utf-8'))


def _ensure_viewer_password() -> None:
    """Blokkeer de app tot de juiste code is ingevoerd (alleen als VIEWER_PASSWORD is gezet)."""
    pwd = _viewer_password_from_config()
    if not pwd:
        return
    if st.session_state.get('_viewer_auth_ok'):
        return
    st.title('Draughts4All — toegang')
    st.caption('Voer de toegangscode in om de viewer te openen.')
    entered = st.text_input('Toegangscode', type='password', key='viewer_password_input')
    if st.button('Doorgaan', type='primary'):
        if _viewer_password_matches(entered, pwd):
            st.session_state['_viewer_auth_ok'] = True
            st.rerun()
        else:
            st.error('Onjuiste code.')
    st.stop()


def _is_obfuscated_email(value):
    if not value:
        return True
    low = str(value).lower()
    if 'email protected' in low or 'email beschermd' in low:
        return True
    if '[' in low and 'protected' in low:
        return True
    return False


def _clean_emails(seq):
    return [e for e in (seq or []) if e and not _is_obfuscated_email(e)]


def _serialize_mongo_doc(doc):
    return json.loads(json.dumps(doc, default=str))


def _build_export_json_bytes(clubs_list):
    payload = [_serialize_mongo_doc(c) for c in clubs_list]
    return json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')


def _build_export_csv_bytes(clubs_list):
    records = [_serialize_mongo_doc(c) for c in clubs_list]
    if not records:
        return 'provincie,naam\n'.encode('utf-8-sig')
    df_exp = pd.json_normalize(records, sep='_')
    for col in df_exp.columns:
        if df_exp[col].dtype == object:
            df_exp[col] = df_exp[col].apply(
                lambda x: json.dumps(x, ensure_ascii=False) if isinstance(x, (dict, list)) else x
            )
    return df_exp.to_csv(index=False).encode('utf-8-sig')


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


LIST_VISIBLE_ROWS = 10
TABLE_ROW_PX = 36
TABLE_HEADER_PX = 52
CLUB_TABLE_HEIGHT = LIST_VISIBLE_ROWS * TABLE_ROW_PX + TABLE_HEADER_PX


def _pydeck_first_picked_object(selection):
    """Parse PydeckState (selection.objects) en oudere selectie-vormen."""
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
        if isinstance(legacy_sel, dict) and legacy_sel.get('club_id') is not None:
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


def _apply_pydeck_club_pick(selection, filtered_reset_df):
    picked = _pydeck_first_picked_object(selection)
    if not picked or not isinstance(picked, dict):
        return
    cid = picked.get('club_id')
    if cid:
        try:
            oid = ObjectId(str(cid))
            if oid in set(filtered_reset_df['_id'].tolist()):
                st.session_state.selected_club_id = oid
        except Exception:
            return
        return
    chosen = picked.get('naam') or picked.get('name') or picked.get('title')
    if chosen:
        match = filtered_reset_df[filtered_reset_df['naam'] == chosen]
        if len(match) == 1:
            st.session_state.selected_club_id = match.iloc[0]['_id']

from viewer_schools import render_schools

def render_clubs(map_height):
    clubs = list(collection.find())
    if not clubs:
        st.warning('Geen clubdata gevonden. Draai eerst scraper met: python3 scraper.py')
        st.stop()

    export_json_bytes = _build_export_json_bytes(clubs)
    export_csv_bytes = _build_export_csv_bytes(clubs)

    rows = []
    for club in clubs:
        details = club.get('details', {}) or {}
        eml = details.get('emails', []) or []
        rows.append({
            '_id': club['_id'],
            'provincie': club.get('provincie', ''),
            'plaats': club.get('plaats', ''),
            'naam': club.get('naam', ''),
            'secretariaat': club.get('secretariaat', ''),
            'clublokaal': club.get('clublokaal', ''),
            'website': details.get('website', ''),
            'lat': club.get('lat'),
            'lon': club.get('lon'),
            'bond_url': club.get('bond_url', ''),
            'club_url': club.get('club_url', ''),
            'logo': club.get('logo', ''),
            'emails': eml,
            'emails_search': ' '.join(str(e) for e in eml if e),
            'telefoons': details.get('telefoons', []),
            'imported_at': club.get('imported_at', ''),
            'updated_at': club.get('updated_at', ''),
            'details': details,
        })

    df = pd.DataFrame(rows)
    show_sidebar = st.sidebar
    show_sidebar.header('Filters')
    province_opts = sorted(df['provincie'].dropna().unique().tolist())
    province_filter = show_sidebar.multiselect(
        'Provincie',
        options=province_opts,
        default=[],
        key='club_filter_prov',
        help='Geen selectie = alle provincies. Meerdere provincies: OR.',
    )
    website_only = show_sidebar.checkbox('Alleen clubs met website', value=False)

    search_term = st.text_input('Zoek club, plaats, provincie, website of contact')

    filtered = df.copy()
    if province_filter:
        filtered = filtered[filtered['provincie'].isin(province_filter)]
    if search_term:
        search_mask = (
            filtered['naam'].str.contains(search_term, case=False, na=False) |
            filtered['plaats'].str.contains(search_term, case=False, na=False) |
            filtered['provincie'].str.contains(search_term, case=False, na=False) |
            filtered['website'].str.contains(search_term, case=False, na=False) |
            filtered['bond_url'].str.contains(search_term, case=False, na=False) |
            filtered['secretariaat'].str.contains(search_term, case=False, na=False) |
            filtered['emails_search'].str.contains(search_term, case=False, na=False)
        )
        filtered = filtered[search_mask]
    if website_only:
        filtered = filtered[filtered['website'].astype(bool)]

    filtered_reset = filtered.reset_index(drop=True)

    if 'selected_club_id' not in st.session_state:
        st.session_state.selected_club_id = None

    if st.session_state.selected_club_id is not None:
        if st.session_state.selected_club_id not in set(filtered_reset['_id'].tolist()):
            st.session_state.selected_club_id = None

    st.subheader('Club overzicht')
    st.write(f'Aantal clubs in deze selectie: {len(filtered_reset)} · Totaal in database: {len(clubs)}')

    dl_json, dl_csv, _ = st.columns([1, 1, 4])
    with dl_json:
        st.download_button(
            label='Download alle clubs (JSON)',
            data=export_json_bytes,
            file_name='damclubs_alle.json',
            mime='application/json',
            help='Volledige export van alle clubdocumenten uit MongoDB (niet alleen de filter).',
        )
    with dl_csv:
        st.download_button(
            label='Download alle clubs (CSV)',
            data=export_csv_bytes,
            file_name='damclubs_alle.csv',
            mime='text/csv',
            help='Volledige export van alle clubs als platte CSV (geneste velden als JSON-tekst).',
        )

    display_cols = ['provincie', 'plaats', 'naam', 'secretariaat', 'clublokaal', 'website', 'lat', 'lon']
    table_df = filtered_reset[display_cols] if len(filtered_reset) else pd.DataFrame(columns=display_cols)

    pre_selected_id = st.session_state.get('selected_club_id')
    selection_default = None
    if pre_selected_id is not None and len(filtered_reset):
        hit_idx = filtered_reset.index[filtered_reset['_id'] == pre_selected_id].tolist()
        if hit_idx:
            selection_default = {'selection': {'rows': [int(hit_idx[0])]}}

    col_table, col_map = st.columns(2, gap='large')

    with col_table:
        df_event = st.dataframe(
            table_df,
            height=map_height,
            use_container_width=True,
            hide_index=True,
            on_select='rerun',
            selection_mode='single-row',
            key='club_table',
            selection_default=selection_default,
        )
        st.caption(
            f'Scroll in de tabel voor meer rijen ({len(filtered_reset)} in selectie).'
        )

    sel_rows = _dataframe_selection_rows(df_event)
    if sel_rows:
        idx = sel_rows[0]
        if 0 <= idx < len(filtered_reset):
            st.session_state.selected_club_id = filtered_reset.iloc[idx]['_id']

    selected_id_for_map = st.session_state.selected_club_id

    with col_map:
        map_df = filtered_reset[['naam', 'plaats', 'website', 'lat', 'lon', '_id']].dropna(subset=['lat', 'lon'])
        if map_df.empty:
            st.info('Geen coördinaten in deze selectie. Laat de scraper geocoden of vul lat/lon handmatig in.')
        else:
            map_df = map_df.rename(columns={'lat': 'latitude', 'lon': 'longitude'})
            map_df['club_id'] = map_df['_id'].astype(str)
            valid_map_df = map_df[
                (map_df['latitude'] >= 50.5) & (map_df['latitude'] <= 53.7) &
                (map_df['longitude'] >= 3.0) & (map_df['longitude'] <= 7.5)
            ].copy()
            invalid_count = len(map_df) - len(valid_map_df)

            if valid_map_df.empty:
                st.warning('Geen valide Nederlandse coördinaten voor deze selectie (of alle punten vallen buiten het NL-venster).')
            else:
                zoom_close = 12.8
                center_lat = float(valid_map_df['latitude'].median())
                center_lon = float(valid_map_df['longitude'].median())
                zoom = 7.0
                lat_span = float(valid_map_df['latitude'].max() - valid_map_df['latitude'].min())
                lon_span = float(valid_map_df['longitude'].max() - valid_map_df['longitude'].min())
                if lat_span < 0.4 and lon_span < 0.4:
                    zoom = 9.0
                elif lat_span < 0.8 and lon_span < 0.8:
                    zoom = 8.5
                elif lat_span < 1.5 and lon_span < 1.5:
                    zoom = 8.0
                elif lat_span < 2.5 and lon_span < 2.5:
                    zoom = 7.5

                sel_match = (
                    valid_map_df[valid_map_df['_id'] == selected_id_for_map]
                    if selected_id_for_map is not None
                    else pd.DataFrame()
                )
                if not sel_match.empty:
                    slat = float(sel_match.iloc[0]['latitude'])
                    slon = float(sel_match.iloc[0]['longitude'])
                    center_lat, center_lon = slat, slon
                    zoom = zoom_close

                layer_all = pdk.Layer(
                    'ScatterplotLayer',
                    data=valid_map_df,
                    id='club_layer',
                    pickable=True,
                    opacity=0.85,
                    stroked=True,
                    filled=True,
                    radius_min_pixels=8,
                    radius_max_pixels=36,
                    get_position='[longitude, latitude]',
                    get_fill_color='[200, 200, 210, 160]',
                    get_line_color='[255, 255, 255]',
                    get_radius=2000,
                    auto_highlight=True,
                )
                layers = [layer_all]
                if not sel_match.empty:
                    highlight = sel_match[['naam', 'plaats', 'website', 'latitude', 'longitude', 'club_id']].copy()
                    layers.append(
                        pdk.Layer(
                            'ScatterplotLayer',
                            data=highlight,
                            id='club_selected',
                            pickable=True,
                            opacity=1.0,
                            stroked=True,
                            filled=True,
                            radius_min_pixels=14,
                            radius_max_pixels=48,
                            get_position='[longitude, latitude]',
                            get_fill_color='[255, 60, 60, 230]',
                            get_line_color='[255, 255, 255]',
                            get_radius=4200,
                            auto_highlight=True,
                        )
                    )

                tooltip = {
                    'html': '<b>{naam}</b><br/>{plaats}<br/><a href="{website}" target="_blank">Website</a>',
                    'style': {'backgroundColor': 'black', 'color': 'white'}
                }
                deck = pdk.Deck(
                    map_style='light',
                    initial_view_state=pdk.ViewState(
                        latitude=center_lat,
                        longitude=center_lon,
                        zoom=zoom,
                        pitch=0,
                    ),
                    layers=layers,
                    tooltip=tooltip,
                    height=map_height,
                )
                selection = st.pydeck_chart(
                    deck,
                    use_container_width=True,
                    selection_mode='single-object',
                    on_select='rerun',
                    key='club_map',
                )
                _apply_pydeck_club_pick(selection, filtered_reset)

                st.caption('Klik op een punt om die club te selecteren; clubgegevens openen automatisch.')
                if invalid_count:
                    st.warning(f'{invalid_count} club(s) buiten het NL-venster worden niet getoond.')

    selected_id = st.session_state.selected_club_id
    selected = next((item for item in rows if item['_id'] == selected_id), None) if selected_id is not None else None

    st.subheader('Clubgegevens')
    with st.expander(
        'Details, contact & bewerken',
        expanded=selected is not None,
    ):
        if not selected:
            st.info('Klik op een rij in de tabel of op een punt op de kaart.')
        else:
            st.markdown(f"### {selected['naam']}")
            st.write('**Provincie:**', selected['provincie'])
            st.write('**Plaats:**', selected['plaats'])
            st.write('**Clublokaal:**', selected['clublokaal'])
            st.write('**Secretariaat:**', selected['secretariaat'])
            st.write('**Website:**', selected['website'])
            if selected['club_url']:
                st.write('**KNDB detailpagina:**', selected['club_url'])
            st.write('**Bond URL:**', selected['bond_url'])
            if selected['emails']:
                show_emails = _clean_emails(selected['emails'])
                if show_emails:
                    st.write('**E-mail(s):**', ', '.join(show_emails))
                else:
                    st.caption(
                        'E-mail(s) in de database zijn nog verborgen (bijv. “[email protected]”). '
                        'Draai `python3 scraper.py` opnieuw om Cloudflare-e-mails te decoderen.'
                    )
            if selected['telefoons']:
                st.write('**Telefoon(s):**', ', '.join(selected['telefoons']))
            if selected['logo']:
                st.image(selected['logo'], width=180)

            locaties = selected['details'].get('locaties', [])
            if locaties:
                st.subheader('Clublocaties')
                for locatie in locaties:
                    st.write('•', locatie.get('naam', ''))
                    st.write('  - Adres:', locatie.get('adres', ''))
                    if locatie.get('postcode'):
                        st.write('  - Postcode:', locatie.get('postcode'))
                    if locatie.get('woonplaats'):
                        st.write('  - Woonplaats:', locatie.get('woonplaats'))
                    if locatie.get('telefoon'):
                        st.write('  - Telefoon:', locatie.get('telefoon'))

            contactpersonen = selected['details'].get('contactpersonen', [])
            if contactpersonen:
                st.subheader('Contactpersonen')
                for contact in contactpersonen:
                    st.write('•', contact.get('functie', ''))
                    st.write('  - Naam:', contact.get('naam', ''))
                    if contact.get('adres'):
                        st.write('  - Adres:', contact.get('adres'))
                    if contact.get('woonplaats'):
                        st.write('  - Woonplaats:', contact.get('woonplaats'))
                    if contact.get('telefoon'):
                        st.write('  - Telefoon:', contact.get('telefoon'))
                    if contact.get('email') and not _is_obfuscated_email(contact.get('email')):
                        st.write('  - E-mail:', contact.get('email'))

            with st.expander('JSON (huidige club)'):
                st.json(json.loads(json.dumps(selected, default=str)))

            st.subheader('Aanpassen / bijwerken')
            with st.form(f"edit_form_{selected['_id']}"):
                naam = st.text_input('Naam', selected['naam'])
                plaats = st.text_input('Plaats', selected['plaats'])
                secretariaat = st.text_input('Secretariaat', selected['secretariaat'])
                clublokaal = st.text_input('Clublokaal', selected['clublokaal'])
                website = st.text_input('Website', selected['website'])
                lat = st.text_input('Latitude', str(selected.get('lat', '') or ''))
                lon = st.text_input('Longitude', str(selected.get('lon', '') or ''))
                details_json = st.text_area('Details JSON', json.dumps(selected['details'], ensure_ascii=False, indent=2), height=240)
                submitted = st.form_submit_button('Opslaan')
                if submitted:
                    try:
                        details = json.loads(details_json)
                        details['website'] = (website or '').strip()
                        update_data = {
                            'naam': naam,
                            'plaats': plaats,
                            'secretariaat': secretariaat,
                            'clublokaal': clublokaal,
                            'details': details,
                            'lat': float(lat) if lat.strip() else None,
                            'lon': float(lon) if lon.strip() else None,
                        }
                        collection.update_one({'_id': selected['_id']}, {'$set': update_data})
                        st.success('Clubgegevens bijgewerkt.')
                        st.rerun()
                    except Exception as exc:
                        st.error(f'Kon niet opslaan: {exc}')

st.set_page_config(page_title='Clubs & scholen', layout='wide')
_ensure_viewer_password()

try:
    client.admin.command('ping')
except Exception as exc:
    st.error(f'MongoDB niet bereikbaar (controleer MONGO_URI en netwerk): {exc}')
    st.stop()

st.title('Clubs & scholen')
if _viewer_password_from_config() and st.session_state.get('_viewer_auth_ok'):
    if st.sidebar.button('Toegang wissen', help='Verwijder de sessiecode op dit apparaat'):
        st.session_state.pop('_viewer_auth_ok', None)
        st.rerun()
weergave = st.sidebar.radio('Menu', ['Damclubs', 'Scholen'], horizontal=True)
map_height = st.sidebar.slider('Kaart hoogte (pixels)', 300, 900, 520, key='map_height_shared')

if weergave == 'Damclubs':
    render_clubs(map_height)
else:
    render_schools(db, map_height)

