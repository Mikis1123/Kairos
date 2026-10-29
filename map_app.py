import sys
import os
import time
import webview
import win32gui
import win32con
from database import get_connection

def generate_map_html(center_lat, center_lon, zoom=12):
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT id, latitude, longitude, file_name, place_city, date_taken, thumbnail_path 
        FROM media_items 
        WHERE latitude IS NOT NULL
    """)
    rows = c.fetchall()
    conn.close()

    markers_js = []
    for r in rows:
        m_id, lat, lon, name, city, dt, thumb = r[0], r[1], r[2], r[3], r[4] or "Místo", r[5] or "", r[6]
        thumb_url = f"file:///{thumb.replace(os.sep, '/')}" if thumb and os.path.exists(thumb) else ""
        img_tag = f"<img src='{thumb_url}' style='width:130px;height:130px;object-fit:cover;border-radius:10px;margin-bottom:6px;'><br>" if thumb_url else ""

        markers_js.append(f"""
            var marker = L.marker([{lat}, {lon}]).addTo(map);
            marker.bindPopup("<div style='text-align:center;'>{img_tag}<b style='font-size:13px;'>{name}</b><br><span style='color:#8E8E93;'>{city}</span><br><small style='color:#636366;'>{dt}</small></div>");
        """)

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8" />
        <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
        <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
        <style>
            body, html {{ margin: 0; padding: 0; height: 100%; width: 100%; background: #0E0E10; font-family: -apple-system, sans-serif; }}
            #map {{ width: 100%; height: 100%; }}
            /* Apple Dark Mode mapový filtr bez API klíčů */
            .leaflet-tile {{
                filter: brightness(0.6) invert(1) contrast(3) hue-rotate(200deg) saturate(0.3) brightness(0.7);
            }}
            .leaflet-container {{ background: #0E0E10 !important; }}
            .leaflet-popup-content-wrapper {{
                background: #1C1C1E;
                color: #FFFFFF;
                border-radius: 12px;
                border: 1px solid #2C2C2E;
                box-shadow: 0 8px 24px rgba(0,0,0,0.6);
            }}
            .leaflet-popup-tip {{ background: #1C1C1E; }}
        </style>
    </head>
    <body>
        <div id="map"></div>
        <script>
            var map = L.map('map', {{ zoomControl: false }}).setView([{center_lat}, {center_lon}], {zoom});
            L.control.zoom({{ position: 'bottomright' }}).addTo(map);

            L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
                maxZoom: 19
            }}).addTo(map);

            {''.join(markers_js)}
        </script>
    </body>
    </html>
    """

def embed_into_parent(parent_hwnd):
    time.sleep(0.4)
    def enum_windows_cb(hwnd, extra):
        if win32gui.IsWindowVisible(hwnd) and "OmniMapEmbed" in win32gui.GetWindowText(hwnd):
            extra.append(hwnd)
        return True

    found = []
    win32gui.EnumWindows(enum_windows_cb, found)
    if found and parent_hwnd:
        target_hwnd = found[0]
        win32gui.SetParent(target_hwnd, parent_hwnd)
        style = win32gui.GetWindowLong(target_hwnd, win32con.GWL_STYLE)
        style &= ~win32con.WS_POPUP
        style |= win32con.WS_CHILD
        style &= ~win32con.WS_CAPTION
        style &= ~win32con.WS_THICKFRAME
        win32gui.SetWindowLong(target_hwnd, win32con.GWL_STYLE, style)

        rect = win32gui.GetClientRect(parent_hwnd)
        win32gui.MoveWindow(target_hwnd, 0, 0, rect[2], rect[3], True)

if __name__ == "__main__":
    lat = float(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] != "None" else 49.8
    lon = float(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] != "None" else 15.5
    zoom = int(sys.argv[3]) if len(sys.argv) > 3 else 11
    parent_h = int(sys.argv[4]) if len(sys.argv) > 4 else None

    html = generate_map_html(lat, lon, zoom)
    map_file = os.path.join(os.getcwd(), "map_embedded.html")
    with open(map_file, "w", encoding="utf-8") as f:
        f.write(html)

    import threading
    if parent_h:
        threading.Thread(target=embed_into_parent, args=(parent_h,), daemon=True).start()

    window = webview.create_window(
        'OmniMapEmbed',
        url=f"file:///{map_file.replace(os.sep, '/')}",
        frameless=bool(parent_h),
        background_color='#0E0E10'
    )
    webview.start(gui='edgechromium')