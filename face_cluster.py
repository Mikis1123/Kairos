"""Přeshlukování obličejů s ochranou uživatelských dat – Patch 2 + Patch 3.

Patch 3 změny:
  * P3.8 – Nastavitelný práh shody (threshold) předávaný z konfigurace.
  * P3.6 – Borderline Review: get_borderline_faces_for_person() přesunuto sem
    (bylo v face_engine.py) a rozšířeno o dynamický rozsah min/max_sim.
  * P3.9 – Dynamická LRU cache centroidů (invalidace při změně DB).
  * Oprava: centroidy se nyní ukládají do modulu-level cache a invalidují se
    při každém volání strict_recluster_faces() (žádné zastaralé centroidy).

Rozdíl proti původní strict_recluster_faces() v main.py:
  * Osoby s ručně zadaným jménem nebo označené jako oblíbené jsou KOTVY: nemažou se,
    nepřevytvářejí a jejich obličeje se nepřepisují. Nové obličeje se k nim mohou jen přidat.
  * Maže a znovu staví se jen automaticky vytvořené ("Neznámá osoba") shluky.
  * Clustering nezávisí na tom, zda ořezy existují na disku (dřív: po smazání cache
    zmizely všechny osoby).
  * Cannot-link: dva obličeje ze stejné fotky nikdy nepatří téže osobě.
  * Obličeje se zpracovávají od nejkvalitnějších (lepší "semínka" shluků).
  * Quality gate (Patch 2.2): příliš malé nebo rozmazané detekce jsou vyřazeny před
    shlukováním, aby neznehodnocovaly centroidy.
  * Centroidy (Patch 2.2): každý shluk udržuje průměrný normalizovaný vektor, který se
    průběžně aktualizuje – žádný naivní pevný práh bez kontextu.
  * Vše v jedné transakci (při chybě rollback), vektorizovaně (numpy matice).
"""
import os
import traceback
import numpy as np
from collections import OrderedDict

from database import get_connection
from paths import resolve_face_path

# --- Výchozí práh shody (P3.8: nastavitelný z konfigurace) ---
SIMILARITY_THRESHOLD = 0.62
UNKNOWN_NAME = "Neznámá osoba"
MIN_MEDIA_PER_AUTO_PERSON = 2

# --- Patch 8: slučovací průchod a návrhy sloučení ---
# Průměrná podobnost všech dvojic obličejů dvou shluků (average linkage). Hladový průchod
# zakládá nový shluk, kdykoli obličej v tu chvíli nedosáhne prahu; dva shluky téže osoby
# (jiné světlo / úhel) se pak už nikdy nespojily -> 44 „osob“ místo ~12.
# Kalibrováno na skutečné knihovně (kontaktní archy každého sloučení): nad ~0.48 se spojují
# stejní lidé (profil, brýle, jiné světlo), pod ~0.475 už začínají splývat podobné, ale různé
# osoby. Chybné sloučení je horší než nesloučení (rozdělit osobu znamená ručně přesouvat
# obličeje), proto konzervativně 0.48 – nejistější páry nabídne „Je to stejná osoba?“.
MERGE_THRESHOLD = 0.48
SUGGEST_MIN_SIMILARITY = 0.30     # „Je to stejná osoba?“ – nabídnout i méně jisté páry
SUGGEST_LIMIT = 12

# --- Quality gate (Patch 2.2) ---
MIN_QUALITY_SCORE = 8.0
MIN_BBOX_RELATIVE = 0.02

# --- P3.9: Modul-level LRU cache centroidů (invaliduje se při reclusteru) ---
_centroid_cache = OrderedDict()
_CENTROID_CACHE_MAX = 256   # max počet osob v cache


def _cache_get_centroid(person_id):
    """Vrátí centroid osoby z cache nebo None."""
    if person_id in _centroid_cache:
        _centroid_cache.move_to_end(person_id)
        return _centroid_cache[person_id]
    return None


def _cache_put_centroid(person_id, centroid):
    """Uloží centroid do LRU cache."""
    _centroid_cache[person_id] = centroid
    _centroid_cache.move_to_end(person_id)
    if len(_centroid_cache) > _CENTROID_CACHE_MAX:
        _centroid_cache.popitem(last=False)


def invalidate_centroid_cache(person_id=None):
    """Invaliduje cache centroidů (celou nebo pro konkrétní osobu)."""
    if person_id is None:
        _centroid_cache.clear()
    else:
        _centroid_cache.pop(person_id, None)


def get_person_centroid(person_id):
    """
    P3.9: Vrátí normalizovaný centroid osoby (průměr všech embeddingů).
    Výsledek je cachován v LRU cache.
    """
    cached = _cache_get_centroid(person_id)
    if cached is not None:
        return cached

    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT embedding FROM face_instances WHERE person_id = ? AND embedding IS NOT NULL "
            "AND COALESCE(is_valid, 1) = 1",
            (person_id,)
        )
        rows = c.fetchall()
    finally:
        conn.close()

    if not rows:
        return None

    vecs = []
    for (blob,) in rows:
        if not blob:
            continue
        arr = np.frombuffer(bytes(blob), dtype=np.float32)
        if arr.size == 0 or not np.all(np.isfinite(arr)):
            continue
        n = np.linalg.norm(arr)
        if n < 1e-6:
            continue
        vecs.append(arr / n)

    if not vecs:
        return None

    centroid = np.mean(np.vstack(vecs), axis=0).astype(np.float32)
    n = np.linalg.norm(centroid)
    if n < 1e-6:
        return None
    centroid = centroid / n
    _cache_put_centroid(person_id, centroid)
    return centroid


def get_borderline_faces_for_person(person_id, min_sim=0.32, max_sim=0.47):
    """
    P3.6 – Borderline Review: Najde dosud nepřiřazené tváře v databázi,
    které se podobají dané osobě v hraničním pásmu jistoty.

    Používá LRU cache centroidů (P3.9) pro rychlost.
    Vrací seznam dict seřazený od nejvyšší podobnosti.
    """
    # Patch 11: podobnost k nejbližšímu PROTOTYPU osoby (ne k jedinému průměru)
    protos = get_person_prototypes(person_id)
    if protos is None or not len(protos):
        return []

    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute(f"""
            SELECT f.id, f.media_id, f.face_crop_path, f.embedding, m.file_name, m.thumbnail_path
            FROM face_instances f
            JOIN media_items m ON f.media_id = m.id
            WHERE f.person_id IS NULL AND f.face_crop_path IS NOT NULL AND f.embedding IS NOT NULL
              AND COALESCE(f.is_valid, 1) = 1
        """)
        unassigned = c.fetchall()
    finally:
        conn.close()

    candidates = []
    for f_id, m_id, crop_path, emb_blob, file_name, thumb_path in unassigned:
        crop_path = resolve_face_path(crop_path)
        if not emb_blob or not crop_path:
            continue
        vec = np.frombuffer(bytes(emb_blob), dtype=np.float32)
        if vec.size == 0 or not np.all(np.isfinite(vec)):
            continue
        n = np.linalg.norm(vec)
        if n < 1e-6:
            continue
        vec = vec / n
        if vec.size != protos.shape[1]:
            continue
        sim = float((protos @ vec).max())
        if min_sim <= sim <= max_sim:
            candidates.append({
                "face_id": f_id,
                "media_id": m_id,
                "crop_path": crop_path,
                "similarity": sim,
                "file_name": file_name,
                "thumb_path": thumb_path,
            })

    candidates.sort(key=lambda x: x["similarity"], reverse=True)
    return candidates


def assign_face_to_person(face_id, person_id):
    """
    P3.3 – Přesun obličeje: přiřadí face_instance k jiné osobě a invaliduje cache.
    Vrací True při úspěchu.
    """
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("UPDATE face_instances SET person_id = ? WHERE id = ?", (person_id, face_id))
        conn.commit()
        invalidate_centroid_cache(person_id)
        return True
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[FaceCluster] Chyba přesunu obličeje: {e}")
        return False
    finally:
        conn.close()


VALID_FACE_SQL = "COALESCE(is_valid, 1) = 1"     # NULL = zatím nekontrolováno -> platný
PROTOTYPE_MAX = 5
PROTOTYPE_MIN_MEMBERS = 3        # prototyp jen z aspoň 3 obličejů (jinak by ho tvořil jeden šum)


def person_prototypes(V, max_k=PROTOTYPE_MAX):
    """Několik reprezentativních tváří osoby (k-medoids na kouli, 1 iterace).

    1. odříznout odlehlé členy (špatně zařazené obličeje nesmí stáhnout identitu jinam),
    2. semínka nejvzdálenějším bodem (pokryjí různé podoby), 3. prototyp = průměr jeho
    přiřazených členů (stabilnější než jedna tvář). Vrací pole k × d normalizovaných vektorů."""
    V = np.asarray(V, dtype=np.float32)
    n = len(V)
    if n == 0:
        return np.zeros((0, V.shape[1] if V.ndim == 2 else 0), np.float32)
    c = V.mean(axis=0)
    c /= (np.linalg.norm(c) + 1e-12)
    sims = V @ c
    if n >= 6:
        keep = sims >= max(float(np.percentile(sims, 15)), float(sims.max()) - 0.45)
        W = V[keep]
    else:
        W = V
    k = int(min(max_k, max(1, len(W) // PROTOTYPE_MIN_MEMBERS)))
    seeds = [int(np.argmax(W @ c))]
    for _ in range(k - 1):
        dist = 1.0 - (W @ W[seeds].T).max(axis=1)
        seeds.append(int(np.argmax(dist)))
    assign = np.argmax(W @ W[seeds].T, axis=1)
    protos = []
    for j in range(len(seeds)):
        group = W[assign == j]
        p = group.mean(axis=0) if len(group) else W[seeds[j]]
        protos.append(p / (np.linalg.norm(p) + 1e-12))
    return np.vstack(protos).astype(np.float32)


def count_quarantined_assigned():
    """Obličeje v karanténě, které ještě patří nějaké osobě (po zpětné kontrole brány)."""
    conn = get_connection()
    try:
        return conn.execute("SELECT COUNT(*) FROM face_instances WHERE is_valid = 0 "
                            "AND person_id IS NOT NULL").fetchone()[0]
    finally:
        conn.close()


def get_person_prototypes(person_id):
    conn = get_connection()
    try:
        rows = conn.execute(f"SELECT embedding FROM face_instances WHERE person_id = ? AND embedding IS NOT NULL "
                            f"AND {VALID_FACE_SQL}", (person_id,)).fetchall()
    finally:
        conn.close()
    vecs = _unit_vectors(b for (b,) in rows)
    if not vecs or len({v.size for v in vecs}) != 1:
        return None
    return person_prototypes(np.vstack(vecs))


def _is_named(name):
    return bool(name) and name.strip() != "" and name != UNKNOWN_NAME


def _is_anchor(name, is_favorite, is_confirmed=0):
    """Kotva = osoba, kterou přeshlukování nikdy nesmaže: pojmenovaná, oblíbená nebo
    potvrzená uživatelem (sloučení, přejmenování, „Ano, je to stejná osoba“)."""
    return _is_named(name) or bool(is_favorite) or bool(is_confirmed)


def _merge_pass(sums, nsum, media_sets, members, is_anchor, threshold, log=None):
    """Aglomerativní slučování shluků podle průměrné podobnosti (average linkage).

    avg(i, j) = (S_i · S_j) / (n_i · n_j), kde S je součet normalizovaných vektorů shluku –
    přesně průměr všech dvojic obličejů, spočítaný bez matice obličej × obličej.
    Omezení: dvě kotvy (různé pojmenované / potvrzené osoby) se nikdy nespojí a nespojí se
    shluky, které mají obličej ze stejné fotky (cannot-link). Vrací počet sloučení."""
    K = len(members)
    alive = np.array([nsum[k] > 0 for k in range(K)])
    if alive.sum() < 2:
        return 0
    S = sums[:K].astype(np.float64)
    n = np.maximum(np.asarray(nsum[:K], dtype=np.float64), 1.0)
    M = (S @ S.T) / np.outer(n, n)
    np.fill_diagonal(M, -np.inf)
    M[~alive, :] = -np.inf
    M[:, ~alive] = -np.inf
    anchors = np.array(is_anchor[:K], dtype=bool)
    M[np.ix_(anchors, anchors)] = -np.inf
    by_media = {}
    for k in range(K):
        for m in media_sets[k]:
            by_media.setdefault(m, []).append(k)
    for ks in by_media.values():
        if len(ks) > 1:
            idx = np.array(ks)
            M[np.ix_(idx, idx)] = -np.inf

    merges = 0
    while True:
        flat = int(np.argmax(M))
        i, j = divmod(flat, K)
        if not np.isfinite(M[i, j]) or M[i, j] < threshold:
            break
        if anchors[j] and not anchors[i]:
            i, j = j, i                       # přežívá kotva (její jméno / id)
        if log is not None:                   # diagnostika: (podobnost, členové i, členové j)
            log.append((float(M[i, j]), list(members[i]), list(members[j])))
        S[i] += S[j]
        n[i] += n[j]
        sums[i] = S[i].astype(sums.dtype)
        nsum[i] += nsum[j]
        nsum[j] = 0
        media_sets[i] |= media_sets[j]
        members[i].extend(members[j])
        members[j] = []
        media_sets[j] = set()
        merges += 1
        M[j, :] = -np.inf
        M[:, j] = -np.inf
        row = (S @ S[i]) / (n * n[i])
        alive_now = np.array([nsum[k] > 0 for k in range(K)])
        row[~alive_now] = -np.inf
        row[i] = -np.inf
        if anchors[i]:
            row[anchors] = -np.inf
        for k in range(K):
            if alive_now[k] and k != i and not media_sets[i].isdisjoint(media_sets[k]):
                row[k] = -np.inf
        M[i, :] = row
        M[:, i] = row
    return merges


def strict_recluster_faces(threshold=None, merge_threshold=None, merge_log=None):
    """Vrací slovník se statistikou (nebo None při chybě / prázdných datech).

    P3.8: threshold je nastavitelný (výchozí SIMILARITY_THRESHOLD z konfigurace).

    Patch 2.2 změny:
    - Quality gate: obličeje s nízkým quality_score nebo příliš malým bboxem jsou
      vyřazeny před shlukováním (neznehodnocují centroidy).
    - Centroidy: každý shluk průběžně aktualizuje průměrný normalizovaný vektor.
    - Kotvy (anchors): pojmenovaní / oblíbení lidé jsou pevné kotvy.
    - Cannot-link: dva obličeje ze stejné fotky se nikdy nespojí.

    P3.9: Po dokončení invaliduje celou LRU cache centroidů.
    """
    if threshold is None:
        threshold = SIMILARITY_THRESHOLD
    if merge_threshold is None:
        merge_threshold = MERGE_THRESHOLD

    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("BEGIN IMMEDIATE")

        c.execute("SELECT id, name, is_favorite, COALESCE(is_confirmed, 0) FROM people")
        people_rows = c.fetchall()
        anchor_ids = {pid for pid, name, fav, conf in people_rows if _is_anchor(name, fav, conf)}
        auto_people_ids = [pid for pid, _, _, _ in people_rows if pid not in anchor_ids]

        # Patch 11: obličeje v karanténě (anatomická brána: týl hlavy, textura) se neshlukují
        # a od osob se odpojí – nic se nemaže, při změně brány se vrátí
        c.execute("UPDATE face_instances SET person_id = NULL WHERE is_valid = 0 AND person_id IS NOT NULL")
        quarantined = c.rowcount
        c.execute(f"""
            SELECT id, media_id, person_id, face_crop_path, embedding,
                   COALESCE(quality_score, 0),
                   COALESCE(bbox_w, 0), COALESCE(bbox_h, 0)
            FROM face_instances
            WHERE embedding IS NOT NULL AND {VALID_FACE_SQL}
            ORDER BY COALESCE(quality_score, 0) DESC, id ASC
        """)
        rows = c.fetchall()
        if not rows:
            conn.rollback()
            return None

        dim = None
        face_ids, media_ids, person_ids, crops, quals, vecs = [], [], [], [], [], []
        skipped_quality = 0
        for fid, mid, pid, crop, blob, q, bw_rel, bh_rel in rows:
            is_anchor_face = (pid in anchor_ids)
            if not is_anchor_face:
                if q < MIN_QUALITY_SCORE:
                    skipped_quality += 1
                    continue
                if (bw_rel > 0 or bh_rel > 0) and (bw_rel < MIN_BBOX_RELATIVE or bh_rel < MIN_BBOX_RELATIVE):
                    skipped_quality += 1
                    continue

            arr = np.frombuffer(bytes(blob), dtype=np.float32)
            if dim is None:
                dim = arr.size
            if arr.size != dim or arr.size == 0:
                continue
            n = np.linalg.norm(arr)
            if n < 1e-6 or not np.isfinite(n):
                continue
            face_ids.append(fid); media_ids.append(mid); person_ids.append(pid)
            crops.append(crop); quals.append(q); vecs.append(arr / n)

        if not vecs:
            conn.rollback()
            return {"anchors": len(anchor_ids), "new_people": 0,
                    "attached_to_anchors": 0, "faces": 0,
                    "skipped_quality": skipped_quality}
        V = np.vstack(vecs).astype(np.float32)

        # --- kotvy: pevné shluky z existujících obličejů pojmenovaných / oblíbených osob ---
        cap = max(64, len(anchor_ids) * 2)
        cent = np.zeros((cap, dim), dtype=np.float32)
        sums = np.zeros((cap, dim), dtype=np.float32)
        media_sets, members, is_anchor_cluster, cluster_person = [], [], [], []
        anchor_cluster_of = {}
        K = 0

        def _new_cluster(anchor_pid=None):
            nonlocal cent, sums, K
            if K >= cent.shape[0]:
                cent = np.vstack([cent, np.zeros_like(cent)])
                sums = np.vstack([sums, np.zeros_like(sums)])
            media_sets.append(set()); members.append([])
            is_anchor_cluster.append(anchor_pid is not None)
            cluster_person.append(anchor_pid)
            K += 1
            return K - 1

        nsum = []                       # počet vektorů sečtených v sums[k] (pro average linkage)
        free_idx = []
        for i, pid in enumerate(person_ids):
            if pid in anchor_ids:
                k = anchor_cluster_of.get(pid)
                if k is None:
                    k = _new_cluster(pid)
                    anchor_cluster_of[pid] = k
                    nsum.append(0)
                sums[k] += V[i]
                nsum[k] += 1
                media_sets[k].add(media_ids[i])
                members[k].append(i)
            else:
                free_idx.append(i)
        for k in range(K):
            cent[k] = sums[k] / (np.linalg.norm(sums[k]) + 1e-12)

        # Patch 11: kotvy (pojmenované / potvrzené osoby) reprezentuje několik PROTOTYPŮ místo
        # jednoho průměru – s kšiltovkou, bez, s brýlemi, z profilu. Nový obličej se přidá,
        # když se podobá kterémukoli z nich; prototypy se při každém přeshlukování přepočítají
        # z (očištěných) členů, takže osoba průběžně „pobírá“ nové podoby.
        anchors_k = K
        proto_list, proto_owner = [], []
        for k in range(anchors_k):
            P = person_prototypes(V[members[k]])
            proto_list.append(P)
            proto_owner.extend([k] * len(P))
        PROTO = np.vstack(proto_list).astype(np.float32) if proto_list else np.zeros((0, dim), np.float32)
        proto_owner = np.array(proto_owner, dtype=np.int64)

        # --- 1. průchod: přiřazení volných obličejů (od nejkvalitnějších) ---
        for i in free_idx:
            v, m = V[i], media_ids[i]
            target = -1
            if K > 0:
                sims = cent[:K] @ v
                if len(PROTO):
                    best_proto = np.full(anchors_k, -np.inf, dtype=np.float32)
                    np.maximum.at(best_proto, proto_owner, PROTO @ v)
                    sims[:anchors_k] = best_proto
                best = int(sims.argmax())
                if sims[best] >= threshold:
                    if m not in media_sets[best]:
                        target = best
                    else:
                        for k in np.argsort(-sims):
                            if sims[k] < threshold:
                                break
                            if m not in media_sets[k]:
                                target = int(k)
                                break
            if target < 0:
                target = _new_cluster(None)
                nsum.append(0)
            media_sets[target].add(m)
            members[target].append(i)
            if not is_anchor_cluster[target]:
                sums[target] += v
                nsum[target] += 1
                cent[target] = sums[target] / (np.linalg.norm(sums[target]) + 1e-12)

        # --- 2. průchod: sloučení roztříštěných shluků téže osoby (average linkage) ---
        merged = 0
        free_set = set(free_idx)
        if merge_threshold is not None and merge_threshold < 1.0:
            # obličeje připojené ke kotvě v 1. průchodu do jejího součtu nepatří (kotva je pevná),
            # pro průměrnou podobnost ale počítáme se všemi obličeji kotvy -> doplnit
            for k in range(K):
                if is_anchor_cluster[k]:
                    extra = [i for i in members[k] if i in free_set]
                    if extra:
                        sums[k] += V[extra].sum(axis=0)
                        nsum[k] += len(extra)
            raw_log = [] if merge_log is not None else None
            merged = _merge_pass(sums, nsum, media_sets, members, is_anchor_cluster, merge_threshold, raw_log)
            if merge_log is not None:     # indexy -> id obličejů (pro kontrolu kvality slučování)
                for sim, a, b in raw_log:
                    merge_log.append((sim, [face_ids[i] for i in a], [face_ids[i] for i in b]))

        attached_to_anchor = {}
        for k in range(K):
            if is_anchor_cluster[k]:
                for i in members[k]:
                    if i in free_set:
                        attached_to_anchor[i] = cluster_person[k]

        # --- zápis do DB (jedna transakce) ---
        free_ids_params = [(face_ids[i],) for i in free_idx]
        c.executemany("UPDATE face_instances SET person_id = NULL WHERE id = ?", free_ids_params)
        if auto_people_ids:
            c.executemany("DELETE FROM people WHERE id = ?", [(p,) for p in auto_people_ids])

        if attached_to_anchor:
            c.executemany("UPDATE face_instances SET person_id = ? WHERE id = ?",
                          [(pid, face_ids[i]) for i, pid in attached_to_anchor.items()])

        new_people = 0
        for k in range(K):
            if is_anchor_cluster[k] or not members[k] or len(media_sets[k]) < MIN_MEDIA_PER_AUTO_PERSON:
                continue
            mem = members[k]
            cover_i = next((i for i in mem if crops[i] and os.path.exists(crops[i])), mem[0])
            cover_exists = bool(crops[cover_i]) and os.path.exists(crops[cover_i])
            c.execute(
                "INSERT INTO people (name, cover_face_path, key_face_id) VALUES (?, ?, ?)",
                (UNKNOWN_NAME, crops[cover_i], face_ids[cover_i] if cover_exists else None),
            )
            pid = c.lastrowid
            c.executemany("UPDATE face_instances SET person_id = ? WHERE id = ?",
                          [(pid, face_ids[i]) for i in mem])
            new_people += 1

        # Odmítnuté návrhy sloučení u smazaných (automatických) osob už nic neznamenají
        c.execute("DELETE FROM person_merge_dismissed WHERE id_a NOT IN (SELECT id FROM people) "
                  "OR id_b NOT IN (SELECT id FROM people)")
        conn.commit()

        # P3.9: Invalidace celé LRU cache centroidů po reclusteru
        invalidate_centroid_cache()
        # Patch 10: portrét = nejtypičtější ostrý čelní obličej (ne první v pořadí kvality)
        try:
            refresh_person_covers()
        except Exception as e:
            print(f"[Face] Výběr portrétů selhal: {e}")

        return {
            "anchors": len(anchor_ids),
            "new_people": new_people,
            "attached_to_anchors": len(attached_to_anchor),
            "faces": len(face_ids),
            "skipped_quality": skipped_quality,
            "threshold": threshold,
            "merge_threshold": merge_threshold,
            "merged": merged,
            "quarantined_detached": quarantined,
            "prototypes": int(len(PROTO)),
        }
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[Face] Chyba přeshlukování (provedeno rollback): {e}")
        traceback.print_exc()
        return None
    finally:
        conn.close()


# ==============================================================================
# Patch 8: ruční slučování osob a návrhy „Je to stejná osoba?“
# ==============================================================================

def _unit_vectors(blobs):
    vecs = []
    for blob in blobs:
        if not blob:
            continue
        arr = np.frombuffer(bytes(blob), dtype=np.float32)
        n = np.linalg.norm(arr) if arr.size else 0.0
        if arr.size == 0 or n < 1e-6 or not np.isfinite(n):
            continue
        vecs.append(arr / n)
    return vecs


def merge_people(target_id, source_ids, name=None, pull_threshold=None):
    """Sloučí osoby source_ids do target_id v jedné transakci.

    * všechny obličeje zdrojových osob přejdou k cílové, zdrojové osoby se smažou,
    * cílová osoba je potvrzená (is_confirmed = 1) -> přeshlukování ji nikdy nesmaže,
    * jméno: `name`, jinak jméno cíle, jinak první pojmenovaný zdroj,
    * přepočítaný centroid přitáhne dosud nepřiřazené obličeje nad `pull_threshold`
      (nejvýš jeden na fotku a nikdy z fotky, kde už osoba je).

    Vrací dict se statistikou a `undo` daty pro undo_merge(); None, pokud není co slučovat.
    """
    from database import db_transaction
    if pull_threshold is None:
        pull_threshold = SIMILARITY_THRESHOLD
    source_ids = [int(s) for s in source_ids if int(s) != int(target_id)]
    if not source_ids:
        return None
    cols = "id, name, cover_face_path, key_face_id, is_favorite, COALESCE(is_confirmed, 0)"
    with db_transaction() as conn:
        c = conn.cursor()
        target = c.execute(f"SELECT {cols} FROM people WHERE id = ?", (target_id,)).fetchone()
        if target is None:
            return None
        marks = ",".join("?" * len(source_ids))
        sources = c.execute(f"SELECT {cols} FROM people WHERE id IN ({marks})", source_ids).fetchall()
        source_ids = [row[0] for row in sources]
        if not source_ids:
            return None
        marks = ",".join("?" * len(source_ids))
        moved = c.execute(f"SELECT id, person_id FROM face_instances WHERE person_id IN ({marks})",
                          source_ids).fetchall()
        c.execute(f"UPDATE face_instances SET person_id = ? WHERE person_id IN ({marks})",
                  [target_id] + source_ids)

        new_name = name.strip() if name and name.strip() else None
        if new_name is None:
            new_name = target[1] if _is_named(target[1]) else next(
                (s[1] for s in sources if _is_named(s[1])), target[1] or UNKNOWN_NAME)
        cover, key_face = target[2], target[3]
        if not cover:
            donor = next((s for s in sources if s[2]), None)
            if donor:
                cover, key_face = donor[2], donor[3]
        favorite = int(bool(target[4]) or any(s[4] for s in sources))
        c.execute("UPDATE people SET name = ?, cover_face_path = ?, key_face_id = ?, is_favorite = ?, "
                  "is_confirmed = 1 WHERE id = ?", (new_name, cover, key_face, favorite, target_id))
        c.execute(f"DELETE FROM people WHERE id IN ({marks})", source_ids)
        c.execute(f"DELETE FROM person_merge_dismissed WHERE id_a IN ({marks}) OR id_b IN ({marks})",
                  source_ids + source_ids)

        # Přepočítané prototypy sloučené osoby -> přitáhnout odpovídající nepřiřazené obličeje
        rows = c.execute(f"SELECT media_id, embedding FROM face_instances WHERE person_id = ? "
                         f"AND embedding IS NOT NULL AND {VALID_FACE_SQL}", (target_id,)).fetchall()
        own_media = {m for m, _ in rows}
        vecs = _unit_vectors(b for _, b in rows)
        pulled = []
        if vecs and len({v.size for v in vecs}) == 1:
            protos = person_prototypes(np.vstack(vecs))
            candidates = []
            for fid, mid, blob in c.execute(f"SELECT id, media_id, embedding FROM face_instances "
                                            f"WHERE person_id IS NULL AND embedding IS NOT NULL AND {VALID_FACE_SQL}"):
                v = _unit_vectors([blob])
                if v and v[0].size == protos.shape[1]:
                    sim = float((protos @ v[0]).max())
                    if sim >= pull_threshold and mid not in own_media:
                        candidates.append((sim, fid, mid))
            for sim, fid, mid in sorted(candidates, reverse=True):
                if mid in own_media:
                    continue
                own_media.add(mid)
                pulled.append(fid)
            if pulled:
                c.executemany("UPDATE face_instances SET person_id = ? WHERE id = ?",
                              [(target_id, fid) for fid in pulled])

    invalidate_centroid_cache()
    try:
        refresh_person_covers([target_id])     # nejlepší portrét ze sloučených obličejů
    except Exception as e:
        print(f"[Face] Portrét po sloučení: {e}")
    return {
        "target": int(target_id),
        "name": new_name,
        "merged": len(source_ids),
        "moved_faces": len(moved),
        "pulled_in": len(pulled),
        "undo": {"target": target, "sources": sources, "faces": moved, "pulled": pulled},
    }


def undo_merge(undo):
    """Vrátí merge_people(): obnoví smazané osoby (se stejnými id), jejich obličeje,
    přitažené obličeje vrátí mezi nepřiřazené a cílové osobě vrátí původní stav."""
    from database import db_transaction
    target, sources = undo["target"], undo["sources"]
    with db_transaction() as conn:
        c = conn.cursor()
        for row in sources:
            c.execute("INSERT OR REPLACE INTO people (id, name, cover_face_path, key_face_id, is_favorite, "
                      "is_confirmed) VALUES (?, ?, ?, ?, ?, ?)", row)
        c.executemany("UPDATE face_instances SET person_id = ? WHERE id = ?",
                      [(pid, fid) for fid, pid in undo["faces"]])
        c.executemany("UPDATE face_instances SET person_id = NULL WHERE id = ?",
                      [(fid,) for fid in undo["pulled"]])
        c.execute("UPDATE people SET name = ?, cover_face_path = ?, key_face_id = ?, is_favorite = ?, "
                  "is_confirmed = ? WHERE id = ?", (target[1], target[2], target[3], target[4], target[5], target[0]))
    invalidate_centroid_cache()
    return True


def confirm_person(person_id):
    """Osoba potvrzená uživatelem (např. přejmenováním) se stane kotvou."""
    from database import db_transaction
    with db_transaction() as conn:
        conn.execute("UPDATE people SET is_confirmed = 1 WHERE id = ?", (person_id,))


def _pair(a, b):
    a, b = int(a), int(b)
    return (a, b) if a < b else (b, a)


def dismiss_person_merge(id_a, id_b):
    """„Ne, jiná osoba“ – pár se už nenabídne."""
    from database import db_transaction
    with db_transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO person_merge_dismissed (id_a, id_b) VALUES (?, ?)", _pair(id_a, id_b))


def suggest_person_merges(limit=SUGGEST_LIMIT, min_similarity=SUGGEST_MIN_SIMILARITY):
    """Páry osob, které jsou nejspíš tatáž osoba (průměrná podobnost všech dvojic obličejů).

    Vynechá páry dvou různě pojmenovaných osob, páry sdílející fotku (dvě osoby na jedné
    fotce nejsou tatáž osoba) a páry, které uživatel odmítl. Seřazeno od nejjistějších."""
    conn = get_connection()
    try:
        people = conn.execute("SELECT id, name FROM people").fetchall()
        faces = conn.execute(f"SELECT person_id, media_id, embedding FROM face_instances "
                             f"WHERE person_id IS NOT NULL AND embedding IS NOT NULL AND {VALID_FACE_SQL}").fetchall()
        dismissed = {_pair(a, b) for a, b in conn.execute("SELECT id_a, id_b FROM person_merge_dismissed")}
    finally:
        conn.close()
    names = dict(people)
    by_person = {}
    for pid, mid, blob in faces:
        v = _unit_vectors([blob])
        if not v:
            continue
        entry = by_person.setdefault(pid, {"vecs": [], "media": set()})
        entry["vecs"].append(v[0])
        entry["media"].add(mid)
    ids = [pid for pid in by_person if pid in names]
    if len(ids) < 2:
        return []
    dim = by_person[ids[0]]["vecs"][0].size
    ids = [pid for pid in ids if by_person[pid]["vecs"][0].size == dim]
    S = np.vstack([np.sum(by_person[p]["vecs"], axis=0) for p in ids]).astype(np.float64)
    n = np.array([len(by_person[p]["vecs"]) for p in ids], dtype=np.float64)
    M = (S @ S.T) / np.outer(n, n)
    out = []
    for a in range(len(ids)):
        for b in range(a + 1, len(ids)):
            sim = float(M[a, b])
            if sim < min_similarity:
                continue
            pa, pb = ids[a], ids[b]
            na, nb = names[pa], names[pb]
            if _is_named(na) and _is_named(nb) and na.strip().lower() != nb.strip().lower():
                continue
            if _pair(pa, pb) in dismissed:
                continue
            if not by_person[pa]["media"].isdisjoint(by_person[pb]["media"]):
                continue
            out.append({"a": pa, "b": pb, "similarity": sim, "name_a": na, "name_b": nb,
                        "faces_a": int(n[a]), "faces_b": int(n[b])})
    out.sort(key=lambda s: -s["similarity"])
    return out[:limit]


# ==============================================================================
# Patch 10: reprezentativní portrét osoby (cover)
# ==============================================================================

# Skóre portrétu. Hlavní je natočení hlavy z landmarků (frontality: 1 čelně, 0 profil / týl,
# face_engine.frontality_from_kps); samotná podobnost s průměrnou tváří osoby to NEpozná
# (ověřeno na knihovně: vybírala profily). Dál kvalita (ostrost × jistota detekce), velikost
# obličeje a typičnost; jiný výrazný obličej ve výřezu (crowded) portrét silně penalizuje.
COVER_WEIGHTS = {"frontality": 0.45, "quality": 0.25, "similarity": 0.20, "size": 0.10, "crowded": -0.35}
UNKNOWN_FRONTALITY = 0.5          # obličej ještě bez analýzy natočení
COVER_DENSITY_K = 8               # sousedů pro „hustotu“ obličeje v rámci osoby
COVER_CORE_QUANTILE = 0.6         # portrét jen z 40 % nejhustších (nejtypičtějších) obličejů


def cover_score(frontality, quality, similarity, size, crowded):
    """Všechny vstupy normalizované do 0..1 (crowded 0/1)."""
    w = COVER_WEIGHTS
    f = UNKNOWN_FRONTALITY if frontality is None else float(frontality)
    return (w["frontality"] * f + w["quality"] * quality + w["similarity"] * similarity
            + w["size"] * size + w["crowded"] * (1 if crowded else 0))


def select_cover_face(faces):
    """faces: [(face_id, crop_path, unit_vector, quality, bbox_w, bbox_h, frontality, crowded)]
    -> (face_id, crop_path) nejlepšího portrétu nebo (None, None). Jen s ořezem na disku."""
    usable = []
    for fid, crop, vec, quality, bw, bh, front, crowded in faces:
        path = resolve_face_path(crop)
        if path and vec is not None:
            usable.append((fid, path, vec, float(quality or 0.0),
                           float(np.sqrt(max(0.0, (bw or 0) * (bh or 0)))), front, crowded))
    if not usable:
        return None, None
    V = np.vstack([u[2] for u in usable])
    centroid = V.mean(axis=0)
    centroid /= (np.linalg.norm(centroid) + 1e-12)
    sims = [float(v @ centroid) for v in V]
    # Portrét vybírat jen z „jádra“ osoby: čelní, ale špatně zařazený obličej jiného člověka
    # by jinak vyhrál (ověřeno na knihovně). Jádro = obličeje s nejvíc blízkými sousedy
    # (průměr k nejpodobnějších), takže ve smíšeném shluku vyhraje většinová identita.
    n = len(usable)
    if n >= 4:
        S = V @ V.T
        np.fill_diagonal(S, -1.0)
        k = min(COVER_DENSITY_K, n - 1)
        density = np.sort(S, axis=1)[:, -k:].mean(axis=1)
        cut = float(np.quantile(density, COVER_CORE_QUANTILE))
        core = [(u, s) for u, s, d in zip(usable, sims, density) if d >= cut]
    else:
        core = list(zip(usable, sims))
    max_q = max(u[3] for u, _ in core) or 1.0
    max_a = max(u[4] for u, _ in core) or 1.0
    best, _ = max(core, key=lambda us: cover_score(us[0][5], us[0][3] / max_q, max(0.0, us[1]),
                                                   us[0][4] / max_a, us[0][6]))
    return best[0], best[1]


def refresh_person_covers(person_ids=None):
    """Přepočítá portrét osob (všech nebo vybraných), kromě uživatelem zamčených (cover_locked).
    Vrací počet osob, kterým se portrét změnil."""
    from database import db_transaction
    conn = get_connection()
    try:
        people = conn.execute("SELECT id, key_face_id, cover_face_path FROM people "
                              "WHERE COALESCE(cover_locked, 0) = 0").fetchall()
        rows = conn.execute("SELECT id, person_id, face_crop_path, embedding, COALESCE(quality_score, 0), "
                            "COALESCE(bbox_w, 0), COALESCE(bbox_h, 0), frontality, COALESCE(crowded, 0) "
                            f"FROM face_instances WHERE person_id IS NOT NULL AND embedding IS NOT NULL "
                            f"AND {VALID_FACE_SQL}").fetchall()
    finally:
        conn.close()
    wanted = set(int(p) for p in person_ids) if person_ids is not None else None
    by_person = {}
    for fid, pid, crop, blob, q, bw, bh, front, crowded in rows:
        v = _unit_vectors([blob])
        if v:
            by_person.setdefault(pid, []).append((fid, crop, v[0], q, bw, bh, front, crowded))
    updates = []
    for pid, key_face, cover in people:
        if wanted is not None and pid not in wanted:
            continue
        faces = by_person.get(pid)
        if not faces:
            continue
        dims = {f[2].size for f in faces}
        if len(dims) != 1:
            continue
        fid, path = select_cover_face(faces)
        if fid is not None and (fid != key_face or path != cover):
            updates.append((path, fid, pid))
    if updates:
        with db_transaction() as conn:
            conn.executemany("UPDATE people SET cover_face_path = ?, key_face_id = ? WHERE id = ?", updates)
    return len(updates)
