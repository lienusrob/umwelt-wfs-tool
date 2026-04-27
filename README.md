# Umwelt WFS Tool

QGIS-Plugin für den direkten Zugriff auf öffentliche Umwelt-Geodienste in Deutschland.

## Funktionen

- **BFN-Schutzgebiete** (Natura 2000 FFH/VSG, NSG, LSG, Nationalparke, Biosphärenreservate, Naturparke, Nationale Naturmonumente, AWZ)
- **Länderdienste** für alle 16 Bundesländer: Schutzgebiete, Biotope, Boden, Wasser, Flurstücke/ALKIS
- **Automatisches Styling** nach EBA/BfN-Kartierkonventionen (nutzt QGIS Style Manager)
- **Verschneidung** mit eigenem Untersuchungsraum (Polygon + Puffer in Metern)
- **UVP-Kategorien**: Layer werden automatisch in Gruppen sortiert
- **Artenkataster**: INSPIRE WMS-Dienste, FloraWeb-Pflanzensuche

## Installation

1. QGIS Plugin Manager öffnen: *Erweiterungen → Erweiterungen verwalten und installieren*
2. Nach „Umwelt WFS Tool" suchen und installieren

Oder manuell: Plugin-Ordner in das QGIS-Plugins-Verzeichnis entpacken und QGIS neu starten.

## Voraussetzungen

- QGIS 3.22 oder neuer
- Internetzugang zu den jeweiligen WFS-Diensten

## Hinweis

Datenintensive Layer (Flurstücke/ALKIS, Bodendaten) sollten einzeln geladen werden, um Timeouts zu vermeiden.

## Lizenz

GNU General Public License v2 or later (GPL-2.0+)

## Autor

Lienus Rob — lienus.rob@hotmail.de
