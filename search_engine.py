"""Hledání 2.0: plánovač dotazu (metadata) + sémantické řazení (ai_engine) ve dvou pásmech.

Dotaz se rozloží na fazety, které znají přesnou odpověď z databáze – místo (place_city, místo
události), osobu (pojmenovaná tvář), čas („10. července“, „červenec 2026“, „léto“), typ
(„videa“, „live“), příznak („oblíbené“) – a na zbytek, který řadí obraz (CLIP). Slova o lidech
(„lidi“, „lidé“, „osoby“) jsou měkká fazeta: fotky s rozpoznanou tváří jsou jisté shody,
ostatní řadí obraz.

Výsledek má dvě pásma: jisté shody a možné shody. Prázdný je jen tehdy, když v knihovně nic
nevyčnívá – pak to UI řekne rovnou („Nic neodpovídá …“).
"""
import calendar
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta

from database import get_connection

# ── normalizace ───────────────────────────────────────────────────────────────

def fold(text):
    """Malá písmena bez diakritiky a apostrofů ('Horažďovice' -> 'horazdovice')."""
    text = unicodedata.normalize("NFD", str(text or ""))
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return text.lower().replace("'", "").replace("’", "")


def tokens(text):
    return re.findall(r"[0-9a-z]+", fold(text))


def _same_word(q, c):
    """Česká skloňovaná shoda: 'kasperskych' ~ 'kasperske', 'rude' ~ 'ruda', 'hor' ~ 'hory'.
    Společný začátek musí pokrýt skoro celé obě slova – 'hory' NENÍ 'horazdovice'."""
    if q == c:
        return True
    n = 0
    for a, b in zip(q, c):
        if a != b:
            break
        n += 1
    return n >= 3 and n >= len(c) - 3 and n >= len(q) - 3


# ── slovník fazet ─────────────────────────────────────────────────────────────

MONTHS = {
    1: ("leden", "ledna", "lednu"), 2: ("unor", "unora", "unoru"), 3: ("brezen", "brezna", "breznu"),
    4: ("duben", "dubna", "dubnu"), 5: ("kveten", "kvetna", "kvetnu"), 6: ("cerven", "cervna", "cervnu"),
    7: ("cervenec", "cervence", "cervenci"), 8: ("srpen", "srpna", "srpnu"), 9: ("zari",),
    10: ("rijen", "rijna", "rijnu"), 11: ("listopad", "listopadu"), 12: ("prosinec", "prosince", "prosinci"),
}
MONTH_WORDS = {w: m for m, forms in MONTHS.items() for w in forms}
SEASONS = {"jaro": (3, 4, 5), "jare": (3, 4, 5), "leto": (6, 7, 8), "lete": (6, 7, 8), "letni": (6, 7, 8),
           "podzim": (9, 10, 11), "podzimni": (9, 10, 11), "zima": (12, 1, 2), "zime": (12, 1, 2),
           "zimni": (12, 1, 2)}
TYPE_WORDS = {"video": "video", "videa": "video", "videi": "video", "videich": "video", "videu": "video",
              "videos": "video", "klip": "video", "klipy": "video", "live": "live_photo"}
FAVORITE_WORDS = {"oblibene", "oblibena", "oblibeny", "oblibenych", "favorites", "favourites", "srdicko"}
PEOPLE_WORDS = {"lidi", "lide", "lidmi", "lidech", "lidi", "clovek", "cloveka", "osoba", "osoby", "osob",
                "people", "person", "persons"}
# spojky / předložky, které samy nic nehledají (zbytek dotazu z nich nevznikne)
FILLER = {"a", "i", "s", "se", "z", "ze", "v", "ve", "na", "u", "do", "od", "po", "pro", "k", "ke", "the", "of",
          "in", "at", "fotky", "fotka", "fotek", "fotografie", "snimky", "vsechny", "moje", "rok", "roku"}


@dataclass
class Item:
    id: int
    city: str          # složené (fold) jméno místa, '' když není
    event_place: str
    day: str           # 'YYYY-MM-DD' nebo ''
    media_type: str
    favorite: bool
    has_face: bool
    person_ids: frozenset


@dataclass
class Vocab:
    """Co knihovna zná: místa (fold -> zobrazené jméno), osoby (fold jméno -> id) a položky."""
    places: dict
    people: dict
    items: dict        # id -> Item
    ocr: dict = field(default_factory=dict)    # id -> tuple složených slov textu na fotce (Sprint 3)


def load_vocab(conn=None):
    own = conn is None
    conn = conn or get_connection()
    try:
        rows = conn.execute(f"""
            SELECT m.id, COALESCE(m.place_city, ''), COALESCE(e.location_name, ''),
                   replace(substr(COALESCE(m.date_taken, ''), 1, 10), ':', '-'),
                   m.media_type, COALESCE(m.is_favorite, 0)
            FROM media_items m LEFT JOIN events e ON e.id = m.event_id
        """).fetchall()          # i koš: hledá se v aktuální kolekci, kterou určuje universe
        faces = {}
        for media_id, person_id in conn.execute(
                "SELECT media_id, person_id FROM face_instances WHERE COALESCE(is_valid, 1) = 1"):
            faces.setdefault(media_id, set())
            if person_id is not None:
                faces[media_id].add(person_id)
        ocr = {}
        try:
            for mid, text in conn.execute("SELECT media_id, text FROM media_ocr WHERE text != ''"):
                ocr[mid] = tuple(tokens(text))
        except Exception:                        # starší DB bez tabulky
            ocr = {}
        people = {}
        for pid, name in conn.execute("SELECT id, name FROM people"):
            if name and fold(name) not in ("neznama osoba", "unknown"):
                people.setdefault(fold(name), set()).add(pid)
    finally:
        if own:
            conn.close()
    items, places = {}, {}
    for mid, city, ev_place, day, mtype, fav in rows:
        for name in (city, ev_place):
            if name:
                places.setdefault(fold(name), name)
        items[mid] = Item(mid, fold(city), fold(ev_place), day if len(day) == 10 else "", mtype or "",
                          bool(fav), mid in faces, frozenset(faces.get(mid, ())))
    return Vocab(places, people, items, ocr)


def ocr_matches(text, vocab, pool):
    """Položky poolu, na kterých je VŠECHNA obsahová slova dotazu vidět jako text (OCR) –
    se skloňováním (_same_word) a od 3 písmen; pořadí poolu (mřížky). [] když dotaz nemá slova."""
    words = [w for w in tokens(text) if w not in FILLER and len(w) >= 3]
    if not words or not vocab.ocr:
        return []
    out = []
    for mid in pool:
        toks = vocab.ocr.get(mid)
        if toks and all(any(_same_word(w, t) or (len(w) >= 4 and t.startswith(w)) for t in toks) for w in words):
            out.append(mid)
    return out


# ── plán dotazu ───────────────────────────────────────────────────────────────

@dataclass
class Plan:
    places: list = field(default_factory=list)        # složená jména míst
    person_ids: set = field(default_factory=set)
    person_names: list = field(default_factory=list)
    days: set = None                                  # konkrétní dny 'YYYY-MM-DD'
    months: set = None                                # čísla měsíců
    years: set = None
    media_types: set = field(default_factory=set)
    favorites: bool = False
    people: bool = False                              # měkká fazeta „lidé“
    text: str = ""                                    # zbytek pro obraz
    labels: list = field(default_factory=list)        # popis fazet pro UI

    def has_hard_facets(self):
        return bool(self.places or self.person_ids or self.days or self.months or self.years
                    or self.media_types or self.favorites)


def _match_phrase(toks, i, name_toks):
    """Kolik tokenů od i pokrývá víceslovné jméno (0 = neshoda)."""
    if i + len(name_toks) > len(toks):
        return 0
    return len(name_toks) if all(_same_word(toks[i + k], name_toks[k]) for k in range(len(name_toks))) else 0


def plan_query(query, vocab, today=None):
    today = today or date.today()
    raw = str(query or "").strip()
    toks = tokens(raw)
    plan, used = Plan(), [False] * len(toks)
    place_names = sorted(((fold_name, tokens(fold_name)) for fold_name in vocab.places),
                         key=lambda x: -len(x[1]))
    person_names = sorted(((n, tokens(n)) for n in vocab.people), key=lambda x: -len(x[1]))
    i = 0
    while i < len(toks):
        t = toks[i]
        hit = 0
        for fold_name, nt in place_names:                      # víceslovná místa první
            hit = _match_phrase(toks, i, nt)
            if hit:
                if fold_name not in plan.places:
                    plan.places.append(fold_name)
                    plan.labels.append(vocab.places[fold_name])
                break
        if not hit:
            for name, nt in person_names:
                hit = _match_phrase(toks, i, nt)
                if hit:
                    plan.person_ids |= vocab.people[name]
                    plan.person_names.append(name)
                    plan.labels.append(name.title())
                    break
        if hit:
            for k in range(hit):
                used[i + k] = True
            i += hit
            continue
        day_hit = _day_at(toks, i, today)
        if day_hit:
            n, d = day_hit
            plan.days = (plan.days or set()) | {d}
            plan.labels.append(f"{int(d[8:])}. {int(d[5:7])}.")
            for k in range(n):
                used[i + k] = True
            i += n
            continue
        if t in MONTH_WORDS:
            plan.months = (plan.months or set()) | {MONTH_WORDS[t]}
            plan.labels.append(MONTHS[MONTH_WORDS[t]][0])
        elif t in SEASONS:
            plan.months = (plan.months or set()) | set(SEASONS[t])
            plan.labels.append(t)
        elif re.fullmatch(r"(19|20)\d\d", t):
            plan.years = (plan.years or set()) | {int(t)}
            plan.labels.append(t)
        elif t in ("dnes", "dneska", "today"):
            plan.days = (plan.days or set()) | {today.isoformat()}
            plan.labels.append("dnes")
        elif t in ("vcera", "yesterday"):
            plan.days = (plan.days or set()) | {(today - timedelta(days=1)).isoformat()}
            plan.labels.append("včera")
        elif t in TYPE_WORDS:
            plan.media_types.add(TYPE_WORDS[t])
            plan.labels.append("videa" if TYPE_WORDS[t] == "video" else "Live Photos")
        elif t in FAVORITE_WORDS:
            plan.favorites = True
            plan.labels.append("oblíbené")
        elif t in PEOPLE_WORDS:
            plan.people = True
        else:
            i += 1
            continue
        used[i] = True
        i += 1
    rest = [t for t, u in zip(toks, used) if not u]
    if any(t not in FILLER for t in rest):
        plan.text = _remainder(raw, rest)
    return plan


def _day_at(toks, i, today):
    """'10 cervence [2026]' / '10 7 [2026]' -> (počet tokenů, 'YYYY-MM-DD')."""
    if i + 1 >= len(toks) or not toks[i].isdigit():
        return None
    d = int(toks[i])
    nxt = toks[i + 1]
    month = MONTH_WORDS.get(nxt) or (int(nxt) if nxt.isdigit() and 1 <= int(nxt) <= 12 else None)
    if not month or not 1 <= d <= 31:
        return None
    n, year = 2, None
    if i + 2 < len(toks) and re.fullmatch(r"(19|20)\d\d", toks[i + 2]):
        n, year = 3, int(toks[i + 2])
    if year is None:
        year = today.year if (month, d) <= (today.month, today.day) else today.year - 1
    if d > calendar.monthrange(year, month)[1]:
        return None
    return n, f"{year:04d}-{month:02d}-{d:02d}"


def _remainder(raw, rest_tokens):
    """Zbytek dotazu pro obraz v PŮVODNÍM zápisu (s diakritikou) – model ji potřebuje."""
    words = re.findall(r"[^\W_]+", raw, re.UNICODE)
    folded = [fold(w) for w in words]
    want = list(rest_tokens)
    out = []
    for w, f in zip(words, folded):
        if want and f == want[0]:
            out.append(w)
            want.pop(0)
    return " ".join(out)


def facet_filter(plan, vocab, universe):
    """(přesné, volné) id z universe, která splní všechny tvrdé fazety plánu. Volné = místo
    sedí jen přes název události (fotka z výletu „Kašperské Hory“ pořízená v Hartmanicích)."""
    out, loose = [], []
    for mid in universe:
        it = vocab.items.get(mid)
        if it is None:
            continue
        in_city = not plan.places or any(p == it.city for p in plan.places)
        if not in_city and not any(p == it.event_place for p in plan.places):
            continue
        if plan.person_ids and not (plan.person_ids & it.person_ids):
            continue
        if plan.media_types and it.media_type not in plan.media_types:
            continue
        if plan.favorites and not it.favorite:
            continue
        if plan.days and it.day not in plan.days:
            continue
        if plan.months and (not it.day or int(it.day[5:7]) not in plan.months):
            continue
        if plan.years and (not it.day or int(it.day[:4]) not in plan.years):
            continue
        (out if in_city else loose).append(mid)
    return out, loose


# ── výsledek ──────────────────────────────────────────────────────────────────

@dataclass
class SearchResult:
    query: str
    confident: list           # id od nejlepší
    possible: list
    labels: list              # rozpoznané fazety („Kasperske Hory“, „videa“)
    semantic: bool            # řadil obraz (model)

    @property
    def ids(self):
        return self.confident + self.possible

    def is_empty(self):
        return not self.confident and not self.possible


def run_search(query, universe, embeddings, vocab=None, semantic_fn=None, today=None):
    """Hlavní vstup (běží v pracovním vlákně). universe = id aktuální kolekce v pořadí mřížky
    (knihovna / oblíbené / videa / koš), embeddings = {id: bytes}. semantic_fn(text, ids,
    embeddings, face_ids) -> (confident, possible) – výchozí ai_engine.rank_semantic."""
    query = str(query or "").strip()
    universe = list(universe or [])
    if not query or not universe:
        return SearchResult(query, [], [], [], False)
    vocab = vocab or load_vocab()
    plan = plan_query(query, vocab, today=today)
    if plan.has_hard_facets():
        strict, loose = facet_filter(plan, vocab, universe)
    else:
        strict, loose = list(universe), []
    pool = strict + loose
    face_ids = {mid for mid in pool if vocab.items.get(mid) and vocab.items[mid].has_face}
    labels = list(plan.labels)

    if not plan.text and not plan.people:
        # jen fazety: přesná odpověď z databáze v pořadí mřížky; jen přes název výletu = možné
        if plan.has_hard_facets():
            return SearchResult(query, strict, loose, labels, False)
        return SearchResult(query, [], [], labels, False)

    if semantic_fn is None:
        from ai_engine import rank_semantic as semantic_fn
    text = plan.text
    if plan.people:
        labels.append("lidé")
        text = f"lidé {text}".strip() if text else "lidé"
    confident, possible = semantic_fn(text, pool, embeddings, face_ids if plan.people else None)
    if plan.text:
        # Sprint 3: text na fotce (cedule, dokument) = jistá shoda, před obrazovými
        read = ocr_matches(plan.text, vocab, pool)
        if read:
            labels.append("text na fotce")
            seen = set(read)
            confident = read + [m for m in confident if m not in seen]
            possible = [m for m in possible if m not in seen]
    if plan.people and not plan.text:
        # „lidi“: rozpoznaná tvář = jistá shoda (řazená podle obrazu), zbytek jen možný
        ranked = confident + possible + [m for m in pool if m in face_ids]
        seen, conf = set(), []
        for m in ranked:
            if m in face_ids and m not in seen:
                seen.add(m)
                conf.append(m)
        poss = [m for m in confident + possible if m not in seen]
        confident, possible = conf, poss
    if loose:
        # místo jen přes název výletu: nanejvýš možná shoda
        loose_set = set(loose)
        possible = [m for m in confident if m in loose_set] + possible
        confident = [m for m in confident if m not in loose_set]
    return SearchResult(query, confident, possible, labels, True)
