from datetime import datetime
import pandas as pd
import streamlit as st
import folium
from streamlit_folium import st_folium

from scraper import scrape


def _terminal_html(lines: list[str]) -> str:
    content = "\n".join(lines[-200:])
    return (
        "<div style='"
        "background:#0d0d0d;color:#39ff14;font-family:monospace;font-size:12px;"
        "padding:12px;border-radius:6px;height:280px;overflow-y:auto;"
        "white-space:pre-wrap;border:1px solid #333;'>"
        f"{content}"
        "<div id='term-end'></div>"
        "</div>"
        "<script>document.getElementById('term-end').scrollIntoView();</script>"
    )

CITY_KECAMATAN = {
    "Jakarta": [
        # Pusat
        "Gambir", "Sawah Besar", "Kemayoran", "Senen", "Cempaka Putih", "Menteng", "Tanah Abang", "Johar Baru",
        # Utara
        "Penjaringan", "Pademangan", "Tanjung Priok", "Koja", "Cilincing", "Kelapa Gading",
        # Barat
        "Cengkareng", "Grogol Petamburan", "Taman Sari", "Tambora", "Kebon Jeruk", "Kali Deres", "Palmerah", "Kembangan",
        # Selatan
        "Kebayoran Baru", "Kebayoran Lama", "Pesanggrahan", "Cilandak", "Pasar Minggu", "Jagakarsa",
        "Mampang Prapatan", "Pancoran", "Tebet", "Setiabudi",
        # Timur
        "Matraman", "Jatinegara", "Kramat Jati", "Makasar", "Pasar Rebo", "Ciracas",
        "Cipayung", "Pulo Gadung", "Duren Sawit", "Cakung",
    ],
    "Bogor": [
        "Bogor Utara", "Bogor Selatan", "Bogor Timur", "Bogor Barat", "Bogor Tengah", "Tanah Sareal",
    ],
    "Depok": [
        "Beji", "Bojongsari", "Cilodong", "Cimanggis", "Cinere", "Cipayung",
        "Limo", "Pancoran Mas", "Sawangan", "Sukmajaya", "Tapos",
    ],
    "Tangerang": [
        # Kota Tangerang
        "Batuceper", "Benda", "Cibodas", "Ciledug", "Cipondoh", "Jatiuwung",
        "Karang Tengah", "Karawaci", "Larangan", "Neglasari", "Periuk", "Pinang", "Tangerang",
        # Tangerang Selatan
        "Ciputat", "Ciputat Timur", "Pamulang", "Pondok Aren", "Serpong", "Serpong Utara", "Setu",
    ],
    "Bekasi": [
        # Kota Bekasi
        "Bantargebang", "Bekasi Barat", "Bekasi Selatan", "Bekasi Timur", "Bekasi Utara",
        "Jatiasih", "Jatisampurna", "Medansatria", "Mustikajaya", "Pondokgede", "Pondokmelati", "Rawalumbu",
        # Kabupaten Bekasi
        "Babelan", "Bojongmangu", "Cabangbungin", "Cibarusah", "Cibitung",
        "Cikarang Barat", "Cikarang Pusat", "Cikarang Selatan", "Cikarang Timur", "Cikarang Utara",
        "Karangbahagia", "Kedungwaringin", "Lemahabang", "Muaragembong", "Pebayuran",
        "Serang Baru", "Sukatani", "Sukawangi", "Tambun Selatan", "Tambun Utara", "Tarumajaya",
    ],
}

st.set_page_config(page_title="Google Maps Scraper", page_icon="📍", layout="wide")
st.title("📍 Google Maps Scraper")
st.caption("Search by keyword and kecamatan across Jabodetabek.")

# --- Sidebar ---
with st.sidebar:
    st.header("Search")
    query = st.text_input("Search keyword", placeholder="e.g. toko plastik")

    city = st.selectbox("City", list(CITY_KECAMATAN.keys()))

    all_kecamatan = CITY_KECAMATAN[city]
    select_all = st.checkbox("Select all kecamatan", value=True)
    selected_kecamatan = st.multiselect(
        "Kecamatan",
        options=all_kecamatan,
        default=all_kecamatan if select_all else [],
    )

    run = st.button(
        "Scrape",
        type="primary",
        disabled=not query or not selected_kecamatan,
    )

# --- Session state ---
if "places" not in st.session_state:
    st.session_state.places = []
if "log_lines" not in st.session_state:
    st.session_state.log_lines = []

# --- Terminal window (always visible once populated) ---
terminal_placeholder = st.empty()
if st.session_state.log_lines:
    terminal_placeholder.markdown(_terminal_html(st.session_state.log_lines), unsafe_allow_html=True)

# --- Run scraper per kecamatan ---
if run and query and selected_kecamatan:
    all_places = []
    seen = set()
    total = len(selected_kecamatan)
    log_lines: list[str] = []
    progress = st.progress(0, text="Starting…")

    def log(msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        log_lines.append(f"[{ts}] {msg}")
        st.session_state.log_lines = log_lines
        terminal_placeholder.markdown(_terminal_html(log_lines), unsafe_allow_html=True)

    for i, kecamatan in enumerate(selected_kecamatan):
        progress.progress(i / total, text=f"Scraping {kecamatan}… ({i + 1}/{total})")
        log(f"▶ [{i + 1}/{total}] {kecamatan}, {city}")
        results = scrape(f"{query} {kecamatan} {city}", headless=True, log_fn=log)
        new = 0
        for p in results:
            key = (p.name.lower().strip(), p.address.lower().strip())
            if key not in seen:
                seen.add(key)
                all_places.append(p)
                new += 1
        log(f"  ✔ {new} new unique places (total so far: {len(all_places)})")

    progress.progress(1.0, text=f"Done — {len(all_places)} unique places found.")
    log(f"━━━ Done. {len(all_places)} total unique places. ━━━")
    st.session_state.places = all_places

    if not all_places:
        st.warning("No results found. Try a different keyword or area.")

places = st.session_state.places

# --- Results ---
if places:
    df = pd.DataFrame([vars(p) for p in places])

    st.subheader(f"Results — {len(places)} places found")
    st.dataframe(df[["name", "address", "phone"]], use_container_width=True, hide_index=True)

    csv_bytes = df.to_csv(index=False).encode("utf-8")
    st.download_button("⬇️ Download CSV", data=csv_bytes, file_name="results.csv", mime="text/csv")

    st.divider()

    # --- Map ---
    valid = [p for p in places if not (p.lat == 0.0 and p.lng == 0.0)]
    if valid:
        st.subheader("Map")
        avg_lat = sum(p.lat for p in valid) / len(valid)
        avg_lng = sum(p.lng for p in valid) / len(valid)

        m = folium.Map(location=[avg_lat, avg_lng], zoom_start=12)
        for p in valid:
            popup_html = f"<b>{p.name}</b><br>{p.address}<br>{p.phone}"
            folium.Marker(
                location=[p.lat, p.lng],
                popup=folium.Popup(popup_html, max_width=250),
                tooltip=p.name,
            ).add_to(m)

        st_folium(m, use_container_width=True, height=550)
    else:
        st.info("No coordinate data available to render a map.")
else:
    st.info("Enter a keyword, pick a city and kecamatan, then click **Scrape**.")
