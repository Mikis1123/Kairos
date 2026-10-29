// Vrstva videa prohlížeče (viewer_hud.MediaCanvas): snímky zůstávají na GPU,
// QQuickWidget se skládá jako běžný widget, takže skleněný HUD nad ním funguje.
// Dva výstupy: aktivní přehrávač + předem načtené sousední video (okamžitý start bez dekódování).
import QtQuick
import QtMultimedia

Rectangle {
    color: "black"
    VideoOutput {
        objectName: "video"
        anchors.fill: parent
        fillMode: VideoOutput.PreserveAspectFit
    }
    VideoOutput {
        objectName: "video2"
        anchors.fill: parent
        fillMode: VideoOutput.PreserveAspectFit
        visible: false
    }
}
