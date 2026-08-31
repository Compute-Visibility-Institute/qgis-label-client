"""Minimal stand-ins for ``qgis`` and ``qgis.PyQt``, so tests run without QGIS.

WHAT THESE ARE FOR

The pure core (:mod:`qgis_label_client.core`) needs no stubs at all -- it imports nothing
from QGIS, which is why it holds the logic worth testing hardest. These stubs exist for
the *other* half: they let the UI and lifecycle modules be imported and exercised in CI,
which is what makes the five-reload teardown test possible on a machine with no QGIS
installed.

They are not a QGIS emulator and must not grow into one. Anything auto-generated below is
a shape, not a behaviour; only the handful of classes with hand-written bodies do
anything real, and each of those is real because a test depends on it:

* ``QgsSettings`` -- an in-memory store, so settings round-tripping can be tested;
* ``QgsTask`` / ``QgsApplication.taskManager`` -- enough to check that the runner holds a
  reference and cancels on shutdown;
* ``pyqtSignal`` -- real connect/emit, so signal wiring is exercised rather than mocked;
* ``QgsAuthMethodConfig`` / ``QgsApplication.authManager`` -- enough of the credential
  store to assert **which values end up in a config map and which do not**. That is a
  security property rather than a convenience: the ``APIHeader`` method emits every key in
  the map as an outgoing HTTP header, so the map is the difference between the history
  track reaching the server (required) and the OAuth refresh token reaching it too
  (never). It is also where the hourly token rotation is checked to reuse config ids, on
  which every saved ``.qgz`` and every loaded layer depends.

If a test starts needing more than that, it is probably a test that belongs against a
real QGIS instead.
"""

from __future__ import annotations

import itertools
import sys
import types
from typing import Any, ClassVar

# ---------------------------------------------------------------------------
# Generic shapes
# ---------------------------------------------------------------------------

_enum_counter = itertools.count(1)


class EnumNamespace:
    """Stands in for a nested Qt/QGIS enum, and for a static method.

    Attribute access yields a stable int, so scoped enum access
    (``Qt.DockWidgetArea.RightDockWidgetArea``) produces something that supports ``|``,
    ``==`` and ``int()``. Calling it yields an :class:`Stub`, so a static factory
    (``QDate.currentDate()``) works too. One object covers both because a stub cannot
    tell them apart from the outside.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._values: dict[str, int] = {}

    def __getattr__(self, item: str) -> int:
        if item.startswith("_"):
            raise AttributeError(item)
        if item not in self._values:
            self._values[item] = next(_enum_counter)
        return self._values[item]

    def __call__(self, *args: Any, **kwargs: Any) -> Stub:
        return Stub()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<EnumNamespace {self._name}>"


class StubMeta(type):
    """Metaclass giving generated classes an inexhaustible supply of class attributes."""

    def __getattr__(cls, name: str) -> EnumNamespace:
        if name.startswith("__"):
            raise AttributeError(name)
        namespace = EnumNamespace(f"{cls.__name__}.{name}")
        setattr(cls, name, namespace)
        return namespace


class Stub(metaclass=StubMeta):
    """A widget-shaped object that accepts anything done to it.

    Comparisons return ``False`` rather than raising, because real code does things like
    ``if combo.findData(x) >= 0``, and a stub that raises there would force the code under
    test to be written around the stub.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __getattr__(self, name: str) -> Stub:
        if name.startswith("__"):
            raise AttributeError(name)
        value = Stub()
        object.__setattr__(self, name, value)
        return value

    def __call__(self, *args: Any, **kwargs: Any) -> Stub:
        return Stub()

    def __bool__(self) -> bool:
        return False

    def __int__(self) -> int:
        return 0

    def __index__(self) -> int:
        return 0

    def __len__(self) -> int:
        return 0

    def __iter__(self):
        return iter(())

    def __lt__(self, other: Any) -> bool:
        return False

    def __le__(self, other: Any) -> bool:
        return False

    def __gt__(self, other: Any) -> bool:
        return False

    def __ge__(self, other: Any) -> bool:
        return False


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


class Signal:
    """A working signal: connect, disconnect, emit."""

    def __init__(self, name: str = "") -> None:
        self.name = name
        self.slots: list[Any] = []

    def connect(self, slot: Any) -> None:
        self.slots.append(slot)

    def disconnect(self, slot: Any = None) -> None:
        if slot is None:
            self.slots.clear()
        elif slot in self.slots:
            self.slots.remove(slot)

    def emit(self, *args: Any) -> None:
        for slot in list(self.slots):
            slot(*args)

    # Lets one signal be connected to another, as Qt allows.
    __call__ = emit


class pyqtSignal:  # noqa: N801 - mirrors the PyQt name exactly
    """Descriptor producing a per-instance :class:`Signal`."""

    def __init__(self, *types_: Any, **kwargs: Any) -> None:
        self._name = ""

    def __set_name__(self, owner: type, name: str) -> None:
        self._name = name

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        store = obj.__dict__.setdefault("__stub_signals__", {})
        if self._name not in store:
            store[self._name] = Signal(self._name)
        return store[self._name]


def pyqtSlot(*args: Any, **kwargs: Any):  # noqa: N802 - mirrors the PyQt name
    def decorate(func):
        return func

    return decorate


# ---------------------------------------------------------------------------
# Behaviour-bearing QGIS stubs
# ---------------------------------------------------------------------------


class QgsSettings(Stub):
    """In-memory settings store, shared across instances like the real one."""

    _store: ClassVar[dict[str, Any]] = {}

    def value(self, key: str, default: Any = None, **kwargs: Any) -> Any:
        return QgsSettings._store.get(key, default)

    def setValue(self, key: str, value: Any) -> None:  # noqa: N802 - Qt naming
        QgsSettings._store[key] = value

    @classmethod
    def reset(cls) -> None:
        cls._store.clear()


class QgsMessageLog(Stub):
    """Captures log lines so tests can assert on diagnostics."""

    records: ClassVar[list[tuple[str, str, Any]]] = []

    @staticmethod
    def logMessage(message: str, tag: str = "", level: Any = None, **kw: Any) -> None:  # noqa: N802
        QgsMessageLog.records.append((message, tag, level))


class QgsFeedback(Stub):
    """Cancellation handle."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def isCanceled(self) -> bool:  # noqa: N802 - Qt naming
        return self._cancelled


class QgsTask(Stub):
    """Enough of QgsTask to exercise the runner's reference-holding and shutdown."""

    taskCompleted = pyqtSignal()
    taskTerminated = pyqtSignal()

    def __init__(self, description: str = "", flags: Any = None) -> None:
        super().__init__()
        self._description = description
        self._cancelled = False

    def description(self) -> str:
        return self._description

    def cancel(self) -> None:
        self._cancelled = True

    def isCanceled(self) -> bool:  # noqa: N802 - Qt naming
        return self._cancelled


class _TaskManager:
    """Records submitted tasks instead of running them."""

    def __init__(self) -> None:
        self.tasks: list[Any] = []

    def addTask(self, task: Any) -> int:  # noqa: N802 - Qt naming
        self.tasks.append(task)
        return len(self.tasks)


_TASK_MANAGER = _TaskManager()


class QgsAuthMethodConfig(Stub):
    """A credential entry: id, name, method, version and the header map.

    Hand-written because the header map is the thing worth asserting about
    :mod:`qgis_label_client.auth`. Under the ``APIHeader`` method every key in it becomes
    an outgoing HTTP request header, so what is in the map and what is merely *near* it
    decides whether a long-lived refresh token is transmitted on every request.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self._id = ""
        self._name = ""
        self._method = ""
        self._version = 0
        self._config: dict[str, str] = {}

    def id(self) -> str:
        return self._id

    def setId(self, value: str) -> None:  # noqa: N802 - Qt naming
        self._id = value

    def name(self) -> str:
        return self._name

    def setName(self, value: str) -> None:  # noqa: N802
        self._name = value

    def method(self) -> str:
        return self._method

    def setMethod(self, value: str) -> None:  # noqa: N802
        self._method = value

    def version(self) -> int:
        return self._version

    def setVersion(self, value: int) -> None:  # noqa: N802
        self._version = value

    def configMap(self) -> dict[str, str]:  # noqa: N802
        return dict(self._config)

    def setConfigMap(self, mapping: dict[str, str]) -> None:  # noqa: N802
        # Replaces the whole map, exactly as the real one does. Anything less would hide
        # the bug where a rotated credential keeps the previous track's header.
        self._config = dict(mapping)

    def clone(self) -> QgsAuthMethodConfig:
        """A detached copy, because the real manager hands back copies, not references."""
        copy = QgsAuthMethodConfig()
        copy._id = self._id
        copy._name = self._name
        copy._method = self._method
        copy._version = self._version
        copy._config = dict(self._config)
        return copy


class _AuthManager:
    """Enough of ``QgsAuthManager`` to exercise storing, rotating and signing out.

    Deliberately keeps auth *settings* in a separate dictionary from the config maps,
    because that separation is the point: settings are encrypted at rest and never
    attached to a request, config maps are emitted as headers.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.configs: dict[str, QgsAuthMethodConfig] = {}
        self.settings: dict[str, tuple[Any, bool]] = {}
        #: ids passed to clearCachedConfig. An hourly rotation that does not clear the
        #: cache writes a fresh token QGIS never sends, which looks identical to no
        #: rotation at all -- so the test asserts the call happened.
        self.cleared: list[str] = []
        self.disabled = False
        self.master_password_set = True
        self._ids = itertools.count(1)

    def isDisabled(self) -> bool:  # noqa: N802
        return self.disabled

    def masterPasswordIsSet(self) -> bool:  # noqa: N802
        return self.master_password_set

    def setMasterPassword(self, verify: bool = False) -> bool:  # noqa: N802
        self.master_password_set = True
        return True

    def configIds(self) -> list[str]:  # noqa: N802
        return list(self.configs)

    def storeAuthenticationConfig(self, config, overwrite=False):  # noqa: N802
        if not config.id():
            config.setId(f"cvi{next(self._ids):04d}")
        self.configs[config.id()] = config.clone()
        return True, config

    def loadAuthenticationConfig(self, authcfg, config, full=False):  # noqa: N802
        stored = self.configs.get(authcfg)
        if stored is None:
            return False, config
        # A copy, like the real one: the caller mutates what it gets back, and handing out
        # the stored object would make every edit take effect before it was stored.
        return True, stored.clone()

    def removeAuthenticationConfig(self, authcfg) -> bool:  # noqa: N802
        return self.configs.pop(authcfg, None) is not None

    def clearCachedConfig(self, authcfg) -> None:  # noqa: N802
        self.cleared.append(authcfg)

    def storeAuthSetting(self, key, value, encrypt=False) -> bool:  # noqa: N802
        self.settings[key] = (value, bool(encrypt))
        return True

    def authSetting(self, key, default="", decrypt=False):  # noqa: N802
        entry = self.settings.get(key)
        return default if entry is None else entry[0]

    def removeAuthSetting(self, key) -> bool:  # noqa: N802
        return self.settings.pop(key, None) is not None


_AUTH_MANAGER = _AuthManager()


class QgsApplication(Stub):
    @staticmethod
    def taskManager() -> _TaskManager:  # noqa: N802 - Qt naming
        return _TASK_MANAGER

    @staticmethod
    def authManager() -> _AuthManager:  # noqa: N802 - Qt naming
        return _AUTH_MANAGER


# ---------------------------------------------------------------------------
# Module assembly
# ---------------------------------------------------------------------------


def _make_module(name: str, explicit: dict[str, Any] | None = None) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in (explicit or {}).items():
        setattr(module, key, value)

    def __getattr__(attr: str) -> Any:  # noqa: N807 - PEP 562 fixes this name
        if attr.startswith("__"):
            raise AttributeError(attr)
        generated = StubMeta(attr, (Stub,), {})
        setattr(module, attr, generated)
        return generated

    module.__getattr__ = __getattr__  # type: ignore[attr-defined]
    return module


_CORE_EXPLICIT = {
    "QgsAuthMethodConfig": QgsAuthMethodConfig,
    "QgsSettings": QgsSettings,
    "QgsMessageLog": QgsMessageLog,
    "QgsFeedback": QgsFeedback,
    "QgsTask": QgsTask,
    "QgsApplication": QgsApplication,
}

_QTCORE_EXPLICIT = {
    "pyqtSignal": pyqtSignal,
    "pyqtSlot": pyqtSlot,
}

_MODULES = (
    "qgis",
    "qgis.core",
    "qgis.gui",
    "qgis.utils",
    "qgis.PyQt",
    "qgis.PyQt.QtCore",
    "qgis.PyQt.QtGui",
    "qgis.PyQt.QtWidgets",
    "qgis.PyQt.QtNetwork",
    "qgis.PyQt.QtXml",
)


def install() -> None:
    """Register the stub modules in ``sys.modules``.

    A no-op when a real QGIS is importable, so running the suite inside the QGIS Python
    environment tests against the real API rather than against these shapes.
    """
    try:  # pragma: no cover - depends on the machine, not the code
        import qgis.core  # noqa: F401

        return
    except ImportError:
        pass

    explicit = {"qgis.core": _CORE_EXPLICIT, "qgis.PyQt.QtCore": _QTCORE_EXPLICIT}
    for name in _MODULES:
        if name in sys.modules:
            continue
        module = _make_module(name, explicit.get(name))
        sys.modules[name] = module
        if "." in name:
            parent_name, _, child = name.rpartition(".")
            setattr(sys.modules[parent_name], child, module)


def auth_manager() -> _AuthManager:
    """The stub credential store, so a test can inspect or preload it."""
    return _AUTH_MANAGER


def reset() -> None:
    """Clear the state the behaviour-bearing stubs accumulate between tests."""
    QgsSettings.reset()
    QgsMessageLog.records.clear()
    _TASK_MANAGER.tasks.clear()
    _AUTH_MANAGER.reset()
