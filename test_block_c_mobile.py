"""Fix Block C: mobile reliability, offline resilience, UI polish.

  1. offline favourites: queued in media_items.is_favorite_dirty, delivered with compare-and-swap,
     never overwritten by an incoming sync page (SQL taken from the Swift source and run on sqlite).
  2. viewer swipe: a page that scrolls out of view cancels its original / motion half / stream;
     the shared download is cancelled with its last waiter; neighbours fetch no original.
  3. Bonjour: the desktop advertises _kairos._tcp (lan_discovery), the phone browses it and
     relocates its client; Info.plist declares the service.
  4. contrast + Dynamic Type: textTertiary >= 4.5:1 on every surface (recomputed from the Swift
     literals), section headers / counters use the scaling text styles.

Swift is not compiled on this machine: those parts are pinned at source level, like Block A.
"""
import os
import re
import socket
import sqlite3
import time
import unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
CORE = "ios/KairosCore/Sources/KairosCore/"
UI = "ios/KairosUI/Sources/KairosUI/"


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read().replace("\r\n", "\n")


def _swift_sql(src, anchor):
    """The first SQL string literal after `anchor` (one-line "..." form)."""
    tail = src[src.index(anchor):]
    return re.search(r'"((?:SELECT|UPDATE|INSERT)[^"]*)"', tail).group(1)


def _props(info):
    p = info.decoded_properties            # a property in zeroconf >= 0.1xx, a method before
    return p() if callable(p) else p


# ── 1. offline favourites ─────────────────────────────────────────────────────

class FavouriteQueue(unittest.TestCase):
    STORE = CORE + "Database/LibraryStore.swift"
    ENGINE = CORE + "Sync/SyncEngine.swift"

    def setUp(self):
        self.src = _read(self.STORE)
        migrations = re.findall(r'"""\n(.*?)\n\s*"""', self.src.split("static let migrations")[1].split("public init")[0], re.S)
        self.db = sqlite3.connect(":memory:")
        for m in migrations:
            self.db.executescript(m)               # the real v1..v4 scripts from the Swift file
        self.db.execute("INSERT INTO media_items (id, is_favorite, payload) VALUES (1, 0, '{}')")
        self.db.execute("INSERT INTO media_items (id, is_favorite, payload) VALUES (2, 1, '{}')")

    def test_schema_v4_adds_the_flag(self):
        self.assertIn("schemaVersion: Int64 = 4", self.src)
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(media_items)")]
        self.assertIn("is_favorite_dirty", cols)
        self.assertEqual(self.db.execute("SELECT is_favorite_dirty FROM media_items WHERE id = 1").fetchone()[0], 0)

    def test_toggle_flags_the_row_and_survives(self):
        sql = _swift_sql(self.src, "func setFavoriteLocally")
        self.assertIn("is_favorite_dirty = 1", sql)
        self.db.execute(sql.replace("?", "1", 1).replace("?", "'{}'", 1).replace("?", "1", 1))
        row = self.db.execute("SELECT is_favorite, is_favorite_dirty FROM media_items WHERE id = 1").fetchone()
        self.assertEqual(row, (1, 1))
        pend = _swift_sql(self.src, "func dirtyFavorites")
        self.assertEqual(self.db.execute(pend).fetchall(), [(1, 1)])

    def _flag(self, id_, value):
        self.db.execute("UPDATE media_items SET is_favorite = ?, is_favorite_dirty = 1 WHERE id = ?", (value, id_))

    def _ack(self, id_, uploaded):
        """favoriteUploaded's compare-and-swap, with the SQL taken from the source."""
        body = self.src[self.src.index("func favoriteUploaded"):]
        check = re.search(r'db\.query\("(SELECT 1 FROM media_items[^"]*)"', body).group(1)
        clear = re.search(r'db\.run\("(UPDATE media_items SET is_favorite_dirty = 0 WHERE id = \?)"', body.split("try db.transaction")[1]).group(1)
        still = self.db.execute(check.replace("?", "%s") % (id_, int(uploaded))).fetchone() is not None
        if still:
            self.db.execute(clear.replace("?", str(id_)))
        return still

    def test_ack_clears_only_while_the_value_is_unchanged(self):
        self._flag(1, 1)                           # phone: favourite, offline
        self.assertTrue(self._ack(1, True))        # delivered
        self.assertEqual(self.db.execute("SELECT is_favorite_dirty FROM media_items WHERE id = 1").fetchone()[0], 0)

    def test_toggle_during_upload_stays_queued(self):
        self._flag(1, 1)
        sent = True                                # PUT in flight with value 1 ...
        self._flag(1, 0)                           # ... user un-hearts before the answer
        self.assertFalse(self._ack(1, sent), "the late answer must not mark the newer toggle clean")
        self.assertEqual(self.db.execute("SELECT is_favorite, is_favorite_dirty FROM media_items WHERE id = 1").fetchone(), (0, 1))

    def test_incoming_page_does_not_revert_a_queued_favourite(self):
        up = self.src[self.src.index("case .media(let m):"):self.src.index("case .event(let e):")]
        self.assertIn("is_favorite_dirty", up.split("INSERT OR REPLACE")[0], "must read the flag before replacing the row")
        self.assertIn("if favoritePending { m.isFavorite =", up)
        self.assertIn(".init(favoritePending)", up, "REPLACE would reset the flag to 0 otherwise")
        self.assertIn("INSERT OR REPLACE INTO media_items", up)
        self.assertIn("is_favorite_dirty)", up)

    def test_404_drops_the_flag_without_the_cas(self):
        body = self.src[self.src.index("func favoriteUploaded"):]
        self.assertIn("if gone {", body)
        self.assertIn("SET is_favorite_dirty = 0 WHERE id = ?", body.split("if gone {")[1].split("var landed")[0])

    def test_engine_queues_then_pushes_in_the_loop_and_offline_throws_nothing(self):
        eng = _read(self.ENGINE)
        fav = eng[eng.index("public func setFavorite"):eng.index("public func pushFavorites")]
        self.assertIn("store.setFavoriteLocally", fav)
        self.assertNotIn("client.setFavorite", fav, "the network call must not run inline (offline used to diverge)")
        self.assertIn("Task { try? await self.pushFavorites() }", fav)
        loop = eng[eng.index("public func start()"):]
        self.assertLess(loop.index("self.pushEdits()"), loop.index("self.pushFavorites()"))
        self.assertLess(loop.index("self.pushFavorites()"), loop.index("self.set(.live)"))
        push = eng[eng.index("public func pushFavorites"):eng.index("/// The sync loop")]
        self.assertIn("favoriteUploaded(f.id, uploaded: f.value)", push)
        self.assertIn("pushFavoritesAgain", push)
        self.assertIn("SyncError.server(404", push)

    def test_app_model_falls_back_to_the_replica_without_an_engine(self):
        m = _read(UI + "Root/AppModel.swift")
        t = m[m.index("func toggleFavorite"):]
        self.assertIn("engine.setFavorite", t)
        self.assertIn("setFavoriteLocally", t)
        self.assertIn("pendingFavorites", m)


# ── 2. viewer swipe ───────────────────────────────────────────────────────────

class ViewerSwipe(unittest.TestCase):
    def setUp(self):
        self.loader = _read(UI + "Components/ImageLoader.swift")
        self.viewer = _read(UI + "Viewer/ViewerView.swift")
        self.video = _read(UI + "Viewer/VideoPageController.swift")

    def test_shared_download_is_cancelled_with_its_last_waiter(self):
        mf = self.loader[self.loader.index("public func mediaFile"):self.loader.index("nonisolated static func pruneOriginals")]
        self.assertIn("withTaskCancellationHandler", mf)
        self.assertIn("pending.waiters += 1", mf)
        self.assertIn("func dropFileWaiter", mf)
        self.assertIn("p.task.cancel()", mf, "cancelling a detached task has to be explicit: it never inherits")
        self.assertNotIn("let result = await task.value", mf, "the old un-cancellable await")

    def test_photo_page_requests_the_original_only_while_visible_and_settled(self):
        self.assertIn("private func startOriginal()", self.viewer)
        so = self.viewer[self.viewer.index("private func startOriginal()"):]
        so = so[:so.index("override func")] if "override func" in so[:2000] else so[:1800]
        self.assertIn("guard visible", so)
        self.assertIn("settleNanos", so)
        reload = self.viewer[self.viewer.index("    func reload() {"):self.viewer.index("private func startThumbnail")]
        self.assertNotIn("images.original(", reload, "reload() (run by preloaded neighbours) must not fetch originals")

    def test_disappearing_page_cancels_everything_it_started(self):
        dd = self.viewer[self.viewer.index("override func viewDidDisappear"):self.viewer.index("private func currentEditKey")]
        for token in ("visible = false", "loadTask?.cancel()", "originalTask?.cancel()", "detailTask?.cancel()", "livePrefetch?.cancel()"):
            self.assertIn(token, dd)
        da = self.viewer[self.viewer.index("override func viewDidAppear"):self.viewer.index("/// The PHLivePhoto of a camera-roll")]
        self.assertIn("visible = true", da)
        self.assertIn("livePrefetch = Task", da, "the Live motion prefetch used to be a fire-and-forget Task")

    def test_thumbnail_never_paints_over_the_original(self):
        st = self.viewer[self.viewer.index("private func startThumbnail"):self.viewer.index("private func startOriginal")]
        self.assertIn("if !self.hasOriginal", st)

    def test_video_page_streams_only_on_screen_and_releases_on_leave(self):
        vdl = self.video[self.video.index("override func viewDidLoad"):self.video.index("private func buildBar")]
        self.assertNotIn("preparePlayer()", vdl, "preloaded neighbours opened a stream each")
        self.assertIn("schedulePrepare()", self.video[self.video.index("override func viewDidAppear"):])
        rel = self.video[self.video.index("private func releasePlayer"):self.video.index("private func preparePlayer")]
        for token in ("prepareTask?.cancel()", "fallbackTask?.cancel()", "tokens.clear()", "replaceCurrentItem(with: nil)", "prepared = false"):
            self.assertIn(token, rel)
        dd = self.video[self.video.index("override func viewDidDisappear"):self.video.index("    func pageDidHide() {")]
        self.assertIn("releasePlayer()", dd)

    def test_stream_loader_cancel_all_cancels_requests(self):
        s = _read(UI + "Viewer/MediaStreamLoader.swift")
        ca = s[s.index("func cancelAll"):s.index("// ── AVAssetResourceLoaderDelegate")]
        self.assertIn("t.cancel()", ca)


# ── 3. Bonjour ────────────────────────────────────────────────────────────────

class Bonjour(unittest.TestCase):
    def test_txt_record_carries_the_pin_and_stays_small(self):
        import lan_discovery as ld
        txt = ld.txt_record("AB" * 32, "lib-1", "My PC", 1)
        self.assertEqual(txt["fp"], ("ab" * 32))
        self.assertEqual((txt["lib"], txt["v"], txt["name"]), ("lib-1", "1", "My PC"))
        for k, v in txt.items():
            self.assertLess(len(f"{k}={v}".encode()), 255)
        self.assertEqual(ld.SERVICE_TYPE, "_kairos._tcp.local.")

    def test_labels_are_dns_safe(self):
        import lan_discovery as ld
        self.assertNotIn(".", ld._label("a.b.c"))
        self.assertLessEqual(len(ld._label("é" * 80).encode("utf-8")), 63)
        self.assertEqual(ld._label(""), "Kairos")
        self.assertRegex(ld._host_label("Mikis PC (home)"), r"^[A-Za-z0-9-]+$")

    def test_service_info(self):
        try:
            import zeroconf  # noqa: F401
        except ImportError:
            self.skipTest("zeroconf not installed")
        import lan_discovery as ld
        info = ld.build_info(52790, ["192.168.1.20", "10.0.0.5"], "cd" * 32, "lib-1", "TEST-PC", 1)
        self.assertEqual(info.port, 52790)
        self.assertEqual(info.type, "_kairos._tcp.local.")
        self.assertEqual(sorted(info.parsed_addresses()), ["10.0.0.5", "192.168.1.20"])
        self.assertEqual(_props(info)["fp"], "cd" * 32)
        self.assertIsNone(ld.build_info(52790, [], "x", "y", "z", 1))

    def test_address_filter_skips_loopback_and_link_local(self):
        import lan_discovery as ld
        for ip in ld.local_addresses():
            self.assertFalse(ip.startswith(("127.", "169.254.", "0.")))

    def test_without_zeroconf_the_bridge_just_does_not_advertise(self):
        import builtins
        import lan_discovery as ld
        real = builtins.__import__

        def no_zc(name, *a, **k):
            if name.split(".")[0] == "zeroconf":
                raise ImportError(name)
            return real(name, *a, **k)
        builtins.__import__ = no_zc
        try:
            self.assertFalse(ld.Advertiser(1, "f", "l", "n").start())
        finally:
            builtins.__import__ = real

    def test_live_round_trip_on_this_machine(self):
        """Advertise for real and find ourselves through a second Zeroconf (skipped when the
        machine has no usable multicast, e.g. a locked-down CI box)."""
        try:
            from zeroconf import IPVersion, ServiceBrowser, Zeroconf
        except ImportError:
            self.skipTest("zeroconf not installed")
        import lan_discovery as ld
        adv = ld.Advertiser(52791, "ef" * 32, "lib-rt", "RT-" + str(os.getpid()), 1)
        if not adv.start() or not adv.active:
            adv.stop()
            self.skipTest("no multicast-capable interface")
        zc = Zeroconf(ip_version=IPVersion.V4Only)
        try:
            found = {}

            class L:
                def add_service(self, z, t, n):
                    info = z.get_service_info(t, n, timeout=2000)
                    if info is not None:
                        found[n] = info

                def update_service(self, *a):
                    pass

                def remove_service(self, *a):
                    pass
            ServiceBrowser(zc, ld.SERVICE_TYPE, L())
            deadline = time.time() + 8
            while time.time() < deadline and not any(i.port == 52791 for i in found.values()):
                time.sleep(0.2)
            mine = [i for i in found.values() if i.port == 52791]
            if not mine:
                self.skipTest("mDNS multicast is blocked here")
            self.assertEqual(_props(mine[0])["fp"], "ef" * 32)
        finally:
            zc.close()
            adv.stop()
        self.assertFalse(adv.active)

    def test_server_advertises_only_when_asked_and_stops_with_the_server(self):
        import database
        import shutil
        import tempfile
        import sync_server as ss
        tmp = tempfile.mkdtemp(prefix="kairos-blockc-")
        old = database.DB_PATH
        database.DB_PATH = os.path.join(tmp, "photos.db")
        database.init_db()
        srv = ss.SyncServer(host="127.0.0.1", port=0, state_dir=os.path.join(tmp, "sync"),
                            packs_dir=os.path.join(tmp, "packs"), use_tls=False, name="T",
                            inbox_dir=os.path.join(tmp, "inbox"))
        try:
            self.assertIsNone(srv.advertiser, "tests and standalone builds must not multicast by default")
            calls = []

            class Stub:
                def start(self):
                    calls.append("start")
                    return True

                def stop(self):
                    calls.append("stop")
            srv.advertiser = Stub()
            self.assertTrue(srv.advertise())
            srv.shutdown()
            self.assertEqual(calls, ["start", "stop"])
            self.assertIsNone(srv.advertiser)
        finally:
            database.DB_PATH = old
            shutil.rmtree(tmp, ignore_errors=True)

    def test_desktop_app_path_advertises_and_tests_do_not(self):
        import inspect
        import sync_server as ss
        self.assertIs(inspect.signature(ss.start).parameters["advertise"].default, False)
        self.assertIn("advertise=True", inspect.getsource(ss.start_async))

    def test_packaging_and_plist(self):
        self.assertIn("zeroconf", _read("requirements.txt"))
        self.assertIn("lan_discovery", _read("app.spec"))
        yml = _read("ios/project.yml")
        self.assertRegex(yml, r"NSBonjourServices:\s*\n\s*- _kairos\._tcp")
        self.assertIn("NSLocalNetworkUsageDescription", yml)

    def test_phone_matches_on_the_certificate_and_relocates_the_shared_client(self):
        d = _read(CORE + "Sync/DesktopDiscovery.swift")
        self.assertIn('"_kairos._tcp"', d)
        self.assertIn("advert.fingerprint == endpoint.fingerprint.lowercased()", d)
        self.assertIn("bonjourWithTXTRecord", d)
        self.assertIn("ip.version = .v4", d)
        c = _read(CORE + "Sync/SyncClient.swift")
        self.assertIn("func relocate(host: String, port: Int)", c)
        self.assertIn("let ep = endpoint", c, "request() must read the endpoint once, under the lock")
        e = _read(CORE + "Sync/SyncEngine.swift")
        self.assertIn("relocateAfter(error)", e)
        self.assertIn("isConnectivity", e)
        self.assertIn("backoff = 1", e.split("relocateAfter(error)")[1][:200], "reconnect at once after a move")
        self.assertIn("func nudge()", e)
        a = _read(UI + "Root/AppModel.swift")
        self.assertIn("DesktopDiscovery.find(matching: ep)", a)
        self.assertIn("func endpointMoved", a)
        self.assertIn("NWPathMonitor", a)
        self.assertIn("UserDefaults.standard.set(data, forKey: Self.endpointKey)", a[a.index("func endpointMoved"):][:300], "the new address is persisted")

    def test_stop_detaches_callbacks_of_a_replaced_engine(self):
        e = _read(CORE + "Sync/SyncEngine.swift")
        stop = e[e.index("public func stop()"):]
        self.assertIn("onStatus = nil", stop)
        self.assertIn("onRelocated = nil", stop)
        self.assertIn("old.stop()", _read(UI + "Root/AppModel.swift"))


# ── 4. contrast + Dynamic Type ────────────────────────────────────────────────

def _lum(h):
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * lin((h >> 16) & 255) + 0.7152 * lin((h >> 8) & 255) + 0.0722 * lin(h & 255)


def _ratio(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


class Typography(unittest.TestCase):
    def setUp(self):
        self.src = _read(UI + "Theme/Tokens.swift")

    def _palette(self, name):
        return int(re.search(rf"static let {name} = Color\(hex: 0x([0-9A-Fa-f]{{6}})\)", self.src).group(1), 16)

    def test_tertiary_text_meets_wcag_aa_on_every_dark_surface(self):
        m = re.search(r"textTertiary = Color\(dark: 0x([0-9A-Fa-f]{6}), light: 0x([0-9A-Fa-f]{6})\)", self.src)
        self.assertIsNotNone(m, "textTertiary must be an adaptive colour")
        dark, light = int(m.group(1), 16), int(m.group(2), 16)
        for name in ("background", "surface", "surface2", "surface3", "placeholder", "sidebar"):
            self.assertGreaterEqual(_ratio(dark, self._palette(name)), 4.5, f"dark tertiary on {name}")
        for bg in (0xFFFFFF, 0xF2F2F7, 0xE5E5EA):
            self.assertGreaterEqual(_ratio(light, bg), 4.5, f"light tertiary on {bg:06x}")

    def test_secondary_stays_above_tertiary_in_the_hierarchy_and_passes(self):
        sec = self._palette("textSecondary")
        for name in ("surface", "surface2", "surface3"):
            self.assertGreaterEqual(_ratio(sec, self._palette(name)), 4.5)

    def test_dynamic_colour_helper_follows_the_interface_style(self):
        self.assertIn("init(dark: UInt32, light: UInt32)", self.src)
        self.assertIn("userInterfaceStyle == .light", self.src)

    def test_headers_and_counters_scale_with_dynamic_type(self):
        self.assertRegex(self.src, r"sectionHeader = Font\.headline")
        self.assertRegex(self.src, r"counter = Font\.caption\.weight\(\.semibold\)\.monospacedDigit\(\)")
        grid = _read(UI + "Library/LibraryGridView.swift")
        head = grid[grid.index("struct SectionHeader"):grid.index("static func date")]
        self.assertNotIn(".system(size:", head)
        self.assertIn("Kairos.Typography.counter", head)
        self.assertIn(".accessibilityAddTraits(.isHeader)", head)
        self.assertIn("Kairos.Typography.badge", grid)
        self.assertIn("@ScaledMetric", _read(UI + "Events/TripMapView.swift"))

    def test_no_fixed_point_size_text_left_where_this_block_touched(self):
        for rel in ("Library/LibraryGridView.swift", "Events/TripMapView.swift", "People/PeopleView.swift"):
            for line in _read(UI + rel).splitlines():
                if "Text(" in line and ".system(size:" in line:
                    self.fail(f"{rel}: {line.strip()}")
        viewer = _read(UI + "Viewer/ViewerView.swift")
        self.assertNotIn('Label("LIVE", systemImage: "livephoto")\n                        .font(.system(size:', viewer)


if __name__ == "__main__":
    unittest.main()
