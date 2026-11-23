"""Precisión media de las listas de rebalanceo en los días hábiles del test, para cada franja y tamaño de lista.

Genera models/precision_rebalanceo.json, que la API usa para poner en contexto el acierto de un día.

Uso, desde la raíz del repositorio:
    python scripts/evaluar_rebalanceo.py
"""
import json
import sys
from datetime import date
from pathlib import Path

import duckdb
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.ecobici.modelo import ARCHIVO_PRECISION, RUTA_MODELOS, ZONA, Franja, cargar, flujo_neto, precision_en_dia, predecir_dia

FIN_TEST = date(2026, 8, 31)  # el test de la entrega, aunque el dataset se haya extendido
TOPS = range(1, 51)
LADOS = ("abastecer", "descargar")

p = cargar()
dias = [f for (f,) in duckdb.sql(f"""
    SELECT DISTINCT CAST(timezone('{ZONA}', hora_local) AS DATE) AS fecha FROM read_parquet('{p.dataset.as_posix()}')
    WHERE dia_habil = 1 AND fecha > DATE '{p.fin_entrenamiento}' AND fecha <= DATE '{FIN_TEST}' ORDER BY fecha
""").fetchall()]

por_dia = {}
for dia in dias:
    pred = predecir_dia(p, dia)
    for franja in Franja:
        flujo = flujo_neto(pred, franja)
        por_dia[dia, franja] = {top: precision_en_dia(flujo, top) for top in TOPS}

precision = {franja: {lado: {top: round(float(np.mean([por_dia[d, franja][top][lado] for d in dias])), 4) for top in TOPS}
                      for lado in LADOS} for franja in Franja}
ruta = RUTA_MODELOS / ARCHIVO_PRECISION
ruta.write_text(json.dumps({"desde": str(dias[0]), "hasta": str(dias[-1]), "dias_habiles": len(dias), "precision": precision},
                           indent=1), encoding="utf-8")

# Día más parecido al promedio, para mostrarlo por defecto en el tablero
distancia = {d: sum(abs(por_dia[d, f][15][lado] - precision[f][lado][15]) for f in Franja for lado in LADOS) for d in dias}
tipico = min(distancia, key=distancia.get)
print(f"{len(dias)} días hábiles. Precisión@15 media:", {f.value: {lado: precision[f][lado][15] for lado in LADOS} for f in Franja})
print(f"Día más parecido al promedio: {tipico}", {f.value: por_dia[tipico, f][15] for f in Franja})
