"""Text na fotkách přes vestavěné OCR Windows (Windows.Media.Ocr) – Sprint 3.

Offline, bez dalších modelů: Windows 10/11 má rozpoznávač pro nainstalované jazyky (čeština
s českým Windows). Cedule, dokumenty, snímky obrazovky, jídelní lístky pak najde hledání slovem
(„Kašperské“, „vstup zakázán“), i když je obrazový model nevidí.

recognize(img) -> text (řádky spojené mezerou). available() = projekce winrt + jazyk.
Obraz se zmenší na MAX_EDGE; šum z textur (kámen, listí čte OCR jako „sgFF“) odfiltruje
plausible() / clean_text(). Běží v démonu (main.BackgroundAIDaemon._index_ocr), výsledky v tabulce media_ocr.
"""
import asyncio
import threading

MAX_EDGE = 2000          # stačí na cedule v záběru, rychlejší než plné rozlišení

_engine = None
_lock = threading.Lock()
_unavailable = False


def available():
    return _get_engine() is not None


def _get_engine():
    global _engine, _unavailable
    if _engine is not None or _unavailable:
        return _engine
    with _lock:
        if _engine is None and not _unavailable:
            try:
                from winrt.windows.media.ocr import OcrEngine
                eng = OcrEngine.try_create_from_user_profile_languages()
                if eng is None:
                    langs = list(OcrEngine.available_recognizer_languages)
                    eng = OcrEngine.try_create_from_language(langs[0]) if langs else None
                _engine = eng
                _unavailable = eng is None
            except Exception as e:                # jiný systém / chybí projekce winrt
                print(f"[OCR] nedostupné: {e}")
                _unavailable = True
    return _engine


def language():
    eng = _get_engine()
    return eng.recognizer_language.language_tag if eng is not None else None


def _to_bitmap(img):
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    img = img.convert("RGBA")
    w, h = img.size
    scale = min(1.0, MAX_EDGE / max(w, h))
    if scale < 1.0:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        w, h = img.size
    r, g, b, a = img.split()
    from PIL import Image
    bgra = Image.merge("RGBA", (b, g, r, a)).tobytes()
    return SoftwareBitmap.create_copy_from_buffer(bgra, BitmapPixelFormat.BGRA8, w, h)


async def _recognize(bitmap):
    return await _get_engine().recognize_async(bitmap)


def recognize(img):
    """PIL obrázek -> rozpoznaný text ('' když nic / OCR nejde)."""
    if _get_engine() is None or img is None:
        return ""
    try:
        bitmap = _to_bitmap(img)
        result = asyncio.run(_recognize(bitmap))
    except Exception as e:
        print(f"[OCR] {e}")
        return ""
    words = [w.text for line in result.lines for w in line.words]
    return clean_text(words)


def plausible(word):
    """Slovo, ne textura: písmena v jednom tvaru (VELKÁ / malá / Začáteční), nebo číslo.
    Kámen a listí OCR čte jako „sgFF“, „i,w“, „zg:FI“ – smíšená velikost, interpunkce uvnitř."""
    # i18n-ok: punctuation set, not text
    w = word.strip(".,:;!?\"'()[]«»„“”-–")
    if len(w) >= 2 and w.isdigit():
        return w
    if len(w) < 3 or not w.isalpha():
        return None
    if not (w.isupper() or w.islower() or w.istitle()):
        return None
    if len(set(w.lower())) < 2:                    # „lll“, „III“
        return None
    return w


def clean_text(words):
    kept = [w for w in (plausible(x or "") for x in words) if w]
    # samotné krátké slovo je skoro vždy šum (textura) – text = aspoň 2 slova nebo jedno dlouhé
    if not kept or (len(kept) == 1 and len(kept[0]) < 5):
        return ""
    return " ".join(kept)


def recognize_file(path):
    from PIL import Image
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    try:
        with Image.open(path) as im:
            from PIL import ImageOps
            return recognize(ImageOps.exif_transpose(im))
    except Exception as e:
        print(f"[OCR] {path}: {e}")
        return ""
