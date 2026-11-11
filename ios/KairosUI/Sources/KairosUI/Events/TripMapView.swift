import KairosCore
import SwiftUI

/// Trip mode (Režim cesta) on the iPhone: the journey's route on the offline map, stop by stop
/// in capture order, with day chapters and a play button that tours the stops like a light
/// version of the desktop's Cesta film.
///
/// Data: the desktop's /v1/journey (stops grouped by journey.build_stops, legs from imported GPX
/// tracks), cached for offline use. Without the desktop and without a cached copy, the journey is
/// rebuilt from the replica's photo positions (JourneyLocal), so the route always draws.
/// Legs with a real GPX line are solid; legs drawn from where a stop was left to where the next
/// was reached are dashed, so the map never pretends to know the road.
struct TripMapView: View {
    let event: Event
    var autoplay = false
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    @State private var journey: Journey?
    @State private var loading = true
    @State private var day: Int?                   // nil = the whole trip
    @State private var focus: Int?
    @State private var playing = false
    @State private var viewer: ViewerRoute?

    private var current: (stops: [Journey.Stop], tracks: [String: [[Double]]]) {
        guard let j = journey else { return ([], [:]) }
        if let n = day, let d = j.days.first(where: { $0.n == n }), !d.stops.isEmpty { return (d.stops, d.tracks) }
        return (j.stops, j.tracks)
    }

    var body: some View {
        let (stops, tracks) = current
        ZStack {
            Kairos.Palette.mapBackground.ignoresSafeArea()
            TripMapLibreView(stops: stops, tracks: tracks, focus: focus) { i in
                Haptics.tick()
                playing = false
                focus = i
            }
            .ignoresSafeArea()
            VStack(spacing: 0) {
                header
                Spacer()
                if !stops.isEmpty { carousel(stops) }
            }
            if loading {
                ProgressView().controlSize(.large).tint(Kairos.Palette.accent)
            } else if stops.isEmpty {
                EmptyStateView(symbol: "point.topleft.down.to.point.bottomright.curvepath", title: "No route",
                               message: "This trip has no photos with a location.")
                    .frame(maxHeight: 260)
                    .kairosCard()
                    .padding(Kairos.Space.xl)
            }
        }
        .preferredColorScheme(.dark)
        .task {
            journey = await model.journey(for: event)
            loading = false
            if autoplay, !(journey?.stops.isEmpty ?? true) { playing = true }
        }
        .task(id: PlayKey(playing: playing, day: day)) {
            guard playing else { return }
            let count = current.stops.count
            var i = (focus ?? -1) + 1
            if i >= count { i = 0 }
            while playing, i < count, !Task.isCancelled {
                withAnimation(Kairos.Motion.standard) { focus = i }
                try? await Task.sleep(nanoseconds: 2_600_000_000)
                i += 1
            }
            if !Task.isCancelled { playing = false }
        }
        .fullScreenCoverCompat(item: $viewer) { ViewerView(ids: $0.ids, start: $0.start) }
    }

    private struct PlayKey: Equatable {
        let playing: Bool
        let day: Int?
    }

    // ── chrome ──

    private var header: some View {
        VStack(alignment: .leading, spacing: Kairos.Space.sm) {
            HStack(spacing: Kairos.Space.sm) {
                Button { dismiss() } label: { Image(systemName: "xmark").padding(10) }
                    .accessibilityLabel("Close")
                VStack(alignment: .leading, spacing: 0) {
                    Text(journey?.title.isEmpty == false ? journey!.title : (event.title ?? "Trip"))
                        .font(Kairos.Typography.headline)
                        .foregroundStyle(Kairos.Palette.text)
                        .lineLimit(1)
                    Text(subtitle)
                        .font(Kairos.Typography.caption)
                        .foregroundStyle(Kairos.Palette.textSecondary)
                        .lineLimit(1)
                }
                Spacer()
                Button {
                    Haptics.release()
                    playing.toggle()
                } label: {
                    Image(systemName: playing ? "pause.fill" : "play.fill").padding(10)
                }
                .disabled(current.stops.isEmpty)
                .accessibilityLabel(playing ? "Pause the tour" : "Play the route")
            }
            .font(.system(size: 17, weight: .semibold))
            .foregroundStyle(.white)
            .padding(.horizontal, 4)
            .background(.ultraThinMaterial, in: Capsule())
            if let days = journey?.days, days.count > 1 {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: Kairos.Space.sm) {
                        chip("Whole trip", selected: day == nil) { select(day: nil) }
                        ForEach(days, id: \.n) { d in
                            chip("Day \(d.n)" + (d.city.isEmpty ? "" : " · \(d.city)"), selected: day == d.n) {
                                select(day: d.n)
                            }
                        }
                    }
                }
            }
        }
        .padding(.horizontal, Kairos.Space.lg)
        .padding(.top, Kairos.Space.sm)
    }

    private var subtitle: String {
        let s = current.stops
        guard let first = s.first?.start else { return "" }
        let last = s.last?.end ?? first
        let span = first.prefix(10) == last.prefix(10) ? String(first.prefix(10)) : "\(first.prefix(10)) – \(last.prefix(10))"
        return "\(s.count) stops · \(span)"
    }

    private func select(day n: Int?) {
        Haptics.tick()
        playing = false
        focus = nil
        withAnimation(Kairos.Motion.standard) { day = n }
    }

    private func chip(_ title: String, selected: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title)
                .font(Kairos.Typography.callout)
                .padding(.horizontal, Kairos.Space.md).padding(.vertical, Kairos.Space.sm)
                .foregroundStyle(selected ? Kairos.Palette.onAccent : Kairos.Palette.text)
                .background(selected ? AnyShapeStyle(Kairos.Palette.accentFill) : AnyShapeStyle(.ultraThinMaterial),
                            in: Capsule())
        }
        .buttonStyle(.plain)
    }

    private func carousel(_ stops: [Journey.Stop]) -> some View {
        ScrollViewReader { proxy in
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: Kairos.Space.md) {
                    ForEach(Array(stops.enumerated()), id: \.offset) { i, stop in
                        StopCard(n: i + 1, stop: stop, selected: focus == i)
                            .id(i)
                            .onTapGesture {
                                Haptics.tick()
                                playing = false
                                if focus == i, !stop.photos.isEmpty {
                                    viewer = ViewerRoute(ids: stop.photos, start: stop.best ?? stop.photos[0])
                                } else {
                                    withAnimation(Kairos.Motion.standard) { focus = i }
                                }
                            }
                    }
                }
                .padding(.horizontal, Kairos.Space.lg)
                .padding(.vertical, Kairos.Space.md)
            }
            .onChange(of: focus) { _, f in
                if let f { withAnimation(Kairos.Motion.standard) { proxy.scrollTo(f, anchor: .center) } }
            }
        }
        .background(LinearGradient(colors: [.clear, .black.opacity(0.65)], startPoint: .top, endPoint: .bottom)
            .ignoresSafeArea())
    }
}

/// One stop in the carousel: its best photo, the stop number, place and time. A second tap on
/// the selected stop opens its photos.
struct StopCard: View {
    let n: Int
    let stop: Journey.Stop
    let selected: Bool
    /// The number badge grows with Dynamic Type (its digits are text), capped so the card holds it.
    @ScaledMetric(relativeTo: .caption) private var badgeSize: CGFloat = 22

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ZStack(alignment: .topLeading) {
                if let best = stop.best {
                    ThumbnailView(id: best)
                } else {
                    Kairos.Palette.surface2
                }
                Text("\(n)")
                    .font(Kairos.Typography.counter)
                    .foregroundStyle(Kairos.Palette.onAccent)
                    .frame(width: min(badgeSize, 40), height: min(badgeSize, 40))
                    .background(Kairos.Palette.accentFill, in: Circle())
                    .padding(6)
            }
            .frame(width: 132, height: 96)
            .clipped()
            VStack(alignment: .leading, spacing: 1) {
                Text(stop.city.isEmpty ? "Stop \(n)" : stop.city)
                    .font(Kairos.Typography.callout.weight(.semibold))
                    .foregroundStyle(Kairos.Palette.text)
                    .lineLimit(1)
                Text("\(stop.start.map { String($0.dropFirst(11).prefix(5)) } ?? "") · \(stop.count) photos")
                    .font(Kairos.Typography.caption)
                    .foregroundStyle(Kairos.Palette.textSecondary)
                    .lineLimit(1)
            }
            .padding(.horizontal, Kairos.Space.sm)
            .padding(.vertical, 6)
        }
        .frame(width: 132)
        .background(.ultraThinMaterial)
        .clipShape(RoundedRectangle(cornerRadius: Kairos.Radius.md, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: Kairos.Radius.md, style: .continuous)
            .strokeBorder(selected ? Kairos.Palette.accent : Kairos.Palette.hairline, lineWidth: selected ? 2 : 1))
        .scaleEffect(selected ? 1.04 : 1)
        .animation(Kairos.Motion.spring, value: selected)
        .accessibilityElement(children: .combine)
        .accessibilityHint(selected ? "Opens the photos of this stop" : "Shows this stop on the map")
    }
}

#if canImport(MapLibre) && os(iOS)
import CoreLocation
import MapLibre
import UIKit

/// MapLibre over the same offline style as the Map tab (bundled world + region packs), with the
/// route as lines (GPX legs solid, inferred legs dashed, both over a dark casing) and the stops
/// as numbered markers. The camera fits the trip, then follows the focused stop.
struct TripMapLibreView: UIViewRepresentable {
    let stops: [Journey.Stop]
    let tracks: [String: [[Double]]]
    let focus: Int?
    var onTapStop: (Int) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeUIView(context: Context) -> MLNMapView {
        let packs = OfflinePacks.base + OfflinePacks.installed()
        let map = MLNMapView(frame: .zero, styleURL: OfflineMapStyle.write(packs: packs))
        map.backgroundColor = UIColor(red: 6 / 255, green: 7 / 255, blue: 12 / 255, alpha: 1)
        map.delegate = context.coordinator
        map.logoView.isHidden = true
        map.attributionButton.tintColor = .systemGray
        map.compassViewPosition = .topRight
        context.coordinator.map = map
        let tap = UITapGestureRecognizer(target: context.coordinator, action: #selector(Coordinator.tapped(_:)))
        for case let r as UITapGestureRecognizer in map.gestureRecognizers ?? [] where r.numberOfTapsRequired == 2 {
            tap.require(toFail: r)
        }
        map.addGestureRecognizer(tap)
        return map
    }

    func updateUIView(_ map: MLNMapView, context: Context) {
        let c = context.coordinator
        c.parent = self
        if c.shownStops != stops || c.shownTracks != tracks {
            c.apply(fit: true)
        }
        if c.shownFocus != focus {
            c.shownFocus = focus
            c.follow()
        }
    }

    @MainActor
    final class Coordinator: NSObject, MLNMapViewDelegate {
        var parent: TripMapLibreView
        weak var map: MLNMapView?
        var shownStops: [Journey.Stop] = []
        var shownTracks: [String: [[Double]]] = [:]
        var shownFocus: Int?
        private var styleReady = false
        static let routeId = "trip-route", stopsId = "trip-stops"

        init(_ parent: TripMapLibreView) { self.parent = parent }

        nonisolated func mapView(_ mapView: MLNMapView, didFinishLoading style: MLNStyle) {
            MainActor.assumeIsolated { self.styleLoaded(style) }
        }

        private func styleLoaded(_ style: MLNStyle) {
            let route = MLNShapeSource(identifier: Self.routeId, features: [], options: nil)
            let stops = MLNShapeSource(identifier: Self.stopsId, features: [], options: nil)
            style.addSource(route)
            style.addSource(stops)
            let accent = UIColor(Kairos.Palette.accent)

            let casing = MLNLineStyleLayer(identifier: "trip-casing", source: route)
            casing.lineColor = NSExpression(forConstantValue: UIColor.black.withAlphaComponent(0.55))
            casing.lineWidth = NSExpression(forConstantValue: 7)
            casing.lineJoin = NSExpression(forConstantValue: "round")
            casing.lineCap = NSExpression(forConstantValue: "round")
            style.addLayer(casing)

            let solid = MLNLineStyleLayer(identifier: "trip-gpx", source: route)
            solid.predicate = NSPredicate(format: "gpx == 1")
            solid.lineColor = NSExpression(forConstantValue: accent)
            solid.lineWidth = NSExpression(forConstantValue: 3.5)
            solid.lineJoin = NSExpression(forConstantValue: "round")
            solid.lineCap = NSExpression(forConstantValue: "round")
            style.addLayer(solid)

            let dashed = MLNLineStyleLayer(identifier: "trip-inferred", source: route)
            dashed.predicate = NSPredicate(format: "gpx == 0")
            dashed.lineColor = NSExpression(forConstantValue: accent.withAlphaComponent(0.85))
            dashed.lineWidth = NSExpression(forConstantValue: 3)
            dashed.lineDashPattern = NSExpression(forConstantValue: [2, 1.6])
            dashed.lineCap = NSExpression(forConstantValue: "round")
            style.addLayer(dashed)

            let markers = MLNSymbolStyleLayer(identifier: "trip-markers", source: stops)
            markers.iconImageName = NSExpression(forKeyPath: "img")
            markers.iconAllowsOverlap = NSExpression(forConstantValue: true)
            markers.iconIgnoresPlacement = NSExpression(forConstantValue: true)
            markers.symbolSortKey = NSExpression(forKeyPath: "sel")
            style.addLayer(markers)

            styleReady = true
            apply(fit: true)
        }

        func apply(fit: Bool) {
            shownStops = parent.stops
            shownTracks = parent.tracks
            guard styleReady, let map, let style = map.style,
                  let route = style.source(withIdentifier: Self.routeId) as? MLNShapeSource,
                  let stopSource = style.source(withIdentifier: Self.stopsId) as? MLNShapeSource else { return }
            let stops = parent.stops
            var legs: [MLNPolylineFeature] = []
            for i in stops.indices.dropFirst() {
                var coords = Journey.leg(i, stops: stops, tracks: parent.tracks)
                    .map { CLLocationCoordinate2D(latitude: $0.lat, longitude: $0.lon) }
                guard coords.count >= 2 else { continue }
                let f = MLNPolylineFeature(coordinates: &coords, count: UInt(coords.count))
                let gpx = (parent.tracks[String(i)]?.count ?? 0) >= 2
                f.attributes = ["gpx": gpx ? 1 : 0, "leg": i]
                legs.append(f)
            }
            route.shape = MLNShapeCollectionFeature(shapes: legs)
            let scale = min(3, map.traitCollection.displayScale)
            stopSource.shape = MLNShapeCollectionFeature(shapes: stops.enumerated().map { i, s in
                for sel in [false, true] {
                    let name = Self.imageName(i, sel)
                    if style.image(forName: name) == nil {
                        style.setImage(Self.marker(n: i + 1, selected: sel, scale: scale), forName: name)
                    }
                }
                let f = MLNPointFeature()
                f.coordinate = CLLocationCoordinate2D(latitude: s.lat, longitude: s.lon)
                let selected = parent.focus == i
                f.attributes = ["img": Self.imageName(i, selected), "i": i, "sel": selected ? 1 : 0]
                return f
            })
            if fit { fitAll() }
        }

        static func imageName(_ i: Int, _ selected: Bool) -> String { "trip-stop-\(i + 1)\(selected ? "-sel" : "")" }

        private func fitAll() {
            guard let map, let first = parent.stops.first else { return }
            var w = first.lon, e = first.lon, s = first.lat, n = first.lat
            for st in parent.stops {
                w = min(w, st.lon); e = max(e, st.lon); s = min(s, st.lat); n = max(n, st.lat)
            }
            if parent.stops.count == 1 {
                map.setCenter(CLLocationCoordinate2D(latitude: first.lat, longitude: first.lon), zoomLevel: 11, animated: false)
                return
            }
            map.setVisibleCoordinateBounds(MLNCoordinateBounds(sw: CLLocationCoordinate2D(latitude: s, longitude: w),
                                                               ne: CLLocationCoordinate2D(latitude: n, longitude: e)),
                                           edgePadding: UIEdgeInsets(top: 150, left: 50, bottom: 230, right: 50),
                                           animated: true, completionHandler: nil)
        }

        func follow() {
            apply(fit: false)                                   // the selected marker grows
            guard let map, let i = parent.focus, parent.stops.indices.contains(i) else { return }
            let s = parent.stops[i]
            let camera = MLNMapCamera(lookingAtCenter: CLLocationCoordinate2D(latitude: s.lat, longitude: s.lon),
                                      altitude: max(4000, min(map.camera.altitude, 60000)), pitch: 35, heading: map.camera.heading)
            map.fly(to: camera, withDuration: 1.6, completionHandler: nil)
        }

        @objc func tapped(_ g: UITapGestureRecognizer) {
            guard let map, g.state == .ended else { return }
            let p = g.location(in: map)
            let rect = CGRect(x: p.x - 22, y: p.y - 22, width: 44, height: 44)
            let hit = map.visibleFeatures(in: rect, styleLayerIdentifiers: ["trip-markers"]).first
            if let i = (hit?.attribute(forKey: "i") as? NSNumber)?.intValue { parent.onTapStop(i) }
        }

        /// A numbered disc: accent fill, white ring; bigger and brighter when selected.
        static func marker(n: Int, selected: Bool, scale: CGFloat) -> UIImage {
            let d: CGFloat = selected ? 34 : 26
            let fmt = UIGraphicsImageRendererFormat()
            fmt.scale = scale
            return UIGraphicsImageRenderer(size: CGSize(width: d + 6, height: d + 6), format: fmt).image { ctx in
                let r = CGRect(x: 3, y: 3, width: d, height: d)
                let cg = ctx.cgContext
                cg.setShadow(offset: CGSize(width: 0, height: 1), blur: 3, color: UIColor.black.withAlphaComponent(0.5).cgColor)
                UIColor(Kairos.Palette.accent).setFill()
                cg.fillEllipse(in: r)
                cg.setShadow(offset: .zero, blur: 0, color: nil)
                UIColor.white.withAlphaComponent(selected ? 1 : 0.85).setStroke()
                cg.setLineWidth(selected ? 2.5 : 1.8)
                cg.strokeEllipse(in: r.insetBy(dx: 1, dy: 1))
                let text = "\(n)" as NSString
                let font = UIFont.systemFont(ofSize: selected ? 15 : 12, weight: .bold)
                let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: UIColor.white]
                let size = text.size(withAttributes: attrs)
                text.draw(at: CGPoint(x: r.midX - size.width / 2, y: r.midY - size.height / 2), withAttributes: attrs)
            }
        }
    }
}
#else
/// Builds without MapLibre (macOS `swift build`): the stops as a list.
struct TripMapLibreView: View {
    let stops: [Journey.Stop]
    let tracks: [String: [[Double]]]
    let focus: Int?
    var onTapStop: (Int) -> Void

    var body: some View {
        Kairos.Palette.mapBackground
            .overlay(Text("\(stops.count) stops").foregroundStyle(Kairos.Palette.textSecondary))
    }
}
#endif
