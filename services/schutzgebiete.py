import os
import tempfile
import xml.etree.ElementTree as ET
from typing import Optional, List, Dict
from qgis.core import QgsVectorLayer, QgsMessageLog, QgsWkbTypes
from ..network import http_get, _FEAT_TTL, _CAPS_TTL
from ..compat import MSG_INFO, MSG_WARNING

WFS_BASE_URL = "https://geodienste.bfn.de/ogc/wfs/schutzgebiet"
WFS_AWZ_URL  = "https://geodienste.bfn.de/ogc/wfs/schutzgebiet_awz"

# Verifizierte Layer aus GetCapabilities (Stand: 2025, geprüft live)
DEFAULT_LAYERS = [
    {"id": "ffh",    "title": "Natura 2000 FFH-Gebiete",                  "typename": "bfn_sch_Schutzgebiet:Fauna_Flora_Habitat_Gebiete",    "url": WFS_BASE_URL, "group": "Natura 2000"},
    {"id": "vsg",    "title": "Natura 2000 Vogelschutzgebiete (VSG)",     "typename": "bfn_sch_Schutzgebiet:Vogelschutzgebiete",             "url": WFS_BASE_URL, "group": "Natura 2000"},
    {"id": "nsg",    "title": "Naturschutzgebiete (NSG)",                 "typename": "bfn_sch_Schutzgebiet:Naturschutzgebiete",             "url": WFS_BASE_URL, "group": "Nationales Schutzgebietssystem"},
    {"id": "lsg",    "title": "Landschaftsschutzgebiete (LSG)",           "typename": "bfn_sch_Schutzgebiet:Landschaftsschutzgebiete",      "url": WFS_BASE_URL, "group": "Nationales Schutzgebietssystem"},
    {"id": "nlp",    "title": "Nationalparke",                            "typename": "bfn_sch_Schutzgebiet:Nationalparke",                 "url": WFS_BASE_URL, "group": "Nationales Schutzgebietssystem"},
    {"id": "br",     "title": "Biosphärenreservate",                      "typename": "bfn_sch_Schutzgebiet:Biosphaerenreservate",          "url": WFS_BASE_URL, "group": "Nationales Schutzgebietssystem"},
    {"id": "br_z",   "title": "Biosphärenreservate – Zonierung",         "typename": "bfn_sch_Schutzgebiet:Biosphaerenreservate_Zonierung", "url": WFS_BASE_URL, "group": "Nationales Schutzgebietssystem"},
    {"id": "np",     "title": "Naturparke",                               "typename": "bfn_sch_Schutzgebiet:Naturparke",                   "url": WFS_BASE_URL, "group": "Nationales Schutzgebietssystem"},
    {"id": "nnm",    "title": "Nationale Naturmonumente",                 "typename": "bfn_sch_Schutzgebiet:Nationale_Naturmonumente",      "url": WFS_BASE_URL, "group": "Nationales Schutzgebietssystem"},
    {"id": "awz_ffh","title": "AWZ – FFH-Gebiete",                       "typename": "bfn_sch_Schutzgebiet_awz:Fauna_Flora_Habitat_Gebiete","url": WFS_AWZ_URL, "group": "AWZ (Nordsee/Ostsee)"},
    {"id": "awz_vsg","title": "AWZ – Vogelschutzgebiete",                "typename": "bfn_sch_Schutzgebiet_awz:Vogelschutzgebiete",        "url": WFS_AWZ_URL,  "group": "AWZ (Nordsee/Ostsee)"},
    {"id": "awz_nsg","title": "AWZ – Naturschutzgebiete",                "typename": "bfn_sch_Schutzgebiet_awz:Naturschutzgebiete",        "url": WFS_AWZ_URL,  "group": "AWZ (Nordsee/Ostsee)"},
]

# Menschenlesbare Titel für bekannte Typnames
_TITLE_MAP = {
    "Fauna_Flora_Habitat_Gebiete":    "Natura 2000 FFH-Gebiete",
    "Vogelschutzgebiete":             "Natura 2000 Vogelschutzgebiete (VSG)",
    "Naturschutzgebiete":             "Naturschutzgebiete (NSG)",
    "Landschaftsschutzgebiete":       "Landschaftsschutzgebiete (LSG)",
    "Nationalparke":                  "Nationalparke",
    "Biosphaerenreservate":           "Biosphärenreservate",
    "Biosphaerenreservate_Zonierung": "Biosphärenreservate – Zonierung",
    "Naturparke":                     "Naturparke",
    "Nationale_Naturmonumente":       "Nationale Naturmonumente",
}

_GROUP_MAP = {
    "Fauna_Flora_Habitat_Gebiete": "Natura 2000",
    "Vogelschutzgebiete":          "Natura 2000",
}


_PREFERRED_SRS = ["EPSG:25832", "EPSG:25833", "EPSG:3857", "EPSG:4326"]


def _local_name(typename: str) -> str:
    """'bfn_sch_Schutzgebiet:Foo' → 'Foo'"""
    return typename.split(":")[-1]


def _normalize_srs(raw: str) -> str:
    """'urn:ogc:def:crs:EPSG::25832' or 'EPSG:25832' → 'EPSG:25832'"""
    if not raw:
        return ""
    raw = raw.strip()
    if "urn:ogc:def:crs:" in raw.lower() or "urn:x-ogc:def:crs:" in raw.lower():
        parts = [p for p in raw.split(":") if p]
        if len(parts) >= 2:
            return f"EPSG:{parts[-1]}"
    return raw


def _pick_srs(available: List[str]) -> str:
    """Choose preferred SRS from available list; default EPSG:25832."""
    for pref in _PREFERRED_SRS:
        if pref in available:
            return pref
    return available[0] if available else "EPSG:25832"


def _local_tag(elem) -> str:
    """Strip XML namespace from element tag: '{ns}Name' → 'Name'."""
    tag = elem.tag
    return tag.split("}", 1)[1] if "}" in tag else tag


def _child_text(parent, *local_names: str) -> str:
    """Return stripped text of first direct child whose local tag matches."""
    for elem in parent:
        if _local_tag(elem) in local_names:
            return (elem.text or "").strip()
    return ""


def fetch_layers(wfs_url: str, group_suffix: str = "") -> List[Dict]:
    """
    Ruft GetCapabilities ab und gibt Layer-Definitionen zurück.
    Namespace-agnostisch (WFS 1.x und 2.0, default namespace oder Präfix).
    Blocking – nur aus Hintergrund-Thread aufrufen.
    """
    sep      = "&" if "?" in wfs_url else "?"
    caps_url = f"{wfs_url}{sep}SERVICE=WFS&REQUEST=GetCapabilities"
    data = http_get(caps_url, use_cache=True, ttl=_CAPS_TTL, disk_cache=True)
    if not data:
        QgsMessageLog.logMessage(
            f"GetCapabilities nicht erreichbar: {caps_url}",
            "Umwelt WFS Tool",
            MSG_WARNING,
        )
        return []
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        QgsMessageLog.logMessage(
            f"GetCapabilities XML-Fehler: {exc}",
            "Umwelt WFS Tool",
            MSG_WARNING,
        )
        return []

    # Namespace-agnostische Suche: funktioniert für WFS 1.x und 2.0,
    # mit explizitem Präfix (wfs:FeatureType) und Default-Namespace (<FeatureType>)
    feature_types = [e for e in root.iter() if _local_tag(e) == "FeatureType"]

    layers = []
    for ft in feature_types:
        typename = _child_text(ft, "Name")
        if not typename:
            continue
        local = _local_name(typename)
        raw_title = _child_text(ft, "Title")
        title = _TITLE_MAP.get(local, raw_title if raw_title else local)
        if group_suffix:
            group = "AWZ (Nordsee/Ostsee)"
        else:
            group = _GROUP_MAP.get(local, "Nationales Schutzgebietssystem")

        # CRS aus DefaultCRS / OtherCRS (WFS 2.0) oder SRS (WFS 1.x)
        srs_candidates = [
            _normalize_srs(e.text)
            for e in ft.iter()
            if _local_tag(e) in ("DefaultCRS", "OtherCRS", "SRS") and e.text
        ]
        srs = _pick_srs(srs_candidates) if srs_candidates else "EPSG:25832"

        layers.append({
            "id":       typename,
            "title":    title,
            "typename": typename,
            "url":      wfs_url,
            "group":    group,
            "srs":      srs,
        })
    return layers


def build_wfs_uri(url: str, typename: str, restrict_to_bbox: bool = False,
                  srs: str = "EPSG:25832") -> str:
    params = {
        "pagingEnabled":               "true",
        "preferCoordinatesForWfsT11":  "false",
        "srsname":                     srs,
        "typename":                    typename,
        "url":                         url,
        "version":                     "auto",
    }
    if restrict_to_bbox:
        params["restrictToRequestBBOX"] = "1"
    return " ".join(f"{k}='{v}'" for k, v in params.items())


def fetch_wfs_to_memory(layer_def: Dict, bbox_geom=None) -> Optional[QgsVectorLayer]:
    """
    Lädt WFS-Features per HTTP-GET direkt in einen Memory-Layer.
    Umgeht QgsWFSProvider vollständig → keine QgsThreadedFeatureDownloader-Threads.
    Für IntersectionTask: verhindert SQLite-Kontention und QGIS-Abstürze.
    bbox_geom: QgsGeometry in EPSG:25832 für räumlichen Filter (optional).
    """
    url      = layer_def["url"]
    typename = layer_def["typename"]
    srs      = layer_def.get("srs", "EPSG:25832")
    sep      = "&" if "?" in url else "?"

    bbox_str = ""
    if bbox_geom:
        bb = bbox_geom.boundingBox()
        bbox_str = (f"&BBOX={bb.xMinimum():.2f},{bb.yMinimum():.2f},"
                    f"{bb.xMaximum():.2f},{bb.yMaximum():.2f},EPSG:25832")

    # Versuche verschiedene WFS-Versionen und Ausgabeformate
    attempts = [
        (f"{url}{sep}SERVICE=WFS&REQUEST=GetFeature&VERSION=2.0.0"
         f"&TYPENAMES={typename}&SRSNAME={srs}"
         f"&OUTPUTFORMAT=application%2Fjson&COUNT=100000{bbox_str}"),
        (f"{url}{sep}SERVICE=WFS&REQUEST=GetFeature&VERSION=1.1.0"
         f"&TYPENAME={typename}&SRSNAME={srs}"
         f"&OUTPUTFORMAT=application%2Fjson&maxFeatures=100000{bbox_str}"),
        (f"{url}{sep}SERVICE=WFS&REQUEST=GetFeature&VERSION=1.1.0"
         f"&TYPENAME={typename}&SRSNAME={srs}"
         f"&OUTPUTFORMAT=json&maxFeatures=100000{bbox_str}"),
        (f"{url}{sep}SERVICE=WFS&REQUEST=GetFeature&VERSION=1.0.0"
         f"&TYPENAME={typename}&maxFeatures=100000{bbox_str}"),
    ]

    for req_url in attempts:
        data = http_get(req_url, timeout_ms=90000, use_cache=True, ttl=_FEAT_TTL)
        if not data:
            continue
        if len(data) > 40 * 1024 * 1024:  # >40 MB → zu groß für Memory-Layer
            QgsMessageLog.logMessage(
                f"Antwort zu groß ({len(data)//1024//1024} MB), überspringe Memory-Layer: {layer_def['title']}",
                "Umwelt WFS Tool", MSG_WARNING,
            )
            return None
        if b"ExceptionReport" in data[:800]:
            continue
        is_json = b"FeatureCollection" in data
        is_gml  = b"FeatureMember" in data or b"featureMember" in data
        if not (is_json or is_gml):
            continue

        suffix = ".geojson" if is_json else ".gml"
        fd, fname = tempfile.mkstemp(suffix=suffix)
        try:
            os.write(fd, data)
            os.close(fd)

            tmp = QgsVectorLayer(fname, "tmp", "ogr")
            if not tmp.isValid():
                continue

            geom_str = QgsWkbTypes.displayString(tmp.wkbType())
            if not geom_str or geom_str in ("Unknown", "NoGeometry"):
                geom_str = "MultiPolygon"

            mem = QgsVectorLayer(f"{geom_str}?crs={srs}", layer_def["title"], "memory")
            prov = mem.dataProvider()
            prov.addAttributes(tmp.fields())
            mem.updateFields()
            feats = list(tmp.getFeatures())
            if feats:
                prov.addFeatures(feats)
                mem.updateExtents()
                return mem
        except Exception as exc:
            QgsMessageLog.logMessage(
                f"fetch_wfs_to_memory Fehler [{layer_def['title']}]: {exc}",
                "Umwelt WFS Tool", MSG_WARNING,
            )
        finally:
            try:
                os.unlink(fname)
            except OSError:
                pass

    return None


def create_layer(layer_def: Dict, restrict_to_bbox: bool = False) -> Optional[QgsVectorLayer]:
    """Erstellt WFS-Layer. addMapLayer() muss im Hauptthread aufgerufen werden."""
    srs = layer_def.get("srs", "EPSG:25832")
    uri = build_wfs_uri(layer_def["url"], layer_def["typename"], restrict_to_bbox, srs)
    layer = QgsVectorLayer(uri, layer_def["title"], "WFS")
    if not layer.isValid():
        QgsMessageLog.logMessage(
            f"WFS-Layer ungültig: {layer_def['title']} (typename: {layer_def['typename']})",
            "Umwelt WFS Tool",
            MSG_WARNING,
        )
        return None
    layer.setAbstract(
        f"Quelle: Bundesamt für Naturschutz (BFN)\n"
        f"Dienst: {layer_def['url']}\n"
        f"FeatureType: {layer_def['typename']}"
    )
    layer.setAttributionUrl("https://geodienste.bfn.de")
    return layer
