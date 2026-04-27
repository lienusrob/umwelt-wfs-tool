import json
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Optional, Tuple
from qgis.core import QgsVectorLayer, QgsRasterLayer, QgsMessageLog
from ..network import http_get
from ..compat import MSG_INFO, MSG_WARNING, MSG_CRITICAL

FLORAWEB_API = "https://www.floraweb.de"
BFN_WFS_BASE = "https://geodienste.bfn.de/ogc/wfs"
BFN_WMS_BASE = "https://geodienste.bfn.de/ogc/wms"

INSPIRE_SPECIES_SERVICES = [
    {
        "id":    "vogel",
        "title": "Vögel – Verbreitungskarten",
        "wms":   f"{BFN_WMS_BASE}/INSPIRE_SD_DE_Vogel_D_E_F_range",
        "wfs":   f"{BFN_WFS_BASE}/INSPIRE_SD_DE_Vogel_D_E_F_range",
        "group": "Artenverbreitung",
    },
    {
        "id":    "farn_blueten",
        "title": "Farn- und Blütenpflanzen",
        "wms":   f"{BFN_WMS_BASE}/INSPIRE_SD_DE_Farn_und_Bluetenpflanzen_range",
        "wfs":   f"{BFN_WFS_BASE}/INSPIRE_SD_DE_Farn_und_Bluetenpflanzen_range",
        "group": "Artenverbreitung",
    },
    {
        "id":    "amphibien",
        "title": "Amphibien",
        "wms":   f"{BFN_WMS_BASE}/INSPIRE_SD_DE_Amphibien_range",
        "wfs":   f"{BFN_WFS_BASE}/INSPIRE_SD_DE_Amphibien_range",
        "group": "Artenverbreitung",
    },
    {
        "id":    "wolf",
        "title": "Wolf – Verbreitung",
        "wms":   f"{BFN_WMS_BASE}/INSPIRE_SD_DE_Wolf_distribution",
        "wfs":   f"{BFN_WFS_BASE}/INSPIRE_SD_DE_Wolf_distribution",
        "group": "Artenverbreitung",
    },
]

# Für Rückwärtskompatibilität mit dock_widget
INSPIRE_SPECIES_WMS = INSPIRE_SPECIES_SERVICES


def _probe_wfs(wfs_url: str) -> Tuple[bool, str]:
    caps_url = f"{wfs_url}?SERVICE=WFS&REQUEST=GetCapabilities&VERSION=2.0.0"
    data = http_get(caps_url, timeout_ms=8000)
    if not data:
        return False, ""
    try:
        root = ET.fromstring(data)
        ns = {"wfs": "http://www.opengis.net/wfs/2.0"}
        for ft in root.findall(".//wfs:FeatureType", ns):
            name_el = ft.find("wfs:Name", ns)
            if name_el is not None and name_el.text:
                return True, name_el.text
    except ET.ParseError:
        pass
    return False, ""


def fetch_wms_layer_name(service_url: str) -> str:
    """Blocking – nur aus Hintergrund-Thread aufrufen."""
    caps_url = f"{service_url}?SERVICE=WMS&REQUEST=GetCapabilities&VERSION=1.3.0"
    data = http_get(caps_url)
    if not data:
        return ""
    try:
        root = ET.fromstring(data)
        ns = {"wms": "http://www.opengis.net/wms"}
        for path, nmap in [
            (".//wms:Layer/wms:Layer/wms:Name", ns),
            (".//Layer/Layer/Name", {}),
        ]:
            el = root.find(path, nmap)
            if el is not None and el.text:
                return el.text
    except ET.ParseError as exc:
        QgsMessageLog.logMessage(f"WMS XML-Fehler: {exc}", "Umwelt WFS Tool", MSG_WARNING)
    return ""


def fetch_best_layer_info(service_def: dict) -> Tuple[str, str, str]:
    """
    Versucht zuerst WFS (Vektor, besser editierbar), dann WMS (Raster) als Fallback.
    Gibt (typ, url_oder_uri, layer_name) zurück. typ ist 'wfs' oder 'wms'.
    """
    wfs_ok, typename = _probe_wfs(service_def["wfs"])
    if wfs_ok:
        return "wfs", service_def["wfs"], typename

    layer_name = fetch_wms_layer_name(service_def["wms"])
    return "wms", service_def["wms"], layer_name


def build_wfs_uri(url: str, typename: str) -> str:
    params = {
        "pagingEnabled": "true",
        "srsname": "EPSG:25832",
        "typename": typename,
        "url": url,
        "version": "auto",
    }
    return " ".join(f"{k}='{v}'" for k, v in params.items())


def build_wms_uri(service_url: str, layer: str, crs: str = "EPSG:25832") -> str:
    return "&".join([
        f"url={service_url}",
        "format=image/png",
        f"layers={layer}",
        "styles=",
        f"crs={crs}",
        "version=1.3.0",
    ])


def create_best_layer(service_def: dict, layer_type: str, source_url: str, layer_name: str):
    """
    Erstellt WFS-VectorLayer oder WMS-RasterLayer je nach verfügbarem Dienst.
    Setzt Quell-Metadaten. Muss im Hauptthread aufgerufen werden.
    """
    attribution = (
        f"Quelle: Bundesamt für Naturschutz (BFN)\n"
        f"Dienst ({layer_type.upper()}): {source_url}\n"
        f"Layer: {layer_name}"
    )

    if layer_type == "wfs":
        uri = build_wfs_uri(source_url, layer_name)
        layer = QgsVectorLayer(uri, service_def["title"], "WFS")
        if layer.isValid():
            layer.setAbstract(attribution)
            layer.setAttributionUrl("https://geodienste.bfn.de")
            return layer
        QgsMessageLog.logMessage(
            f"WFS-Layer ungültig: {service_def['title']} – versuche WMS-Fallback",
            "Umwelt WFS Tool",
            MSG_WARNING,
        )
        # WFS fehlgeschlagen → WMS-Fallback
        fallback_name = fetch_wms_layer_name(service_def["wms"])
        uri = build_wms_uri(service_def["wms"], fallback_name)
        layer = QgsRasterLayer(uri, f"{service_def['title']} (WMS)", "wms")
        if layer.isValid():
            layer.setAbstract(attribution + "\n(WMS-Fallback, da WFS nicht verfügbar)")
            return layer
    else:
        uri = build_wms_uri(source_url, layer_name)
        layer = QgsRasterLayer(uri, f"{service_def['title']} (WMS)", "wms")
        if layer.isValid():
            layer.setAbstract(attribution + "\n(WMS – kein WFS verfügbar)")
            layer.setAttributionUrl("https://geodienste.bfn.de")
            return layer

    QgsMessageLog.logMessage(
        f"Layer konnte nicht geladen werden: {service_def['title']}",
        "Umwelt WFS Tool",
        MSG_CRITICAL,
    )
    return None


# Wird von WmsCapabilitiesThread in dock_widget genutzt
def create_wms_layer(service_def: dict, layer_name: str):
    return create_best_layer(service_def, "wms", service_def["wms"], layer_name)


# --- FloraWeb API ---

def floraweb_search(search_term: str) -> list:
    """Sucht Pflanzenarten über FloraWeb (Hintergrund-Thread)."""
    encoded = urllib.parse.urlencode({"name": search_term})
    for url in [
        f"{FLORAWEB_API}/php/taxonbyname_json.php?{encoded}",
        f"{FLORAWEB_API}/xsql/taxon_suggest.xsql?{urllib.parse.urlencode({'q': search_term, 'format': 'json'})}",
    ]:
        raw = http_get(url, timeout_ms=10000)
        if not raw:
            continue
        try:
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, list) and data:
                return data
            if isinstance(data, dict) and data:
                return [data]
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    QgsMessageLog.logMessage(
        f"FloraWeb: Keine Ergebnisse für '{search_term}'",
        "Umwelt WFS Tool",
        MSG_INFO,
    )
    return []


def floraweb_taxon_by_id(taxon_id: int) -> dict:
    url = f"{FLORAWEB_API}/php/taxonbyid_json.php?taxonId={taxon_id}"
    raw = http_get(url, timeout_ms=10000)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        QgsMessageLog.logMessage(f"FloraWeb Taxon-Fehler: {exc}", "Umwelt WFS Tool", MSG_WARNING)
    return {}
