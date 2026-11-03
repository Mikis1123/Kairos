"""Natural regions for trip names: "Šumava" rather than "Kolinec a okolí".

The offline geocoder (reverse_geocoder / GeoNames cities1000) knows towns plus English admin
units, and no mountain ranges, national parks or tourist regions. A trip through Sušice,
Kašperské Hory, Železná Ruda and Frauenau was therefore named after its lodging village.

This is a small curated gazetteer: named natural regions with coarse polygons (a dozen
vertices, accurate to a few km, which is enough for naming), grouped into ranges that cross
borders. Each part carries the name used on its side (Šumava | Bayerischer Wald |
Böhmerwald), plus localized display names for the 7 UI languages.

Naming rule (trips.region_name):
  * the trip must span a landscape: at least REGION_MIN_CITIES distinct towns and
    REGION_MIN_SPREAD_KM from its centroid to its farthest photo;
  * one range must hold at least REGION_MIN_SHARE of the trip's GPS photos;
  * within that range, the side with at least SIDE_MAJORITY of those photos gives the name
    ("Šumava"). Without a clear majority, both sides are named ("Šumava a Bavorský les").
Stored titles keep the canonical Czech name, like the rest of the stored title (see
i18n.event_place); localized() turns it into the UI language.

Coverage: Czechia and the ranges that cross its borders. Adding a region = one entry in RANGES.
"""

REGION_MIN_SHARE = 0.6
REGION_MIN_CITIES = 2
REGION_MIN_SPREAD_KM = 6.0
SIDE_MAJORITY = 0.7

# polygon vertices are (lon, lat)
RANGES = [
    {"id": "bohemian-forest", "parts": [
        {"id": "sumava", "cc": "CZ",
         "names": {"cs": "Šumava", "en": "Šumava", "de": "Böhmerwald", "fr": "Šumava", "es": "Šumava",
                   "ja": "シュマヴァ", "zh-CN": "舒马瓦"},
         # Šumava with Pošumaví foothills (Sušice, Kolinec, Vimperk, Prachatice), Lipno
         "poly": [(13.02, 49.30), (13.20, 49.37), (13.45, 49.37), (13.66, 49.30), (13.82, 49.17), (13.98, 49.08),
                  (14.12, 48.98), (14.25, 48.80), (14.36, 48.62), (14.08, 48.59), (13.83, 48.77), (13.50, 48.95),
                  (13.25, 49.11), (13.06, 49.24)]},
        {"id": "bayerischer-wald", "cc": "DE",
         "names": {"cs": "Bavorský les", "en": "Bavarian Forest", "de": "Bayerischer Wald",
                   "fr": "Forêt bavaroise", "es": "Selva de Baviera", "ja": "バイエルンの森", "zh-CN": "巴伐利亚森林"},
         "poly": [(12.62, 49.22), (12.86, 49.34), (13.06, 49.24), (13.25, 49.11), (13.50, 48.95), (13.83, 48.77),
                  (13.80, 48.62), (13.45, 48.56), (13.10, 48.74), (12.93, 48.82), (12.72, 48.98)]},
        {"id": "boehmerwald-at", "cc": "AT",
         "names": {"cs": "Šumava (Rakousko)", "en": "Bohemian Forest (Austria)", "de": "Böhmerwald",
                   "fr": "Forêt de Bohême (Autriche)", "es": "Selva de Bohemia (Austria)",
                   "ja": "ベーマーヴァルト", "zh-CN": "波希米亚森林（奥地利）"},
         "poly": [(13.80, 48.62), (13.83, 48.77), (14.08, 48.59), (13.95, 48.52), (13.75, 48.52)]},
    ]},
    {"id": "giant-mountains", "parts": [
        {"id": "krkonose", "cc": "CZ",
         "names": {"cs": "Krkonoše", "en": "Giant Mountains", "de": "Riesengebirge", "fr": "Monts des Géants",
                   "es": "Montes de los Gigantes", "ja": "クルコノシェ山地", "zh-CN": "克尔科诺谢山"},
         "poly": [(15.38, 50.62), (15.45, 50.80), (15.75, 50.76), (16.02, 50.67), (15.95, 50.55), (15.60, 50.52)]},
        {"id": "karkonosze", "cc": "PL",
         "names": {"cs": "Krkonoše (Polsko)", "en": "Giant Mountains (Poland)", "de": "Riesengebirge",
                   "fr": "Monts des Géants (Pologne)", "es": "Montes de los Gigantes (Polonia)",
                   "ja": "カルコノシェ山地", "zh-CN": "卡尔科诺谢山"},
         "poly": [(15.45, 50.80), (15.55, 50.90), (15.85, 50.88), (16.02, 50.67), (15.75, 50.76)]},
    ]},
    {"id": "jizera", "parts": [
        {"id": "jizerske-hory", "cc": "CZ",
         "names": {"cs": "Jizerské hory", "en": "Jizera Mountains", "de": "Isergebirge", "fr": "Monts de la Jizera",
                   "es": "Montes Jizera", "ja": "イゼラ山地", "zh-CN": "伊泽拉山"},
         "poly": [(14.95, 50.75), (15.05, 50.92), (15.30, 50.90), (15.45, 50.80), (15.38, 50.70), (15.10, 50.70)]},
    ]},
    {"id": "ore-mountains", "parts": [
        {"id": "krusne-hory", "cc": "CZ",
         "names": {"cs": "Krušné hory", "en": "Ore Mountains", "de": "Erzgebirge", "fr": "Monts Métallifères",
                   "es": "Montes Metálicos", "ja": "クルシュネー山脈", "zh-CN": "克鲁什内山"},
         "poly": [(12.30, 50.28), (12.55, 50.42), (13.00, 50.50), (13.50, 50.70), (13.95, 50.78), (14.00, 50.70),
                  (13.60, 50.58), (13.10, 50.40), (12.65, 50.27)]},
        {"id": "erzgebirge", "cc": "DE",
         "names": {"cs": "Krušné hory (Sasko)", "en": "Ore Mountains (Saxony)", "de": "Erzgebirge",
                   "fr": "Monts Métallifères (Saxe)", "es": "Montes Metálicos (Sajonia)",
                   "ja": "エルツ山地", "zh-CN": "厄尔士山脉"},
         "poly": [(12.30, 50.28), (12.40, 50.55), (12.90, 50.68), (13.50, 50.82), (13.95, 50.78), (13.50, 50.70),
                  (13.00, 50.50), (12.55, 50.42)]},
    ]},
    {"id": "bohemian-switzerland", "parts": [
        {"id": "ceske-svycarsko", "cc": "CZ",
         "names": {"cs": "České Švýcarsko", "en": "Bohemian Switzerland", "de": "Böhmische Schweiz",
                   "fr": "Suisse bohémienne", "es": "Suiza Bohemia", "ja": "チェコ・スイス", "zh-CN": "波希米亚瑞士"},
         "poly": [(14.18, 50.80), (14.22, 50.90), (14.40, 50.96), (14.62, 50.93), (14.58, 50.80), (14.35, 50.76)]},
        {"id": "saechsische-schweiz", "cc": "DE",
         "names": {"cs": "Saské Švýcarsko", "en": "Saxon Switzerland", "de": "Sächsische Schweiz",
                   "fr": "Suisse saxonne", "es": "Suiza Sajona", "ja": "ザクセン・スイス", "zh-CN": "萨克森瑞士"},
         "poly": [(13.90, 50.85), (13.95, 50.98), (14.20, 51.02), (14.40, 50.96), (14.22, 50.90), (14.18, 50.80),
                  (14.00, 50.80)]},
    ]},
    {"id": "bohemian-paradise", "parts": [
        {"id": "cesky-raj", "cc": "CZ",
         "names": {"cs": "Český ráj", "en": "Bohemian Paradise", "de": "Böhmisches Paradies",
                   "fr": "Paradis de Bohême", "es": "Paraíso de Bohemia", "ja": "チェスキー・ラーイ", "zh-CN": "波希米亚天堂"},
         "poly": [(15.00, 50.48), (15.05, 50.62), (15.25, 50.66), (15.42, 50.58), (15.35, 50.42), (15.12, 50.40)]},
    ]},
    {"id": "jeseniky", "parts": [
        {"id": "jeseniky", "cc": "CZ",
         "names": {"cs": "Jeseníky", "en": "Jeseníky Mountains", "de": "Altvatergebirge", "fr": "Jeseníky",
                   "es": "Montes Jeseníky", "ja": "イェセニーキ山地", "zh-CN": "耶塞尼克山"},
         "poly": [(16.85, 50.05), (16.95, 50.30), (17.20, 50.38), (17.50, 50.25), (17.55, 50.00), (17.25, 49.90)]},
    ]},
    {"id": "beskids", "parts": [
        {"id": "beskydy", "cc": "CZ",
         "names": {"cs": "Beskydy", "en": "Beskids", "de": "Beskiden", "fr": "Beskides", "es": "Beskides",
                   "ja": "ベスキディ山地", "zh-CN": "贝斯基德山"},
         "poly": [(17.95, 49.45), (18.05, 49.60), (18.45, 49.65), (18.85, 49.62), (18.90, 49.45), (18.55, 49.38),
                  (18.15, 49.33)]},
    ]},
    {"id": "orlicke", "parts": [
        {"id": "orlicke-hory", "cc": "CZ",
         "names": {"cs": "Orlické hory", "en": "Eagle Mountains", "de": "Adlergebirge", "fr": "Monts de l'Aigle",
                   "es": "Montes del Águila", "ja": "オルリツケー山地", "zh-CN": "奥尔利采山"},
         "poly": [(16.20, 50.15), (16.25, 50.38), (16.45, 50.40), (16.62, 50.20), (16.52, 50.05), (16.30, 50.05)]},
    ]},
    {"id": "novohradske", "parts": [
        {"id": "novohradske-hory", "cc": "CZ",
         "names": {"cs": "Novohradské hory", "en": "Gratzen Mountains", "de": "Gratzener Bergland",
                   "fr": "Monts de Novohrad", "es": "Montes de Novohrad", "ja": "ノヴォフラツケー山地", "zh-CN": "诺沃赫拉德山"},
         "poly": [(14.55, 48.62), (14.60, 48.80), (14.78, 48.84), (14.98, 48.75), (14.92, 48.58), (14.70, 48.55)]},
    ]},
    {"id": "palava", "parts": [
        {"id": "palava", "cc": "CZ",
         "names": {"cs": "Pálava", "en": "Pálava Hills", "de": "Pollauer Berge", "fr": "Pálava", "es": "Pálava",
                   "ja": "パーラヴァ", "zh-CN": "帕拉瓦"},
         "poly": [(16.55, 48.76), (16.58, 48.92), (16.72, 48.94), (16.82, 48.84), (16.75, 48.73), (16.62, 48.72)]},
    ]},
    {"id": "moravian-karst", "parts": [
        {"id": "moravsky-kras", "cc": "CZ",
         "names": {"cs": "Moravský kras", "en": "Moravian Karst", "de": "Mährischer Karst", "fr": "Karst morave",
                   "es": "Karst de Moravia", "ja": "モラヴィア・カルスト", "zh-CN": "摩拉维亚喀斯特"},
         "poly": [(16.62, 49.25), (16.65, 49.45), (16.78, 49.47), (16.85, 49.35), (16.80, 49.22), (16.68, 49.20)]},
    ]},
    {"id": "tatras", "parts": [
        {"id": "tatry", "cc": "SK",
         "names": {"cs": "Tatry", "en": "Tatra Mountains", "de": "Tatra", "fr": "Tatras", "es": "Montes Tatras",
                   "ja": "タトラ山脈", "zh-CN": "塔特拉山"},
         "poly": [(19.55, 49.15), (19.65, 49.25), (19.95, 49.24), (20.20, 49.25), (20.40, 49.18), (20.30, 49.05),
                  (19.90, 49.08), (19.62, 49.08)]},
        {"id": "tatry-pl", "cc": "PL",
         "names": {"cs": "Tatry (Polsko)", "en": "Tatra Mountains (Poland)", "de": "Tatra (Polen)",
                   "fr": "Tatras (Pologne)", "es": "Montes Tatras (Polonia)", "ja": "タトラ山脈（ポーランド）",
                   "zh-CN": "塔特拉山（波兰）"},
         "poly": [(19.65, 49.25), (19.75, 49.35), (20.10, 49.35), (20.20, 49.25), (19.95, 49.24)]},
    ]},
]

_BY_CS = {}
for _r in RANGES:
    for _p in _r["parts"]:
        _BY_CS[_p["names"]["cs"]] = _p
        lons = [v[0] for v in _p["poly"]]
        lats = [v[1] for v in _p["poly"]]
        _p["bbox"] = (min(lons), min(lats), max(lons), max(lats))


def _inside(lon, lat, poly):
    """Ray casting: is (lon, lat) inside the polygon?"""
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-15) + xi:
            inside = not inside
        j = i
    return inside


def part_at(lat, lon):
    """The region part containing the point, or None."""
    for r in RANGES:
        for p in r["parts"]:
            b = p["bbox"]
            if b[0] <= lon <= b[2] and b[1] <= lat <= b[3] and _inside(lon, lat, p["poly"]):
                return r, p
    return None


def classify(points):
    """points: [(lat, lon)] -> (canonical Czech name, share of points in the range) or None.
    The name follows SIDE_MAJORITY (one side, or both sides joined with ' a ')."""
    if not points:
        return None
    by_range, by_part = {}, {}
    for lat, lon in points:
        hit = part_at(lat, lon)
        if not hit:
            continue
        r, p = hit
        by_range[r["id"]] = by_range.get(r["id"], 0) + 1
        by_part.setdefault(r["id"], {})
        by_part[r["id"]][p["id"]] = by_part[r["id"]].get(p["id"], 0) + 1
    if not by_range:
        return None
    rid, n = max(by_range.items(), key=lambda kv: kv[1])
    share = n / len(points)
    if share < REGION_MIN_SHARE:
        return None
    parts = {p["id"]: p for r in RANGES if r["id"] == rid for p in r["parts"]}
    ranked = sorted(by_part[rid].items(), key=lambda kv: -kv[1])
    top_id, top_n = ranked[0]
    if top_n / n >= SIDE_MAJORITY or len(ranked) == 1:
        return parts[top_id]["names"]["cs"], share
    second = parts[ranked[1][0]]["names"]["cs"]
    return f"{parts[top_id]['names']['cs']} a {second}", share   # i18n-ok: stored title (localized())


def localized(name, lang):
    """Canonical (Czech) region name -> UI language; '<A> a <B>' joins both sides. None when the
    name is not a region (a town name stays as it is)."""
    if not name:
        return None
    p = _BY_CS.get(name)
    if p:
        return p["names"].get(lang) or p["names"]["en"]
    if " a " in name:
        a, b = name.split(" a ", 1)
        pa, pb = _BY_CS.get(a), _BY_CS.get(b)
        if pa and pb:
            return (pa["names"].get(lang) or pa["names"]["en"]), (pb["names"].get(lang) or pb["names"]["en"])
    return None
