"""Actualiza los datos crudos y regenera data/processed/ para predecir días nuevos sin reentrenar.

1. Descarga lo que cambió: los viajes de BA Data del año en curso y del anterior, y las fotos
   mensuales de bike-sharing-history desde el último mes local. Compara Content-Length y
   Last-Modified con lo registrado en <datos>/descargas.json; una segunda corrida no baja nada.
   Los pronósticos de bike-sharing-history no se publican en el bucket y el dataset no los usa
   (el clima sale de Open-Meteo en el notebook 01).
2. Toma como fecha de fin el último día completo de viajes.
3. Respalda data/processed/ en data/processed.anterior/ y ejecuta los notebooks 01 y 03 con
   ECOBICI_FECHA_FIN. Los notebooks ejecutados quedan en data/interim/notebooks_ejecutados/.
4. Verifica el dataset nuevo contra el respaldo.

Uso, desde la raíz del repositorio:
    python scripts/actualizar_datos.py                 # descarga, regenera y verifica
    python scripts/actualizar_datos.py --solo-descargar
    python scripts/actualizar_datos.py --sin-descargar --fecha-fin 2026-09-15
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import requests

REPO_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("ECOBICI_DATA_DIR", REPO_DIR / "datasets")).resolve()
BA_DATA_DIR = next((DATA_DIR / d for d in ["ba_data", "ba-ecobici-datasets"] if (DATA_DIR / d).exists()),
                   DATA_DIR / "ba_data")
FOTOS_DIR = DATA_DIR / "archive" / "stations" / "buenos-aires" / "ecobici"
ESTADO = DATA_DIR / "descargas.json"

PROCESSED_DIR = REPO_DIR / "data" / "processed"
RESPALDO_DIR = REPO_DIR / "data" / "processed.anterior"
EJECUTADOS_DIR = REPO_DIR / "data" / "interim" / "notebooks_ejecutados"
DATASET = "dataset_estacion_hora.parquet"
NOTEBOOKS = ["01_adquisicion.ipynb", "03_transformaciones.ipynb"]

URL_VIAJES = ("https://cdn.buenosaires.gob.ar/datosabiertos/datasets/transporte-y-obras-publicas/"
              "bicicletas-publicas/recorridos-realizados-{anio}.zip")
URL_FOTOS = "https://storage.googleapis.com/bike-sharing-history/buenos-aires/ecobici/{anio}/{mes}.parquet"
MESES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

ZONA = "America/Argentina/Buenos_Aires"
FECHA_CONTROL = date(2026, 6, 2)  # día del test: sus viajes no deberían cambiar al actualizar


def leer_estado() -> dict:
    return json.loads(ESTADO.read_text(encoding="utf-8")) if ESTADO.exists() else {}


def guardar_estado(estado: dict) -> None:
    parcial = ESTADO.with_name(ESTADO.name + ".parcial")
    parcial.write_text(json.dumps(estado, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(parcial, ESTADO)


def descargar_si_cambio(url: str, destino: Path, estado: dict) -> None:
    """Baja url a destino si cambió desde la última descarga."""
    nombre = destino.relative_to(DATA_DIR).as_posix()
    cabecera = requests.head(url, timeout=60, allow_redirects=True)
    if cabecera.status_code == 404:
        print(f"  {nombre}: todavía no publicado")
        return
    cabecera.raise_for_status()
    remoto = {"url": url, "last_modified": cabecera.headers.get("Last-Modified"),
              "content_length": int(cabecera.headers["Content-Length"])}
    registrado = estado.get(nombre)
    # Sin registro (archivo bajado a mano), alcanza con que coincida el tamaño
    if destino.exists() and destino.stat().st_size == remoto["content_length"] and registrado in (None, remoto):
        if registrado is None:
            estado[nombre] = remoto
            guardar_estado(estado)
        print(f"  {nombre}: al día ({remoto['last_modified']})")
        return

    print(f"  {nombre}: descargando {remoto['content_length'] / 1e6:.1f} MB ({remoto['last_modified']}) ...")
    destino.parent.mkdir(parents=True, exist_ok=True)
    parcial = destino.with_name(destino.name + ".parcial")
    with requests.get(url, stream=True, timeout=60) as respuesta:
        respuesta.raise_for_status()
        with open(parcial, "wb") as archivo:
            for bloque in respuesta.iter_content(chunk_size=1 << 20):
                archivo.write(bloque)
    if parcial.stat().st_size != remoto["content_length"]:
        raise SystemExit(f"Descarga incompleta de {url}: {parcial.stat().st_size} de {remoto['content_length']} bytes")
    os.replace(parcial, destino)
    estado[nombre] = remoto
    guardar_estado(estado)


def extraer_viajes(zip_viajes: Path) -> None:
    """Deja el CSV del zip en recorridos-realizados-AAAA/, la carpeta que usa el notebook 01."""
    carpeta = zip_viajes.with_suffix("")
    with zipfile.ZipFile(zip_viajes) as z:
        miembro = next(m for m in z.infolist() if m.filename.endswith(".csv") and "__MACOSX" not in m.filename)
        csv = carpeta / Path(miembro.filename).name
        if [p.name for p in carpeta.glob("*")] == [csv.name] and csv.stat().st_size == miembro.file_size:
            return
        print(f"  extrayendo {miembro.filename} en {carpeta.relative_to(DATA_DIR)}/ ...")
        nueva = carpeta.with_name(carpeta.name + ".parcial")
        shutil.rmtree(nueva, ignore_errors=True)
        nueva.mkdir()
        with z.open(miembro) as origen, open(nueva / csv.name, "wb") as copia:
            shutil.copyfileobj(origen, copia, length=1 << 20)
    # La carpeta vieja puede tener enlaces simbólicos: rmtree borra el enlace, no el archivo enlazado
    vieja = carpeta.with_name(carpeta.name + ".anterior")
    shutil.rmtree(vieja, ignore_errors=True)
    if carpeta.exists():
        carpeta.rename(vieja)
    nueva.rename(carpeta)
    shutil.rmtree(vieja, ignore_errors=True)


def meses_desde(anio: int, mes: int, hasta: date):
    while (anio, mes) <= (hasta.year, hasta.month):
        yield anio, mes
        anio, mes = (anio + 1, 1) if mes == 12 else (anio, mes + 1)


def descargar(estado: dict) -> None:
    hoy = date.today()

    print("Viajes (BA Data):")
    for anio in [hoy.year - 1, hoy.year]:
        zip_viajes = BA_DATA_DIR / f"recorridos-realizados-{anio}.zip"
        descargar_si_cambio(URL_VIAJES.format(anio=anio), zip_viajes, estado)
        if zip_viajes.exists():
            extraer_viajes(zip_viajes)

    print("Fotos de las estaciones (bike-sharing-history):")
    locales = [(int(p.parent.name), MESES.index(p.stem) + 1) for p in FOTOS_DIR.glob("*/*.parquet")]
    # El último mes local se vuelve a pedir porque pudo haberse bajado a medio mes
    for anio, mes in meses_desde(*max(locales), hasta=hoy):
        destino = FOTOS_DIR / str(anio) / f"{MESES[mes - 1]}.parquet"
        descargar_si_cambio(URL_FOTOS.format(anio=anio, mes=MESES[mes - 1]), destino, estado)


def csv_viajes_mas_reciente() -> Path:
    carpeta = max((c for c in BA_DATA_DIR.glob("recorridos-realizados-*") if c.is_dir() and c.name[-4:].isdigit()),
                  key=lambda c: c.name)
    return next(c for c in sorted(carpeta.rglob("*.csv")) if "__MACOSX" not in c.parts)


def ultimo_dia_completo() -> tuple[date, datetime]:
    """Último día con viajes. Si el último viaje sale antes de las 23 h, el día se publicó a medias."""
    ruta = csv_viajes_mas_reciente()
    ultimo_viaje = duckdb.sql(f"SELECT max(TRY_CAST(fecha_origen_recorrido AS TIMESTAMP)) "
                              f"FROM read_csv('{ruta.as_posix()}', header = true, all_varchar = true)").fetchone()[0]
    dia = ultimo_viaje.date()
    return (dia if ultimo_viaje.hour == 23 else dia - timedelta(days=1)), ultimo_viaje


def resumen_dataset(ruta: Path) -> tuple[date, date, int] | None:
    if not ruta.exists():
        return None
    return duckdb.sql(f"SELECT min(CAST(timezone('{ZONA}', hora_local) AS DATE)), "
                      f"max(CAST(timezone('{ZONA}', hora_local) AS DATE)), count(*) "
                      f"FROM read_parquet('{ruta.as_posix()}')").fetchone()


def hay_que_regenerar(fin: date, anterior, estado: dict) -> bool:
    if anterior is None or anterior[1] != fin:
        return True
    # Como make: si algún crudo descargado es más nuevo que el dataset, se regenera
    generado = (PROCESSED_DIR / DATASET).stat().st_mtime
    return any((DATA_DIR / nombre).stat().st_mtime > generado for nombre in estado if (DATA_DIR / nombre).exists())


def ejecutar_notebooks(fin: date) -> None:
    EJECUTADOS_DIR.mkdir(parents=True, exist_ok=True)
    entorno = os.environ | {"ECOBICI_FECHA_FIN": fin.isoformat()}
    for notebook in NOTEBOOKS:
        inicio = time.time()
        print(f"  {notebook} ...", flush=True)
        subprocess.run([sys.executable, "-m", "nbconvert", "--to", "notebook", "--execute",
                        "--ExecutePreprocessor.timeout=-1", "--output-dir", str(EJECUTADOS_DIR),
                        str(REPO_DIR / "notebooks" / notebook)], check=True, env=entorno)
        print(f"  {notebook}: {time.time() - inicio:.0f} s")


def flujos_del_dia(ruta: Path, dia: date):
    return duckdb.sql(f"SELECT station_id, sum(retiros) AS retiros, sum(arribos) AS arribos "
                      f"FROM read_parquet('{ruta.as_posix()}') "
                      f"WHERE CAST(timezone('{ZONA}', hora_local) AS DATE) = DATE '{dia}' "
                      f"GROUP BY 1 ORDER BY 1").df()


def verificar(fin: date, anterior) -> None:
    nuevo = resumen_dataset(PROCESSED_DIR / DATASET)
    errores = []
    if nuevo[1] != fin:
        errores.append(f"el dataset termina el {nuevo[1]}, no el {fin}")
    if anterior is not None and fin > anterior[1] and nuevo[2] <= anterior[2]:
        errores.append(f"no crecieron las filas ({anterior[2]:,} → {nuevo[2]:,})")
    respaldo = RESPALDO_DIR / DATASET
    if respaldo.exists():
        antes, despues = flujos_del_dia(respaldo, FECHA_CONTROL), flujos_del_dia(PROCESSED_DIR / DATASET, FECHA_CONTROL)
        iguales = antes.equals(despues)
        print(f"  {FECHA_CONTROL}: retiros y arribos por estación {'iguales' if iguales else 'DISTINTOS'} "
              f"al respaldo ({len(despues)} estaciones, {int(despues.retiros.sum()):,} retiros)")
        if not iguales:
            errores.append(f"cambiaron los viajes del {FECHA_CONTROL}")
    print(f"  rango nuevo: {nuevo[0]} → {nuevo[1]}, {nuevo[2]:,} filas")
    if errores:
        raise SystemExit("Verificación fallida: " + "; ".join(errores))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    modo = parser.add_mutually_exclusive_group()
    modo.add_argument("--solo-descargar", action="store_true", help="descarga y termina, sin regenerar el dataset")
    modo.add_argument("--sin-descargar", action="store_true", help="regenera con los datos crudos que ya hay")
    parser.add_argument("--fecha-fin", type=date.fromisoformat, metavar="AAAA-MM-DD",
                        help="último día del dataset (por defecto, el último día completo de viajes); "
                             "con esta opción siempre se regenera")
    args = parser.parse_args()
    tiempos = {}

    inicio = time.time()
    estado = leer_estado()
    if not args.sin_descargar:
        descargar(estado)
        tiempos["descargas"] = time.time() - inicio

    fin_viajes, ultimo_viaje = ultimo_dia_completo()
    fin = args.fecha_fin or fin_viajes
    anterior = resumen_dataset(PROCESSED_DIR / DATASET)
    print(f"Último viaje publicado: {ultimo_viaje:%Y-%m-%d %H:%M} → último día completo: {fin_viajes}")
    print(f"Rango anterior: {f'{anterior[0]} → {anterior[1]}, {anterior[2]:,} filas' if anterior else 'sin dataset'}")
    print(f"Fecha de fin: {fin}")
    if args.solo_descargar:
        return
    if args.fecha_fin is None and not hay_que_regenerar(fin, anterior, estado):
        print("El dataset ya está al día: no se regenera.")
        return

    if PROCESSED_DIR.exists():
        shutil.rmtree(RESPALDO_DIR, ignore_errors=True)
        shutil.copytree(PROCESSED_DIR, RESPALDO_DIR)
        print(f"Respaldo: {RESPALDO_DIR.relative_to(REPO_DIR)}/")

    print(f"Ejecutando notebooks con ECOBICI_FECHA_FIN={fin}:")
    inicio = time.time()
    ejecutar_notebooks(fin)
    tiempos["notebooks"] = time.time() - inicio

    print("Verificación:")
    verificar(fin, anterior)
    print("Listo. " + " · ".join(f"{paso}: {segundos:.0f} s" for paso, segundos in tiempos.items())
          + ". Reiniciar la API para que tome el rango nuevo.")


if __name__ == "__main__":
    main()
