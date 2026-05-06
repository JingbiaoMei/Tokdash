from __future__ import annotations

import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


DEFAULT_APP_NAME = "Tokdash"
DEFAULT_BUNDLE_ID = "io.github.jingbiaomei.tokdash.native"


def _write_icon(resources_dir: Path) -> str | None:
    icon_png = Path(__file__).resolve().parent / "static" / "icons" / "icon-512.png"
    if not icon_png.exists():
        return None

    iconset = resources_dir / "Tokdash.iconset"
    iconset.mkdir(parents=True, exist_ok=True)
    sizes = [
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
    ]

    try:
        for size, name in sizes:
            subprocess.run(
                ["sips", "-z", str(size), str(size), str(icon_png), "--out", str(iconset / name)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(resources_dir / "Tokdash.icns")],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        shutil.copy2(icon_png, resources_dir / "icon-512.png")
        shutil.rmtree(iconset, ignore_errors=True)
        return None

    shutil.rmtree(iconset, ignore_errors=True)
    return "Tokdash.icns"


def _write_info_plist(contents_dir: Path, app_name: str, executable_name: str, icon_file: str | None) -> None:
    info = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": app_name,
        "CFBundleExecutable": executable_name,
        "CFBundleIdentifier": DEFAULT_BUNDLE_ID,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": app_name,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "10.15",
        "NSHighResolutionCapable": True,
    }
    if icon_file:
        info["CFBundleIconFile"] = icon_file
    with (contents_dir / "Info.plist").open("wb") as handle:
        plistlib.dump(info, handle)


def _copy_python_resources(resources_dir: Path) -> Path:
    bundled_python_dir = resources_dir / "python"
    bundled_python_dir.mkdir(parents=True)
    shutil.copytree(
        Path(__file__).resolve().parent,
        bundled_python_dir / "tokdash",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return bundled_python_dir


def _swift_source(app_name: str, python_executable: str) -> str:
    return f"""
import SwiftUI
import Foundation

struct Metric: Decodable, Identifiable {{
    var id: String {{ label }}
    let label: String
    let value: String
    let delta: String
}}

struct AppRow: Decodable, Identifiable {{
    var id: String {{ name }}
    let name: String
    let tokens: String
    let tokens_raw: Int
    let cost: String
    let messages: String
}}

struct ModelRow: Decodable, Identifiable {{
    var id: String {{ name }}
    let name: String
    let tokens: String
    let tokens_raw: Int
    let input: String
    let output: String
    let cache: String
    let cost: String
}}

struct DashboardData: Decodable {{
    let period: String
    let refreshed: String
    let range_label: String
    let date_from: String
    let date_to: String
    let metrics: [Metric]
    let breakdown: [AppRow]
    let models: [ModelRow]
}}

@main
struct {app_name}App: App {{
    var body: some Scene {{
        WindowGroup {{
            DashboardView()
                .frame(minWidth: 920, minHeight: 640)
        }}
        .windowStyle(.hiddenTitleBar)
    }}
}}

struct DashboardView: View {{
    @State private var selectedPeriod = "today"
    @State private var dashboard: DashboardData?
    @State private var errorMessage: String?
    @State private var isLoading = false

    private let periods = [("Today", "today"), ("Last 7 Days", "week"), ("This Month", "month")]

    var body: some View {{
        ZStack {{
            MeshGradient(width: 3, height: 3, points: [
                [0.0, 0.0], [0.5, 0.0], [1.0, 0.0],
                [0.0, 0.5], [0.55, 0.45], [1.0, 0.55],
                [0.0, 1.0], [0.5, 1.0], [1.0, 1.0]
            ], colors: [
                Color(red: 0.92, green: 0.96, blue: 0.93),
                Color(red: 0.98, green: 0.91, blue: 0.78),
                Color(red: 0.82, green: 0.91, blue: 0.95),
                Color(red: 0.95, green: 0.97, blue: 0.92),
                Color(red: 1.00, green: 0.98, blue: 0.90),
                Color(red: 0.88, green: 0.94, blue: 0.91),
                Color(red: 0.90, green: 0.88, blue: 0.82),
                Color(red: 0.77, green: 0.88, blue: 0.83),
                Color(red: 0.96, green: 0.93, blue: 0.86)
            ])
            .ignoresSafeArea()

            VStack(alignment: .leading, spacing: 22) {{
                header
                if let dashboard {{
                    metricGrid(dashboard)
                    HStack(alignment: .top, spacing: 18) {{
                        modelPanel(dashboard)
                        appPanel(dashboard)
                    }}
                    footer(dashboard)
                }} else {{
                    ContentUnavailableView("No usage data", systemImage: "chart.bar.xaxis")
                        .tokdashGlass(cornerRadius: 28)
                }}
            }}
            .padding(28)
        }}
        .task {{ load() }}
    }}

    private var header: some View {{
        HStack(alignment: .top) {{
            VStack(alignment: .leading, spacing: 4) {{
                Text("Tokdash")
                    .font(.system(size: 34, weight: .bold, design: .rounded))
                Text("Local AI coding usage")
                    .foregroundStyle(.secondary)
            }}
            Spacer()
            Picker("Range", selection: $selectedPeriod) {{
                ForEach(periods, id: \\.1) {{ label, value in
                    Text(label).tag(value)
                }}
            }}
            .pickerStyle(.segmented)
            .frame(width: 330)
            .onChange(of: selectedPeriod) {{ _, _ in load() }}

            Button {{ load() }} label: {{
                Label(isLoading ? "Refreshing" : "Refresh", systemImage: "arrow.clockwise")
            }}
            .buttonStyle(.borderedProminent)
        }}
    }}

    private func metricGrid(_ data: DashboardData) -> some View {{
        HStack(spacing: 14) {{
            ForEach(data.metrics) {{ metric in
                VStack(alignment: .leading, spacing: 8) {{
                    Text(metric.label.uppercased())
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                    Text(metric.value)
                        .font(.system(size: 34, weight: .bold, design: .rounded))
                        .contentTransition(.numericText())
                    Text(metric.delta)
                        .font(.caption)
                        .foregroundStyle(metric.delta.hasPrefix("+") ? .green : .secondary)
                }}
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(18)
                .tokdashGlass(cornerRadius: 26)
            }}
        }}
    }}

    private func modelPanel(_ data: DashboardData) -> some View {{
        VStack(alignment: .leading, spacing: 14) {{
            Text("Model Flow")
                .font(.title3.bold())
            ForEach(data.models.prefix(8)) {{ model in
                modelBar(model, maxTokens: maxModelTokens(data.models))
            }}
        }}
        .padding(18)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .tokdashGlass(cornerRadius: 30)
    }}

    private func modelBar(_ model: ModelRow, maxTokens: Int) -> some View {{
        VStack(alignment: .leading, spacing: 6) {{
            HStack {{
                Text(model.name)
                    .font(.subheadline.weight(.semibold))
                    .lineLimit(1)
                Spacer()
                Text(model.tokens)
                    .font(.subheadline.monospacedDigit())
                    .foregroundStyle(.secondary)
            }}
            GeometryReader {{ proxy in
                ZStack(alignment: .leading) {{
                    Capsule().fill(.white.opacity(0.34))
                    Capsule()
                        .fill(LinearGradient(colors: [.green.opacity(0.75), .teal.opacity(0.78)], startPoint: .leading, endPoint: .trailing))
                        .frame(width: max(8, proxy.size.width * CGFloat(Double(model.tokens_raw) / Double(max(maxTokens, 1)))))
                }}
            }}
            .frame(height: 12)
            HStack(spacing: 12) {{
                Text("In \\(model.input)")
                Text("Out \\(model.output)")
                Text("Cache \\(model.cache)")
                Spacer()
                Text(model.cost)
            }}
            .font(.caption)
            .foregroundStyle(.secondary)
        }}
    }}

    private func appPanel(_ data: DashboardData) -> some View {{
        VStack(alignment: .leading, spacing: 14) {{
            Text("Apps")
                .font(.title3.bold())
            ForEach(data.breakdown.prefix(8)) {{ app in
                HStack(spacing: 12) {{
                    Circle()
                        .fill(.teal.opacity(0.64))
                        .frame(width: 10, height: 10)
                    VStack(alignment: .leading, spacing: 2) {{
                        Text(app.name)
                            .font(.subheadline.weight(.semibold))
                        Text("\\(app.messages) messages")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }}
                    Spacer()
                    VStack(alignment: .trailing, spacing: 2) {{
                        Text(app.tokens)
                            .font(.subheadline.monospacedDigit())
                        Text(app.cost)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }}
                }}
                Divider().opacity(0.35)
            }}
        }}
        .padding(18)
        .frame(minWidth: 300, idealWidth: 300, maxWidth: 300, maxHeight: .infinity, alignment: .topLeading)
        .tokdashGlass(cornerRadius: 30)
    }}

    private func footer(_ data: DashboardData) -> some View {{
        HStack {{
            Text("\\(data.range_label) · \\(data.date_from) to \\(data.date_to)")
            Spacer()
            if let errorMessage {{
                Text(errorMessage).foregroundStyle(.red)
            }} else {{
                Text("Updated \\(data.refreshed)")
            }}
        }}
        .font(.caption)
        .foregroundStyle(.secondary)
    }}

    private func maxModelTokens(_ models: [ModelRow]) -> Int {{
        max(models.map(\\.tokens_raw).max() ?? 1, 1)
    }}

    private func load() {{
        let period = selectedPeriod
        isLoading = true
        errorMessage = nil
        Task.detached {{
            do {{
                let data = try loadDashboardData(period: period)
                await MainActor.run {{
                    withAnimation(.smooth(duration: 0.32)) {{
                        dashboard = data
                        isLoading = false
                    }}
                }}
            }} catch {{
                await MainActor.run {{
                    errorMessage = error.localizedDescription
                    isLoading = false
                }}
            }}
        }}
    }}

    nonisolated private func loadDashboardData(period: String) throws -> DashboardData {{
        guard let resourcePath = Bundle.main.resourcePath else {{
            throw NSError(domain: "Tokdash", code: 1, userInfo: [NSLocalizedDescriptionKey: "Missing app resources"])
        }}
        let pythonRoot = URL(fileURLWithPath: resourcePath).appendingPathComponent("python").path
        let code = "import sys; sys.path.insert(0, '\\(pythonRoot)'); from tokdash.macos_native_app import json_main; raise SystemExit(json_main())"
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "{python_executable}")
        process.arguments = ["-c", code, period]
        let pipe = Pipe()
        let errorPipe = Pipe()
        process.standardOutput = pipe
        process.standardError = errorPipe
        try process.run()
        process.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        if process.terminationStatus != 0 {{
            let errorData = errorPipe.fileHandleForReading.readDataToEndOfFile()
            let message = String(data: errorData, encoding: .utf8) ?? "Python helper failed"
            throw NSError(domain: "Tokdash", code: Int(process.terminationStatus), userInfo: [NSLocalizedDescriptionKey: message])
        }}
        return try JSONDecoder().decode(DashboardData.self, from: data)
    }}
}}

extension View {{
    @ViewBuilder
    func tokdashGlass(cornerRadius: CGFloat) -> some View {{
        let shape = RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
        if #available(macOS 26.0, *) {{
            self.glassEffect(.regular.interactive(), in: shape)
        }} else {{
            self.background(.regularMaterial, in: shape)
                .overlay(shape.stroke(.white.opacity(0.34), lineWidth: 1))
        }}
    }}
}}
"""


def _create_swiftui_app(output_path: Path, app_name: str, python_executable: str) -> bool:
    swiftc = shutil.which("swiftc")
    if not swiftc:
        return False

    contents_dir = output_path / "Contents"
    macos_dir = contents_dir / "MacOS"
    resources_dir = contents_dir / "Resources"
    macos_dir.mkdir(parents=True)
    resources_dir.mkdir(parents=True)
    _copy_python_resources(resources_dir)
    icon_file = _write_icon(resources_dir)
    _write_info_plist(contents_dir, app_name, app_name, icon_file)

    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / f"{app_name}.swift"
        source.write_text(_swift_source(app_name, python_executable), encoding="utf-8")
        try:
            subprocess.run([swiftc, "-parse-as-library", str(source), "-o", str(macos_dir / app_name)], check=True)
        except Exception:
            return False
    return True


def _create_shell_script_app(output_path: Path, app_name: str) -> None:
    contents_dir = output_path / "Contents"
    macos_dir = contents_dir / "MacOS"
    resources_dir = contents_dir / "Resources"
    macos_dir.mkdir(parents=True)
    resources_dir.mkdir(parents=True)
    _copy_python_resources(resources_dir)

    icon_file = _write_icon(resources_dir)
    _write_info_plist(contents_dir, app_name, app_name, icon_file)

    executable = macos_dir / app_name
    import_root = contents_dir / "Resources" / "python"
    launcher_code = (
        "import sys; "
        f"sys.path.insert(0, {str(import_root)!r}); "
        "from tokdash.macos_native_app import main; "
        "raise SystemExit(main())"
    )
    executable.write_text(
        f"""#!/bin/sh
exec "{sys.executable}" -c "{launcher_code}" "$@" >>/tmp/tokdash-native-app.log 2>&1
""",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def create_macos_app_bundle(
    output: str | os.PathLike[str] | None = None,
    *,
    app_name: str = DEFAULT_APP_NAME,
    force: bool = False,
) -> Path:
    output_path = Path(output).expanduser() if output else Path.cwd() / f"{app_name}.app"
    output_path = output_path.resolve()
    if output_path.suffix != ".app":
        output_path = output_path / f"{app_name}.app"

    if output_path.exists():
        if not force:
            raise FileExistsError(f"{output_path} already exists. Use --force to replace it.")
        shutil.rmtree(output_path)

    if not _create_swiftui_app(output_path, app_name, sys.executable):
        if output_path.exists():
            shutil.rmtree(output_path)
        _create_shell_script_app(output_path, app_name)
    return output_path
