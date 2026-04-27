from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QGroupBox, QCheckBox, QPushButton,
    QLabel, QListWidget, QLineEdit, QComboBox,
    QProgressBar, QListWidgetItem, QMessageBox,
    QScrollArea, QFrame, QDoubleSpinBox,
)
from qgis.PyQt.QtCore import Qt, QThread, QTimer, pyqtSignal
from qgis.core import (
    QgsProject, QgsMessageLog, QgsTask, QgsApplication,
    QgsMapLayerProxyModel,
)
from qgis.gui import QgsMapLayerComboBox

from .compat import ALIGN_CENTER, DOCK_RIGHT, ITEM_USER_ROLE, FRAME_NONE
from .compat import MSG_SUCCESS, MSG_WARNING, FIELD_STRING
from .compat import TASK_CAN_CANCEL, LAYER_FILTER_POLYGON, LAYER_FILTER_LINE
from .services.schutzgebiete import (
    WFS_BASE_URL, WFS_AWZ_URL,
    DEFAULT_LAYERS, fetch_layers, create_layer, fetch_wfs_to_memory,
)
from .services.styles import apply_style
from .services.intersection import build_mask, intersect_wfs_with_mask
from .services.artenkataster import (
    INSPIRE_SPECIES_SERVICES as INSPIRE_SPECIES_WMS,
    fetch_best_layer_info, create_best_layer,
    floraweb_search,
)


# ──────────────────────────────────────────────────────────────────────
# UVP-Kategorie-Zuordnung für Layerbaum-Gruppen
# ──────────────────────────────────────────────────────────────────────

_UVP_CATEGORIES = [
    ("Schutzgebiete & Natura 2000", ["schutzgebiet", "schutzgeb", "natura", "ffh", "vsg",
                                      "nsg", "lsg", "vogelschutz", "biosphä", "nationalpark",
                                      "naturpark", "naturmonument", "lapro", "inspire",
                                      "prozessschutz"]),
    ("Wasser",                       ["wasser", "gewässer", "überschwemmung", "grundwasser",
                                       "hydrogeol", "hochwasser", "nitrat", "wrrl"]),
    ("Boden",                        ["boden", "altlast", "versiegelung", "bodennutzung",
                                       "bodenkund"]),
    ("Biotope",                      ["biotop"]),
    ("Luft & Klima",                 ["luft", "klima", "emission", "immission"]),
    ("Landwirtschaft",               ["landwirtschaft", "agrar", "flur"]),
    ("Verwaltung",                   ["verwaltung", "gemeinde", "landkreis", "grenz",
                                       "vg1000", "vg250"]),
]
_UVP_DEFAULT_CATEGORY = "Sonstiges"


def _uvp_category(title: str) -> str:
    lower = title.lower()
    for cat, keywords in _UVP_CATEGORIES:
        if any(kw in lower for kw in keywords):
            return cat
    return _UVP_DEFAULT_CATEGORY


# ──────────────────────────────────────────────────────────────────────
# Hintergrund-Threads  (nur Netzwerk-I/O, keine QGIS-Projekt-Operationen)
# ──────────────────────────────────────────────────────────────────────

class IntersectionTask(QgsTask):
    """
    Lädt BFN-WFS-Layer und verschneidet sie mit dem Untersuchungsraum.
    Läuft im QGIS-Task-System: zeigt Fortschritt in der QGIS-Statusleiste
    und unterstützt Abbruch über den QGIS-Task-Manager.
    """
    layer_done   = pyqtSignal(object, object, str, object)  # (inside, outside, title, layer_def)
    layer_failed = pyqtSignal(str)
    all_done     = pyqtSignal()

    def __init__(self, poly_layer, line_layer, buffer_m, selected_defs):
        n = len(selected_defs)
        super().__init__(f"BFN Verschneidung ({n} Layer)", TASK_CAN_CANCEL)
        self.poly_layer    = poly_layer
        self.line_layer    = line_layer
        self.buffer_m      = buffer_m
        self.selected_defs = selected_defs

    def run(self):
        from qgis.core import QgsCoordinateReferenceSystem, QgsProject
        target_crs    = QgsCoordinateReferenceSystem("EPSG:25832")
        transform_ctx = QgsProject.instance().transformContext()

        mask = build_mask(
            self.poly_layer, self.line_layer,
            self.buffer_m, target_crs, transform_ctx,
        )
        if mask is None or mask.isEmpty():
            for d in self.selected_defs:
                self.layer_failed.emit(d["title"])
            return True

        n = len(self.selected_defs)
        for i, layer_def in enumerate(self.selected_defs):
            if self.isCanceled():
                return False
            wfs_layer = fetch_wfs_to_memory(layer_def, bbox_geom=mask)
            if wfs_layer is None:
                wfs_layer = create_layer(layer_def, restrict_to_bbox=True)
            if wfs_layer is None:
                self.layer_failed.emit(layer_def["title"])
            else:
                inside, outside = intersect_wfs_with_mask(
                    wfs_layer, mask, target_crs, transform_ctx,
                )
                if inside is None and outside is None:
                    self.layer_failed.emit(layer_def["title"])
                else:
                    if inside is not None:
                        inside.setName(f"{layer_def['title']} [innerhalb UR]")
                    if outside is not None:
                        outside.setName(f"{layer_def['title']} [außerhalb UR]")
                    self.layer_done.emit(inside, outside, layer_def["title"], layer_def)
            self.setProgress((i + 1) / n * 100)
        return True

    def finished(self, result):
        self.all_done.emit()


class CapabilitiesTask(QgsTask):
    """Lädt WFS-Layer-Liste im QGIS-Task-System (zuverlässiger Netzwerkzugriff)."""
    caps_ready = pyqtSignal(list)

    def __init__(self):
        super().__init__("BFN Schutzgebiete: GetCapabilities laden")
        self._layers = []

    def run(self):
        self._layers  = fetch_layers(WFS_BASE_URL)
        self._layers += fetch_layers(WFS_AWZ_URL, group_suffix="awz")
        return True

    def finished(self, result):
        self.caps_ready.emit(self._layers if result else [])


_HEAVY_KEYWORDS = frozenset([
    "flurstück", "flurstucke", "alkis", "cadastral", "parcel",
    "boden", "bodenkarte", "bodenfunktion", "bodennutzung", "bodenversiegelung",
])


def _is_heavy(layer_def: dict) -> bool:
    """True für datenintensive Layer, die nicht als Memory-Layer geladen werden sollten."""
    text = (layer_def.get("title", "") + " " + layer_def.get("typename", "")).lower()
    return any(kw in text for kw in _HEAVY_KEYWORDS)


class SingleWfsLoaderTask(QgsTask):
    """Lädt genau einen WFS-Layer im QGIS-Task-System (parallel aufrufbar)."""
    layer_ready  = pyqtSignal(object, str, object)  # (QgsVectorLayer, title, layer_def)
    layer_failed = pyqtSignal(str)

    def __init__(self, layer_def, restrict_bbox=False):
        super().__init__(f"WFS: {layer_def['title']}", TASK_CAN_CANCEL)
        self.layer_def     = layer_def
        self.restrict_bbox = restrict_bbox

    def run(self):
        if self.isCanceled():
            return False
        if _is_heavy(self.layer_def):
            # Schwere Layer nie als Memory-Layer laden – WFS-Provider mit BBOX nutzen
            layer = create_layer(self.layer_def, restrict_to_bbox=True)
        else:
            layer = fetch_wfs_to_memory(self.layer_def)
            if layer is None:
                layer = create_layer(self.layer_def, self.restrict_bbox)
        if layer:
            self.layer_ready.emit(layer, self.layer_def["title"], self.layer_def)
        else:
            self.layer_failed.emit(self.layer_def["title"])
        return True


class ExternerWfsCapabilitiesThread(QThread):
    """Lädt GetCapabilities von einer beliebigen WFS-URL."""
    caps_ready = pyqtSignal(list)
    failed     = pyqtSignal(str)

    def __init__(self, url, parent=None):
        super().__init__(parent)
        self.url = url

    def run(self):
        layers = fetch_layers(self.url)
        if layers:
            self.caps_ready.emit(layers)
        else:
            self.failed.emit(self.url)


class ArtCapabilitiesThread(QThread):
    """Prüft WFS (bevorzugt) oder WMS für Artverbreitung (blockierende HTTP-Requests)."""
    ready  = pyqtSignal(dict, str, str, str)  # (service_def, layer_type, url, layer_name)
    failed = pyqtSignal(str)                  # title

    def __init__(self, service_def, parent=None):
        super().__init__(parent)
        self.service_def = service_def

    def run(self):
        layer_type, url, layer_name = fetch_best_layer_info(self.service_def)
        if layer_name:
            self.ready.emit(self.service_def, layer_type, url, layer_name)
        else:
            self.failed.emit(self.service_def["title"])


class FloraWebSearchThread(QThread):
    results_ready = pyqtSignal(list)

    def __init__(self, search_term, parent=None):
        super().__init__(parent)
        self.search_term = search_term

    def run(self):
        self.results_ready.emit(floraweb_search(self.search_term))


# ──────────────────────────────────────────────────────────────────────
# Haupt-DockWidget
# ──────────────────────────────────────────────────────────────────────

class UmweltDockWidget(QDockWidget):
    def __init__(self, iface):
        super().__init__("Umwelt WFS Tool")
        self.iface  = iface
        self._threads = []   # QThread-Instanzen
        self._tasks        = []   # QgsTask-Instanzen
        self._ext_bl_groups: dict       = {}   # bundesland → QgsLayerTreeGroup
        self._ext_bl_subgroups: dict    = {}   # bundesland → {category → QgsLayerTreeGroup}
        self._preset_caps_pending       = 0
        self._preset_caps_results: list = []   # [(bundesland, [layer_defs])]
        self._sg_pending = 0    # Zähler noch ausstehender WFS-Layer
        self._sg_total   = 0
        self._ur_summary: dict = {}   # title → {inside_ha, outside_ha}
        self._ur_group_inside       = None
        self._ur_group_outside      = None
        self._ext_group             = None
        self._ext_int_group_inside  = None
        self._ext_int_group_outside = None
        self._ur_result_layer_ids   = []
        self._ext_pending           = 0
        self._canvas_frozen         = False
        self._preload_done          = False
        self.setMinimumWidth(320)
        self.setObjectName("UmweltPluginBFN")

        container = QWidget()
        layout    = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)

        header = QLabel("<b>Umwelt WFS Tool</b> – Geodatenabfragen")
        header.setAlignment(ALIGN_CENTER)
        header.setStyleSheet("color: #2e7d32; padding: 4px;")
        layout.addWidget(header)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_externer_wfs_tab(), "Umweltrelevante WFS Server")
        layout.addWidget(self.tabs)

        self._sg_checkboxes = {}
        self._sg_layer_defs = []

        self.setWidget(container)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._preload_done:
            self._preload_done = True
            self._preload_capabilities()

    def _preload_capabilities(self):
        """Wärmt den Capabilities-Cache im Hintergrund auf (BFN + häufige Dienste)."""
        urls_to_warm = [WFS_BASE_URL, WFS_AWZ_URL]
        for url in urls_to_warm:
            t = ExternerWfsCapabilitiesThread(url)
            t.caps_ready.connect(lambda *a: None)   # Ergebnis verwerfen – nur cachen
            t.failed.connect(lambda *a: None)
            self._threads.append(t)
            t.start()

    # ------------------------------------------------------------------
    # Tab: Schutzgebiete
    # ------------------------------------------------------------------

    def _build_schutzgebiete_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        # Scroll-Bereich für dynamische Checkboxen
        self._sg_scroll = QScrollArea()
        self._sg_scroll.setWidgetResizable(True)
        self._sg_scroll.setFrameShape(FRAME_NONE)
        self._sg_inner        = QWidget()
        self._sg_inner_layout = QVBoxLayout(self._sg_inner)
        self._sg_inner_layout.setSpacing(2)
        self._sg_caps_label   = QLabel("Lade Layer-Liste vom BFN-Dienst …")
        self._sg_caps_label.setStyleSheet("color:#888; font-style:italic; font-size:11px")
        self._sg_inner_layout.addWidget(self._sg_caps_label)
        self._sg_inner_layout.addStretch()
        self._sg_scroll.setWidget(self._sg_inner)
        layout.addWidget(self._sg_scroll)

        self._sg_checkboxes    = {}   # typename → QCheckBox
        self._sg_layer_defs    = []   # alle geladenen layer_defs

        sel_row = QHBoxLayout()
        btn_all  = QPushButton("Alle");   btn_all.setMaximumWidth(60)
        btn_none = QPushButton("Keine");  btn_none.setMaximumWidth(60)
        btn_reload = QPushButton("↺");    btn_reload.setMaximumWidth(30)
        btn_reload.setToolTip("Layer-Liste neu laden")
        btn_all.clicked.connect(lambda: [cb.setChecked(True)  for cb in self._sg_checkboxes.values()])
        btn_none.clicked.connect(lambda: [cb.setChecked(False) for cb in self._sg_checkboxes.values()])
        btn_reload.clicked.connect(self._load_capabilities)
        sel_row.addWidget(QLabel("Auswahl:")); sel_row.addWidget(btn_all)
        sel_row.addWidget(btn_none); sel_row.addWidget(btn_reload); sel_row.addStretch()
        layout.addLayout(sel_row)

        opt_group  = QGroupBox("Optionen")
        opt_layout = QVBoxLayout(opt_group)
        self._sg_bbox_cb = QCheckBox("Nur aktuellen Kartenausschnitt laden")
        opt_layout.addWidget(self._sg_bbox_cb)
        layout.addWidget(opt_group)

        self._sg_load_btn = QPushButton("Ausgewählte Schutzgebiete laden")
        self._sg_load_btn.setEnabled(False)
        self._sg_load_btn.setStyleSheet(
            "QPushButton{background:#2e7d32;color:white;padding:6px;border-radius:4px}"
            "QPushButton:hover{background:#388e3c}"
            "QPushButton:disabled{background:#aaa}"
        )
        self._sg_load_btn.clicked.connect(self._load_schutzgebiete)
        layout.addWidget(self._sg_load_btn)

        self._sg_progress = QProgressBar()
        self._sg_progress.setVisible(False)
        layout.addWidget(self._sg_progress)

        self._sg_status = QLabel("")
        self._sg_status.setWordWrap(True)
        self._sg_status.setStyleSheet("font-size:11px;color:#555")
        layout.addWidget(self._sg_status)
        return widget

    def _load_capabilities(self):
        """Aktualisiert Layer-Liste aus GetCapabilities (nur auf ↺-Klick)."""
        self._sg_load_btn.setEnabled(False)
        self._sg_caps_label.setText("Aktualisiere Liste vom BFN-Dienst …")
        self._sg_caps_label.setVisible(True)
        for cb in self._sg_checkboxes.values():
            cb.setParent(None)
        self._sg_checkboxes.clear()
        self._sg_layer_defs.clear()

        task = CapabilitiesTask()
        task.caps_ready.connect(self._on_capabilities_ready)
        QgsApplication.taskManager().addTask(task)

    def _on_capabilities_ready(self, layer_defs):
        """Wird im Hauptthread ausgeführt — baut Checkboxen dynamisch auf."""
        self._sg_caps_label.setVisible(False)
        self._sg_layer_defs = layer_defs

        # Stretch entfernen, Checkboxen einfügen, Stretch wieder ans Ende
        stretch_item = self._sg_inner_layout.takeAt(self._sg_inner_layout.count() - 1)

        if not layer_defs:
            self._sg_caps_label.setText("Aktualisierung fehlgeschlagen – verwende bekannte Layer.")
            self._sg_caps_label.setVisible(True)
            self._sg_inner_layout.addItem(stretch_item)
            # Fallback auf DEFAULT_LAYERS
            layer_defs = DEFAULT_LAYERS

        current_group = None
        for layer_def in layer_defs:
            if layer_def.get("group") != current_group:
                current_group = layer_def["group"]
                lbl = QLabel(f"<b>{current_group}</b>")
                lbl.setStyleSheet("color:#555; margin-top:6px;")
                self._sg_inner_layout.addWidget(lbl)
            cb = QCheckBox(layer_def["title"])
            cb.setChecked(True)
            self._sg_checkboxes[layer_def["typename"]] = cb
            self._sg_inner_layout.addWidget(cb)

        self._sg_inner_layout.addItem(stretch_item)
        self._sg_load_btn.setEnabled(True)
        self._sg_status.setText(f"{len(layer_defs)} Layer verfügbar.")

    def _load_schutzgebiete(self):
        selected = [d for d in self._sg_layer_defs if self._sg_checkboxes.get(d["typename"], QCheckBox()).isChecked()]
        if not selected:
            QMessageBox.information(self, "Auswahl leer", "Bitte mindestens einen Layer auswählen.")
            return

        self._sg_total   = len(selected)
        self._sg_pending = self._sg_total
        self._sg_load_btn.setEnabled(False)
        self._sg_progress.setVisible(True)
        self._sg_progress.setRange(0, self._sg_total)
        self._sg_progress.setValue(0)
        self._sg_status.setText(f"Lade {self._sg_total} Layer …")

        self.iface.mapCanvas().freeze(True)
        self._canvas_frozen = True
        restrict = self._sg_bbox_cb.isChecked()
        for ld in selected:
            task = SingleWfsLoaderTask(ld, restrict)
            task.layer_ready.connect(self._on_wfs_layer_ready)
            task.layer_failed.connect(self._on_wfs_layer_failed)
            task.layer_ready.connect(lambda *a, t=task: self._cleanup_task(t))
            task.layer_failed.connect(lambda *a, t=task: self._cleanup_task(t))
            self._tasks.append(task)
            QgsApplication.taskManager().addTask(task)

    def _on_wfs_layer_ready(self, layer, title, layer_def):
        QgsProject.instance().addMapLayer(layer)
        apply_style(layer, layer_def)
        self._sg_pending -= 1
        self._sg_progress.setValue(self._sg_total - self._sg_pending)
        self._sg_status.setText(f"✓ {title}")
        self.iface.messageBar().pushMessage("BFN Schutzgebiete", f"Geladen: {title}", MSG_SUCCESS, 3)
        if self._sg_pending <= 0:
            self._on_wfs_all_done()

    def _on_wfs_layer_failed(self, title):
        self._sg_pending -= 1
        self._sg_status.setText(f"✗ Fehler: {title}")
        self.iface.messageBar().pushMessage("BFN Schutzgebiete", f"Fehler: {title}", MSG_WARNING, 5)
        if self._sg_pending <= 0:
            self._on_wfs_all_done()

    def _on_wfs_all_done(self):
        self._sg_load_btn.setEnabled(True)
        self._sg_progress.setVisible(False)
        self._sg_status.setText("Fertig.")
        if self._canvas_frozen:
            self._canvas_frozen = False
            self.iface.mapCanvas().freeze(False)
            self.iface.mapCanvas().refresh()

    # ------------------------------------------------------------------
    # Tab: Artenkataster
    # ------------------------------------------------------------------

    def _build_artenkataster_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        wms_group  = QGroupBox("Artverbreitung (INSPIRE WMS)")
        wms_layout = QVBoxLayout(wms_group)
        wms_layout.addWidget(QLabel("Artengruppe:"))

        self._art_combo = QComboBox()
        for svc in INSPIRE_SPECIES_WMS:
            self._art_combo.addItem(svc["title"], svc)
        wms_layout.addWidget(self._art_combo)

        self._art_load_btn = QPushButton("Verbreitungskarte laden")
        self._art_load_btn.setStyleSheet(
            "QPushButton{background:#1565c0;color:white;padding:6px;border-radius:4px}"
            "QPushButton:hover{background:#1976d2}"
            "QPushButton:disabled{background:#aaa}"
        )
        self._art_load_btn.clicked.connect(self._load_artenverbreitung)
        wms_layout.addWidget(self._art_load_btn)

        self._art_status = QLabel("")
        self._art_status.setStyleSheet("font-size:11px;color:#555")
        wms_layout.addWidget(self._art_status)
        layout.addWidget(wms_group)

        flora_group  = QGroupBox("FloraWeb – Pflanzensuche")
        flora_layout = QVBoxLayout(flora_group)

        search_row = QHBoxLayout()
        self._flora_search = QLineEdit()
        self._flora_search.setPlaceholderText("Artname eingeben (z.B. Fagus sylvatica)")
        self._flora_search.returnPressed.connect(self._search_floraweb)
        search_row.addWidget(self._flora_search)
        self._flora_search_btn = QPushButton("Suchen")
        self._flora_search_btn.setMaximumWidth(70)
        self._flora_search_btn.clicked.connect(self._search_floraweb)
        search_row.addWidget(self._flora_search_btn)
        flora_layout.addLayout(search_row)

        self._flora_results = QListWidget()
        self._flora_results.setMaximumHeight(150)
        self._flora_results.itemDoubleClicked.connect(self._show_taxon_details)
        flora_layout.addWidget(self._flora_results)

        self._flora_detail_btn = QPushButton("Details anzeigen")
        self._flora_detail_btn.setEnabled(False)
        self._flora_detail_btn.clicked.connect(
            lambda: self._show_taxon_details(self._flora_results.currentItem())
        )
        self._flora_results.itemSelectionChanged.connect(
            lambda: self._flora_detail_btn.setEnabled(self._flora_results.currentItem() is not None)
        )
        flora_layout.addWidget(self._flora_detail_btn)

        self._flora_info = QLabel("")
        self._flora_info.setWordWrap(True)
        self._flora_info.setStyleSheet("font-size:11px;color:#555")
        flora_layout.addWidget(self._flora_info)
        layout.addWidget(flora_group)

        note = QLabel(
            "<i>Hinweis: FloraWeb liefert Taxonomie-Daten. "
            "Artverbreitung über INSPIRE WMS-Dienste.</i>"
        )
        note.setWordWrap(True)
        note.setStyleSheet("font-size:10px;color:#777")
        layout.addWidget(note)
        layout.addStretch()
        return widget

    def _load_artenverbreitung(self):
        svc = self._art_combo.currentData()
        if not svc:
            return
        self._art_load_btn.setEnabled(False)
        self._art_status.setText(f"Hole Capabilities für {svc['title']} …")

        thread = ArtCapabilitiesThread(svc)
        thread.ready.connect(self._on_wms_caps_ready)     # Hauptthread ✓
        thread.failed.connect(self._on_wms_caps_failed)
        thread.finished.connect(lambda: self._cleanup_thread(thread))
        self._threads.append(thread)
        thread.start()

    def _on_wms_caps_ready(self, svc, layer_type, url, layer_name):
        layer = create_best_layer(svc, layer_type, url, layer_name)   # Hauptthread ✓
        if layer:
            QgsProject.instance().addMapLayer(layer)
            self._art_status.setText(f"✓ Geladen: {svc['title']}")
            self.iface.messageBar().pushMessage("BFN Artenkataster", f"WMS geladen: {svc['title']}", MSG_SUCCESS, 3)
        else:
            self._art_status.setText(f"✗ Fehler: {svc['title']}")
            self.iface.messageBar().pushMessage("BFN Artenkataster", f"Fehler: {svc['title']}", MSG_WARNING, 5)
        self._art_load_btn.setEnabled(True)

    def _on_wms_caps_failed(self, title):
        self._art_status.setText(f"✗ Capabilities nicht erreichbar: {title}")
        self.iface.messageBar().pushMessage("BFN Artenkataster", f"Fehler: {title}", MSG_WARNING, 5)
        self._art_load_btn.setEnabled(True)

    def _search_floraweb(self):
        term = self._flora_search.text().strip()
        if not term:
            return
        self._flora_results.clear()
        self._flora_results.addItem("Suche läuft …")
        self._flora_search_btn.setEnabled(False)

        thread = FloraWebSearchThread(term)
        thread.results_ready.connect(self._on_flora_results)
        thread.finished.connect(lambda: self._flora_search_btn.setEnabled(True))
        thread.finished.connect(lambda: self._cleanup_thread(thread))
        self._threads.append(thread)
        thread.start()

    def _on_flora_results(self, results):
        self._flora_results.clear()
        if not results:
            self._flora_results.addItem("Keine Ergebnisse gefunden.")
            return
        for item_data in results:
            name     = item_data.get("scientificName") or item_data.get("name", "Unbekannt")
            taxon_id = item_data.get("taxonId") or item_data.get("id", "")
            item = QListWidgetItem(f"{name} (ID: {taxon_id})")
            item.setData(ITEM_USER_ROLE, item_data)
            self._flora_results.addItem(item)
        self._flora_info.setText(f"{len(results)} Ergebnis(se) gefunden.")

    def _show_taxon_details(self, item):
        if item is None:
            return
        data = item.data(ITEM_USER_ROLE)
        if not data:
            return
        lines = []
        for key, label in [
            ("scientificName", "Wissenschaftlicher Name"),
            ("germanName",     "Deutscher Name"),
            ("taxonId",        "Taxon-ID"),
            ("taxonomicStatus","Status"),
            ("family",         "Familie"),
            ("order",          "Ordnung"),
        ]:
            val = data.get(key)
            if val:
                lines.append(f"<b>{label}:</b> {val}")
        self._flora_info.setText("<br>".join(lines) if lines else str(data))

    # ==================================================================
    # Tab: Externer WFS
    # ==================================================================

    def _build_externer_wfs_tab(self):
        outer = QWidget()
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(FRAME_NONE)
        outer_layout.addWidget(scroll)

        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        # ── Voreinstellungen: Checkbox-Liste ────────────────────────────
        preset_group = QGroupBox("Voreinstellungen – Geodienste auswählen")
        preset_vlay  = QVBoxLayout(preset_group)

        preset_hint = QLabel(
            "Dienste anhaken → <b>Ausgewählte laden</b>. "
            "Mehrere Dienste werden nacheinander abgefragt. "
            "Oder URL manuell eingeben → 'Abrufen'."
        )
        preset_hint.setWordWrap(True)
        preset_hint.setStyleSheet("font-size:11px; color:#555")
        preset_vlay.addWidget(preset_hint)

        # Scroll-Bereich für die Checkbox-Liste
        cb_scroll = QScrollArea()
        cb_scroll.setWidgetResizable(True)
        cb_scroll.setFrameShape(FRAME_NONE)
        cb_scroll.setFixedHeight(220)
        cb_inner  = QWidget()
        cb_layout = QVBoxLayout(cb_inner)
        cb_layout.setContentsMargins(2, 2, 2, 2)
        cb_layout.setSpacing(1)
        cb_scroll.setWidget(cb_inner)
        preset_vlay.addWidget(cb_scroll)

        self._preset_service_checks: list = []   # [(QCheckBox, url, bundesland)]

        # (display_name, url, bundesland) — None-URL = Bundesland-Überschrift
        _ALL_PRESETS = [
            # ── Deutschland – Bundesebene ─────────────────────────────
            ("── Deutschland – Bundesebene ──", None, None),
            ("Schutzgebiete Deutschland (BFN)",          WFS_BASE_URL, "Deutschland – Bund"),
            ("AWZ-Schutzgebiete Nordsee/Ostsee (BFN)",  WFS_AWZ_URL,  "Deutschland – Bund"),
            # ── Verwaltungsgrenzen (BKG) ─────────────────────────────
            ("── Verwaltungsgrenzen ──", None, None),
            ("Verwaltungsgebiete 1:1 Mio (BKG)",  "https://sgx.geodatenzentrum.de/wfs_vg1000", "Verwaltungsgrenzen"),
            ("Verwaltungsgebiete 1:250.000 (BKG)", "https://sgx.geodatenzentrum.de/wfs_vg250",  "Verwaltungsgrenzen"),
            # ── Berlin ───────────────────────────────────────────────
            ("── Berlin ──", None, None),
            ("Schutzgebiete / Natura 2000", "https://gdi.berlin.de/services/wfs/schutzgebiete",         "Berlin"),
            ("Flurstücke (ALKIS)",          "https://gdi.berlin.de/services/wfs/alkis_flurstuecke",     "Berlin"),
            ("Bodenversiegelung",           "https://gdi.berlin.de/services/wfs/ua_versiegelung_2021",   "Berlin"),
            ("Boden Wasserregulation",      "https://gdi.berlin.de/services/wfs/ua_boden_regelfkt_2015", "Berlin"),
            ("Grünanlagen",                 "https://gdi.berlin.de/services/wfs/gruenanlagen",           "Berlin"),
            # ── Brandenburg ──────────────────────────────────────────
            ("── Brandenburg ──", None, None),
            ("Schutzgebiete (NSG/LSG/FFH/SPA)", "https://inspire.brandenburg.de/services/schutzg_wfs",    "Brandenburg"),
            ("Schutzgebiete INSPIRE",            "https://inspire.brandenburg.de/services/ps_schutzg_wfs", "Brandenburg"),
            ("Biotopkataster",                   "https://inspire.brandenburg.de/services/bbk_wfs",        "Brandenburg"),
            ("Flurstücke (ALKIS vereinf.)",      "https://isk.geobasis-bb.de/ows/alkis_vereinf_wfs",       "Brandenburg"),
            # ── Bremen ───────────────────────────────────────────────
            ("── Bremen ──", None, None),
            ("Biotope",                  "https://gis-hub.bremen.de/ags1/services/inspire/Biotope_Land_Bremen/MapServer/WFSServer",                  "Bremen"),
            ("Geschützte Biotope",       "https://gis-hub.bremen.de/ags1/services/inspire/Geschuetzte_Biotope_Land_Bremen/MapServer/WFSServer",      "Bremen"),
            ("Schutzgebiete (INSPIRE)",  "https://gis-hub.bremen.de/ags1/services/inspire/Schutzgebiete_Land_Bremen/MapServer/WFSServer",            "Bremen"),
            ("Kompensationsverzeichnis", "https://gis-hub.bremen.de/ags1/services/inspire/Kompensationsverzeichnis_Land_Bremen/MapServer/WFSServer", "Bremen"),
            # ── Hamburg ──────────────────────────────────────────────
            ("── Hamburg ──", None, None),
            ("Schutzgebiete",                  "https://geodienste.hamburg.de/HH_WFS_Schutzgebiete",                           "Hamburg"),
            ("Flurstücke (ALKIS INSPIRE)",     "https://geodienste.hamburg.de/HH_WFS_INSPIRE_Flurstuecke",                     "Hamburg"),
            ("Biotopkataster",                 "https://geodienste.hamburg.de/HH_WFS_Biotopkataster",                          "Hamburg"),
            ("Ausgleichsflächen",              "https://geodienste.hamburg.de/HH_WFS_Ausgleichsflaechen",                      "Hamburg"),
            ("Bodenformen",                    "https://geodienste.hamburg.de/HH_WFS_Bodenformen",                             "Hamburg"),
            ("Versickerungspotenzial",         "https://geodienste.hamburg.de/HH_WFS_Versickerung",                            "Hamburg"),
            ("Gewässer-Einzugsgebiete",        "https://geodienste.hamburg.de/HH_WFS_Gewaesser_Einzugsgebiete",                "Hamburg"),
            ("Sturmflut / Hochwasser",         "https://geodienste.hamburg.de/HH_WFS_Sturmflut",                               "Hamburg"),
            ("Wasserschutzgebiete",            "https://geodienste.hamburg.de/HH_WFS_Wasserschutzgebiete",                     "Hamburg"),
            ("Grünplan",                       "https://geodienste.hamburg.de/HH_WFS_Gruenplan",                               "Hamburg"),
            ("Landschaftsprogramm",            "https://geodienste.hamburg.de/HH_WFS_Landschaftsprogramm_Freiraumverbund",     "Hamburg"),
            ("Stadtgrün",                      "https://geodienste.hamburg.de/HH_WFS_Stadtgruen",                              "Hamburg"),
            ("Straßenbaumkataster",            "https://qs-geodienste.hamburg.de/HH_WFS_Strassenbaumkataster",                 "Hamburg"),
            ("Ruheinseln",                     "https://geodienste.hamburg.de/HH_WFS_Ruheinseln",                              "Hamburg"),
            ("Fluglärmschutzzonen",            "https://geodienste.hamburg.de/HH_WFS_Fluglaermschutzzonen",                    "Hamburg"),
            ("Siedlungsbeschränkung Fluglärm", "https://geodienste.hamburg.de/HH_WFS_Siedlungsbeschraenkungsbereiche",         "Hamburg"),
            ("Luftmessnetz",                   "https://geodienste.hamburg.de/HH_WFS_Luftmessnetz",                            "Hamburg"),
            ("Geologische Karte 1:5000",       "https://geodienste.hamburg.de/HH_WFS_Geologische_Karte_5000",                  "Hamburg"),
            ("Quartärbasis",                   "https://geodienste.hamburg.de/HH_WFS_Quartaerbasis",                           "Hamburg"),
            ("INSPIRE Hydro Gewässer",         "https://geodienste.hamburg.de/HH_WFS_INSPIRE_Hydro_Physische_Gewaesser_ALKIS", "Hamburg"),
            # ── Hessen ───────────────────────────────────────────────
            ("── Hessen ──", None, None),
            ("Wasserschutzgebiete",         "https://www.geoportal.hessen.de/mapbender/php/wfs.php?INSPIRE=1&FEATURETYPE_ID=2737", "Hessen"),
            ("Überschwemmungsgebiete",       "https://www.geoportal.hessen.de/mapbender/php/wfs.php?FEATURETYPE_ID=1139",           "Hessen"),
            ("Flurstücke (ALKIS vereinf.)", "https://www.gds.hessen.de/wfs2/aaa-suite/cgi-bin/alkis/vereinf/wfs",                  "Hessen"),
            # ── Mecklenburg-Vorpommern ───────────────────────────────
            ("── Mecklenburg-Vorpommern ──", None, None),
            ("Schutzgebiete (20 Typen)",    "https://www.umweltkarten.mv-regierung.de/script/mv_a2_schutzgeb_wfs.php",   "Mecklenburg-Vorpommern"),
            ("Wasserschutzgebiete",         "https://umweltkarten.lung-mv.de/dienste/wg_schutzgebiete",                  "Mecklenburg-Vorpommern"),
            ("Biotope",                     "https://www.umweltkarten.mv-regierung.de/script/mv_a2_biotope_wfs.php",     "Mecklenburg-Vorpommern"),
            ("Artenkataster (Fauna/Flora)", "https://www.umweltkarten.mv-regierung.de/script/mv_a2_arten_wfs.php",       "Mecklenburg-Vorpommern"),
            ("Naturräume",                  "https://www.umweltkarten.mv-regierung.de/script/mv_a2_naturraum_wfs.php",   "Mecklenburg-Vorpommern"),
            ("Flurstücke (ALKIS INSPIRE)",  "https://www.geodaten-mv.de/dienste/inspire_cp_alkis_download",              "Mecklenburg-Vorpommern"),
            # ── Niedersachsen ─────────────────────────────────────────
            ("── Niedersachsen ──", None, None),
            ("Schutzgebiete NI (Umweltkarten)",    "https://www.umweltkarten-niedersachsen.de/inspire/rest/services/SchutzgebieteNI/MapServer/exts/InspireFeatureDownload/service", "Niedersachsen"),
            ("Biosphäre Wattenmeer – Zonierung",   "https://mdi.niedersachsen.de/geoserver/Biosphaere/wfs",                                                                          "Niedersachsen"),
            ("Verwaltungsgrenzen (LGLN)",          "https://opendata.lgln.niedersachsen.de/doorman/noauth/verwaltungsgrenzen_wfs",                                                   "Niedersachsen"),
            ("Gebäude INSPIRE (LGLN)",             "https://www.inspire.niedersachsen.de/doorman/noauth/alkis-dls-bu-core2d",                                                        "Niedersachsen"),
            ("Adressen INSPIRE (LGLN)",            "https://www.inspire.niedersachsen.de/doorman/noauth/alkis-dls-ad",                                                               "Niedersachsen"),
            ("Flurstücke ALKIS vereinf. (LGLN)",   "https://opendata.lgln.niedersachsen.de/doorman/noauth/alkis_wfs_einfach",                                                        "Niedersachsen"),
            ("Flurstücke ALKIS INSPIRE ⚠ fehleranf.", "https://www.inspire.niedersachsen.de/doorman/noauth/alkis-dls-cp",                                                           "Niedersachsen"),
            # ── Nordrhein-Westfalen ───────────────────────────────────
            ("── Nordrhein-Westfalen ──", None, None),
            ("Umweltdaten (WSG / Natura 2000)", "https://www.wfs.nrw.de/umwelt/linfos",                        "Nordrhein-Westfalen"),
            ("Flurstücke (ALKIS vereinf.)",     "https://www.wfs.nrw.de/geobasis/wfs_nw_alkis_vereinfacht",    "Nordrhein-Westfalen"),
            # ── Rheinland-Pfalz ───────────────────────────────────────
            ("── Rheinland-Pfalz ──", None, None),
            ("Wasserschutzgebiete", "https://www.geoportal.rlp.de/mapbender/php/wfs.php?FEATURETYPE_ID=4638", "Rheinland-Pfalz"),
            # ── Saarland ─────────────────────────────────────────────
            ("── Saarland ──", None, None),
            ("Wasserschutzgebiete",    "https://geoportal.saarland.de/arcgis/services/Internet/Wasser_WFS/MapServer/WFSServer",     "Saarland"),
            ("Überschwemmungsgebiete", "https://geoportal.saarland.de/arcgis/services/Internet/Hochwasser_WFS/MapServer/WFSServer", "Saarland"),
            ("Naturschutz / Biotope",  "https://geoportal.saarland.de/arcgis/services/Internet/Naturschutz/MapServer/WFSServer",    "Saarland"),
            ("Schutzgebiete INSPIRE",  "https://geoportal.saarland.de/gdi-sl/inspirewfs_Schutzgebiete",                             "Saarland"),
            # ── Sachsen ───────────────────────────────────────────────
            ("── Sachsen ──", None, None),
            ("Schutzgebiete (NSG/LSG/NP/BR)",  "https://luis.sachsen.de/arcgis/services/natur/schutzgebiete/MapServer/WFSServer",                   "Sachsen"),
            ("Gesetzl. geschützte Biotope",    "https://luis.sachsen.de/arcgis/services/natur/gesetz_gesch_biotope/MapServer/WFSServer",             "Sachsen"),
            ("Wasserschutzgebiete",            "https://luis.sachsen.de/arcgis/services/wasser/wasserschutzgebiete/MapServer/WFSServer",             "Sachsen"),
            ("Gewässernetz",                   "https://luis.sachsen.de/arcgis/services/wasser/gewaesser/MapServer/WFSServer",                       "Sachsen"),
            ("Natura 2000 (FFH / SPA)",        "https://luis.sachsen.de/arcgis/services/natur/natura2000/MapServer/WFSServer",                       "Sachsen"),
            ("Naturentwicklung/Prozessschutz", "https://luis.sachsen.de/arcgis/services/natur/naturentwicklung_prozessschutz/MapServer/WFSServer",   "Sachsen"),
            ("Grundwasser",                    "https://luis.sachsen.de/arcgis/services/wasser/grundwasser/MapServer/WFSServer",                     "Sachsen"),
            ("Lärmkartierung",                 "https://luis.sachsen.de/arcgis/services/laerm/laermkartierung/MapServer/WFSServer",                  "Sachsen"),
            ("Bodenversiegelung",              "https://luis.sachsen.de/arcgis/services/boden/bodenversiegelung/MapServer/WFSServer",                "Sachsen"),
            ("Erosion",                        "https://luis.sachsen.de/arcgis/services/boden/erosion/MapServer/WFSServer",                          "Sachsen"),
            ("Moore / Feuchtgebiete",          "https://luis.sachsen.de/arcgis/services/boden/simon/MapServer/WFSServer",                            "Sachsen"),
            ("Bodenfunktionen",                "https://luis.sachsen.de/arcgis/services/boden/bodenfunktionen/MapServer/WFSServer",                  "Sachsen"),
            ("Bodenkarte BK50",                "https://luis.sachsen.de/arcgis/services/boden/bk50/MapServer/WFSServer",                             "Sachsen"),
            ("Luftmessdaten",                  "https://luis.sachsen.de/arcgis/services/luft/luftmessdaten/MapServer/WFSServer",                     "Sachsen"),
            ("Radonvorsorgegebiete",           "https://luis.sachsen.de/arcgis/services/luft/radonvorsorgegebiete/MapServer/WFSServer",              "Sachsen"),
            ("Nitratgebiete (Landwirtschaft)", "https://luis.sachsen.de/arcgis/services/landwirtschaft/nitratgebiete/MapServer/WFSServer",           "Sachsen"),
            ("Hochwasserrisikokarte",          "https://luis.sachsen.de/arcgis/services/wasser/hochwasserrisikokarte/MapServer/WFSServer",           "Sachsen"),
            ("Nitratkulisse Grundwasser",      "https://luis.sachsen.de/arcgis/services/wasser/nitratkulisse/MapServer/WFSServer",                   "Sachsen"),
            ("Flurstücke (ALKIS vereinf.)",   "https://geodienste.sachsen.de/aaa/public_alkis/vereinf/wfs",                                         "Sachsen"),
            # ── Sachsen-Anhalt ────────────────────────────────────────
            ("── Sachsen-Anhalt ──", None, None),
            ("Schutzgebiete (INSPIRE)", "https://www.geodatenportal.sachsen-anhalt.de/gfds/ws/wfs/942f5d74-6c2b-263a/GDI-LSA_Schutzgebiete/ows.wfs",          "Sachsen-Anhalt"),
            ("Bodendaten (BK50)",       "https://www.geodatenportal.sachsen-anhalt.de/arcgis/services/LAGB/LAGB_Bodendaten_B1_OpenData/MapServer/WFSServer",    "Sachsen-Anhalt"),
            ("Flurstücke (ALKIS INSPIRE)", "https://geodatenportal.sachsen-anhalt.de/ows_INSPIRE_LVermGeo_ALKIS_CP_WFS",                                        "Sachsen-Anhalt"),
            # ── Schleswig-Holstein ────────────────────────────────────
            ("── Schleswig-Holstein ──", None, None),
            ("Schutzgebiete / Biotope",       "https://umweltgeodienste.schleswig-holstein.de/WFS_UWAT",               "Schleswig-Holstein"),
            ("Flurstücke (ALKIS INSPIRE)",   "https://service.gdi-sh.de/SH_INSPIREDOWNLOAD_AI_CP_ALKIS",             "Schleswig-Holstein"),
            ("Bodenkundliche Karten",         "https://umweltgeodienste.schleswig-holstein.de/WFS_BodenkundlicheKarten","Schleswig-Holstein"),
            ("Hydrogeologie",                 "https://umweltgeodienste.schleswig-holstein.de/WFS_Hydrogeologie",      "Schleswig-Holstein"),
            ("LaPro 1 (Biotop / Schutzgeb.)", "https://umweltgeodienste.schleswig-holstein.de/WFS_LRP_Karte1_2020",   "Schleswig-Holstein"),
            ("LaPro 2 (Wald / Erholung)",     "https://umweltgeodienste.schleswig-holstein.de/WFS_LRP_Karte2_2020",   "Schleswig-Holstein"),
            ("LaPro 3 (Klima / Hochwasser)",  "https://umweltgeodienste.schleswig-holstein.de/WFS_LRP_Karte3_2020",   "Schleswig-Holstein"),
            ("Geologische Bohrpunkte",        "https://umweltgeodienste.schleswig-holstein.de/WFS_UWAT_Bohrpunkte",   "Schleswig-Holstein"),
            ("INSPIRE Schutzgebiete",         "https://service.gdi-sh.de/SH_INSPIREDOWNLOAD_AI_PS",                   "Schleswig-Holstein"),
            # ── Thüringen ─────────────────────────────────────────────
            ("── Thüringen ──", None, None),
            ("Schutzgebiete (NSG/LSG/NP/FFH/VSG)", "https://www.geoproxy.geoportal-th.de/geoproxy/services/schutzgeb_wfs",     "Thüringen"),
            ("Wasserschutzgebiete",                "https://www.geoproxy.geoportal-th.de/geoproxy/services/wsg_hqsg_wfs",      "Thüringen"),
            ("Bodennutzung (Biotoptypen)",          "https://www.geoproxy.geoportal-th.de/geoproxy/services/bodennutzung_wfs",  "Thüringen"),
            ("Verwaltungsgrenzen TH",               "https://www.geoproxy.geoportal-th.de/geoproxy/services/GRENZUEB_wfs",      "Thüringen"),
            ("Flurstücke (ALKIS)",                 "https://www.geoproxy.geoportal-th.de/geoproxy/services/adv_alkis_wfs",     "Thüringen"),
            # ── Baden-Württemberg ─────────────────────────────────────
            ("── Baden-Württemberg ──", None, None),
            ("Flurstücke (ALKIS)",  "https://owsproxy.lgl-bw.de/owsproxy/wfs/WFS_LGL-BW_ALKIS", "Baden-Württemberg"),
        ]

        for _name, _url, _bl in _ALL_PRESETS:
            if _url is None:
                lbl = QLabel(f"<b>{_name}</b>")
                lbl.setStyleSheet(
                    "font-size:11px; color:#2e7d32; margin-top:5px; margin-bottom:1px;"
                )
                cb_layout.addWidget(lbl)
            else:
                cb = QCheckBox(_name)
                cb.setChecked(False)
                cb.setStyleSheet("font-size:11px;")
                cb_layout.addWidget(cb)
                self._preset_service_checks.append((cb, _url, _bl))

        cb_layout.addStretch()

        # Alle / Keine + Laden
        ps_btn_row = QHBoxLayout()
        ps_all_btn  = QPushButton("Alle auswählen");  ps_all_btn.setFixedWidth(110)
        ps_none_btn = QPushButton("Alle abwählen");   ps_none_btn.setFixedWidth(110)
        ps_all_btn.clicked.connect(
            lambda: [cb.setChecked(True)  for cb, *_ in self._preset_service_checks]
        )
        ps_none_btn.clicked.connect(
            lambda: [cb.setChecked(False) for cb, *_ in self._preset_service_checks]
        )
        self._preset_load_btn = QPushButton("Ausgewählte laden")
        self._preset_load_btn.setStyleSheet(
            "QPushButton{background:#1565c0;color:white;padding:4px 8px;border-radius:3px}"
            "QPushButton:hover{background:#1976d2}"
        )
        ps_btn_row.addWidget(ps_all_btn)
        ps_btn_row.addWidget(ps_none_btn)
        ps_btn_row.addStretch()
        ps_btn_row.addWidget(self._preset_load_btn)
        preset_vlay.addLayout(ps_btn_row)

        layout.addWidget(preset_group)

        # ── URL-Eingabe ──────────────────────────────────────────────
        url_group = QGroupBox("WFS-Dienst-URL")
        url_vlay  = QVBoxLayout(url_group)

        url_row = QHBoxLayout()
        self._ext_url_edit = QLineEdit()
        self._ext_url_edit.setPlaceholderText(
            "https://example.com/wfs  oder  …?SERVICE=WFS&REQUEST=GetCapabilities"
        )
        url_row.addWidget(self._ext_url_edit)
        self._ext_fetch_btn = QPushButton("Abrufen")
        self._ext_fetch_btn.setFixedWidth(80)
        url_row.addWidget(self._ext_fetch_btn)
        url_vlay.addLayout(url_row)

        self._ext_caps_status = QLabel("URL eingeben und auf 'Abrufen' klicken.")
        self._ext_caps_status.setStyleSheet("color:#888; font-style:italic; font-size:11px")
        self._ext_caps_status.setWordWrap(True)
        url_vlay.addWidget(self._ext_caps_status)
        layout.addWidget(url_group)

        # ── Layer-Auswahl ────────────────────────────────────────────
        self._ext_layers_group = QGroupBox("Verfügbare Layer")
        ext_layers_vlay = QVBoxLayout(self._ext_layers_group)
        self._ext_layer_inner  = QWidget()
        self._ext_layer_layout = QVBoxLayout(self._ext_layer_inner)
        self._ext_layer_layout.setContentsMargins(0, 0, 0, 0)
        self._ext_layer_layout.setSpacing(2)
        self._ext_placeholder = QLabel("Noch keine Layer geladen.")
        self._ext_placeholder.setStyleSheet("color:#aaa; font-size:11px")
        self._ext_layer_layout.addWidget(self._ext_placeholder)
        ext_layers_vlay.addWidget(self._ext_layer_inner)
        layout.addWidget(self._ext_layers_group)

        self._ext_layer_checks = {}
        self._ext_layer_defs   = []

        # ── Alle / Keine Toggle ───────────────────────────────────────
        sel_row = QHBoxLayout()
        self._ext_toggle_all_btn = QPushButton("Alle auswählen")
        self._ext_toggle_all_btn.setEnabled(False)
        self._ext_toggle_all_btn.setFixedWidth(130)
        sel_row.addWidget(self._ext_toggle_all_btn)
        sel_row.addStretch()
        layout.addLayout(sel_row)

        # ── Suche ────────────────────────────────────────────────────
        self._ext_search = QLineEdit()
        self._ext_search.setPlaceholderText("Layer suchen …")
        self._ext_search.setClearButtonEnabled(True)
        self._ext_search.textChanged.connect(self._filter_ext_layers)
        layout.addWidget(self._ext_search)

        # ── Laden / Abbrechen ────────────────────────────────────────
        load_cancel_row = QHBoxLayout()
        self._ext_load_btn = QPushButton("Ausgewählte Layer hinzufügen")
        self._ext_load_btn.setEnabled(False)
        self._ext_load_btn.setMinimumHeight(36)
        self._ext_load_btn.setStyleSheet(
            "QPushButton{background:#2e7d32;color:white;font-weight:bold;"
            "font-size:13px;padding:6px 12px;border-radius:5px}"
            "QPushButton:hover{background:#388e3c}"
            "QPushButton:disabled{background:#bdbdbd;color:#fff}"
        )
        load_cancel_row.addWidget(self._ext_load_btn)

        self._ext_cancel_btn = QPushButton("✕ Abbrechen")
        self._ext_cancel_btn.setVisible(False)
        self._ext_cancel_btn.setMinimumHeight(36)
        self._ext_cancel_btn.setStyleSheet(
            "QPushButton{background:#c62828;color:white;font-weight:bold;"
            "font-size:13px;padding:6px 12px;border-radius:5px}"
            "QPushButton:hover{background:#b71c1c}"
        )
        self._ext_cancel_btn.clicked.connect(self._cancel_ext_load)
        load_cancel_row.addWidget(self._ext_cancel_btn)
        layout.addLayout(load_cancel_row)

        self._ext_progress = QProgressBar()
        self._ext_progress.setVisible(False)
        layout.addWidget(self._ext_progress)

        self._ext_status = QLabel("")
        self._ext_status.setWordWrap(True)
        self._ext_status.setStyleSheet("font-size:11px; color:#555")
        layout.addWidget(self._ext_status)

        # ── Verschneidung (optional) ─────────────────────────────────
        ur_group  = QGroupBox("Mit Untersuchungsraum verschneiden (optional)")
        ur_layout = QVBoxLayout(ur_group)
        self._ext_ur_chk = QCheckBox("Verschneidung aktivieren")
        ur_layout.addWidget(self._ext_ur_chk)

        self._ext_poly_combo = QgsMapLayerComboBox()
        self._ext_poly_combo.setFilters(LAYER_FILTER_POLYGON)
        self._ext_poly_combo.setAllowEmptyLayer(True)
        self._ext_poly_combo.setCurrentIndex(0)
        self._ext_poly_combo.setEnabled(False)
        ur_layout.addWidget(QLabel("Polygon-Layer (optional):"))
        ur_layout.addWidget(self._ext_poly_combo)

        self._ext_line_combo = QgsMapLayerComboBox()
        self._ext_line_combo.setFilters(LAYER_FILTER_LINE)
        self._ext_line_combo.setAllowEmptyLayer(True)
        self._ext_line_combo.setCurrentIndex(0)
        self._ext_line_combo.setEnabled(False)
        ur_layout.addWidget(QLabel("Linien-Layer (optional, wird gepuffert):"))
        ur_layout.addWidget(self._ext_line_combo)

        ext_buf_row = QHBoxLayout()
        ext_buf_row.addWidget(QLabel("Pufferabstand:"))
        self._ext_buffer_spin = QDoubleSpinBox()
        self._ext_buffer_spin.setRange(0, 100000)
        self._ext_buffer_spin.setValue(100)
        self._ext_buffer_spin.setSuffix(" m")
        self._ext_buffer_spin.setDecimals(1)
        self._ext_buffer_spin.setMinimumWidth(110)
        self._ext_buffer_spin.setEnabled(False)
        ext_buf_row.addWidget(self._ext_buffer_spin)
        ext_buf_row.addStretch()
        ur_layout.addLayout(ext_buf_row)
        layout.addWidget(ur_group)

        self._ext_ur_chk.toggled.connect(self._ext_poly_combo.setEnabled)
        self._ext_ur_chk.toggled.connect(self._ext_line_combo.setEnabled)
        self._ext_ur_chk.toggled.connect(self._ext_buffer_spin.setEnabled)

        layout.addStretch()
        scroll.setWidget(widget)

        # ── Signale verbinden ─────────────────────────────────────────
        self._preset_load_btn.clicked.connect(self._load_selected_presets)
        self._ext_fetch_btn.clicked.connect(self._fetch_external_caps)
        self._ext_url_edit.returnPressed.connect(self._fetch_external_caps)
        self._ext_load_btn.clicked.connect(self._load_external_layers)
        self._ext_toggle_all_btn.clicked.connect(self._toggle_ext_all)

        return outer

    def _load_selected_presets(self):
        checked = [(cb, url, bl) for cb, url, bl in self._preset_service_checks if cb.isChecked() and url]
        if not checked:
            return

        # Deselect preset checkboxes immediately
        for cb, _url, _bl in checked:
            cb.setChecked(False)

        # Reset layer list
        self._ext_layer_defs = []
        self._ext_layer_checks.clear()
        while self._ext_layer_layout.count():
            item = self._ext_layer_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._ext_load_btn.setEnabled(False)
        self._ext_toggle_all_btn.setEnabled(False)
        self._ext_toggle_all_btn.setText("Alle auswählen")
        self._preset_load_btn.setEnabled(False)

        self._preset_caps_pending = len(checked)
        self._preset_caps_results = []
        self._ext_caps_status.setText(f"Lade Capabilities von {len(checked)} Diensten …")

        for cb, url, bl in checked:
            base = url.split("?")[0]
            thread = ExternerWfsCapabilitiesThread(base)
            thread.caps_ready.connect(
                lambda layers, _bl=bl: self._on_preset_caps_ready(layers, _bl)
            )
            thread.failed.connect(
                lambda _url, _bl=bl: self._on_preset_caps_partial_fail(_url, _bl)
            )
            thread.finished.connect(lambda t=thread: self._cleanup_thread(t))
            self._threads.append(thread)
            thread.start()

    def _on_preset_caps_ready(self, layer_defs, bundesland):
        for ld in layer_defs:
            ld["bundesland"] = bundesland
        self._preset_caps_results.append((bundesland, layer_defs))
        self._preset_caps_pending -= 1
        if self._preset_caps_pending == 0:
            self._on_preset_all_caps_done()

    def _on_preset_caps_partial_fail(self, url, bundesland):
        self._preset_caps_pending -= 1
        if self._preset_caps_pending == 0:
            self._on_preset_all_caps_done()

    def _on_preset_all_caps_done(self):
        # Merge all results, sorted by bundesland name
        all_defs = []
        for _bl, defs in sorted(self._preset_caps_results, key=lambda x: x[0] or ""):
            all_defs.extend(defs)

        self._ext_layer_defs = all_defs

        # Clear and rebuild layer list grouped by bundesland
        while self._ext_layer_layout.count():
            item = self._ext_layer_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._ext_layer_checks.clear()

        current_bl = None
        for i, ld in enumerate(all_defs):
            bl = ld.get("bundesland", "")
            if bl != current_bl:
                current_bl = bl
                lbl = QLabel(f"<b>{bl}</b>")
                lbl.setStyleSheet(
                    "font-size:11px; color:#2e7d32; margin-top:6px; margin-bottom:1px;"
                )
                self._ext_layer_layout.addWidget(lbl)
            cb = QCheckBox(ld["title"])
            cb.setChecked(True)
            cb.setStyleSheet("font-size:11px")
            self._ext_layer_layout.addWidget(cb)
            key = f"__p{i}"
            ld["_ui_key"] = key
            self._ext_layer_checks[key] = cb

        n_services = len(self._preset_caps_results)
        n_layers   = len(all_defs)
        self._ext_caps_status.setText(
            f"{n_layers} Layer von {n_services} Diensten verfügbar – Auswahl treffen und laden."
        )
        self._preset_load_btn.setEnabled(True)
        if all_defs:
            self._ext_toggle_all_btn.setEnabled(True)
            self._ext_toggle_all_btn.setText("Alle abwählen")
            self._ext_load_btn.setEnabled(True)
        self._show_heavy_layer_hint(all_defs)

    def _fetch_external_caps(self):
        url = self._ext_url_edit.text().strip()
        if not url:
            return
        base = url.split("?")[0]

        self._ext_fetch_btn.setEnabled(False)
        self._ext_caps_status.setText("Lade GetCapabilities …")
        self._ext_layer_checks.clear()
        self._ext_layer_defs = []
        self._ext_toggle_all_btn.setEnabled(False)
        self._ext_toggle_all_btn.setText("Alle auswählen")

        thread = ExternerWfsCapabilitiesThread(base)
        thread.caps_ready.connect(self._on_ext_caps_ready)
        thread.failed.connect(self._on_ext_caps_failed)
        thread.finished.connect(lambda: self._ext_fetch_btn.setEnabled(True))
        thread.finished.connect(lambda: self._cleanup_thread(thread))
        self._threads.append(thread)
        thread.start()

    def _on_ext_caps_ready(self, layer_defs):
        self._ext_layer_defs = layer_defs
        # Alte Checkboxen entfernen
        while self._ext_layer_layout.count():
            item = self._ext_layer_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._ext_layer_checks.clear()

        # Nach Gruppe sortieren und anzeigen
        groups: dict = {}
        for ld in layer_defs:
            groups.setdefault(ld.get("group", "Sonstige"), []).append(ld)

        idx = 0
        for grp_name, defs in groups.items():
            grp_label = QLabel(f"<b>{grp_name}</b>")
            grp_label.setStyleSheet("font-size:11px; margin-top:4px")
            self._ext_layer_layout.addWidget(grp_label)
            for ld in defs:
                cb = QCheckBox(ld["title"])
                cb.setChecked(True)
                cb.setStyleSheet("font-size:11px")
                self._ext_layer_layout.addWidget(cb)
                key = f"__m{idx}"
                ld["_ui_key"] = key
                self._ext_layer_checks[key] = cb
                idx += 1

        self._ext_caps_status.setText(f"{len(layer_defs)} Layer gefunden.")
        self._ext_toggle_all_btn.setEnabled(True)
        self._ext_toggle_all_btn.setText("Alle abwählen")
        self._ext_load_btn.setEnabled(True)
        self._show_heavy_layer_hint(layer_defs)

    def _show_heavy_layer_hint(self, layer_defs: list) -> None:
        _HEAVY = ["flurstück", "alkis", "boden", "bodenkarte", "bodenfunktion",
                  "bodennutzung", "bodenversiegelung", "erosion", "geologie",
                  "grundwasser", "nitrat", "gewässernetz", "hydrogeol"]
        titles = " ".join(ld.get("title", "").lower() for ld in layer_defs)
        if any(kw in titles for kw in _HEAVY):
            self._ext_status.setText(
                "⚠ Datenintensive Layer (Flurstücke, Boden, ALKIS …) bitte einzeln laden. "
                "Zuerst weit in den gewünschten Bereich reinzoomen und laden – "
                "klappt das nicht, auf einen kleineren Maßstab wechseln und erneut versuchen. "
                "Flurstück-Layer können je nach Dienst fehleranfällig sein."
            )
            self._ext_status.setStyleSheet(
                "font-size:11px; color:#e65100; background:#fff3e0; "
                "padding:4px; border-radius:3px;"
            )
        else:
            self._ext_status.setText("")
            self._ext_status.setStyleSheet("font-size:11px; color:#555")

    def _toggle_ext_all(self):
        all_checked = (bool(self._ext_layer_checks) and
                       all(cb.isChecked() for cb in self._ext_layer_checks.values()))
        new_state = not all_checked
        for cb in self._ext_layer_checks.values():
            cb.setChecked(new_state)
        self._ext_toggle_all_btn.setText("Alle abwählen" if new_state else "Alle auswählen")

    def _on_ext_caps_failed(self, url):
        self._ext_caps_status.setText(f"✗ Dienst nicht erreichbar: {url}")
        self.iface.messageBar().pushMessage(
            "Externer WFS", f"GetCapabilities fehlgeschlagen: {url}", MSG_WARNING, 6
        )

    def _load_external_layers(self):
        selected = [
            ld for ld in self._ext_layer_defs
            if self._ext_layer_checks.get(ld.get("_ui_key", ld["id"]), QCheckBox()).isChecked()
        ]
        if not selected:
            return

        self._ext_load_btn.setEnabled(False)
        self._ext_cancel_btn.setVisible(True)
        self._ext_progress.setVisible(True)
        self._ext_progress.setRange(0, len(selected))
        self._ext_progress.setValue(0)

        if self._ext_ur_chk.isChecked():
            self._run_ext_intersection(selected)
        else:
            has_bundesland = any(ld.get("bundesland") for ld in selected)
            if has_bundesland:
                # Preset-Modus: Gruppen pro Bundesland vorab anlegen
                self._ext_bl_groups    = {}
                self._ext_bl_subgroups = {}
                root = QgsProject.instance().layerTreeRoot()
                for ld in selected:
                    bl = ld.get("bundesland") or "Sonstige"
                    if bl not in self._ext_bl_groups:
                        try:
                            self._ext_bl_groups[bl] = root.insertGroup(0, f"Externe WFS – {bl}")
                        except RuntimeError:
                            self._ext_bl_groups[bl] = None
                self._ext_group = None
            else:
                # Manueller URL-Aufruf: eine Gruppe nach Hostname
                import urllib.parse as _up
                raw_url = self._ext_url_edit.text().strip()
                try:
                    host = _up.urlparse(raw_url).hostname or raw_url[:40]
                except Exception:
                    host = raw_url[:40]
                try:
                    root = QgsProject.instance().layerTreeRoot()
                    self._ext_group = root.insertGroup(0, f"Externer WFS – {host}")
                except RuntimeError:
                    self._ext_group = None

            self._ext_status.setText("Lade Layer …")
            self._ext_pending = len(selected)
            self.iface.mapCanvas().freeze(True)
            self._canvas_frozen = True
            for ld in selected:
                task = SingleWfsLoaderTask(ld)
                task.layer_ready.connect(self._on_ext_layer_ready)
                task.layer_failed.connect(self._on_ext_layer_failed)
                task.layer_ready.connect(lambda *a, t=task: self._cleanup_task(t))
                task.layer_failed.connect(lambda *a, t=task: self._cleanup_task(t))
                self._tasks.append(task)
                QgsApplication.taskManager().addTask(task)

    def _run_ext_intersection(self, selected_defs):
        poly_layer = self._ext_poly_combo.currentLayer()
        line_layer = self._ext_line_combo.currentLayer()
        buffer_m   = self._ext_buffer_spin.value()

        if poly_layer is None and line_layer is None:
            QMessageBox.warning(
                self, "Kein Layer",
                "Bitte mindestens einen Polygon- oder Linien-Layer auswählen."
            )
            self._ext_load_btn.setEnabled(True)
            self._ext_progress.setVisible(False)
            return
        if line_layer is not None and buffer_m == 0:
            QMessageBox.warning(
                self, "Pufferabstand fehlt",
                "Bitte einen Pufferabstand > 0 m für den Linien-Layer angeben."
            )
            self._ext_load_btn.setEnabled(True)
            self._ext_progress.setVisible(False)
            return

        try:
            root = QgsProject.instance().layerTreeRoot()
            self._ext_int_group_outside = root.insertGroup(0, "Externer WFS – außerhalb UR")
            self._ext_int_group_inside  = root.insertGroup(0, "Externer WFS – innerhalb UR")
        except RuntimeError:
            self._ext_int_group_inside  = None
            self._ext_int_group_outside = None

        self._ext_status.setText("Lade Daten und führe Verschneidung durch …")
        task = IntersectionTask(poly_layer, line_layer, buffer_m, selected_defs)
        task.layer_done.connect(self._on_ext_intersect_layer_done)
        task.layer_failed.connect(self._on_ext_intersect_layer_failed)
        task.all_done.connect(self._on_ext_intersect_all_done)
        task.all_done.connect(lambda: self._cleanup_task(task))
        self._tasks.append(task)
        QgsApplication.taskManager().addTask(task)

    def _on_ext_layer_ready(self, layer, title, layer_def):
        try:
            apply_style(layer, layer_def)
        except Exception:
            pass
        bl = layer_def.get("bundesland", "")
        try:
            if bl:
                # Preset-Modus: Bundesland-Gruppe + UVP-Untergruppe
                grp = self._ext_bl_groups.get(bl)
                if grp is not None:
                    cat    = _uvp_category(title)
                    bl_sub = self._ext_bl_subgroups.setdefault(bl, {})
                    if cat not in bl_sub:
                        bl_sub[cat] = grp.addGroup(cat)
                    QgsProject.instance().addMapLayer(layer, False)
                    bl_sub[cat].addLayer(layer)
                else:
                    QgsProject.instance().addMapLayer(layer)
            elif self._ext_group is not None:
                QgsProject.instance().addMapLayer(layer, False)
                self._ext_group.addLayer(layer)
            else:
                QgsProject.instance().addMapLayer(layer)
        except RuntimeError:
            QgsProject.instance().addMapLayer(layer)
        self._ext_progress.setValue(self._ext_progress.value() + 1)
        self._ext_status.setText(f"✓ {title}")
        self.iface.messageBar().pushMessage(
            "Externer WFS", f"Layer geladen: {title}", MSG_SUCCESS, 3
        )
        self._ext_pending -= 1
        if self._ext_pending <= 0:
            self._on_ext_all_done()

    def _on_ext_layer_failed(self, title):
        self._ext_progress.setValue(self._ext_progress.value() + 1)
        self._ext_status.setText(f"○ Fehler: {title}")
        self._ext_pending -= 1
        if self._ext_pending <= 0:
            self._on_ext_all_done()

    def _on_ext_all_done(self):
        self._ext_load_btn.setEnabled(True)
        self._ext_cancel_btn.setVisible(False)
        self._ext_progress.setVisible(False)
        self._ext_status.setText("Fertig.")
        if self._canvas_frozen:
            self._canvas_frozen = False
            self.iface.mapCanvas().freeze(False)
            self.iface.mapCanvas().refresh()
        # Leere Hostname-Gruppe entfernen (manueller URL-Modus)
        try:
            root = QgsProject.instance().layerTreeRoot()
            if self._ext_group is not None and len(self._ext_group.children()) == 0:
                root.removeChildNode(self._ext_group)
        except RuntimeError:
            pass
        finally:
            self._ext_group = None

    def _cancel_ext_load(self):
        for task in list(self._tasks):
            try:
                task.cancel()
            except Exception:
                pass
        self._tasks.clear()
        self._ext_pending = 0
        self._ext_cancel_btn.setVisible(False)
        self._ext_load_btn.setEnabled(True)
        self._ext_progress.setVisible(False)
        self._ext_status.setText("Abgebrochen.")
        if self._canvas_frozen:
            self._canvas_frozen = False
            self.iface.mapCanvas().freeze(False)
            self.iface.mapCanvas().refresh()

    def _filter_ext_layers(self, text: str):
        text = text.strip().lower()
        for key, cb in self._ext_layer_checks.items():
            cb.setVisible(not text or text in cb.text().lower())

    def _on_ext_intersect_layer_done(self, inside_layer, outside_layer, title, layer_def):
        for lyr, grp, is_inside in (
            (inside_layer,  self._ext_int_group_inside,  True),
            (outside_layer, self._ext_int_group_outside, False),
        ):
            if lyr is None:
                continue
            try:
                if grp is not None:
                    QgsProject.instance().addMapLayer(lyr, False)
                    grp.addLayer(lyr)
                else:
                    QgsProject.instance().addMapLayer(lyr)
                apply_style(lyr, layer_def, is_inside=is_inside)
            except RuntimeError:
                pass
        self._ext_progress.setValue(self._ext_progress.value() + 1)
        parts = [s for s, l in (("innerhalb", inside_layer), ("außerhalb", outside_layer)) if l]
        self._ext_status.setText(f"✓ {title} ({', '.join(parts)})")

    def _on_ext_intersect_layer_failed(self, title):
        self._ext_progress.setValue(self._ext_progress.value() + 1)
        self._ext_status.setText(f"○ Keine Treffer: {title}")

    def _on_ext_intersect_all_done(self):
        self._ext_load_btn.setEnabled(True)
        self._ext_progress.setVisible(False)
        self._ext_status.setText("Verschneidung abgeschlossen.")
        try:
            root = QgsProject.instance().layerTreeRoot()
            for grp in (self._ext_int_group_inside, self._ext_int_group_outside):
                try:
                    if grp is not None and len(grp.children()) == 0:
                        root.removeChildNode(grp)
                except RuntimeError:
                    pass
        finally:
            self._ext_int_group_inside  = None
            self._ext_int_group_outside = None

    # ------------------------------------------------------------------
    # Hilfsmethoden
    # ------------------------------------------------------------------

    def _cleanup_thread(self, thread):
        if thread in self._threads:
            self._threads.remove(thread)

    def _cleanup_task(self, task):
        # Defer by one event-loop tick so Qt can finish disconnecting signals
        # before SIP releases the Python wrapper (prevents SIGSEGV in sipQgsTask).
        QTimer.singleShot(0, lambda: self._tasks.remove(task) if task in self._tasks else None)

    def closeEvent(self, event):
        for t in list(self._threads):
            t.quit()
            t.wait(2000)
        for t in list(self._tasks):
            t.cancel()
        super().closeEvent(event)

    # ==================================================================
    # Tab: Untersuchungsraum
    # ==================================================================

    def _build_untersuchungsraum_tab(self):
        # Äußeres Widget enthält nur die ScrollArea
        outer = QWidget()
        outer_layout = QVBoxLayout(outer)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(FRAME_NONE)
        outer_layout.addWidget(scroll)

        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(8)

        # ── Eingabe-Layer ────────────────────────────────────────────
        mask_group  = QGroupBox("Untersuchungsraum-Layer")
        mask_layout = QVBoxLayout(mask_group)

        mask_layout.addWidget(QLabel("Polygon-Layer (optional):"))
        self._ur_poly_combo = QgsMapLayerComboBox()
        self._ur_poly_combo.setFilters(LAYER_FILTER_POLYGON)
        self._ur_poly_combo.setAllowEmptyLayer(True)
        self._ur_poly_combo.setCurrentIndex(0)
        mask_layout.addWidget(self._ur_poly_combo)

        mask_layout.addWidget(QLabel("Linien-Layer (optional, wird gepuffert):"))
        self._ur_line_combo = QgsMapLayerComboBox()
        self._ur_line_combo.setFilters(LAYER_FILTER_LINE)
        self._ur_line_combo.setAllowEmptyLayer(True)
        self._ur_line_combo.setCurrentIndex(0)
        mask_layout.addWidget(self._ur_line_combo)

        buf_row = QHBoxLayout()
        buf_row.addWidget(QLabel("Pufferabstand:"))
        self._ur_buffer_spin = QDoubleSpinBox()
        self._ur_buffer_spin.setRange(0, 100000)
        self._ur_buffer_spin.setValue(100)
        self._ur_buffer_spin.setSuffix(" m")
        self._ur_buffer_spin.setDecimals(1)
        self._ur_buffer_spin.setMinimumWidth(110)
        buf_row.addWidget(self._ur_buffer_spin)
        buf_row.addStretch()
        mask_layout.addLayout(buf_row)

        proj_note = QLabel(
            "<i>Projektion wird automatisch nach EPSG:25832 transformiert.</i>"
        )
        proj_note.setStyleSheet("font-size:10px; color:#777;")
        proj_note.setWordWrap(True)
        mask_layout.addWidget(proj_note)

        self._ur_mask_btn = QPushButton("Untersuchungsraum als Layer speichern")
        self._ur_mask_btn.setToolTip(
            "Erstellt einen Polygon-Layer aus dem gepufferten Untersuchungsraum\n"
            "ohne WFS-Verschneidung – für Karten und Dokumentation."
        )
        self._ur_mask_btn.clicked.connect(self._save_mask_as_layer)
        mask_layout.addWidget(self._ur_mask_btn)

        layout.addWidget(mask_group)

        # ── BFN-Layer-Auswahl ────────────────────────────────────────
        bfn_group  = QGroupBox("BFN-Layer verschneiden")
        bfn_layout = QVBoxLayout(bfn_group)

        self._ur_layer_checks = {}
        current_group = None
        for layer_def in DEFAULT_LAYERS:
            if layer_def.get("group") != current_group:
                current_group = layer_def["group"]
                lbl = QLabel(f"<b>{current_group}</b>")
                lbl.setStyleSheet("color:#555; margin-top:4px;")
                bfn_layout.addWidget(lbl)
            cb = QCheckBox(layer_def["title"])
            cb.setChecked(False)
            self._ur_layer_checks[layer_def["id"]] = cb
            bfn_layout.addWidget(cb)

        sel_row = QHBoxLayout()
        btn_all  = QPushButton("Alle");  btn_all.setMaximumWidth(55)
        btn_none = QPushButton("Keine"); btn_none.setMaximumWidth(55)
        btn_all.clicked.connect(
            lambda: [cb.setChecked(True)  for cb in self._ur_layer_checks.values()])
        btn_none.clicked.connect(
            lambda: [cb.setChecked(False) for cb in self._ur_layer_checks.values()])
        sel_row.addWidget(btn_all); sel_row.addWidget(btn_none); sel_row.addStretch()
        bfn_layout.addLayout(sel_row)
        layout.addWidget(bfn_group)

        # ── Verschneidung starten ────────────────────────────────────
        self._ur_run_btn = QPushButton("Verschneidung starten")
        self._ur_run_btn.setStyleSheet(
            "QPushButton{background:#6a1b9a;color:white;padding:6px;border-radius:4px}"
            "QPushButton:hover{background:#7b1fa2}"
            "QPushButton:disabled{background:#aaa}"
        )
        self._ur_run_btn.clicked.connect(self._run_intersection)
        layout.addWidget(self._ur_run_btn)

        self._ur_progress = QProgressBar()
        self._ur_progress.setVisible(False)
        layout.addWidget(self._ur_progress)

        self._ur_status = QLabel("")
        self._ur_status.setWordWrap(True)
        self._ur_status.setStyleSheet("font-size:11px; color:#555;")
        layout.addWidget(self._ur_status)

        self._ur_export_btn = QPushButton("Ergebnisse als GeoPackage exportieren")
        self._ur_export_btn.setEnabled(False)
        self._ur_export_btn.clicked.connect(self._export_results)
        layout.addWidget(self._ur_export_btn)

        self._ur_summary_label = QLabel("")
        self._ur_summary_label.setWordWrap(True)
        self._ur_summary_label.setStyleSheet(
            "font-size:11px; color:#1a237e; background:#e8eaf6; "
            "padding:6px; border-radius:4px; margin-top:4px;"
        )
        self._ur_summary_label.setVisible(False)
        layout.addWidget(self._ur_summary_label)

        layout.addStretch()
        scroll.setWidget(widget)
        return outer

    def _export_results(self):
        from qgis.PyQt.QtWidgets import QFileDialog
        from qgis.core import QgsVectorFileWriter

        layers = [
            QgsProject.instance().mapLayer(lid)
            for lid in self._ur_result_layer_ids
            if QgsProject.instance().mapLayer(lid)
        ]
        if not layers:
            QMessageBox.information(self, "Keine Ergebnisse",
                                    "Keine Verschneidungsergebnisse vorhanden.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Ergebnisse exportieren",
            "verschneidung.gpkg", "GeoPackage (*.gpkg)"
        )
        if not path:
            return
        if not path.endswith(".gpkg"):
            path += ".gpkg"

        errors = []
        for i, lyr in enumerate(layers):
            opts = QgsVectorFileWriter.SaveVectorOptions()
            opts.driverName  = "GPKG"
            opts.layerName   = lyr.name()[:63]
            opts.actionOnExistingFile = (
                QgsVectorFileWriter.CreateOrOverwriteFile if i == 0
                else QgsVectorFileWriter.CreateOrOverwriteLayer
            )
            err, msg, *_ = QgsVectorFileWriter.writeAsVectorFormatV3(
                lyr, path, QgsProject.instance().transformContext(), opts
            )
            if err != QgsVectorFileWriter.NoError:
                errors.append(f"{lyr.name()}: {msg}")

        if errors:
            QMessageBox.warning(self, "Export-Fehler", "\n".join(errors))
        else:
            self.iface.messageBar().pushMessage(
                "Export", f"{len(layers)} Layer → {path}", MSG_SUCCESS, 6
            )

    def _save_mask_as_layer(self):
        from qgis.core import (
            QgsCoordinateReferenceSystem, QgsVectorLayer, QgsFeature,
            QgsFields, QgsField,
        )
        poly_layer = self._ur_poly_combo.currentLayer()
        line_layer = self._ur_line_combo.currentLayer()
        buffer_m   = self._ur_buffer_spin.value()

        if poly_layer is None and line_layer is None:
            QMessageBox.warning(
                self, "Kein Layer",
                "Bitte mindestens einen Polygon- oder Linien-Layer auswählen."
            )
            return
        if line_layer is not None and buffer_m == 0:
            QMessageBox.warning(
                self, "Pufferabstand fehlt",
                "Bitte einen Pufferabstand > 0 m für den Linien-Layer angeben."
            )
            return

        target_crs    = QgsCoordinateReferenceSystem("EPSG:25832")
        transform_ctx = QgsProject.instance().transformContext()
        mask_geom     = build_mask(
            poly_layer, line_layer, buffer_m, target_crs, transform_ctx
        )
        if mask_geom is None or mask_geom.isEmpty():
            QMessageBox.warning(
                self, "Leere Geometrie",
                "Der Untersuchungsraum ist leer – bitte Layer prüfen."
            )
            return

        name = "Untersuchungsraum"
        if line_layer is not None:
            name += f" (Puffer {int(buffer_m)} m)"

        mem_layer = QgsVectorLayer(
            f"MultiPolygon?crs=EPSG:25832", name, "memory"
        )
        prov   = mem_layer.dataProvider()
        fields = QgsFields()
        fields.append(QgsField("name", FIELD_STRING))
        prov.addAttributes(fields)
        mem_layer.updateFields()

        feat = QgsFeature(mem_layer.fields())
        feat.setAttribute("name", name)
        feat.setGeometry(mask_geom)
        prov.addFeature(feat)
        mem_layer.updateExtents()

        QgsProject.instance().addMapLayer(mem_layer)
        self.iface.messageBar().pushMessage(
            "Untersuchungsraum", f"Layer erstellt: {name}", MSG_SUCCESS, 4
        )

    def _run_intersection(self):
        poly_layer = self._ur_poly_combo.currentLayer()
        line_layer = self._ur_line_combo.currentLayer()
        buffer_m   = self._ur_buffer_spin.value()

        if poly_layer is None and line_layer is None:
            QMessageBox.warning(
                self, "Kein Layer",
                "Bitte mindestens einen Polygon- oder Linien-Layer auswählen."
            )
            return
        if line_layer is not None and buffer_m == 0:
            QMessageBox.warning(
                self, "Pufferabstand fehlt",
                "Bitte einen Pufferabstand > 0 m für den Linien-Layer angeben."
            )
            return

        selected_defs = [
            d for d in DEFAULT_LAYERS
            if self._ur_layer_checks.get(d["id"], QCheckBox()).isChecked()
        ]
        if not selected_defs:
            QMessageBox.information(
                self, "Keine BFN-Layer",
                "Bitte mindestens einen BFN-Layer zum Verschneiden auswählen."
            )
            return

        self._ur_run_btn.setEnabled(False)
        self._ur_export_btn.setEnabled(False)
        self._ur_result_layer_ids.clear()
        self._ur_summary.clear()
        self._ur_summary_label.setVisible(False)
        self._ur_progress.setVisible(True)
        self._ur_progress.setRange(0, len(selected_defs))
        self._ur_progress.setValue(0)
        self._ur_status.setText("Lade Daten und führe Verschneidung durch …")

        root = QgsProject.instance().layerTreeRoot()
        self._ur_group_inside  = root.addGroup("BFN Verschneidung – innerhalb UR")
        self._ur_group_outside = root.addGroup("BFN Verschneidung – außerhalb UR")

        task = IntersectionTask(poly_layer, line_layer, buffer_m, selected_defs)
        task.layer_done.connect(self._on_intersection_layer_done)
        task.layer_failed.connect(self._on_intersection_layer_failed)
        task.all_done.connect(self._on_intersection_all_done)
        task.all_done.connect(lambda: self._cleanup_task(task))
        self._tasks.append(task)
        QgsApplication.taskManager().addTask(task)

    @staticmethod
    def _sum_ha(lyr) -> float:
        if lyr is None:
            return 0.0
        idx = lyr.fields().indexOf("flaeche_ha")
        if idx < 0:
            return 0.0
        return round(sum((f.attributes()[idx] or 0.0) for f in lyr.getFeatures()), 2)

    def _on_intersection_layer_done(self, inside_layer, outside_layer, title, layer_def):
        for lyr, grp, is_inside in (
            (inside_layer,  self._ur_group_inside,  True),
            (outside_layer, self._ur_group_outside, False),
        ):
            if lyr is None:
                continue
            try:
                if grp is not None:
                    QgsProject.instance().addMapLayer(lyr, False)
                    grp.addLayer(lyr)
                else:
                    QgsProject.instance().addMapLayer(lyr)
                apply_style(lyr, layer_def, is_inside=is_inside)
                self._ur_result_layer_ids.append(lyr.id())
            except RuntimeError:
                pass

        self._ur_summary[title] = {
            "inside_ha":  self._sum_ha(inside_layer),
            "outside_ha": self._sum_ha(outside_layer),
        }

        done = self._ur_progress.value() + 1
        self._ur_progress.setValue(done)
        parts = [s for s, l in (("innerhalb", inside_layer), ("außerhalb", outside_layer)) if l is not None]
        self._ur_status.setText(f"✓ {title} ({', '.join(parts)})")
        self.iface.messageBar().pushMessage(
            "BFN Verschneidung", f"Layer erstellt: {title}", MSG_SUCCESS, 3
        )

    def _on_intersection_layer_failed(self, title):
        self._ur_progress.setValue(self._ur_progress.value() + 1)
        self._ur_status.setText(f"○ Keine Treffer: {title}")

    def _on_intersection_all_done(self):
        # UI-Reset immer zuerst – unabhängig von Folgefehlern
        self._ur_run_btn.setEnabled(True)
        self._ur_progress.setVisible(False)
        self._ur_status.setText("Verschneidung abgeschlossen.")
        if self._ur_result_layer_ids:
            self._ur_export_btn.setEnabled(True)

        # Ergebnis-Zusammenfassung anzeigen
        if self._ur_summary:
            lines      = ["<b>Zusammenfassung:</b>"]
            total_in   = 0.0
            total_out  = 0.0
            for t in sorted(self._ur_summary):
                d   = self._ur_summary[t]
                inn = d.get("inside_ha",  0.0)
                out = d.get("outside_ha", 0.0)
                total_in  += inn
                total_out += out
                line = f"• {t}: <b>{inn:.2f} ha</b> innerhalb"
                if out > 0:
                    line += f", {out:.2f} ha außerhalb"
                lines.append(line)
            lines.append(
                f"<b>Σ innerhalb UR: {total_in:.2f} ha"
                + (f" &nbsp;|&nbsp; außerhalb: {total_out:.2f} ha" if total_out > 0 else "")
                + "</b>"
            )
            self._ur_summary_label.setText("<br>".join(lines))
            self._ur_summary_label.setVisible(True)

        # Leere Gruppen aufräumen (C++-Objekte können bereits ungültig sein)
        try:
            root = QgsProject.instance().layerTreeRoot()
            for grp in (self._ur_group_inside, self._ur_group_outside):
                try:
                    if grp is not None and len(grp.children()) == 0:
                        root.removeChildNode(grp)
                except RuntimeError:
                    pass
        finally:
            self._ur_group_inside  = None
            self._ur_group_outside = None
