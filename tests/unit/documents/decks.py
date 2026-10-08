"""Decks de referencia de las pruebas: largos, en español y con todos los layouts."""

# Plan comercial de 15 diapositivas (gate A1): cada layout salvo imagen y
# viñetas, títulos largos, cifras negativas y fuentes.
EXECUTIVE = {
    "title": "Plan comercial 2027",
    "language": "es",
    "theme": "executive-light",
    "slides": [
        {
            "layout": "cover",
            "eyebrow": "Comité de dirección",
            "title": "Plan comercial 2027: crecer en Norte sin perder margen",
            "subtitle": "Diagnóstico de 2026, apuestas y presupuesto",
            "meta": "Dirección comercial · Noviembre 2026",
        },
        {
            "layout": "summary",
            "title": "Tres decisiones para 2027",
            "points": [
                {
                    "head": "Ampliar Norte",
                    "text": "Tres comerciales más; retorno estimado en dos trimestres.",
                },
                {
                    "head": "Blindar Sur",
                    "text": "Plan de retención para las tres cuentas que renegocian.",
                },
                {
                    "head": "Canal online",
                    "text": "Duplicar la inversión en captación con objetivo de CAC 120 €.",
                },
                {"head": "Presupuesto", "text": "1,9 M de inversión total, 62 % en personas."},
            ],
        },
        {
            "layout": "section",
            "number": "01",
            "title": "Dónde estamos",
            "subtitle": "Resultados de 2026",
        },
        {
            "layout": "kpi",
            "title": "2026 cerró por encima del plan",
            "kpis": [
                {"label": "Ventas", "value": "16,8 M", "delta": "+14 %", "note": "vs. 2025"},
                {
                    "label": "Margen bruto",
                    "value": "38,2 %",
                    "delta": "\u22120,6 pp",
                    "trend": "down",
                },
                {"label": "Clientes activos", "value": "1.412", "delta": "+11 %"},
                {
                    "label": "Rotación",
                    "value": "6,1 %",
                    "delta": "+0,8 pp",
                    "trend": "up",
                    "good": "down",
                },
            ],
            "takeaway": "Crecimos en volumen; el margen cede por logística y descuentos en Sur.",
        },
        {
            "layout": "chart",
            "title": "Norte aporta más de la mitad del crecimiento",
            "chart": {
                "type": "bar",
                "categories": ["Norte", "Centro", "Este", "Sur", "Oeste"],
                "series": [
                    {
                        "name": "Aportación al crecimiento (M)",
                        "values": [1.12, 0.48, 0.31, 0.12, -0.09],
                    }
                ],
                "number_format": "0.00",
            },
            "takeaway": "Concentrar capacidad donde la demanda está probada.",
            "source": "Fuente: CRM, ventas cerradas a 31 de octubre.",
        },
        {
            "layout": "chart",
            "title": "El canal online crece más rápido que el resto",
            "chart": {
                "type": "line",
                "categories": ["T1", "T2", "T3", "T4"],
                "series": [
                    {"name": "Directo", "values": [2.9, 3.1, 3.3, 3.5]},
                    {"name": "Distribuidores", "values": [0.9, 1.0, 1.1, 1.2]},
                    {"name": "Online", "values": [0.25, 0.32, 0.41, 0.52]},
                ],
            },
            "source": "Fuente: ERP, ventas trimestrales (M).",
        },
        {
            "layout": "table",
            "title": "Detalle por región",
            "columns": ["Región", "Ventas (M)", "Crec. %", "Margen %", "Clientes"],
            "rows": [
                ["Norte", 6.1, "22 %", "39,4 %", 512],
                ["Centro", 4.2, "13 %", "38,1 %", 371],
                ["Este", 3.0, "11 %", "38,9 %", 268],
                ["Sur", 2.6, "5 %", "36,2 %", 190],
                ["Oeste", 0.9, "\u22129 %", "35,7 %", 71],
            ],
            "highlight_row": 0,
            "source": "Fuente: ERP.",
        },
        {
            "layout": "section",
            "number": "02",
            "title": "Qué proponemos",
            "subtitle": "Tres apuestas",
        },
        {
            "layout": "comparison",
            "title": "Ampliar Norte frente a recuperar Sur",
            "columns": [
                {
                    "heading": "Ampliar Norte",
                    "points": [
                        "Tres comerciales en enero",
                        "Retorno en dos trimestres",
                        "Riesgo bajo: demanda probada",
                    ],
                },
                {
                    "heading": "Recuperar Sur",
                    "points": [
                        "Plan para tres cuentas clave",
                        "Descuentos acotados al 4 %",
                        "Riesgo medio: depende de la renegociación",
                    ],
                },
            ],
        },
        {
            "layout": "matrix",
            "title": "Priorización de iniciativas",
            "x_axis": "Esfuerzo",
            "y_axis": "Impacto",
            "quadrants": [
                {
                    "heading": "Ganancias rápidas",
                    "text": "Precios de logística, scoring de leads",
                    "highlight": True,
                },
                {"heading": "Apuestas", "text": "Ampliar Norte, canal online"},
                {"heading": "Mantener", "text": "Formación de producto"},
                {"heading": "Evitar", "text": "Nueva región Oeste"},
            ],
        },
        {
            "layout": "process",
            "title": "Cómo lo ejecutamos",
            "steps": [
                {"heading": "Selección", "text": "Perfiles y entrevistas en diciembre"},
                {"heading": "Formación", "text": "Producto y procesos, dos semanas"},
                {"heading": "Arranque", "text": "Cartera asignada en febrero"},
                {"heading": "Revisión", "text": "Indicadores a 90 días"},
            ],
        },
        {
            "layout": "timeline",
            "title": "Calendario 2027",
            "events": [
                {"date": "Ene", "heading": "Equipo Norte", "text": "Incorporaciones"},
                {"date": "Mar", "heading": "Renovación Sur", "text": "Tres contratos"},
                {"date": "Jun", "heading": "Online", "text": "Nueva campaña"},
                {"date": "Oct", "heading": "Revisión", "text": "Plan 2028"},
            ],
        },
        {
            "layout": "statement",
            "title": "Recomendación: aprobar 1,9 M para Norte, Sur y online",
            "support": (
                "La decisión es necesaria antes del 15 de diciembre para incorporar al equipo "
                "en enero."
            ),
        },
        {
            "layout": "quote",
            "quote": (
                "Si los tres comerciales llegan en enero, Norte puede cerrar el año un 25 % "
                "por encima de 2026."
            ),
            "author": "Directora regional Norte",
            "role": "Revisión de pipeline, octubre 2026",
        },
        {
            "layout": "closing",
            "title": "Gracias",
            "subtitle": "Preguntas y próximos pasos",
            "contact": "comercial@empresa.com",
        },
    ],
}

# Los dos layouts que faltan, con una imagen como recurso.
EXTRA = {
    "title": "Anexo",
    "slides": [
        {
            "layout": "bullets",
            "title": "Supuestos del plan",
            "points": ["Precios estables", "Sin cambios regulatorios", "Tipo de cambio a 1,08"],
        },
        {
            "layout": "image",
            "title": "Mapa de cobertura",
            "image": "mapa",
            "caption": "Cobertura comercial por región, 2026",
        },
    ],
}
