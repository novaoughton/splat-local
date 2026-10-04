// objcap: Apple Object Capture (RealityKit PhotogrammetrySession) on a folder of frames.
//
// usage: objcap <images-dir> <output-dir> <preview|reduced|medium|full|raw> <poses.json>
//
// Writes a textured OBJ (+ MTL and texture maps) into <output-dir>, and the camera
// pose Object Capture solved for each frame into <poses.json>, both from one
// session so they share a coordinate frame. The pipeline uses the poses to move
// the mesh into COLMAP's frame, i.e. onto the splat.
//
// stdout, one line each, for the server to parse:
//   progress <0..1>
//   poses <count>
//   model <path>
//   done invalid=<n> skipped=<n>
//   error <message>
// Exit status: 0 done, 1 failed, 2 Object Capture unsupported, 64 usage.
import Foundation
import RealityKit

setvbuf(stdout, nil, _IOLBF, 0) // line-buffered even into a pipe, so progress streams

let args = CommandLine.arguments
guard args.count == 5 else {
    print("usage: objcap <images-dir> <output-dir> <preview|reduced|medium|full|raw> <poses.json>")
    exit(64)
}
let input = URL(fileURLWithPath: args[1], isDirectory: true)
let output = URL(fileURLWithPath: args[2], isDirectory: true)
let posesPath = URL(fileURLWithPath: args[4])
let details: [String: PhotogrammetrySession.Request.Detail] = [
    "preview": .preview, "reduced": .reduced, "medium": .medium, "full": .full, "raw": .raw,
]
guard let detail = details[args[3]] else { print("error unknown detail \(args[3])"); exit(64) }
guard PhotogrammetrySession.isSupported else {
    print("error Object Capture is not supported on this Mac")
    exit(2)
}

var config = PhotogrammetrySession.Configuration()
config.sampleOrdering = .sequential      // video frames: neighbours overlap
config.featureSensitivity = .high        // rooms have plain walls
config.isObjectMaskingEnabled = false    // a room, not an object on a turntable

let session: PhotogrammetrySession
do {
    session = try PhotogrammetrySession(input: input, configuration: config)
} catch {
    print("error could not start Object Capture: \(error)")
    exit(1)
}

// Sample ids follow the folder's sorted file order.
let names = ((try? FileManager.default.contentsOfDirectory(atPath: args[1])) ?? [])
    .filter { ["jpg", "jpeg", "png", "heic"].contains(($0 as NSString).pathExtension.lowercased()) }
    .sorted()

var invalid = 0, skipped = 0, lastProgress = -1.0
Task {
    do {
        for try await out in session.outputs {
            switch out {
            case .requestProgress(_, let fraction):
                if fraction - lastProgress >= 0.01 || fraction >= 1 {
                    lastProgress = fraction
                    print(String(format: "progress %.3f", fraction))
                }
            case .requestComplete(_, let result):
                switch result {
                case .poses(let poses):
                    var rows: [[String: Any]] = []
                    for (id, pose) in poses.posesBySample.sorted(by: { $0.key < $1.key }) {
                        let t = pose.translation, r = pose.rotation.vector
                        rows.append(["id": id, "name": id < names.count ? names[id] : "",
                                     "t": [t.x, t.y, t.z], "q_xyzw": [r.x, r.y, r.z, r.w]])
                    }
                    let data = try JSONSerialization.data(withJSONObject: ["poses": rows], options: [.prettyPrinted])
                    try data.write(to: posesPath)
                    print("poses \(rows.count)")
                case .modelFile(let url):
                    print("model \(url.path)")
                default:
                    break
                }
            case .requestError(_, let error):
                print("error \(error)")
                exit(1)
            case .invalidSample:
                invalid += 1
            case .skippedSample:
                skipped += 1
            case .processingComplete:
                print("done invalid=\(invalid) skipped=\(skipped)")
                exit(0)
            case .processingCancelled:
                print("error cancelled")
                exit(1)
            default:
                break
            }
        }
    } catch {
        print("error \(error)")
        exit(1)
    }
}

do {
    try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
    try session.process(requests: [.modelFile(url: output, detail: detail), .poses])
} catch {
    print("error \(error)")
    exit(1)
}
RunLoop.main.run()
