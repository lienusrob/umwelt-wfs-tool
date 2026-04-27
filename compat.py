"""
QGIS 3 / QGIS 4 compatibility shim.

QGIS 4 (Qt6) moved many enums to scoped namespaces:
  QgsWkbTypes.Point           → Qgis.WkbType.Point
  QgsProcessing.TypeVector*   → Qgis.ProcessingSourceType.*
  QgsFeatureSink.FastInsert   → QgsFeatureSink.SinkFlag.FastInsert
  QgsProcessingParameterNumber.Double → …Number.Type.Double
  Qt.AlignCenter              → Qt.AlignmentFlag.AlignCenter  (PyQt6)
  Qt.RightDockWidgetArea      → Qt.DockWidgetArea.RightDockWidgetArea
  Qt.UserRole                 → Qt.ItemDataRole.UserRole
  QFrame.NoFrame              → QFrame.Shape.NoFrame
"""

from qgis.core import Qgis

QGIS4 = Qgis.QGIS_VERSION_INT >= 40000

# ── WKB geometry types ────────────────────────────────────────────────
if QGIS4:
    WKB_POINT        = Qgis.WkbType.Point
    WKB_MULTIPOLY    = Qgis.WkbType.MultiPolygon
    WKB_NO_GEOM      = Qgis.WkbType.NoGeometry
else:
    from qgis.core import QgsWkbTypes
    WKB_POINT        = QgsWkbTypes.Point
    WKB_MULTIPOLY    = QgsWkbTypes.MultiPolygon
    WKB_NO_GEOM      = QgsWkbTypes.NoGeometry

# ── Processing source types ───────────────────────────────────────────
if QGIS4:
    SRC_POLYGON = Qgis.ProcessingSourceType.VectorPolygon
    SRC_LINE    = Qgis.ProcessingSourceType.VectorLine
    SRC_ANY     = Qgis.ProcessingSourceType.VectorAnyGeometry
    SRC_VECTOR  = Qgis.ProcessingSourceType.Vector
else:
    from qgis.core import QgsProcessing
    SRC_POLYGON = QgsProcessing.TypeVectorPolygon
    SRC_LINE    = QgsProcessing.TypeVectorLine
    SRC_ANY     = QgsProcessing.TypeVectorAnyGeometry
    SRC_VECTOR  = QgsProcessing.TypeVector

# ── QgsFeatureSink flags ──────────────────────────────────────────────
from qgis.core import QgsFeatureSink
try:
    SINK_FAST = QgsFeatureSink.SinkFlag.FastInsert   # QGIS 4
except AttributeError:
    SINK_FAST = QgsFeatureSink.FastInsert             # QGIS 3

# ── QgsProcessingParameterNumber type ────────────────────────────────
from qgis.core import QgsProcessingParameterNumber
try:
    PARAM_DOUBLE = QgsProcessingParameterNumber.Type.Double   # QGIS 4
except AttributeError:
    PARAM_DOUBLE = QgsProcessingParameterNumber.Double         # QGIS 3

# ── Qgis message levels ──────────────────────────────────────────────
try:                                           # QGIS 4
    MSG_INFO     = Qgis.MessageLevel.Info
    MSG_WARNING  = Qgis.MessageLevel.Warning
    MSG_CRITICAL = Qgis.MessageLevel.Critical
    MSG_SUCCESS  = Qgis.MessageLevel.Success
except AttributeError:                         # QGIS 3
    MSG_INFO     = Qgis.Info
    MSG_WARNING  = Qgis.Warning
    MSG_CRITICAL = Qgis.Critical
    MSG_SUCCESS  = Qgis.Success

# ── Qt enum wrappers (PyQt5 vs PyQt6) ────────────────────────────────
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QFrame

try:                                          # PyQt6 / QGIS 4
    ALIGN_CENTER   = Qt.AlignmentFlag.AlignCenter
    DOCK_RIGHT     = Qt.DockWidgetArea.RightDockWidgetArea
    ITEM_USER_ROLE = Qt.ItemDataRole.UserRole
    FRAME_NONE     = QFrame.Shape.NoFrame
except AttributeError:                        # PyQt5 / QGIS 3
    ALIGN_CENTER   = Qt.AlignCenter
    DOCK_RIGHT     = Qt.RightDockWidgetArea
    ITEM_USER_ROLE = Qt.UserRole
    FRAME_NONE     = QFrame.NoFrame

# ── QgsTask flags ────────────────────────────────────────────────────
from qgis.core import QgsTask
try:
    TASK_CAN_CANCEL = QgsTask.Flag.CanCancel   # QGIS 4
except AttributeError:
    TASK_CAN_CANCEL = QgsTask.CanCancel         # QGIS 3

# ── QgsMapLayerProxyModel filters ─────────────────────────────────────
from qgis.core import QgsMapLayerProxyModel
try:
    LAYER_FILTER_POLYGON = QgsMapLayerProxyModel.Filter.PolygonLayer   # QGIS 4
    LAYER_FILTER_LINE    = QgsMapLayerProxyModel.Filter.LineLayer
except AttributeError:
    LAYER_FILTER_POLYGON = QgsMapLayerProxyModel.PolygonLayer           # QGIS 3
    LAYER_FILTER_LINE    = QgsMapLayerProxyModel.LineLayer

# ── QgsField type constants (QVariant → QMetaType in QGIS 4 / PyQt6) ─
try:
    from qgis.PyQt.QtCore import QMetaType   # PyQt6 / QGIS 4
    FIELD_STRING = QMetaType.Type.QString
    FIELD_DOUBLE = QMetaType.Type.Double
    FIELD_INT    = QMetaType.Type.Int
    FIELD_BOOL   = QMetaType.Type.Bool
except (ImportError, AttributeError):
    from qgis.PyQt.QtCore import QVariant    # PyQt5 / QGIS 3
    FIELD_STRING = QVariant.String
    FIELD_DOUBLE = QVariant.Double
    FIELD_INT    = QVariant.Int
    FIELD_BOOL   = QVariant.Bool
