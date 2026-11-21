"""Predicción de demanda por estación y hora para un día, y listas de rebalanceo.

Sin dependencias de la capa de servicio: la API y los tests usan estas funciones.
"""
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path

import duckdb
import joblib
import pandas as pd

REPO_DIR = Path(__file__).resolve().parents[2]
RUTA_MODELOS = REPO_DIR / os.environ.get("ECOBICI_MODELS_DIR", "models")
RUTA_DATOS = REPO_DIR / os.environ.get("ECOBICI_DATA_DIR", "data/processed")

ARCHIVO_DATASET = "dataset_estacion_hora.parquet"
DIAS_PREVIOS = 29  # la ventana más larga de la consulta es de 28 días antes del día anterior
ZONA = "America/Argentina/Buenos_Aires"


class FechaFueraDeRango(ValueError):
    def __init__(self, fecha: date, primera: date, ultima: date):
        super().__init__(f"No se puede predecir el {fecha:%d/%m/%Y}: el dataset permite predecir "
                         f"del {primera:%d/%m/%Y} al {ultima:%d/%m/%Y}.")


class Franja(StrEnum):
    manana = "manana"
    tarde = "tarde"

    @property
    def horas(self) -> range:
        return HORAS_FRANJA[self]


HORAS_FRANJA = {Franja.manana: range(6, 10), Franja.tarde: range(16, 20)}


@dataclass(frozen=True)
class ClimaDia:
    temperatura_c: float
    precipitacion_mm: float
    viento_kmh: float


@dataclass(frozen=True)
class Predictor:
    meta: dict
    modelo_retiros: object
    modelo_arribos: object
    modelo_glm: object
    estaciones: pd.DataFrame  # índice station_id: nombre, lat, lon, grupo, nombre_grupo
    dataset: Path
    primera_fecha: date
    ultima_fecha: date


@dataclass(frozen=True)
class Rebalanceo:
    abastecer: pd.DataFrame  # índice station_id: flujo_predicho, flujo_real + datos de la estación
    descargar: pd.DataFrame
    precision_abastecer: float | None
    precision_descargar: float | None


def _conexion() -> duckdb.DuckDBPyConnection:
    conexion = duckdb.connect()
    conexion.execute("SET enable_progress_bar = false")
    conexion.execute(f"SET TimeZone = '{ZONA}'")
    return conexion


def cargar(dir_modelos: Path = RUTA_MODELOS, dir_datos: Path = RUTA_DATOS) -> Predictor:
    archivos = {
        "metadata": dir_modelos / "metadata.json",
        "retiros": dir_modelos / "hgb_retiros.joblib",
        "arribos": dir_modelos / "hgb_arribos.joblib",
        "glm": dir_modelos / "glm_retiros.joblib",
        "grupos": dir_modelos / "grupos_estaciones.csv",
        "dataset": dir_datos / ARCHIVO_DATASET,
        "estaciones": dir_datos / "estaciones.csv",
    }
    faltantes = [str(ruta) for ruta in archivos.values() if not ruta.exists()]
    if faltantes:
        raise FileNotFoundError(
            "Faltan archivos para cargar el modelo: " + ", ".join(faltantes)
            + ". Los modelos los genera el notebook 04 y el dataset el notebook 03 "
            "(ver ECOBICI_MODELS_DIR y ECOBICI_DATA_DIR).")

    grupos = pd.read_csv(archivos["grupos"], index_col="station_id")[["grupo", "nombre_grupo"]]
    estaciones = pd.read_csv(archivos["estaciones"], index_col="station_id").join(grupos).sort_index()
    estaciones["grupo"] = estaciones.grupo.astype("Int64")

    with _conexion() as conexion:
        primera, ultima = conexion.execute(
            f"SELECT min(CAST(timezone('{ZONA}', hora_local) AS DATE)), "
            f"max(CAST(timezone('{ZONA}', hora_local) AS DATE)) FROM read_parquet(?)",
            [str(archivos["dataset"])]).fetchone()

    return Predictor(
        meta=json.loads(archivos["metadata"].read_text(encoding="utf-8")),
        modelo_retiros=joblib.load(archivos["retiros"]),
        modelo_arribos=joblib.load(archivos["arribos"]),
        modelo_glm=joblib.load(archivos["glm"]),
        estaciones=estaciones,
        dataset=archivos["dataset"],
        primera_fecha=primera + timedelta(days=DIAS_PREVIOS),
        ultima_fecha=ultima,
    )


def _variables(p: Predictor, fecha: date) -> pd.DataFrame:
    # Las ventanas de la consulta sólo miran hacia atrás: sobre el tramo [fecha - 29 días, fecha + 1)
    # dan lo mismo que sobre todo el dataset, y para fechas viejas es 25 veces más rápido.
    desde = fecha - timedelta(days=DIAS_PREVIOS)
    hasta = fecha + timedelta(days=1)
    with _conexion() as conexion, tempfile.TemporaryDirectory() as carpeta:
        tramo = Path(carpeta) / ARCHIVO_DATASET
        conexion.execute(
            f"COPY (SELECT * FROM read_parquet('{p.dataset.as_posix()}') "
            f"WHERE hora_local >= TIMESTAMPTZ '{desde}' AND hora_local < TIMESTAMPTZ '{hasta}') "
            f"TO '{tramo.as_posix()}' (FORMAT parquet)")
        consulta = p.meta["consulta_variables"].format(fuente=tramo.as_posix(), desde=desde)
        return conexion.execute(f"SELECT * FROM ({consulta}) WHERE ts >= ? AND ts < ?",
                                [pd.Timestamp(fecha).to_pydatetime(), pd.Timestamp(hasta).to_pydatetime()]).df()


def predecir_dia(p: Predictor, fecha: date, clima: ClimaDia | None = None) -> pd.DataFrame:
    if not p.primera_fecha <= fecha <= p.ultima_fecha:
        raise FechaFueraDeRango(fecha, p.primera_fecha, p.ultima_fecha)
    datos = _variables(p, fecha)
    if clima is not None:
        datos = datos.assign(temperatura_c=clima.temperatura_c, precipitacion_mm=clima.precipitacion_mm,
                             viento_kmh=clima.viento_kmh, llueve=int(clima.precipitacion_mm > 0))
    datos["pred_retiros"] = p.modelo_retiros.predict(datos[p.meta["variables"]])
    datos["pred_arribos"] = p.modelo_arribos.predict(datos[p.meta["variables"]])
    datos["pred_glm"] = p.modelo_glm.predict(datos[p.meta["columnas_glm"]])
    return datos[["station_id", "ts", "hora", "pred_retiros", "pred_arribos", "pred_glm", "retiros", "arribos"]]


def rebalanceo(p: Predictor, pred: pd.DataFrame, franja: Franja, top: int) -> Rebalanceo:
    flujo = (pred[pred.hora.isin(franja.horas)]
             .assign(flujo_predicho=lambda d: d.pred_arribos - d.pred_retiros,
                     flujo_real=lambda d: d.arribos - d.retiros)
             .groupby("station_id")
             .agg(flujo_predicho=("flujo_predicho", "sum"),
                  flujo_real=("flujo_real", lambda s: s.sum(min_count=1)))
             .join(p.estaciones))
    abastecer = flujo.nsmallest(top, "flujo_predicho")
    descargar = flujo.nlargest(top, "flujo_predicho")
    con_real = flujo.flujo_real.notna().all()

    def precision(elegidas: pd.DataFrame, reales: pd.Series) -> float | None:
        return len(set(elegidas.index) & set(reales.index)) / top if con_real else None

    return Rebalanceo(abastecer=abastecer, descargar=descargar,
                      precision_abastecer=precision(abastecer, flujo.flujo_real.nsmallest(top)),
                      precision_descargar=precision(descargar, flujo.flujo_real.nlargest(top)))
