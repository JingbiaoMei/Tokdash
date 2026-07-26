import SwiftUI

@main
struct TokdashCompanionApp: App {
    @StateObject private var store = CompanionStore()

    var body: some Scene {
        MenuBarExtra {
            ContentView()
                .environmentObject(store)
                .frame(width: 352)
                .task { store.refresh() }
        } label: {
            Image(systemName: "chart.bar.fill")
                .accessibilityLabel("Tokdash")
        }
        .menuBarExtraStyle(.window)

        Settings {
            SettingsView()
                .environmentObject(store)
        }
    }
}
