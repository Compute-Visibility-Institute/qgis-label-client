"""CVI Label Client - a thin QGIS client for the labeling platform.

Copyright (C) 2026 Compute Visibility Institute

This program is free software; you can redistribute it and/or modify it under the terms
of the GNU General Public License as published by the Free Software Foundation; either
version 2 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with this program;
if not, see <https://www.gnu.org/licenses/>.

SPDX-License-Identifier: GPL-2.0-or-later
"""

from __future__ import annotations

__version__ = "0.1.0"


def classFactory(iface):  # noqa: N802 - name fixed by the QGIS plugin contract
    """Return the plugin instance. QGIS calls this once per load.

    The import is inside the function on purpose. QGIS imports this module while merely
    *listing* installed plugins, and a plugin that fails to import fails silently in the
    plugin manager -- so keeping the heavy imports until load time means a broken
    dependency shows up as a load error with a traceback in the Log Messages panel
    rather than as a plugin that quietly stops appearing.
    """
    from .plugin import LabelClientPlugin

    return LabelClientPlugin(iface)
