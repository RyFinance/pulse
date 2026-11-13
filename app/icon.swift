// Renders the app icon: icon.swift out.png BG FG GLYPH FONT
import Cocoa
let a = CommandLine.arguments
func hex(_ s: String) -> NSColor {
    var v: UInt64 = 0; Scanner(string: s.replacingOccurrences(of: "#", with: "")).scanHexInt64(&v)
    return NSColor(srgbRed: CGFloat((v >> 16) & 255) / 255, green: CGFloat((v >> 8) & 255) / 255,
                   blue: CGFloat(v & 255) / 255, alpha: 1)
}
let size = 1024.0, inset = 100.0, side = size - 2 * inset
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 1024, pixelsHigh: 1024, bitsPerSample: 8,
                           samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                           bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
let rect = NSRect(x: inset, y: inset, width: side, height: side)
let shape = NSBezierPath(roundedRect: rect, xRadius: side * 0.225, yRadius: side * 0.225)
NSGraphicsContext.current!.saveGraphicsState()
let shadow = NSShadow(); shadow.shadowBlurRadius = 24; shadow.shadowOffset = NSSize(width: 0, height: -10)
shadow.shadowColor = NSColor.black.withAlphaComponent(0.35); shadow.set()
hex(a[2]).setFill(); shape.fill()
NSGraphicsContext.current!.restoreGraphicsState()
let font = NSFont(name: a[5], size: side * 0.56) ?? NSFont.systemFont(ofSize: side * 0.56, weight: .bold)
let str = NSAttributedString(string: a[4], attributes: [.font: font, .foregroundColor: hex(a[3])])
let b = str.boundingRect(with: rect.size, options: [.usesLineFragmentOrigin, .usesFontLeading])
str.draw(at: NSPoint(x: rect.midX - b.width / 2, y: rect.midY - b.height / 2 - font.descender * 0.35))
NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: a[1]))
