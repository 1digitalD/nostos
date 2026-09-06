import Foundation
import Vision

guard CommandLine.arguments.count == 2 else {
    FileHandle.standardError.write(Data("usage: vision_ocr.swift IMAGE\n".utf8))
    exit(2)
}

let imageURL = URL(fileURLWithPath: CommandLine.arguments[1])
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
request.minimumTextHeight = 0.008

do {
    let handler = VNImageRequestHandler(url: imageURL, options: [:])
    try handler.perform([request])
    let lines: [[String: Any]] = (request.results ?? []).compactMap { observation in
        guard let candidate = observation.topCandidates(1).first else { return nil }
        return ["text": candidate.string, "confidence": Double(candidate.confidence)]
    }
    let data = try JSONSerialization.data(withJSONObject: lines, options: [])
    FileHandle.standardOutput.write(data)
} catch {
    FileHandle.standardError.write(Data("Vision OCR failed\n".utf8))
    exit(1)
}
