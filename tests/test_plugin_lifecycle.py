"""The five-reload test, made mechanical.

QGIS cleans up nothing. The canonical way to find a broken ``unload()`` is to reload five
times with Plugin Reloader and count toolbar buttons; five buttons means it is wrong. That
loop is reproduced here against a recording ``iface``, so the regression is caught in CI
rather than by someone noticing a duplicated panel.
"""

from __future__ import annotations

from qgis_label_client.plugin import MENU_NAME, LabelClientPlugin


def _cycle(plugin: LabelClientPlugin) -> None:
    plugin.initGui()
    plugin.unload()


def test_one_cycle_attaches_and_detaches_everything(fake_iface):
    plugin = LabelClientPlugin(fake_iface)

    plugin.initGui()
    assert len(fake_iface.toolbar_icons) == 1
    assert len(fake_iface.docks) == 1
    # Two menu entries: the panel toggle and the imagery refresh.
    assert [menu for menu, _ in fake_iface.plugin_menu] == [MENU_NAME, MENU_NAME]

    plugin.unload()
    assert fake_iface.toolbar_icons == []
    assert fake_iface.docks == []
    assert fake_iface.plugin_menu == []


def test_five_reloads_leave_nothing_behind(fake_iface):
    for _ in range(5):
        _cycle(LabelClientPlugin(fake_iface))

    assert fake_iface.toolbar_icons == []
    assert fake_iface.docks == []
    assert fake_iface.plugin_menu == []


def test_unload_is_idempotent(fake_iface):
    plugin = LabelClientPlugin(fake_iface)
    plugin.initGui()
    plugin.unload()
    plugin.unload()
    assert fake_iface.toolbar_icons == []


def test_every_attachment_registers_a_teardown(fake_iface):
    plugin = LabelClientPlugin(fake_iface)
    plugin.initGui()
    # dock, toolbar icon, two menu entries.
    assert plugin.teardown.labels == [
        "dock widget",
        "toolbar icon",
        "menu: panel",
        "menu: refresh imagery",
    ]
    plugin.unload()
    assert len(plugin.teardown) == 0


def test_in_flight_tasks_are_cancelled_on_unload(fake_iface):
    plugin = LabelClientPlugin(fake_iface)
    plugin.initGui()

    task = plugin.tasks.run("pretend work", lambda feedback: None)
    assert len(plugin.tasks) == 1

    plugin.unload()
    assert task.isCanceled()
    assert len(plugin.tasks) == 0
