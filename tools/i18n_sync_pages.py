"""Gate 1: embeds the English map-* strings into map_vault.html as DEFAULT_STRINGS.

The page works standalone (tests, before Python pushes setStrings) in the source language.
Run after editing locales/en/kairos.ftl:  python tools/i18n_sync_pages.py
test_gate1_i18n.test_map_page_defaults_in_sync fails when this is stale.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import i18n  # noqa: E402

MARK = re.compile(r"/\*I18N-DEFAULTS\*/.*?/\*END\*/", re.S)


def defaults_json():
    old = i18n.current()
    i18n.set_language("en", retranslate=False)
    try:
        return json.dumps(i18n.page_strings("map-"), ensure_ascii=False, sort_keys=True)
    finally:
        i18n.set_language(old, retranslate=False)


def sync(path=os.path.join(HERE, "map_vault.html")):
    src = open(path, encoding="utf-8").read()
    new = MARK.sub(lambda m: "/*I18N-DEFAULTS*/" + defaults_json() + "/*END*/", src, count=1)
    if new != src:
        open(path, "w", encoding="utf-8", newline="").write(new)
    return new != src


if __name__ == "__main__":
    print("updated" if sync() else "up to date")
