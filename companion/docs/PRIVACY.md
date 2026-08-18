# Tokdash Companion privacy policy

Partner Center accepts either a privacy policy URL or policy text entered directly into
the submission. A hosted URL is the more durable option and is what this text is written
for; GitHub Pages on the Tokdash repository is the least-effort route. Note that a raw
GitHub Markdown file renders as source rather than a policy page, so publish it as a
page rather than linking the .md directly.

Published at <https://tokdash.github.io/privacy/>.

Last updated: 2026-08-18.

---

## What Tokdash Companion is

Tokdash Companion is a notification-area (system tray) client for Tokdash, a service you
run on your own machine or on a machine you control. The companion displays usage and
quota figures that the Tokdash service reports. It is a viewer; it does not itself gather
usage data.

## Data collected

None. Tokdash Companion collects no personal information, sends no analytics or telemetry,
and has no server component operated by the developer.

## Data stored on your device

Your settings — server addresses and labels, notification thresholds, language, launch-at-
login preference, and update-check preference — are stored on your device. In the
Microsoft Store build they live in the app's own package storage and are removed when you
uninstall the app.

Usage and quota figures are held in memory while the app is running and are not written to
disk.

## Network connections

Tokdash Companion connects only to:

1. **The Tokdash server addresses you configure.** The default is `http://127.0.0.1:55423`
   on your own machine. If you add another address, the companion connects to that one
   too. It connects to nothing you have not entered.
2. **GitHub's public releases API**, only in the portable (non-Store) build, and only if
   you turn update checking on. It is off by default. The request is unauthenticated and
   sends no information about you or your usage. The Microsoft Store build never contacts
   GitHub; the Store delivers updates instead.

No data is sent to the developer.

## What the app does not do

It does not read browser data, credential stores, or session files. It does not scan your
network or enumerate ports. It does not run background services beyond its own tray
process, and it does not install or download additional software.

## Permissions

The app runs as a full-trust desktop application (`runFullTrust`). This is what a packaged
WPF desktop application requires; it is not used to access anything described above as
out of scope.

## Website analytics

This section is about the tokdash.github.io **website**, not the app.

The website uses Google Analytics, which sets cookies and collects standard usage data
such as pages visited, approximate location, browser, and device. That is a property of
the website only. The application itself contains no analytics and no telemetry, and
visiting the website is not required to use it.

Keeping the two separate matters: "the app collects nothing" and "the website uses
analytics" are both true, and a policy that stated only the first would be misleading to
anyone who inspected the site.

## Children

The app is not directed at children and collects no information from anyone.

## Changes

Material changes to this policy will be published at this URL with an updated date.

## Contact

Open an issue at <https://github.com/JingbiaoMei/Tokdash/issues>.
