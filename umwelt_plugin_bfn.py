import os
from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsApplication


class UmweltPluginBFN:
    def __init__(self, iface):
        self.iface = iface
        self.dock = None
        self.action = None
        self._provider = None

    def initGui(self):
        # Processing-Provider registrieren
        from .processing.provider import BFNProvider
        self._provider = BFNProvider()
        QgsApplication.processingRegistry().addProvider(self._provider)

        icon_path = os.path.join(os.path.dirname(__file__), "icon.svg")
        self.action = QAction(QIcon(icon_path), "Umwelt WFS Tool", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.setToolTip("BFN Geodatenabfragen: Schutzgebiete & Artenkataster")
        self.action.triggered.connect(self._toggle_dock)

        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("Umwelt WFS Tool", self.action)

        from .compat import DOCK_RIGHT
        from .dock_widget import UmweltDockWidget
        self.dock = UmweltDockWidget(self.iface)
        self.iface.addDockWidget(DOCK_RIGHT, self.dock)
        self.dock.visibilityChanged.connect(self.action.setChecked)

    def unload(self):
        if self._provider:
            QgsApplication.processingRegistry().removeProvider(self._provider)
            self._provider = None
        self.iface.removeToolBarIcon(self.action)
        self.iface.removePluginMenu("Umwelt WFS Tool", self.action)
        if self.dock:
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None

    def _toggle_dock(self, checked: bool):
        if self.dock:
            self.dock.setVisible(checked)
