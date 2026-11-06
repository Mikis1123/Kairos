#if os(iOS)
import CoreGraphics
import UIKit

/// One photo with native iOS zoom physics: UIScrollView's pinch, rubber-banding past the edges
/// and below Fit, and the spring settle back. No custom physics is written here. The point is
/// to feel exactly like Photos.
///
/// Content coordinates are photo PIXELS of the edited full-resolution image (`pixelSize`), so
/// the zoom scale has a fixed meaning:
///     fit    = the photo fills the screen (minimumZoomScale)
///     1:1    = 1 / screenScale: one photo pixel per device pixel (100 %)
///     max    = 4 / screenScale (400 %), but never less than 4 × fit, so small images still zoom
/// The bitmap shown can be smaller than pixelSize (a thumbnail, the screen pass). It is stretched
/// over the same content rect, and `onNeedsDetail` asks for a sharper one when the zoom outruns
/// it.
///
/// Paging: the pager's scroll view is horizontal too. At Fit this view cannot scroll
/// horizontally (alwaysBounceHorizontal = false), so swipes page. Zoomed in, it pans to its
/// edge first and then hands the drag to the pager. UIKit does that for nested same-axis scroll
/// views (Apple's PhotoScroller design).
final class ZoomingImageView: UIScrollView, UIScrollViewDelegate {
    let imageView = UIImageView()
    private(set) var pixelSize: CGSize = .zero
    private(set) var bitmapScale: CGFloat = 1          // bitmap pixels per content pixel
    var onSingleTap: (() -> Void)?
    var onZoomedChange: ((Bool) -> Void)?
    var onNeedsDetail: (() -> Void)?
    private var wasZoomed = false
    private var lastBounds: CGSize = .zero

    static let maxPercent: CGFloat = 4                  // 400 % of 1:1
    static let detailHeadroom: CGFloat = 1.15           // ask for more pixels past this oversampling

    override init(frame: CGRect) {
        super.init(frame: frame)
        delegate = self
        showsVerticalScrollIndicator = false
        showsHorizontalScrollIndicator = false
        decelerationRate = .fast
        bouncesZoom = true
        bounces = true
        alwaysBounceVertical = false
        alwaysBounceHorizontal = false
        contentInsetAdjustmentBehavior = .never
        backgroundColor = .clear
        imageView.contentMode = .scaleToFill
        imageView.isUserInteractionEnabled = false
        imageView.layer.minificationFilter = .trilinear   // no shimmer at Fit on a large bitmap
        addSubview(imageView)

        let double = UITapGestureRecognizer(target: self, action: #selector(doubleTapped(_:)))
        double.numberOfTapsRequired = 2
        addGestureRecognizer(double)
        let single = UITapGestureRecognizer(target: self, action: #selector(singleTapped))
        single.require(toFail: double)
        addGestureRecognizer(single)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

    var screenScale: CGFloat { window?.screen.scale ?? traitCollection.displayScale }
    var fitScale: CGFloat {
        guard pixelSize.width > 0, pixelSize.height > 0, bounds.width > 0, bounds.height > 0 else { return 1 }
        return min(bounds.width / pixelSize.width, bounds.height / pixelSize.height)
    }
    var oneToOneScale: CGFloat { 1 / max(1, screenScale) }
    var isZoomedIn: Bool { zoomScale > minimumZoomScale * 1.01 }

    /// Shows `image` stretched over a photo of `pixelSize` pixels. When the size changes (the
    /// thumbnail is replaced by the original), the visible part of the photo stays where it was.
    func display(_ image: CGImage, pixelSize newSize: CGSize) {
        guard newSize.width > 0, newSize.height > 0 else { return }
        let ui = UIImage(cgImage: image)
        bitmapScale = CGFloat(image.width) / newSize.width
        if newSize == pixelSize {
            imageView.image = ui
            return
        }
        // the viewport in photo-relative terms before the swap
        let wasZoomedIn = isZoomedIn && pixelSize.width > 0
        let relZoom = wasZoomedIn ? zoomScale / fitScale : 1
        let centre = CGPoint(x: (contentOffset.x + bounds.width / 2) / max(1, contentSize.width),
                             y: (contentOffset.y + bounds.height / 2) / max(1, contentSize.height))
        pixelSize = newSize
        imageView.image = ui
        minimumZoomScale = 1                            // setting zoomScale clamps to the old range
        maximumZoomScale = 1
        zoomScale = 1
        imageView.frame = CGRect(origin: .zero, size: newSize)
        contentSize = newSize
        configureScales()
        if wasZoomedIn {
            zoomScale = min(maximumZoomScale, fitScale * relZoom)
            let cx = centre.x * contentSize.width - bounds.width / 2
            let cy = centre.y * contentSize.height - bounds.height / 2
            contentOffset = clampedOffset(CGPoint(x: cx, y: cy))
        } else {
            zoomScale = minimumZoomScale
        }
        centreContent()
    }

    private func configureScales() {
        let fit = fitScale
        minimumZoomScale = fit
        maximumZoomScale = max(Self.maxPercent * oneToOneScale, fit * 4)
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        guard bounds.size != lastBounds, pixelSize.width > 0 else { return }
        let atFit = !isZoomedIn
        lastBounds = bounds.size
        configureScales()                               // rotation / split view: Fit changes
        if atFit || zoomScale < minimumZoomScale { zoomScale = minimumZoomScale }
        centreContent()
    }

    /// Smaller than the screen on an axis -> centred with insets (contentInset keeps the
    /// rubber band symmetric, unlike moving the image view's frame).
    private func centreContent() {
        let w = contentSize.width, h = contentSize.height
        let ix = max(0, (bounds.width - w) / 2), iy = max(0, (bounds.height - h) / 2)
        let inset = UIEdgeInsets(top: iy, left: ix, bottom: iy, right: ix)
        if contentInset != inset { contentInset = inset }
    }

    private func clampedOffset(_ p: CGPoint) -> CGPoint {
        let maxX = max(-contentInset.left, contentSize.width - bounds.width + contentInset.right)
        let maxY = max(-contentInset.top, contentSize.height - bounds.height + contentInset.bottom)
        return CGPoint(x: min(max(p.x, -contentInset.left), maxX), y: min(max(p.y, -contentInset.top), maxY))
    }

    // ── UIScrollViewDelegate ──

    func viewForZooming(in scrollView: UIScrollView) -> UIView? { imageView }

    func scrollViewDidZoom(_ scrollView: UIScrollView) {
        centreContent()
        let zoomed = isZoomedIn
        if zoomed != wasZoomed {
            wasZoomed = zoomed
            onZoomedChange?(zoomed)
        }
    }

    func scrollViewDidEndZooming(_ scrollView: UIScrollView, with view: UIView?, at scale: CGFloat) {
        // device pixels needed per content pixel vs what the bitmap holds
        if scale * screenScale > bitmapScale * Self.detailHeadroom { onNeedsDetail?() }
    }

    // ── taps ──

    @objc private func singleTapped() { onSingleTap?() }

    /// Fit <-> 1:1 at the tapped point. If 1:1 is barely larger than Fit (a small image), the
    /// zoom goes to 2.5 × Fit instead, so the tap always does something visible.
    @objc private func doubleTapped(_ g: UITapGestureRecognizer) {
        if isZoomedIn {
            setZoomScale(minimumZoomScale, animated: true)
            return
        }
        var target = oneToOneScale
        if target < minimumZoomScale * 1.5 { target = minimumZoomScale * 2.5 }
        target = min(target, maximumZoomScale)
        let p = g.location(in: imageView)                  // content (photo pixel) coordinates
        let size = CGSize(width: bounds.width / target, height: bounds.height / target)
        zoom(to: CGRect(x: p.x - size.width / 2, y: p.y - size.height / 2, width: size.width, height: size.height),
             animated: true)
    }

    func resetZoom() {
        if pixelSize.width > 0 { setZoomScale(minimumZoomScale, animated: false) }
    }
}
#endif
