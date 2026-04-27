"""
Kartographische Darstellung von Umwelt-WFS-Layern nach den Konventionen
der deutschen Umweltplanung (BfN-Kartieranleitungen, EBA-Richtlinien
für Umweltverträglichkeitsstudien bei Eisenbahnprojekten).

Farbschema:
  FFH-Gebiete             → Dunkelgrün,    45°-Schraffur
  Vogelschutzgebiete      → Blauviolett,   135°-Schraffur
  NSG                     → Dunkelgrün,    45°-Schraffur (dichter)
  LSG                     → Hellgrün,      keine Schraffur
  Nationalparke           → Sattes Grün,   keine Schraffur
  Biosphärenreservate     → Olivgrün,      45°-Schraffur
  Naturparke              → Blassgrün,     keine Schraffur
  Nat. Naturmonumente     → Dunkelviolett, 45°-Schraffur
  AWZ-Gebiete             → Blaugrün,      Schraffur
  Schutzgebiet (generisch)→ Mittelgrün,    45°-Schraffur
  Biotope / LaPro         → Hellgrün,      keine Schraffur
  Wasserschutzgebiete     → Hellblau,      keine Schraffur
  Überschwemmungsgebiete  → Blau,          45°-Schraffur
  Grundwasser             → Hellblau,      keine Schraffur
  Gewässer                → Kräftig Blau,  keine Schraffur
  Hydrogeologie           → Stahlblau,     135°-Schraffur
  Nitrat/Düngung          → Gelb,          keine Schraffur
  Altlasten               → Dunkelrot,     45°-Schraffur
  Bodendaten              → Braun,         keine Schraffur
  Bodennutzung            → Hellbraun,     45°-Schraffur
  Bodenversiegelung       → Grau,          45°-Schraffur
  Luft & Klima            → Blaugrau,      keine Schraffur
  Landwirtschaft          → Gelbgrün,      keine Schraffur
  Geologie                → Beige,         keine Schraffur
  Verwaltungsgrenzen      → Blau/Grau,     keine Schraffur
"""

from qgis.PyQt.QtGui import QColor
from qgis.core import (
    QgsFillSymbol,
    QgsSimpleFillSymbolLayer,
    QgsLinePatternFillSymbolLayer,
    QgsCentroidFillSymbolLayer,
    QgsPointPatternFillSymbolLayer,
    QgsSingleSymbolRenderer,
    QgsVectorLayer,
    QgsStyle,
    QgsMapUnitScale,
    QgsUnitTypes,
)

# (fill_rgba, border_rgb, hatch_rgb, hatch_angle_deg, hatch_distance_mm, hatch_width_mm)
# fill_rgba: (R, G, B, Alpha 0-255)
_STYLE_DEFS = {
    # ── Natura 2000 ───────────────────────────────────────────────────
    "Fauna_Flora_Habitat_Gebiete": {
        "fill":         (0,   100,  0,   60),
        "border":       (0,   100,  0),
        "border_width": 0.6,
        "hatch":        (0,   100,  0),
        "hatch_angle":  45,
        "hatch_dist":   2.5,
        "hatch_width":  0.35,
    },
    "Vogelschutzgebiete": {
        "fill":         (30,   80, 160,  60),
        "border":       (30,   80, 160),
        "border_width": 0.6,
        "hatch":        (30,   80, 160),
        "hatch_angle":  135,
        "hatch_dist":   2.5,
        "hatch_width":  0.35,
    },
    # ── Nationales Schutzgebietssystem ────────────────────────────────
    "Naturschutzgebiete": {
        "fill":         (27,   94,  32,  80),
        "border":       (27,   94,  32),
        "border_width": 0.7,
        "hatch":        (27,   94,  32),
        "hatch_angle":  45,
        "hatch_dist":   1.8,
        "hatch_width":  0.4,
    },
    "Landschaftsschutzgebiete": {
        "fill":         (144, 238, 144,  70),
        "border":       (56,  142,  60),
        "border_width": 0.5,
        "hatch":        None,
    },
    "Nationalparke": {
        "fill":         (0,   105,  62, 110),
        "border":       (0,   105,  62),
        "border_width": 0.8,
        "hatch":        None,
    },
    "Biosphaerenreservate": {
        "fill":         (130, 119,  23,  60),
        "border":       (130, 119,  23),
        "border_width": 0.6,
        "hatch":        (130, 119,  23),
        "hatch_angle":  45,
        "hatch_dist":   3.0,
        "hatch_width":  0.3,
    },
    "Biosphaerenreservate_Zonierung": {
        "fill":         (130, 119,  23,  30),
        "border":       (130, 119,  23),
        "border_width": 0.4,
        "hatch":        None,
    },
    "Naturparke": {
        "fill":         (174, 213, 129,  55),
        "border":       (85,  139,  47),
        "border_width": 0.5,
        "hatch":        None,
    },
    "Nationale_Naturmonumente": {
        "fill":         (74,   20, 140,  70),
        "border":       (74,   20, 140),
        "border_width": 0.7,
        "hatch":        (74,   20, 140),
        "hatch_angle":  45,
        "hatch_dist":   2.0,
        "hatch_width":  0.4,
    },
    # ── Schutzgebiet generisch (für unbekannte Typen) ─────────────────
    "schutzgebiet_generic": {
        "fill":         (46,  125,  50,  55),
        "border":       (27,   94,  32),
        "border_width": 0.6,
        "hatch":        (27,   94,  32),
        "hatch_angle":  45,
        "hatch_dist":   3.0,
        "hatch_width":  0.3,
    },
    # ── Biotope & Landschaftsplanung ──────────────────────────────────
    "biotop": {
        "fill":         (76,  175,  80,  55),
        "border":       (27,   94,  32),
        "border_width": 0.5,
        "hatch":        None,
    },
    "lapro": {
        "fill":         (129, 199, 132,  50),
        "border":       (56,  142,  60),
        "border_width": 0.5,
        "hatch":        None,
    },
    # ── Wasserschutzgebiete ───────────────────────────────────────────
    "wasserschutzgebiete": {
        "fill":         (33,  150, 243,  50),
        "border":       (13,   71, 161),
        "border_width": 0.6,
        "hatch":        None,
    },
    "Wasserschutzgebiete": {
        "fill":         (33,  150, 243,  50),
        "border":       (13,   71, 161),
        "border_width": 0.6,
        "hatch":        None,
    },
    "Wasserschutzgebiete_geplant": {
        "fill":         (100, 181, 246,  40),
        "border":       (13,   71, 161),
        "border_width": 0.5,
        "hatch":        (13,   71, 161),
        "hatch_angle":  135,
        "hatch_dist":   4.0,
        "hatch_width":  0.25,
    },
    # ── Überschwemmungsgebiete ────────────────────────────────────────
    "ueberschwemmungsgebiete": {
        "fill":         (3,   169, 244,  60),
        "border":       (1,    87, 155),
        "border_width": 0.6,
        "hatch":        (1,    87, 155),
        "hatch_angle":  45,
        "hatch_dist":   3.0,
        "hatch_width":  0.3,
    },
    # ── Grundwasser / Gewässer / Hydrogeologie ────────────────────────
    "grundwasser": {
        "fill":         (79,  195, 247,  45),
        "border":       (2,   119, 189),
        "border_width": 0.5,
        "hatch":        None,
    },
    "gewaesser": {
        "fill":         (3,   155, 229,  70),
        "border":       (1,    87, 155),
        "border_width": 0.7,
        "hatch":        None,
    },
    "hydrogeologie": {
        "fill":         (100, 181, 246,  40),
        "border":       (21,  101, 192),
        "border_width": 0.5,
        "hatch":        (21,  101, 192),
        "hatch_angle":  135,
        "hatch_dist":   4.5,
        "hatch_width":  0.2,
    },
    "nitrat": {
        "fill":         (255, 241, 118,  60),
        "border":       (249, 168,  37),
        "border_width": 0.5,
        "hatch":        None,
    },
    # ── Boden ─────────────────────────────────────────────────────────
    "altlasten": {
        "fill":         (183,  28,  28,  70),
        "border":       (183,  28,  28),
        "border_width": 0.7,
        "hatch":        (183,  28,  28),
        "hatch_angle":  45,
        "hatch_dist":   2.0,
        "hatch_width":  0.4,
    },
    "bodendaten": {
        "fill":         (188, 143, 100,  55),
        "border":       (141, 110,  99),
        "border_width": 0.5,
        "hatch":        None,
    },
    "bodennutzung": {
        "fill":         (220, 193, 140,  50),
        "border":       (188, 143, 100),
        "border_width": 0.5,
        "hatch":        (188, 143, 100),
        "hatch_angle":  45,
        "hatch_dist":   3.5,
        "hatch_width":  0.25,
    },
    "bodenversiegelung": {
        "fill":         (97,   97,  97,  80),
        "border":       (66,   66,  66),
        "border_width": 0.6,
        "hatch":        (66,   66,  66),
        "hatch_angle":  45,
        "hatch_dist":   2.0,
        "hatch_width":  0.3,
    },
    # ── Luft & Klima ──────────────────────────────────────────────────
    "luft_klima": {
        "fill":         (144, 164, 174,  45),
        "border":       (84,  110, 122),
        "border_width": 0.5,
        "hatch":        None,
    },
    # ── Landwirtschaft ────────────────────────────────────────────────
    "landwirtschaft": {
        "fill":         (220, 230, 170,  60),
        "border":       (156, 204, 101),
        "border_width": 0.5,
        "hatch":        None,
    },
    # ── Geologie / Bohrpunkte ─────────────────────────────────────────
    "geologie": {
        "fill":         (215, 204, 200,  50),
        "border":       (161, 136, 127),
        "border_width": 0.5,
        "hatch":        None,
    },
    # ── Verwaltungsgrenzen (BKG vg1000 / vg250) ──────────────────────
    "vg1000_lan": {
        "fill":         (255, 255, 255,  30),
        "border":       (30,   60, 140),
        "border_width": 1.8,
        "hatch":        None,
    },
    "vg250_lan": {
        "fill":         (255, 255, 255,  30),
        "border":       (30,   60, 140),
        "border_width": 1.8,
        "hatch":        None,
    },
    "vg1000_krs": {
        "fill":         (0,    0,   0,    0),
        "border":       (90,  90,  90),
        "border_width": 0.8,
        "hatch":        None,
    },
    "vg250_krs": {
        "fill":         (0,    0,   0,    0),
        "border":       (90,  90,  90),
        "border_width": 0.8,
        "hatch":        None,
    },
    "vg1000_gem": {
        "fill":         (0,    0,   0,    0),
        "border":       (160, 160, 160),
        "border_width": 0.4,
        "hatch":        None,
    },
    "vg250_gem": {
        "fill":         (0,    0,   0,    0),
        "border":       (160, 160, 160),
        "border_width": 0.4,
        "hatch":        None,
    },
    "vg1000_rbz": {
        "fill":         (255, 255, 255,  15),
        "border":       (60,   90, 160),
        "border_width": 1.2,
        "hatch":        None,
    },
    "vg250_rbz": {
        "fill":         (255, 255, 255,  15),
        "border":       (60,   90, 160),
        "border_width": 1.2,
        "hatch":        None,
    },
    # Generische Verwaltungsgrenze für unbekannte Typen
    "verwaltung_generic": {
        "fill":         (224, 224, 224,  20),
        "border":       (97,   97,  97),
        "border_width": 0.8,
        "hatch":        None,
    },
    # ── AWZ ───────────────────────────────────────────────────────────
    "awz_ffh": {
        "fill":         (0,   100, 130,  60),
        "border":       (0,   100, 130),
        "border_width": 0.6,
        "hatch":        (0,   100, 130),
        "hatch_angle":  45,
        "hatch_dist":   2.5,
        "hatch_width":  0.35,
    },
    "awz_vsg": {
        "fill":         (0,    80, 180,  60),
        "border":       (0,    80, 180),
        "border_width": 0.6,
        "hatch":        (0,    80, 180),
        "hatch_angle":  135,
        "hatch_dist":   2.5,
        "hatch_width":  0.35,
    },
    "awz_nsg": {
        "fill":         (0,   120,  80,  70),
        "border":       (0,   120,  80),
        "border_width": 0.6,
        "hatch":        (0,   120,  80),
        "hatch_angle":  45,
        "hatch_dist":   2.0,
        "hatch_width":  0.35,
    },
}

# AWZ-Typnames auf Style-Keys mappen
_AWZ_KEY_MAP = {
    "Fauna_Flora_Habitat_Gebiete": "awz_ffh",
    "Vogelschutzgebiete":          "awz_vsg",
    "Naturschutzgebiete":          "awz_nsg",
}

# Keyword-basiertes Matching auf Titel + Typename (spezifisch → allgemein sortiert)
_KEYWORD_STYLE_MAP = [
    # Wasser – spezifisch zuerst
    (["wasserschutzgebiet"],                                  "Wasserschutzgebiete"),
    (["überschwemmung", "überschwemmungsgebiet",
      "hochwasserrisikogebiet", "hqgebiet", "hq100", "hq200"], "ueberschwemmungsgebiete"),
    (["grundwasser", "gwmst", "gwmess", "grundwassermess"],   "grundwasser"),
    (["hydrogeol"],                                            "hydrogeologie"),
    (["nitrat", "nitratkulisse"],                              "nitrat"),
    (["gewässer", "gewaesser", "oberflächenwasser",
      "fließgewässer", "stillgewässer"],                       "gewaesser"),
    (["wasser"],                                               "grundwasser"),
    # Schutzgebiete – spezifisch zuerst
    (["fauna_flora", "ffh"],                                   "Fauna_Flora_Habitat_Gebiete"),
    (["vogelschutz", "vsg"],                                   "Vogelschutzgebiete"),
    (["naturschutzgebiet", "nsg"],                             "Naturschutzgebiete"),
    (["landschaftsschutz", "lsg"],                             "Landschaftsschutzgebiete"),
    (["nationalpark"],                                         "Nationalparke"),
    (["biosphä", "biosphaerenreservat"],                       "Biosphaerenreservate"),
    (["naturpark"],                                            "Naturparke"),
    (["naturmonument", "nationales naturmonument"],            "Nationale_Naturmonumente"),
    (["natura 2000", "natura2000"],                            "Fauna_Flora_Habitat_Gebiete"),
    # Biotope & Landschaftsplanung
    (["biotop"],                                               "biotop"),
    (["lapro", "landschaftsplan", "landschaftsrahmen",
      "lrp_karte"],                                            "lapro"),
    # Schutzgebiet generisch
    (["schutzgebiet", "schutzgeb", "protected site",
      "protected area", "inspire"],                            "schutzgebiet_generic"),
    # Boden
    (["altlast", "kontaminat", "schadstoffe", "bodenschutz"], "altlasten"),
    (["versiegelung", "bodenversiegelung"],                    "bodenversiegelung"),
    (["bodennutzung", "flächennutzung", "nutzungsart"],        "bodennutzung"),
    (["boden", "bk50", "bk25", "bk200", "bodenkund",
      "bodentyp", "bodenprofil"],                              "bodendaten"),
    # Luft & Klima
    (["luft", "luftquali", "emission", "immission", "klima",
      "lärm", "laerm", "schalltechnik"],                       "luft_klima"),
    # Landwirtschaft
    (["landwirtschaft", "agrar", "flurneuordnung",
      "flurberein", "ackerfläche"],                            "landwirtschaft"),
    # Verwaltung
    (["vg1000_lan", "landesgrenz"],                            "vg1000_lan"),
    (["vg1000_krs", "kreisgrenz"],                             "vg1000_krs"),
    (["vg1000_gem"],                                           "vg1000_gem"),
    (["vg250_lan"],                                            "vg250_lan"),
    (["vg250_krs"],                                            "vg250_krs"),
    (["vg250_gem"],                                            "vg250_gem"),
    (["vg1000_rbz", "vg250_rbz", "regierungsbezirk"],          "vg1000_rbz"),
    (["verwaltungsgrenz", "verwaltungseinheit",
      "gemeindegrenz", "landkreisgrenz", "bundeslandgrenz",
      "staatsgrenz", "grenz"],                                  "verwaltung_generic"),
    # Geologie
    (["geolog", "hydrogeologi", "bohrpunkt", "bohrung",
      "lithologi"],                                             "geologie"),
]


# Mapping: interner Style-Key → bevorzugte Namen im QGIS Style Manager (Fill-Symbole)
_STYLE_MANAGER_CANDIDATES: dict = {
    # Natura 2000 / Schutzgebiete
    "Fauna_Flora_Habitat_Gebiete": ["Fauna-Flora-Habitat (EBA)", "FFH-Gebiet (FGSV)"],
    "Vogelschutzgebiete":          ["VSG-Gebiet (FGSV)"],
    "Naturschutzgebiete":          ["Naturschutzgebiet (EBA)", "Naturschutzgebiet (FGSV)"],
    "Landschaftsschutzgebiete":    ["Landschaftsschutzgebiet (FGSV)"],
    "Nationalparke":               ["Nationalpark (FGSV)"],
    "Biosphaerenreservate":        ["Biosphärenreservat (FGSV)"],
    "schutzgebiet_generic":        ["Naturschutzgebiet (EBA)", "Naturschutzgebiet (FGSV)"],
    # AWZ-Pendants
    "awz_ffh":                     ["Fauna-Flora-Habitat (EBA)", "FFH-Gebiet (FGSV)"],
    "awz_vsg":                     ["VSG-Gebiet (FGSV)"],
    "awz_nsg":                     ["Naturschutzgebiet (EBA)", "Naturschutzgebiet (FGSV)"],
    # Wasser / WRRL
    "wasserschutzgebiete":         ["Wasserschutzgebiet Zone II(EBA)", "Wasserschutzgebiet Zone II (EBA)"],
    "Wasserschutzgebiete":         ["Wasserschutzgebiet Zone II(EBA)", "Wasserschutzgebiet Zone II (EBA)"],
    "Wasserschutzgebiete_geplant": ["Wasserschutzgebiet Zone III(EBA)", "Wasserschutzgebiet Zone III (EBA)"],
    "gewaesser":                   ["(F) Binnengewässer"],
    "grundwasser":                 ["(F) Binnengewässer"],
    # Boden
    "bodendaten":                  ["Bodenfunktionen"],
}


_MARKER_TYPES = (QgsCentroidFillSymbolLayer, QgsPointPatternFillSymbolLayer)


def _strip_markers(sym) -> None:
    """Entfernt Centroid-Fill und Point-Pattern-Layers aus einem Symbol (in-place)."""
    for i in range(sym.symbolLayerCount() - 1, -1, -1):
        if isinstance(sym.symbolLayer(i), _MARKER_TYPES):
            sym.deleteSymbolLayer(i)


def _apply_from_style_manager(layer: QgsVectorLayer, layer_def: dict,
                               is_inside: bool) -> bool:
    """Versucht ein Fill-Symbol aus dem QGIS Style Manager anzuwenden.
    Gibt True zurück wenn erfolgreich, sonst False (→ programmatischer Fallback).
    """
    try:
        mgr = QgsStyle.defaultStyle()
        key = _style_key(layer_def)
        for sym_name in _STYLE_MANAGER_CANDIDATES.get(key, []):
            sym = mgr.symbol(sym_name)
            if sym is None:
                continue
            if sym.type() != 2:   # 2 = QgsSymbol.Fill
                continue
            sym = sym.clone()
            _strip_markers(sym)
            if not is_inside:
                sym.setOpacity(0.25)
            layer.setRenderer(QgsSingleSymbolRenderer(sym))
            layer.triggerRepaint()
            return True
    except Exception:
        pass
    return False


def _style_key(layer_def: dict) -> str:
    """Bestimmt den Stil-Schlüssel: erst exakter Typename-Match, dann Keyword-Suche."""
    local = layer_def["typename"].split(":")[-1]

    # 1. Exakter Typename-Match
    if local in _STYLE_DEFS:
        return local

    # 2. AWZ-Prefix
    if "awz" in layer_def.get("url", "").lower():
        awz_key = _AWZ_KEY_MAP.get(local)
        if awz_key:
            return awz_key

    # 3. Keyword-Suche auf Typename (lokal) + Titel
    search = (local + " " + layer_def.get("title", "")).lower()
    for keywords, key in _KEYWORD_STYLE_MAP:
        if any(kw in search for kw in keywords):
            return key

    return local   # löst Fallback aus


def _make_mm_scale(min_mm: float, max_mm: float) -> QgsMapUnitScale:
    s = QgsMapUnitScale()
    s.minSizeMMEnabled = True
    s.minSizeMM = min_mm
    s.maxSizeMMEnabled = True
    s.maxSizeMM = max_mm
    return s


_MAP_UNIT = QgsUnitTypes.RenderMetersInMapUnits
_STROKE_METERS = 150   # Basisbreite in Karteneinheiten → auto-dünn bei Übersichtsmaßstab
_HATCH_METERS  = 100   # Schraffurlinie analog


def _build_symbol(style: dict) -> QgsFillSymbol:
    symbol = QgsFillSymbol()
    symbol.deleteSymbolLayer(0)

    fill_layer = QgsSimpleFillSymbolLayer()
    r, g, b, a = style["fill"]
    fill_layer.setColor(QColor(r, g, b, a))
    br, bg, bb = style["border"]
    fill_layer.setStrokeColor(QColor(br, bg, bb, 220))
    # Maßstabsabhängige Rahmenbreite: Haarlinie bei 1:500 000, normal bei 1:50 000
    fill_layer.setStrokeWidth(_STROKE_METERS)
    fill_layer.setStrokeWidthUnit(_MAP_UNIT)
    fill_layer.setStrokeWidthMapUnitScale(
        _make_mm_scale(0.1, style["border_width"])
    )
    symbol.appendSymbolLayer(fill_layer)

    if style.get("hatch"):
        hr, hg, hb = style["hatch"]
        hatch = QgsLinePatternFillSymbolLayer()
        hatch.setColor(QColor(hr, hg, hb, 200))
        hatch.setLineAngle(style["hatch_angle"])
        hatch.setDistance(style["hatch_dist"])
        # Schraffurlinie ebenfalls maßstabsabhängig dünner
        hatch.setLineWidth(_HATCH_METERS)
        hatch.setLineWidthUnit(_MAP_UNIT)
        hatch.setLineWidthMapUnitScale(
            _make_mm_scale(0.05, style["hatch_width"])
        )
        symbol.appendSymbolLayer(hatch)

    return symbol


_FALLBACK_STYLE = {
    "fill":         (200, 220, 230,  40),
    "border":       (120, 144, 156),
    "border_width": 0.4,
    "hatch":        None,
}


def apply_style(layer: QgsVectorLayer, layer_def: dict, is_inside: bool = True) -> None:
    """Wendet Styling an: zuerst Style Manager, dann programmatischer Fallback."""
    layer.setLabelsEnabled(False)
    if _apply_from_style_manager(layer, layer_def, is_inside):
        return

    key   = _style_key(layer_def)
    style = dict(_STYLE_DEFS.get(key) or _FALLBACK_STYLE)

    if not is_inside:
        r, g, b, _ = style["fill"]
        style["fill"]         = (r, g, b, 25)
        style["border_width"] = max(style["border_width"] - 0.1, 0.3)
        style["hatch"]        = None

    symbol   = _build_symbol(style)
    renderer = QgsSingleSymbolRenderer(symbol)
    layer.setRenderer(renderer)
    layer.triggerRepaint()
