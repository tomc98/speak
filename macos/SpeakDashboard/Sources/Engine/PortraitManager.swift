import AppKit

struct PortraitFrames {
    let closed: NSImage
    let slight: NSImage
    let open: NSImage
}

@Observable
@MainActor
final class PortraitManager {

    private var cache: [String: PortraitFrames] = [:]
    private var inFlight: [String: Task<PortraitFrames, Never>] = [:]

    private var port: Int {
        if let env = ProcessInfo.processInfo.environment["SPEAK_PORT"],
           let p = Int(env) {
            return p
        }
        return 7865
    }

    func frames(for voiceName: String) async -> PortraitFrames {
        let key = voiceName.lowercased()

        if let cached = cache[key] {
            return cached
        }

        if let existing = inFlight[key] {
            return await existing.value
        }

        let task = Task<PortraitFrames, Never> { @MainActor in
            let result = await self.load(voiceName: key)
            self.cache[key] = result
            self.inFlight.removeValue(forKey: key)
            return result
        }
        inFlight[key] = task
        return await task.value
    }

    // MARK: - Private

    private func load(voiceName: String) async -> PortraitFrames {
        let base = "http://127.0.0.1:\(port)/portraits/\(voiceName)"
        async let closedData = fetchImage("\(base).png")
        async let slightData = fetchImage("\(base)_slight.png")
        async let openData = fetchImage("\(base)_open.png")

        let closed = await closedData
        let slight = await slightData
        let open = await openData

        guard let closed else { return fallback(for: voiceName) }
        return PortraitFrames(
            closed: closed,
            slight: slight ?? closed,
            open: open ?? closed
        )
    }

    private func fetchImage(_ urlString: String) async -> NSImage? {
        guard let url = URL(string: urlString) else { return nil }
        do {
            let (data, response) = try await URLSession.shared.data(from: url)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else { return nil }
            return NSImage(data: data)
        } catch {
            return nil
        }
    }

    private func fallback(for voiceName: String) -> PortraitFrames {
        let size = NSSize(width: 128, height: 128)
        let letter = String(voiceName.prefix(1)).uppercased()

        let colors: [NSColor] = [.systemBlue, .systemPurple, .systemTeal, .systemOrange, .systemPink, .systemGreen]
        let colorIndex = abs(voiceName.hashValue) % colors.count
        let color = colors[colorIndex]

        let img = NSImage(size: size)
        img.lockFocus()

        let path = NSBezierPath(ovalIn: NSRect(origin: .zero, size: size))
        color.setFill()
        path.fill()

        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 56, weight: .bold),
            .foregroundColor: NSColor.white,
        ]
        let str = NSAttributedString(string: letter, attributes: attrs)
        let strSize = str.size()
        let point = NSPoint(
            x: (size.width - strSize.width) / 2,
            y: (size.height - strSize.height) / 2
        )
        str.draw(at: point)

        img.unlockFocus()

        return PortraitFrames(closed: img, slight: img, open: img)
    }
}
