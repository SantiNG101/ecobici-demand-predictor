"""Levanta el sistema para la demo, verifica que responda y muestra el recorrido con los números que se van a ver.

Uso, desde la raíz del repositorio:
    python scripts/demo.py                  # con Docker Compose
    python scripts/demo.py --local          # sin Docker, con las dependencias de requirements.txt
    python scripts/demo.py --solo-verificar # contra un sistema que ya está levantado
"""
import argparse
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import requests

REPO_DIR = Path(__file__).resolve().parents[1]
API = os.environ.get("ECOBICI_API_URL", "http://localhost:8000").rstrip("/")
TABLERO = os.environ.get("ECOBICI_TABLERO_URL", "http://localhost:8501").rstrip("/")
ARCHIVOS = ["models/metadata.json", "models/hgb_retiros.joblib", "data/processed/dataset_estacion_hora.parquet",
            "data/processed/estaciones.csv"]
FECHA, ESTACION, HORA = "2026-07-14", 175, 8  # el mismo día y estación con los que abre el tablero


def api(metodo: str, ruta: str, esperado: int = 200, **kwargs):
    inicio = time.perf_counter()
    respuesta = requests.request(metodo, f"{API}{ruta}", timeout=60, **kwargs)
    if respuesta.status_code != esperado:
        raise SystemExit(f"  ERROR: {metodo} {ruta} respondió {respuesta.status_code} en lugar de {esperado}: {respuesta.text[:300]}")
    return respuesta.json(), time.perf_counter() - inicio


def levantar(local: bool) -> list[subprocess.Popen]:
    faltan = [a for a in ARCHIVOS if not (REPO_DIR / a).exists()]
    if faltan:
        raise SystemExit("Faltan archivos (ver el README): " + ", ".join(faltan))
    if not local:
        # la carpeta se crea acá para que no la cree Docker como root
        (REPO_DIR / "logs").mkdir(exist_ok=True)
        subprocess.run(["docker", "compose", "up", "--build", "-d"], cwd=REPO_DIR, check=True)
        return []
    return [subprocess.Popen([sys.executable, "-m", "uvicorn", "api.main:app", "--log-level", "warning"], cwd=REPO_DIR),
            subprocess.Popen([sys.executable, "-m", "streamlit", "run", "interfaz/app.py", "--server.headless=true", "--server.address=localhost",
                              "--browser.gatherUsageStats=false"], cwd=REPO_DIR)]


def esperar(url: str, nombre: str, segundos: int = 90) -> None:
    limite = time.time() + segundos
    while time.time() < limite:
        try:
            if requests.get(url, timeout=5).ok:
                print(f"  {nombre}: listo")
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    raise SystemExit(f"  ERROR: {nombre} no respondió en {segundos} s ({url}). Con Docker, mirar `docker compose logs`.")


def recorrido() -> None:
    salud, _ = api("GET", "/salud")
    print(f"\nDatos del {salud['fecha_minima']} al {salud['fecha_maxima']}, modelo entrenado hasta el {salud['fin_entrenamiento']}.")

    plan, t = api("POST", "/rebalanceo", json={"fecha": FECHA, "franja": "manana", "top": 15})
    print(f"\n1. PLANIFICAR EL REBALANCEO — pestaña 1, {FECHA}, mañana ({t:.1f} s)")
    for lado in ("abastecer", "descargar"):
        nombres = ", ".join(f"{e['nombre'].strip()} ({e['flujo_predicho']:+.0f}, real {e['flujo_real']:+d})" for e in plan[lado][:3])
        promedio = plan[f"promedio_{lado}"]
        contexto = f" (promedio de los días hábiles del test: {promedio:.0%})" if promedio is not None else ""
        print(f"   {lado.capitalize()}: {nombres}\n     acierto {plan[f'precision_{lado}']:.0%}{contexto}")

    caso, _ = api("GET", f"/estaciones/{ESTACION}/caso", params={"fecha": FECHA, "hora": HORA})
    lluvia = caso | {"clima": caso["clima"] | {"precipitacion_mm": 5.0}}
    feriado = caso | {"es_feriado": True}
    respuesta, t = api("POST", "/prediccion/casos", json={"casos": [caso, lluvia, feriado]})
    original, con_lluvia, en_feriado = (r["pred_retiros"] for r in respuesta["resultados"])
    print(f"\n2. PROBAR CON DATOS NUEVOS — pestaña 2, 147 - Constitución, {HORA} h ({t:.1f} s)")
    print(f"   Retiros esperados: {original:.1f} · con 5 mm de lluvia: {con_lluvia:.1f} · si fuera feriado: {en_feriado:.1f}")

    entrenamiento, _ = api("POST", "/rebalanceo", json={"fecha": "2026-03-03", "franja": "manana"})
    assert entrenamiento["en_entrenamiento"], "el 03/03/2026 tendría que ser de entrenamiento"
    error, _ = api("POST", "/rebalanceo", esperado=404, json={"fecha": "2030-01-01", "franja": "manana"})
    api("POST", "/rebalanceo", esperado=422, json={"fecha": FECHA, "franja": "noche"})
    print(f"\n3. CASOS LÍMITE — {API}/docs\n   Día de entrenamiento (03/03/2026): avisa y no muestra el acierto\n"
          f"   Fecha fuera de rango: 404, {error['detail']}\n   Franja inexistente: 422")

    modelo, _ = api("GET", "/monitoreo/modelo")
    print("\n4. MONITOREO — pestaña 3")
    for mes in modelo["meses"]:
        print(f"   {mes['mes']}{'' if mes['referencia'] else ' (posterior al test)'}: error {mes['wape']:.1f} %, "
              f"semana anterior {mes['wape_semana_anterior']:.1f} %, avisos: {len(mes['avisos'])}")
    servicio, _ = api("GET", "/monitoreo/servicio")
    print(f"   Servicio: {servicio['total']['pedidos']} pedidos en 7 días, {servicio['total']['errores_servidor']} errores, "
          f"95 % en menos de {servicio['total']['ms_p95']:.0f} ms")


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)  # para que el avance se vea aunque la salida vaya a un archivo
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    modo = parser.add_mutually_exclusive_group()
    modo.add_argument("--local", action="store_true", help="sin Docker")
    modo.add_argument("--solo-verificar", action="store_true", help="no levanta nada")
    parser.add_argument("--sin-navegador", action="store_true", help="no abre el tablero")
    args = parser.parse_args()

    procesos = [] if args.solo_verificar else levantar(args.local)
    try:
        print("Esperando a que arranque:")
        esperar(f"{API}/salud", "API")
        esperar(f"{TABLERO}/_stcore/health", "Tablero")
        recorrido()
        print(f"\nTodo listo. Tablero: {TABLERO} · API: {API}/docs")
        if not args.sin_navegador:
            webbrowser.open(TABLERO)
        if procesos:
            print("Ctrl+C para terminar.")
            procesos[0].wait()
        elif not args.solo_verificar:
            print("Para terminar: docker compose down")
    except KeyboardInterrupt:
        pass
    finally:
        for proceso in procesos:
            proceso.terminate()


if __name__ == "__main__":
    main()
