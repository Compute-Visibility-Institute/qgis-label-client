# Updating CVI Label Client in QGIS

**Save your edits and project, upgrade the plugin, restart QGIS, then reconnect.**
An ordinary plugin update does not require deleting your editable layers or importing
the labels again.

## 1. Save your work

1. Use **Save Layer Edits** for each edited remote layer and check that saving succeeds.
2. Save your QGIS project (`.qgz` or `.qgs`).
3. If saving edits fails, keep QGIS open and export the edited layer to a local
   GeoPackage before restarting. Saving the project alone does not preserve a native
   layer's unsaved edit buffer.

## 2. Upgrade through the plugin manager

1. Open **Plugins → Manage and Install Plugins → Settings**.
2. Enable **Show also experimental plugins** while CVI Label Client is published as
   experimental.
3. Ensure the CVI repository is enabled. If it is missing, click **Add** and use:

   ```text
   https://github.com/Compute-Visibility-Institute/qgis-label-client/releases/latest/download/plugins.xml
   ```

4. Click **Reload all repositories** to fetch the current release list.
5. Open **Upgradeable**, select **CVI Label Client**, and click **Upgrade Plugin**
   or **Upgrade Experimental Plugin**, whichever QGIS offers.
6. Check the installed version in the plugin's details against the latest release:

   https://github.com/Compute-Visibility-Institute/qgis-label-client/releases/latest

You do not need to uninstall first. Reinstalling without refreshing the repository
can simply install the same version again. QGIS's update checks notify you about
available versions; use the upgrade action to install them.

The available tabs and upgrade actions are described in the
[QGIS plugin manager guide](https://docs.qgis.org/3.44/en/docs/user_manual/plugins/plugins.html).

### If the repository update is unavailable

Download the packaged **`qgis_label_client.<version>.zip`** from the release's
**Assets**, then choose **Plugins → Manage and Install Plugins → Install from ZIP**.
Use the plugin ZIP, not GitHub's **Source code (zip)** download: the release package
contains the configuration needed for Google sign-in.

## 3. Restart and reconnect

1. Close QGIS and open it again. A restart is recommended after an upgrade so the
   session uses the newly installed Python code.
2. Reopen your saved project and open the **CVI Label Client** panel.
3. Click **Connect**. If your session needs a new Google sign-in, choose
   **Sign in with Google**, select your work account, and then **Connect**.
4. Confirm the intended server and history track before editing. Production data
   uses the `default` track; `dev` is for testing. An update preserves saved choices.
5. Click **Refresh imagery URLs** if purchased imagery needs fresh access links.

You can follow the same reconnect steps whenever you reopen QGIS later, even when
you have not updated the plugin.

### Keep the existing layers

The saved project retains remote layer sources and their connection references.
The plugin recognises its layers using stored metadata, so renaming a layer does
not require importing it again. Ordinary upgrades do not require running bootstrap
or publishing your local source layers again; doing so can create duplicate labels.

**Connect is not a full database download.** The provider fetches features as the
map or attribute table requests them, subject to the layer's filters and history
track. If a clean layer still shows old data, use QGIS's layer refresh/reload action.
Save outstanding edits before reloading a layer.

## If the server address changes

A deployment move is a separate operation. A plugin upgrade does not rewrite a
saved API URL or the layer sources in an existing project.

Wait for the platform operator's migration instructions. Save your edits and a
backup project first; moving to another server may require reloading the remote
collections and restoring their styles and time filters. See
[upgrading an existing profile after a deployment move](../README.md#upgrading-an-existing-profile-after-the-deployment-moves).

## Release status

This guide describes the update procedure, not a new plugin release. As of
**22 September 2026**, the new startup connection prompt, durable unpushed-edit
recovery and push-all-local workflow are under development and are **not included
in a published release yet**. Do not rely on those features to protect unsaved
work until a release explicitly includes them.
