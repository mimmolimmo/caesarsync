# CaesarSync

**Experimental preview · 1.0**

CaesarSync is an independent Linux desktop sync client for **Swisscom myCloud**. It uses Swisscom's WebDAV file access. It is independently developed and is not affiliated with, endorsed by, or supported by Swisscom.

Choose specific cloud folders, work with normal local copies, and check the latest upload, download, or removal from the tray. The tray distinguishes ongoing checks from the last completed transfer and shows the transfer size.

CaesarSync watches selected local folders for saves and can upload them while a full scan is in progress. With Swisscom sign-in enabled, CaesarSync asks the desktop change feed for recently changed paths and verifies them through WebDAV. The saved checkpoint lets it request changes made while it was closed. The first connection reconciles all selected folders. **Check all folders now** in the tray triggers a manual reconciliation.

## Before you try it

- Start with a dedicated test folder and keep a separate backup. Sync propagates deletions in both directions, including files moved out of a selected local folder.
- Simultaneous edits that differ are reported in **Show activity** and both copies are kept. Make a backup of both before resolving the conflict manually.
- This preview has been tested on Ubuntu 26.04 with GNOME. Conflict handling is covered by a mock WebDAV server. Compatibility with other distributions and enforcement of conditional writes on Swisscom's live server still need verification.
- Swisscom's WebDAV access covers its file area. The Photos and Videos library is outside this client's scope.

## Install

### Ubuntu software installer

Download the `.deb` package when it is listed under **Assets** on the release page. Open it with Ubuntu App Center and click **Install**. Ubuntu may ask you to authenticate to install system software. The package manager installs any missing libraries from the configured Ubuntu repositories.

If your desktop has no graphical installer for local packages, run `sudo apt install ./caesarsync_1.0_all.deb` from your downloads folder.

### Per-user archive

Download and extract the bundle from [GitHub Releases](https://github.com/mimmolimmo/caesarsync/releases). The app requires Python 3.11+, GTK 3, a desktop with AppIndicator support, a Secret Service keyring, and `lsof`. On recent Ubuntu or Debian systems:

```sh
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 gir1.2-secret-1 gir1.2-webkit2-4.1 lsof
```

Run `./install.sh` from the extracted directory and open **CaesarSync** from your applications menu. To try it without installing, run `./launch` from that directory. Run `./launch --version` to print the version.

CaesarSync starts in the system tray and immediately uses its saved settings. Click the tray icon to view activity, open Settings, or quit.

## Configure

1. Open the settings at mycloud.ch, enable WebDAV, and set a WebDAV password. See [Swisscom's setup guide](https://www.swisscom.ch/en/residential/help/apps-and-services/mycloud-guide.html).
2. Click the CaesarSync tray icon and choose **Settings**. Enter the WebDAV address, username, and password, then connect.
3. Choose a dedicated local folder and the cloud folders to sync. Click **Start sync**. Until then, the tray shows **Sync is off**.

4. Reopen **Settings** and choose **Sign in to Swisscom for faster downloads**. Use the same myCloud account. The Swisscom session token is stored separately from the WebDAV password in the desktop keyring. If Swisscom rejects the saved session, CaesarSync opens Settings once and prompts you to sign in again. If you have yet to enable the feed, CaesarSync checks WebDAV periodically.

CaesarSync uses HTTPS and stores the WebDAV password in your desktop keyring. Local copies are readable files. Use a trusted desktop and check the address against your myCloud settings before entering credentials.

The desktop change feed is an experimental integration with Swisscom’s desktop service. It was tested with a live myCloud account for new files, deletions, and checkpoint reuse. Rename and catch-up scenarios are covered by local tests.

## Contribute

Reports from other Linux distributions and improvements to packaging and sync tests are welcome. For a bug report, include the distribution, desktop environment, CaesarSync version, and steps to reproduce. Remove credentials, private URLs, and personal filenames.

```sh
/usr/bin/python3 -m unittest discover -s tests -v
```

To build the Ubuntu package from this checkout, run `./packaging/build-deb.sh`.

Tests use a local mock WebDAV server and temporary directories. Please report security issues privately via the repository's **Security** tab.

Released under the [MIT License](LICENSE).
