"""Gate 0: zástupné soubory cloudu (OneDrive, iCloud pro Windows, Dropbox Smart Sync).

Soubor „jen online“ vypadá v Průzkumníku jako každý jiný, ale jeho data leží v cloudu.
Jakékoli otevření (exiftool, miniatura, tváře, OCR, náhled) spustí stažení – u knihovny
s desítkami GB by import nebo démon AI nechtěně stáhl celý OneDrive přes mobilní data.

Rozpozná se z atributů (Cloud Files API), které vrací už výpis složky – soubor se kvůli
tomu neotevírá, takže kontrola sama nic nestáhne:
  FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS (0x400000) – data se stáhnou při čtení (OneDrive, iCloud)
  FILE_ATTRIBUTE_RECALL_ON_OPEN        (0x040000) – stáhne se už při otevření
  FILE_ATTRIBUTE_OFFLINE               (0x001000) – data nejsou okamžitě k dispozici (HSM, starší sync)

Pravidlo: práce na pozadí (sken, démon AI) zástupné soubory přeskakuje. Stáhnout je smí jen
výslovná akce uživatele – souhlas v dialogu importu nebo otevření fotky v prohlížeči.
"""
import os
import stat as _stat

FILE_ATTRIBUTE_OFFLINE = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
FILE_ATTRIBUTE_PINNED = 0x00080000          # „Vždy ponechat na tomto zařízení“
PLACEHOLDER_MASK = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS


def attrs_are_placeholder(attrs):
    return bool(int(attrs or 0) & PLACEHOLDER_MASK)


def stat_is_placeholder(st):
    """os.stat_result / DirEntry.stat() -> True = data nejsou na disku."""
    return attrs_are_placeholder(getattr(st, "st_file_attributes", 0))


def is_placeholder(path):
    """True = soubor je jen v cloudu (otevření by spustilo stažení). Neexistující = False."""
    try:
        return stat_is_placeholder(os.stat(path))
    except (OSError, ValueError, TypeError):
        return False


def local_ok(path):
    """Smí práce na pozadí soubor otevřít? Existuje a jeho data jsou na disku."""
    if not path:
        return False
    try:
        st = os.stat(path)
    except (OSError, ValueError, TypeError):
        return False
    return _stat.S_ISREG(st.st_mode) and not stat_is_placeholder(st)


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "kB") else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024
    return f"{n:.1f} TB"
