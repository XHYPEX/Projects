import folium

from models import Place


def generate(places: list[Place], output_path: str) -> None:
    if not places:
        print("No places to map.")
        return

    avg_lat = sum(p.lat for p in places) / len(places)
    avg_lng = sum(p.lng for p in places) / len(places)

    m = folium.Map(location=[avg_lat, avg_lng], zoom_start=13)

    for place in places:
        if place.lat == 0.0 and place.lng == 0.0:
            continue
        popup_html = (
            f"<b>{place.name}</b><br>"
            f"{place.address}<br>"
            f"{place.phone}"
        )
        folium.Marker(
            location=[place.lat, place.lng],
            popup=folium.Popup(popup_html, max_width=250),
            tooltip=place.name,
        ).add_to(m)

    m.save(output_path)
    print(f"Map saved to {output_path}")
