# -*- mode: python ; coding: utf-8 -*-

import os
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# Bezpečný sběr souborů (přidá soubor pouze tehdy, když fyzicky existuje)
datas = []

# config.json se NEPŘIBALUJE: obsahoval by předvolby a cesty ke složkám toho, kdo build
# vytvořil. Gate 0: data uživatele nejsou vedle .exe – config.json, photos.db, vault a zálohy
# vzniknou v %APPDATA%\Kairos, mezipaměť a ořezy tváří v %LOCALAPPDATA%\Kairos (paths.py);
# instalace do Program Files je tak jen pro čtení. portable.txt vedle .exe = přenosný režim.
candidates = [
    'ffmpeg.exe',
    'face_detection_yunet_2023mar.onnx',
    'face_recognition_sface_2021dec.onnx'
]

for item in candidates:
    if os.path.exists(item):
        datas.append((item, '.'))

# exiftool.exe (13.x) potřebuje vedle sebe exiftool_files (Perl + knihovny) a musí mít
# VLASTNÍ složku: přímo v _internal by si načetl perl532.dll, který tam PyInstaller zkopíroval
# z analýzy závislostí, s prázdným @INC ("Can't locate strict.pm") – import by tiše přišel
# o data pořízení, GPS i Live Photos. paths.EXIFTOOL_PATH ho hledá v tools/exiftool.
EXIFTOOL_DIR = os.path.join('tools', 'exiftool')
if os.path.exists('exiftool.exe') and os.path.isdir('exiftool_files'):
    datas.append(('exiftool.exe', EXIFTOOL_DIR))
    datas.append(('exiftool_files', os.path.join(EXIFTOOL_DIR, 'exiftool_files')))

# Mapa: stránky + lokálně přibalený MapLibre (main.py je; minimapa prohlížeče je nativní – mini_map.py,
# hledají v paths.RESOURCE_DIR = sys._MEIPASS, stránky odkazují relativně na vendor/maplibre)
for page in ('map_vault.html', 'map_style.js', 'director.js', 'reel_player.html'):
    if os.path.exists(page):
        datas.append((page, '.'))
# Vrstva videa prohlížeče (QML VideoOutput, viewer_hud.py)
# Gate 1: katalogy překladů (Project Fluent) – i18n.LOCALES_DIR = _MEIPASS/locales
if os.path.isdir('locales'):
    datas.append(('locales', 'locales'))
if os.path.exists('video_output.qml'):
    datas.append(('video_output.qml', '.'))
# Batch 6: MapLibre GL JS 5 (zeměkoule, terén, Cesta), vč. licence – jediný mapový stack
if os.path.isdir(os.path.join('vendor', 'maplibre')):
    datas.append((os.path.join('vendor', 'maplibre'), os.path.join('vendor', 'maplibre')))
# Gate 2: mapa ve WebView2 (map_host.py) – SDK z NuGet Microsoft.Web.WebView2: spravované
# Microsoft.Web.WebView2.Core.dll a nativní WebView2Loader.dll (x64) MUSÍ ležet vedle sebe
# (Core.dll si loader hledá ve své složce). Jako data, ne binárky: analýza závislostí by
# loader přesunula do kořene _internal. Runtime WebView2 (Evergreen) je součástí Windows 11;
# bez něj map_host.available() = False a mapa zůstane v QtWebEngine.
WEBVIEW2_SDK = os.path.join('vendor', 'webview2')
for f in ('Microsoft.Web.WebView2.Core.dll', 'WebView2Loader.dll', 'LICENSE.txt'):
    if os.path.exists(os.path.join(WEBVIEW2_SDK, f)):
        datas.append((os.path.join(WEBVIEW2_SDK, f), WEBVIEW2_SDK))
# Cesta 3.0 (C6): hudební podkres – vlastní skladby z `python ambient_music.py` (ambient_music.tracks
# je hledá v RESOURCE_DIR/assets/music)
if os.path.isdir(os.path.join('assets', 'music')):
    datas.append((os.path.join('assets', 'music'), os.path.join('assets', 'music')))

# Sprint 3: modely hledání v ONNX (tools/export_onnx.py) – ai_engine.onnx_models_dir() = RESOURCE_DIR/models
for f in ('clip_b32_vision.onnx', 'clip_b32_text_multi.onnx', 'clip_b32_text_multi_int8.onnx',
          'clip_b32_text_tokenizer.json'):
    if os.path.exists(os.path.join('models', f)):
        datas.append((os.path.join('models', f), 'models'))

# Kairos dávka 5: ikona .exe (16–256 px) – vygeneruje `python brand.py` do assets/.
# Okno a hlavní panel si ikonu kreslí za běhu (brand.app_icon), do balíčku se nic nepřidává.
APP_ICON = os.path.join('assets', 'kairos.ico')
if not os.path.exists(APP_ICON):
    import subprocess, sys
    subprocess.run([sys.executable, 'brand.py'], check=True)

# Přibalení podpory pro reverzní geokódování a HEIF
hidden_imports = [
    'fluent.syntax',          # Gate 1: parser katalogů (i18n.py)
    'i18n',
    'PyQt6.QtWebEngineWidgets',
    'PyQt6.QtWebEngineCore',
    'PyQt6.QtWebChannel',
    'PyQt6.QtMultimedia',
    'PyQt6.QtMultimediaWidgets',
    # Prohlížeč: OpenGL plátno + video přes QML VideoOutput (QtQuick + plugin QtMultimedia)
    'PyQt6.QtOpenGLWidgets',
    'PyQt6.QtQuickWidgets',
    'PyQt6.QtQuick',
    'PyQt6.QtQml',
    'reverse_geocoder',
    'pillow_heif',
    'rawpy',                 # ProRAW / camera RAW (raw_decode.py); its LibRaw DLL ships in the wheel
    # Gate 0: obsahový otisk médií (media_identity) – C rozšíření, ať ho analýza nepřehlédne
    'xxhash',
    'data_migration',
    # Gate 2: WebView2 přes pythonnet (.NET Framework 4.x je ve Windows); hooky contrib přibalí
    # Python.Runtime.dll (clr) a ffi knihovny clr_loader
    'map_host',
    'clr',
    'clr_loader',
    'pythonnet',
]
hidden_imports += collect_submodules('pillow_heif')
# Sprint 3: OCR Windows (ocr_engine) – projekce winrt se načítají dynamicky
try:
    hidden_imports += collect_submodules('winrt')
except Exception:
    pass

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Sprint 3: hledání běží v ONNX Runtime (ai_onnx) – torch a spol. do balíčku nepatří
    # (ai_engine je importuje jen jako zálohu vývojového stroje bez models/clip_b32_*.onnx)
    excludes=['tkinter', 'matplotlib', 'torch', 'torchvision', 'sentence_transformers', 'transformers',
              'triton', 'sympy'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Kairos',
    icon=APP_ICON,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Kairos',
)