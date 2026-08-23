"""Test fixtures. Installs the QGIS stubs before any plugin module is imported."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from qgis_stubs import install, reset  # noqa: E402

# Must happen at import time: pytest imports every test module before running anything,
# and those imports pull in qgis_label_client modules that import qgis at module level.
install()


@pytest.fixture(autouse=True)
def _clean_stub_state():
    reset()
    yield
    reset()


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


class FakeInterface:
    """Records every attach and detach ``QgisInterface`` offers a plugin.

    This is what makes the five-reload test mechanical: after five load/unload cycles the
    recorded toolbar icons, menu entries and dock widgets must all be back to zero.
    """

    def __init__(self) -> None:
        self.toolbar_icons: list[object] = []
        self.plugin_menu: list[tuple[str, object]] = []
        self.docks: list[object] = []
        self.messages: list[tuple[str, str, object]] = []

    # --- attach -----------------------------------------------------------
    def addToolBarIcon(self, action):  # noqa: N802 - Qt naming
        self.toolbar_icons.append(action)
        return len(self.toolbar_icons)

    def addPluginToMenu(self, menu, action):  # noqa: N802
        self.plugin_menu.append((menu, action))

    def addDockWidget(self, area, dock):  # noqa: N802
        self.docks.append(dock)

    # --- detach -----------------------------------------------------------
    def removeToolBarIcon(self, action):  # noqa: N802
        if action in self.toolbar_icons:
            self.toolbar_icons.remove(action)

    def removePluginMenu(self, menu, action):  # noqa: N802
        entry = (menu, action)
        if entry in self.plugin_menu:
            self.plugin_menu.remove(entry)

    def removeDockWidget(self, dock):  # noqa: N802
        if dock in self.docks:
            self.docks.remove(dock)

    # --- accessors --------------------------------------------------------
    def mainWindow(self):  # noqa: N802
        return None

    def messageBar(self):  # noqa: N802
        return self

    def pushMessage(self, title, text, level=None, duration=0):  # noqa: N802
        self.messages.append((title, text, level))

    def mapCanvas(self):  # noqa: N802
        return None

    def activeLayer(self):  # noqa: N802
        return None


@pytest.fixture
def fake_iface() -> FakeInterface:
    return FakeInterface()
