"""Calcula el error del modelo mes a mes desde que se entrenó y el cambio en los datos, y guarda models/monitoreo.json.

Se corre después de scripts/actualizar_datos.py. Termina con código 1 si hay avisos, para usarlo en una tarea programada.

Uso, desde la raíz del repositorio:
    python scripts/monitorear_modelo.py
"""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.ecobici.modelo import ARCHIVO_MONITOREO, RUTA_MODELOS, cargar
from src.ecobici.monitoreo import monitorear

FIN_REFERENCIA = date(2026, 8, 31)  # el test de la entrega: con él se aprobó el modelo

resultado = monitorear(cargar(), FIN_REFERENCIA)
ruta = RUTA_MODELOS / ARCHIVO_MONITOREO
ruta.write_text(json.dumps(resultado, ensure_ascii=False, indent=1), encoding="utf-8")

print(f"Modelo entrenado hasta el {resultado['fin_entrenamiento']}, datos hasta el {resultado['ultima_fecha']}")
for m in resultado["meses"]:
    cambiadas = [f"{c['variable']} ({c['psi']:.2f})" for c in m["cambio"] if c["nivel"] == "alto"]
    print(f"  {m['mes']}{' (referencia)' if m['referencia'] else ''}: WAPE {m['wape']:.1f} % · semana anterior "
          f"{m['wape_semana_anterior']:.1f} % · sesgo {m['sesgo']:+.1f} % · estaciones nuevas {m['estaciones_nuevas']} · "
          f"cambio alto: {', '.join(cambiadas) or 'ninguno'}")
    for aviso in m["avisos"]:
        print(f"    AVISO: {aviso}")
sys.exit(1 if any(m["avisos"] for m in resultado["meses"]) else 0)
