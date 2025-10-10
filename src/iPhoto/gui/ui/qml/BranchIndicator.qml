import QtQuick 2.15
import QtQuick.Shapes 1.15

Item {
    id: root
    width: 16
    height: 16

    /*!\n        Angle of the indicator in degrees.\n        0 degrees renders a ">" chevron, 90 degrees renders a "v" chevron.\n    */
    property real angle: 0

    /*! Color of the indicator stroke. */
    property color indicatorColor: "#2b2b2b"

    antialiasing: true

    Shape {
        anchors.fill: parent
        antialiasing: true

        transform: Rotation {
            origin.x: root.width / 2
            origin.y: root.height / 2
            angle: root.angle
        }

        ShapePath {
            strokeWidth: 2
            strokeColor: root.indicatorColor
            capStyle: ShapePath.RoundCap

            startX: 6
            startY: 4
            PathLine { x: 10; y: 8 }
            PathLine { x: 6; y: 12 }
        }
    }
}
