"""Česko-anglický slovník běžných pojmů z fotek pro rozšíření dotazu.

Vícejazyčný textový model zná řídká česká slova hůř než anglická a krátká slova si plete
s jinými jazyky („les“ = francouzský člen). Dotaz se proto do souboru (ensemble) vektorů
přidá i v angličtině. Klíče jsou základní tvary bez diakritiky; skloňované tvary se najdou
porovnáním kmene (search_engine._same_word): „lesy“, „lesem“, „jezera“, „mraků“ …
Slovník je obecný (příroda, lidé, místa, věci, činnosti, počasí) – ne seznam testovacích dotazů.
"""

GLOSSARY = {
    # příroda a krajina
    "les": "forest", "prales": "primeval forest", "strom": "tree", "stromy": "trees", "jehlican": "conifer tree",
    "smrk": "spruce tree", "borovice": "pine tree", "briza": "birch tree", "dub": "oak tree", "list": "leaves",
    "tráva": "grass", "trava": "grass", "louka": "meadow", "pole": "field", "pastvina": "pasture",
    "kvetina": "flower", "kvetiny": "flowers", "kytka": "flower", "kytky": "flowers", "ruze": "rose",
    "slunecnice": "sunflower", "houba": "mushroom", "houby": "mushrooms", "mech": "moss", "kapradi": "fern",
    "hora": "mountain", "hory": "mountains", "kopec": "hill", "kopce": "hills", "vrchol": "mountain peak",
    "udoli": "valley", "skala": "rock", "skaly": "rocks", "kamen": "stone", "kameny": "stones",
    "jeskyne": "cave", "sopka": "volcano", "pisek": "sand", "poust": "desert", "ostrov": "island",
    "voda": "water", "jezero": "lake", "rybnik": "pond", "reka": "river", "potok": "stream creek",
    "vodopad": "waterfall", "more": "sea", "ocean": "ocean", "plaz": "beach", "pobrezi": "coast",
    "vlny": "waves", "bazen": "swimming pool", "prehrada": "dam reservoir", "bazina": "swamp",
    "raseliniste": "peat bog", "krajina": "landscape", "vyhled": "scenic view", "panorama": "panorama",
    "priroda": "nature", "zahrada": "garden", "park": "park",
    # nebe a počasí
    "nebe": "sky", "obloha": "sky", "mrak": "cloud", "mraky": "clouds", "oblaka": "clouds",
    "slunce": "sun", "slunecno": "sunny day", "zapad": "sunset", "vychod": "sunrise",
    "duha": "rainbow", "dest": "rain", "bourka": "thunderstorm", "mlha": "fog", "snih": "snow",
    "led": "ice", "zima": "winter", "noc": "night", "hvezdy": "stars", "mesic": "moon", "tma": "darkness",
    # lidé
    "clovek": "person", "lide": "people", "lidi": "people", "muz": "man", "zena": "woman",
    "dite": "child", "deti": "children", "miminko": "baby", "kluk": "boy", "holka": "girl",
    "kamaradi": "friends", "pratele": "friends", "rodina": "family", "dav": "crowd",
    "oblicej": "face", "portret": "portrait", "selfie": "selfie",
    "skupina": "group of people", "skupinova": "group photo", "usmev": "smile", "polibek": "kiss",
    "objeti": "hug", "ruka": "hand", "ruce": "hands", "nohy": "legs", "vlasy": "hair",
    # stavby a místa
    "hrad": "castle", "zamek": "chateau castle", "zricenina": "castle ruin", "vez": "tower",
    "rozhledna": "observation tower", "kostel": "church", "katedrala": "cathedral", "klaster": "monastery",
    "most": "bridge", "lavka": "footbridge boardwalk", "dum": "house", "chata": "cottage cabin",
    "chalupa": "cottage", "budova": "building", "mesto": "city", "vesnice": "village", "namesti": "town square",
    "ulice": "street", "silnice": "road", "cesta": "path road", "stezka": "trail path", "chodnik": "sidewalk",
    "dalnice": "highway", "parkoviste": "parking lot", "nadrazi": "train station", "letiste": "airport",
    "pristav": "harbor", "muzeum": "museum", "galerie": "gallery", "obchod": "shop", "restaurace": "restaurant",
    "kavarna": "cafe", "hospoda": "pub", "hotel": "hotel", "pokoj": "room", "loznice": "bedroom",
    "kuchyne": "kitchen", "koupelna": "bathroom", "obyvak": "living room", "okno": "window", "dvere": "door",
    "schody": "stairs", "zabradli": "railing", "plot": "fence", "zed": "wall", "strecha": "roof",
    "skola": "school", "kancelar": "office", "nemocnice": "hospital", "stadion": "stadium", "hriste": "playground",
    "kemp": "campsite", "stan": "tent", "ohniste": "campfire",
    # věci
    "auto": "car", "auta": "cars", "autobus": "bus", "vlak": "train", "tramvaj": "tram", "kolo": "bicycle",
    "motorka": "motorcycle", "letadlo": "airplane", "lod": "boat ship", "clun": "small boat", "lodka": "boat",
    "postel": "bed", "stul": "table", "zidle": "chair", "gauc": "sofa couch", "lavicka": "bench",
    "televize": "television", "pocitac": "computer", "notebook": "laptop", "telefon": "phone", "mobil": "mobile phone",
    "kniha": "book", "papir": "paper", "dokument": "document", "text": "text", "mapa": "map", "cedule": "sign",
    "obrazovka": "screen", "snimek": "screenshot", "lahev": "bottle", "flaska": "bottle", "sklenice": "glass",
    "hrnek": "mug", "batoh": "backpack", "taska": "bag", "klobouk": "hat", "cepice": "cap",
    "bryle": "glasses", "boty": "shoes", "obleceni": "clothes", "tricko": "t-shirt",
    "saty": "dress", "plavky": "swimsuit", "hracka": "toy", "balon": "balloon", "mic": "ball",
    "kytara": "guitar", "klavir": "piano", "gril": "grill barbecue", "dort": "cake", "hodinky": "watch",
    "penize": "money", "zbran": "weapon", "drevo": "wood", "kov": "metal", "sklo": "glass", "plast": "plastic",
    "pingpong": "table tennis", "ping": "table tennis ping pong", "stolni": "table tennis",
    # jídlo a pití
    "jidlo": "food", "obed": "lunch", "vecere": "dinner", "snidane": "breakfast", "pizza": "pizza",
    "burger": "burger", "zmrzlina": "ice cream", "ovoce": "fruit", "jablko": "apple", "zelenina": "vegetables",
    "chleba": "bread", "maso": "meat", "ryba": "fish", "kava": "coffee", "caj": "tea", "pivo": "beer",
    "vino": "wine", "napoj": "drink", "piti": "drink",
    # zvířata
    "zvire": "animal", "zvirata": "animals", "pes": "dog", "psi": "dogs", "kocka": "cat", "kone": "horses",
    "kun": "horse", "krava": "cow", "ovce": "sheep", "koza": "goat", "prase": "pig", "slepice": "chicken",
    "ptak": "bird", "ptaci": "birds", "kachna": "duck", "labut": "swan", "ryby": "fish", "hmyz": "insect",
    "motyl": "butterfly", "vcela": "bee", "kobylka": "grasshopper", "pavouk": "spider", "had": "snake",
    "zaba": "frog", "jelen": "deer", "srna": "roe deer", "veverka": "squirrel", "medved": "bear",
    "slon": "elephant", "lev": "lion", "opice": "monkey",
    # činnosti a události
    "vylet": "trip", "tura": "hike", "turistika": "hiking", "chuze": "walking", "prochazka": "walk",
    "plavani": "swimming", "koupani": "bathing swimming", "lyzovani": "skiing", "sport": "sport",
    "fotbal": "football soccer", "tenis": "tennis", "beh": "running", "cyklistika": "cycling",
    "kempovani": "camping", "piknik": "picnic", "oslava": "celebration party", "narozeniny": "birthday",
    "svatba": "wedding", "vanoce": "christmas", "koncert": "concert", "hudba": "music", "tanec": "dance",
    "hra": "game", "vecirek": "party", "festival": "festival", "jizda": "ride",
}


def english_for(words, fold, same_word):
    """Anglické protějšky slov dotazu (skloňované tvary přes same_word), nebo ''."""
    out = []
    keys = list(GLOSSARY)
    for w in words:
        f = fold(w)
        hit = GLOSSARY.get(f) or next((GLOSSARY[k] for k in keys if len(k) >= 3 and same_word(f, k)), None)
        if hit and hit not in out:
            out.append(hit)
    return " ".join(out)
