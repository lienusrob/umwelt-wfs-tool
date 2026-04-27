from qgis.core import (
    QgsProcessingAlgorithm,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterNumber,
    QgsProcessingParameterBoolean,
    QgsProcessingException,
    QgsCoordinateTransform,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsGeometry,
    QgsFeatureSink,
    QgsSpatialIndex,
    QgsFields,
    QgsField,
    QgsFeatureRequest,
)
from ..compat import (
    QGIS4,
    WKB_POINT, WKB_MULTIPOLY, WKB_NO_GEOM,
    SRC_POLYGON, SRC_LINE, SRC_ANY,
    SINK_FAST, PARAM_DOUBLE,
    FIELD_STRING,
)


class VerschneidungAlgorithm(QgsProcessingAlgorithm):
    MASK_POLYGON = "MASK_POLYGON"
    MASK_LINE    = "MASK_LINE"
    BUFFER_DIST  = "BUFFER_DIST"
    GRID_LAYER   = "GRID_LAYER"
    DATA_LAYER   = "DATA_LAYER"
    KEEP_GEOM    = "KEEP_GEOM"
    OUTPUT       = "OUTPUT"

    _BUFFER_CRS = "EPSG:25832"

    def createInstance(self):
        return VerschneidungAlgorithm()

    def name(self):
        return "bfn_verschneidung"

    def displayName(self):
        return "BFN Verschneidungsanalyse"

    def group(self):
        return "Umwelt WFS Tool"

    def groupId(self):
        return "umwelt_bfn"

    def shortHelpString(self):
        return (
            "Verschneidet eine Eingabemaske (Polygon- oder gepufferter Linien-Layer) "
            "mit einem Datenlayer (z.B. BFN-Schutzgebiete).\n\n"
            "Optionaler Raster/Grid-Layer (z.B. UTM-Quadranten):\n"
            "Ist ein Grid-Layer angegeben, wird geprüft welche Grid-Zellen die Maske "
            "schneiden. Der Zentroid jeder treffenden Zelle wird berechnet und mit dem "
            "Datenlayer verschnitten. Ausgabe sind Punkte mit kombinierten Attributen.\n\n"
            "Ohne Grid-Layer: direkte Verschneidung Maske → Datenlayer.\n\n"
            "Alle CRS-Transformationen werden automatisch durchgeführt."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.MASK_POLYGON,
                "Masken-Layer (Polygone)",
                [SRC_POLYGON],
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.MASK_LINE,
                "Masken-Layer (Linien, optional – wird gepuffert)",
                [SRC_LINE],
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.BUFFER_DIST,
                "Pufferabstand für Linien-Layer (Meter)",
                type=PARAM_DOUBLE,
                defaultValue=100.0,
                minValue=0.0,
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.GRID_LAYER,
                "Raster/Grid-Layer (z.B. UTM-Quadranten, optional)",
                [SRC_POLYGON],
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.DATA_LAYER,
                "Datenlayer (z.B. BFN-Schutzgebiete)",
                [SRC_ANY],
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.KEEP_GEOM,
                "Geometrie in Ausgabe beibehalten",
                defaultValue=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                "Verschneidungsergebnis",
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        polygon_source = self.parameterAsSource(parameters, self.MASK_POLYGON, context)
        line_source    = self.parameterAsSource(parameters, self.MASK_LINE, context)
        grid_source    = self.parameterAsSource(parameters, self.GRID_LAYER, context)
        data_source    = self.parameterAsSource(parameters, self.DATA_LAYER, context)
        buffer_dist    = self.parameterAsDouble(parameters, self.BUFFER_DIST, context)
        keep_geom      = self.parameterAsBool(parameters, self.KEEP_GEOM, context)

        if polygon_source is None and line_source is None:
            raise QgsProcessingException(
                "Mindestens ein Masken-Layer (Polygon oder Linie) muss angegeben werden."
            )

        ref_crs = (
            polygon_source.sourceCrs()
            if polygon_source is not None
            else data_source.sourceCrs()
        )

        feedback.pushInfo("Baue Maskengeometrie auf …")
        mask_geom = self._build_mask_geometry(
            polygon_source, line_source, buffer_dist, ref_crs, context, feedback
        )
        if mask_geom is None or mask_geom.isEmpty():
            raise QgsProcessingException("Die Maskengeometrie ist leer oder ungültig.")

        if grid_source is not None:
            return self._process_with_grid(
                mask_geom, ref_crs, grid_source, data_source,
                keep_geom, parameters, context, feedback,
            )
        return self._process_direct(
            mask_geom, ref_crs, data_source,
            keep_geom, parameters, context, feedback,
        )

    # ------------------------------------------------------------------
    # Modus A: Mit Grid (UTM-Quadranten)
    # Maske ∩ Grid-Zelle → Zentroid → Join Datenlayer
    # ------------------------------------------------------------------

    def _process_with_grid(self, mask_geom, mask_crs, grid_source, data_source,
                            keep_geom, parameters, context, feedback):
        grid_crs = grid_source.sourceCrs()
        data_crs = data_source.sourceCrs()

        data_source_url = data_source.publicSource()

        out_fields = QgsFields()
        for f in grid_source.fields():
            out_fields.append(f)
        for f in data_source.fields():
            out_fields.append(f)
        out_fields.append(QgsField("bfn_datenquelle", FIELD_STRING))

        out_wkb = WKB_POINT if keep_geom else WKB_NO_GEOM
        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context, out_fields, out_wkb, mask_crs
        )
        if sink is None:
            raise QgsProcessingException("Ausgabe-Datei konnte nicht erstellt werden.")

        xform_mask_to_grid = QgsCoordinateTransform(mask_crs, grid_crs, context.transformContext())
        xform_grid_to_data = QgsCoordinateTransform(grid_crs, data_crs, context.transformContext())
        xform_grid_to_mask = QgsCoordinateTransform(grid_crs, mask_crs, context.transformContext())

        feedback.pushInfo("Erstelle Spatial-Index für Datenlayer …")
        data_index    = QgsSpatialIndex(data_source.getFeatures())
        data_features = {f.id(): f for f in data_source.getFeatures()}

        mask_in_grid = QgsGeometry(mask_geom)
        if mask_crs != grid_crs:
            mask_in_grid.transform(xform_mask_to_grid)

        request     = QgsFeatureRequest().setFilterRect(mask_in_grid.boundingBox())
        grid_total  = grid_source.featureCount()
        matched     = 0

        feedback.pushInfo("Verarbeite Grid-Zellen …")
        for i, grid_feat in enumerate(grid_source.getFeatures(request)):
            if feedback.isCanceled():
                break
            if not mask_in_grid.intersects(grid_feat.geometry()):
                continue

            centroid_grid = grid_feat.geometry().centroid()

            centroid_data = QgsGeometry(centroid_grid)
            if grid_crs != data_crs:
                centroid_data.transform(xform_grid_to_data)

            for d_id in data_index.intersects(centroid_data.boundingBox()):
                data_feat = data_features[d_id]
                if not data_feat.geometry().intersects(centroid_data):
                    continue

                out_feat = QgsFeature(out_fields)
                out_feat.setAttributes(grid_feat.attributes() + data_feat.attributes() + [data_source_url])

                if keep_geom:
                    pt = QgsGeometry(centroid_grid)
                    if grid_crs != mask_crs:
                        pt.transform(xform_grid_to_mask)
                    out_feat.setGeometry(pt)

                sink.addFeature(out_feat, SINK_FAST)
                matched += 1

            if grid_total > 0:
                feedback.setProgress(int((i + 1) / grid_total * 100))

        feedback.pushInfo(f"Fertig. {matched} Treffer (Grid × Datenlayer).")
        return {self.OUTPUT: dest_id}

    # ------------------------------------------------------------------
    # Modus B: Direkte Verschneidung (ohne Grid)
    # ------------------------------------------------------------------

    def _process_direct(self, mask_geom, mask_crs, data_source,
                         keep_geom, parameters, context, feedback):
        data_crs = data_source.sourceCrs()

        mask_in_data = QgsGeometry(mask_geom)
        xform_back   = None
        if mask_crs != data_crs:
            mask_in_data.transform(
                QgsCoordinateTransform(mask_crs, data_crs, context.transformContext())
            )
            xform_back = QgsCoordinateTransform(data_crs, mask_crs, context.transformContext())

        data_source_url = data_source.publicSource()

        out_fields = QgsFields()
        for f in data_source.fields():
            out_fields.append(f)
        out_fields.append(QgsField("bfn_datenquelle", FIELD_STRING))

        out_wkb = WKB_MULTIPOLY if keep_geom else WKB_NO_GEOM
        (sink, dest_id) = self.parameterAsSink(
            parameters, self.OUTPUT, context, out_fields, out_wkb, mask_crs
        )
        if sink is None:
            raise QgsProcessingException("Ausgabe-Datei konnte nicht erstellt werden.")

        feedback.pushInfo("Erstelle Spatial-Index für Datenlayer …")
        data_index    = QgsSpatialIndex(data_source.getFeatures())
        data_features = {f.id(): f for f in data_source.getFeatures()}

        candidate_ids = data_index.intersects(mask_in_data.boundingBox())
        total         = len(candidate_ids)
        matched       = 0

        feedback.pushInfo("Führe direkte Verschneidung durch …")
        for i, fid in enumerate(candidate_ids):
            if feedback.isCanceled():
                break
            data_feat = data_features[fid]
            if not data_feat.geometry().intersects(mask_in_data):
                continue

            out_feat = QgsFeature(out_fields)
            out_feat.setAttributes(data_feat.attributes() + [data_source_url])

            if keep_geom:
                geom = data_feat.geometry().intersection(mask_in_data)
                if xform_back and not geom.isEmpty():
                    geom.transform(xform_back)
                out_feat.setGeometry(geom)

            sink.addFeature(out_feat, SINK_FAST)
            matched += 1

            if total > 0:
                feedback.setProgress(int((i + 1) / total * 100))

        feedback.pushInfo(f"Fertig. {matched} von {total} Features verschnitten.")
        return {self.OUTPUT: dest_id}

    # ------------------------------------------------------------------
    # Maskengeometrie aufbauen (Polygon + optionale gepufferte Linie)
    # ------------------------------------------------------------------

    def _build_mask_geometry(self, polygon_source, line_source, buffer_dist,
                              target_crs, context, feedback):
        parts = []

        if polygon_source is not None:
            polygon_crs = polygon_source.sourceCrs()
            xform = (
                QgsCoordinateTransform(polygon_crs, target_crs, context.transformContext())
                if polygon_crs != target_crs else None
            )
            for feat in polygon_source.getFeatures():
                g = feat.geometry()
                if xform:
                    g.transform(xform)
                parts.append(g)

        if line_source is not None and buffer_dist > 0:
            line_crs  = line_source.sourceCrs()
            buf_crs   = QgsCoordinateReferenceSystem(self._BUFFER_CRS)
            xform_in  = QgsCoordinateTransform(line_crs, buf_crs, context.transformContext())
            xform_out = QgsCoordinateTransform(buf_crs, target_crs, context.transformContext())
            n = line_source.featureCount()

            for idx, feat in enumerate(line_source.getFeatures()):
                if feedback.isCanceled():
                    break
                g = feat.geometry()
                if line_crs != buf_crs:
                    g.transform(xform_in)
                buffered = g.buffer(buffer_dist, 25)
                if buf_crs != target_crs:
                    buffered.transform(xform_out)
                parts.append(buffered)
                if n > 0:
                    feedback.setProgress(int(idx / n * 20))

        return QgsGeometry.unaryUnion(parts) if parts else None
