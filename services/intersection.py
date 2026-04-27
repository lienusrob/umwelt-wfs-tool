"""
Verschneidung eines Untersuchungsraums (Polygon oder gepufferte Linie)
mit BFN-WFS-Daten. Alle CRS-Transformationen werden automatisch durchgeführt.
"""

from typing import Optional, List, Tuple
from qgis.core import (
    QgsVectorLayer, QgsFeature, QgsGeometry, QgsField,
    QgsFields, QgsSpatialIndex, QgsFeatureRequest,
    QgsCoordinateTransform, QgsCoordinateReferenceSystem,
    QgsProject, QgsMessageLog,
)
from ..compat import FIELD_STRING, FIELD_DOUBLE, MSG_INFO

_METRIC_CRS = "EPSG:25832"   # für Puffer-Berechnungen in Metern


def build_mask(
    polygon_layer: Optional[QgsVectorLayer],
    line_layer:    Optional[QgsVectorLayer],
    buffer_m:      float,
    target_crs:    QgsCoordinateReferenceSystem,
    transform_ctx,
) -> Optional[QgsGeometry]:
    """
    Baut die Maskengeometrie aus Polygon- und/oder Linien-Layer.
    Linien werden in EPSG:25832 gepuffert (metrisch), dann nach target_crs transformiert.
    Gibt None zurück wenn kein Layer angegeben oder Geometrie leer.
    """
    parts = []

    if polygon_layer and polygon_layer.isValid():
        src_crs = polygon_layer.sourceCrs()
        xform   = (QgsCoordinateTransform(src_crs, target_crs, transform_ctx)
                   if src_crs != target_crs else None)
        for feat in polygon_layer.getFeatures():
            g = feat.geometry()
            if g.isEmpty():
                continue
            if xform:
                g.transform(xform)
            parts.append(g)

    if line_layer and line_layer.isValid() and buffer_m > 0:
        src_crs    = line_layer.sourceCrs()
        metric_crs = QgsCoordinateReferenceSystem(_METRIC_CRS)
        xform_in   = QgsCoordinateTransform(src_crs,    metric_crs, transform_ctx)
        xform_out  = QgsCoordinateTransform(metric_crs, target_crs, transform_ctx)
        for feat in line_layer.getFeatures():
            g = feat.geometry()
            if g.isEmpty():
                continue
            if src_crs != metric_crs:
                g.transform(xform_in)
            buffered = g.buffer(buffer_m, 32)
            if metric_crs != target_crs:
                buffered.transform(xform_out)
            parts.append(buffered)

    if not parts:
        return None
    result = QgsGeometry.unaryUnion(parts)
    return result if result and not result.isEmpty() else None


def intersect_wfs_with_mask(
    wfs_layer: QgsVectorLayer,
    mask_geom: QgsGeometry,
    mask_crs:  QgsCoordinateReferenceSystem,
    transform_ctx,
) -> Tuple[Optional[QgsVectorLayer], Optional[QgsVectorLayer]]:
    """
    Verschneidet einen WFS-Layer mit der Maskengeometrie.
    Gibt (innerhalb_layer, ausserhalb_layer) zurück – beide können None sein.
    Alle CRS-Transformationen werden automatisch durchgeführt.
    """
    if not wfs_layer.isValid():
        return None, None

    wfs_crs = wfs_layer.sourceCrs()

    # Maske in WFS-CRS für Vorfilter und Geometrie-Operationen
    mask_in_wfs = QgsGeometry(mask_geom)
    if mask_crs != wfs_crs:
        xform_to_wfs = QgsCoordinateTransform(mask_crs, wfs_crs, transform_ctx)
        mask_in_wfs.transform(xform_to_wfs)

    # Ausgabe-CRS
    out_crs   = QgsProject.instance().crs()
    xform_out = (QgsCoordinateTransform(wfs_crs, out_crs, transform_ctx)
                 if wfs_crs != out_crs else None)

    # Ausgabe-Schema: WFS-Felder + Quellenfeld + Fläche
    out_fields = QgsFields()
    for f in wfs_layer.fields():
        out_fields.append(f)
    out_fields.append(QgsField("bfn_datenquelle", FIELD_STRING))
    out_fields.append(QgsField("flaeche_ha",      FIELD_DOUBLE))

    source_url = wfs_layer.publicSource()
    layer_name = wfs_layer.name()
    mem_uri    = f"MultiPolygon?crs={out_crs.authid()}"

    inside_layer  = QgsVectorLayer(mem_uri, layer_name, "memory")
    outside_layer = QgsVectorLayer(mem_uri, layer_name, "memory")
    for lyr in (inside_layer, outside_layer):
        prov = lyr.dataProvider()
        prov.addAttributes(out_fields)
        lyr.updateFields()

    inside_prov  = inside_layer.dataProvider()
    outside_prov = outside_layer.dataProvider()

    bbox = mask_in_wfs.boundingBox()

    # Einzel-Durchlauf: Spatial Index und Feature-Map gleichzeitig aufbauen
    feat_map = {}
    idx = QgsSpatialIndex()
    for f in wfs_layer.getFeatures(QgsFeatureRequest().setFilterRect(bbox)):
        feat_map[f.id()] = QgsFeature(f)
        idx.addFeature(f)

    added_inside  = 0
    added_outside = 0

    for fid in idx.intersects(bbox):
        src_feat = feat_map.get(fid)
        if src_feat is None:
            continue
        wfs_geom = src_feat.geometry()
        if not wfs_geom.isGeosValid():
            fixed = wfs_geom.makeValid()
            if fixed and not fixed.isEmpty():
                wfs_geom = fixed
        attrs    = src_feat.attributes() + [source_url]

        if wfs_geom.intersects(mask_in_wfs):
            # Teil innerhalb der Maske
            clipped = wfs_geom.intersection(mask_in_wfs)
            if not clipped.isEmpty():
                area_ha = round(clipped.area() / 10000.0, 4)
                if xform_out:
                    clipped.transform(xform_out)
                feat = QgsFeature(out_fields)
                feat.setAttributes(attrs + [area_ha])
                feat.setGeometry(clipped)
                inside_prov.addFeature(feat)
                added_inside += 1

            # Teil außerhalb der Maske (Differenz)
            diff = wfs_geom.difference(mask_in_wfs)
            if not diff.isEmpty():
                area_ha = round(diff.area() / 10000.0, 4)
                if xform_out:
                    diff.transform(xform_out)
                feat = QgsFeature(out_fields)
                feat.setAttributes(attrs + [area_ha])
                feat.setGeometry(diff)
                outside_prov.addFeature(feat)
                added_outside += 1
        else:
            # Feature vollständig außerhalb – komplette Geometrie übernehmen
            geom = QgsGeometry(wfs_geom)
            area_ha = round(geom.area() / 10000.0, 4)
            if xform_out:
                geom.transform(xform_out)
            feat = QgsFeature(out_fields)
            feat.setAttributes(attrs + [area_ha])
            feat.setGeometry(geom)
            outside_prov.addFeature(feat)
            added_outside += 1

    inside_layer.updateExtents()
    outside_layer.updateExtents()

    if added_inside == 0:
        QgsMessageLog.logMessage(
            f"Keine Treffer innerhalb UR für {layer_name}",
            "Umwelt WFS Tool", MSG_INFO,
        )
    if added_outside == 0:
        QgsMessageLog.logMessage(
            f"Keine Treffer außerhalb UR für {layer_name}",
            "Umwelt WFS Tool", MSG_INFO,
        )

    return (
        inside_layer  if added_inside  > 0 else None,
        outside_layer if added_outside > 0 else None,
    )
