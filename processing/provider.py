import os
from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon
from .verschneidung_algorithm import VerschneidungAlgorithm


class BFNProvider(QgsProcessingProvider):
    def loadAlgorithms(self):
        self.addAlgorithm(VerschneidungAlgorithm())

    def id(self):
        return "umwelt_bfn"

    def name(self):
        return "Umwelt WFS Tool"

    def longName(self):
        return "Umwelt WFS Tool – Geodatenanalyse"

    def icon(self):
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "icon.svg")
        return QIcon(path)
