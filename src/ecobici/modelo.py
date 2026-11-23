"""Predicción de demanda por estación y hora para un día, y listas de rebalanceo.

Sin dependencias de la capa de servicio: la API y los tests usan estas funciones.
"""
import json
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from datetime import date, timedelta
from enum import StrEnum
from pathlib import Path

import duckdb
import joblib
import numpy as np
import pandas as pd

REPO_DIR = Path(__file__).resolve().parents[2]
RUTA_MODELOS = REPO_DIR / os.environ.get("ECOBICI_MODELS_DIR", "models")
RUTA_DATOS = REPO_DIR / os.environ.get("ECOBICI_DATA_DIR", "data/processed")

ARCHIVO_DATASET = "dataset_estacion_hora.parquet"
ARCHIVO_PRECISION = "precision_rebalanceo.json"  # lo genera scripts/evaluar_rebalanceo.py; es opcional
ARCHIVO_MONITOREO = "monitoreo.json"  # lo genera scripts/monitorear_modelo.py; es opcional
DIAS_PREVIOS = 29  # la ventana más larga de la consulta es de 28 días antes del día anterior
ZONA = "America/Argentina/Buenos_Aires"


class FechaFueraDeRango(ValueError):
    def __init__(self, fecha: date, primera: date, ultima: date):
        super().__init__(f"No se puede predecir el {fecha:%d/%m/%Y}: el dataset permite predecir "
                         f"del {primera:%d/%m/%Y} al {ultima:%d/%m/%Y}.")


class SinDatos(LookupError):
    pass


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
class DemandaReciente:
    """Viajes de la estación (retiros o arribos) que se conocen al cierre del día anterior."""
    ayer_misma_hora: float
    semana_anterior_misma_hora: float
    promedio_7d_misma_hora: float
    total_dia_anterior: float
    promedio_diario_28d: float


@dataclass(frozen=True)
class EstadoDiaAnterior:
    """Promedios de las fotos del día anterior. None es "sin dato": el boosting lo acepta."""
    capacidad_util: float | None = None
    bicis_rotas: float | None = None
    pct_capacidad_rota: float | None = None
    prop_vacia: float | None = None
    prop_llena: float | None = None


@dataclass(frozen=True)
class Caso:
    """Una estación en una hora, con datos que no hace falta que estén en el dataset."""
    fecha: date
    hora: int
    es_feriado: bool
    lat: float
    lon: float
    clima: ClimaDia
    retiros: DemandaReciente
    arribos: DemandaReciente
    estado: EstadoDiaAnterior


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
    fin_entrenamiento: date
    precision_promedio: dict | None  # franja → lado → top → precisión media en los días hábiles del test

    def en_entrenamiento(self, fecha: date) -> bool:
        return fecha <= self.fin_entrenamiento


@dataclass(frozen=True)
class Rebalanceo:
    abastecer: pd.DataFrame  # índice station_id: flujo_predicho, flujo_real + datos de la estación
    descargar: pd.DataFrame
    precision_abastecer: float | None
    precision_descargar: float | None
    promedio_abastecer: float | None
    promedio_descargar: float | None


def conexion() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET enable_progress_bar = false")
    con.execute(f"SET TimeZone = '{ZONA}'")
    return con


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

    with conexion() as con:
        primera, ultima = con.execute(
            f"SELECT min(CAST(timezone('{ZONA}', hora_local) AS DATE)), "
            f"max(CAST(timezone('{ZONA}', hora_local) AS DATE)) FROM read_parquet(?)",
            [str(archivos["dataset"])]).fetchone()

    meta = json.loads(archivos["metadata"].read_text(encoding="utf-8"))
    ruta_precision = dir_modelos / ARCHIVO_PRECISION
    precision_promedio = (json.loads(ruta_precision.read_text(encoding="utf-8"))["precision"]
                          if ruta_precision.exists() else None)

    return Predictor(
        meta=meta,
        modelo_retiros=joblib.load(archivos["retiros"]),
        modelo_arribos=joblib.load(archivos["arribos"]),
        modelo_glm=joblib.load(archivos["glm"]),
        estaciones=estaciones,
        dataset=archivos["dataset"],
        primera_fecha=primera + timedelta(days=DIAS_PREVIOS),
        ultima_fecha=ultima,
        fin_entrenamiento=pd.Timestamp(meta["periodo_entrenamiento"][1]).date(),
        precision_promedio=precision_promedio,
    )


def _variables(p: Predictor, fecha: date) -> pd.DataFrame:
    # Las ventanas de la consulta sólo miran hacia atrás: sobre el tramo [fecha - 29 días, fecha + 1)
    # dan lo mismo que sobre todo el dataset, y para fechas viejas es 25 veces más rápido.
    desde = fecha - timedelta(days=DIAS_PREVIOS)
    hasta = fecha + timedelta(days=1)
    with conexion() as con, tempfile.TemporaryDirectory() as carpeta:
        tramo = Path(carpeta) / ARCHIVO_DATASET
        con.execute(
            f"COPY (SELECT * FROM read_parquet('{p.dataset.as_posix()}') "
            f"WHERE hora_local >= TIMESTAMPTZ '{desde}' AND hora_local < TIMESTAMPTZ '{hasta}') "
            f"TO '{tramo.as_posix()}' (FORMAT parquet)")
        consulta = p.meta["consulta_variables"].format(fuente=tramo.as_posix(), desde=desde)
        return con.execute(f"SELECT * FROM ({consulta}) WHERE ts >= ? AND ts < ?",
                                [pd.Timestamp(fecha).to_pydatetime(), pd.Timestamp(hasta).to_pydatetime()]).df()


def _verificar_rango(p: Predictor, fecha: date) -> None:
    if not p.primera_fecha <= fecha <= p.ultima_fecha:
        raise FechaFueraDeRango(fecha, p.primera_fecha, p.ultima_fecha)


def predecir_dia(p: Predictor, fecha: date, clima: ClimaDia | None = None) -> pd.DataFrame:
    _verificar_rango(p, fecha)
    datos = _variables(p, fecha)
    if clima is not None:
        datos = datos.assign(temperatura_c=clima.temperatura_c, precipitacion_mm=clima.precipitacion_mm,
                             viento_kmh=clima.viento_kmh, llueve=int(clima.precipitacion_mm > 0))
    datos["pred_retiros"] = p.modelo_retiros.predict(datos[p.meta["variables"]])
    datos["pred_arribos"] = p.modelo_arribos.predict(datos[p.meta["variables"]])
    datos["pred_glm"] = p.modelo_glm.predict(datos[p.meta["columnas_glm"]])
    return datos[["station_id", "ts", "hora", "pred_retiros", "pred_arribos", "pred_glm", "retiros", "arribos"]]


def _fila(caso: Caso) -> dict:
    fin_de_semana = caso.fecha.weekday() >= 5

    def rezagos(prefijo: str, d: DemandaReciente) -> dict:
        return {f"{prefijo}_lag24": d.ayer_misma_hora, f"{prefijo}_lag168": d.semana_anterior_misma_hora,
                f"{prefijo}_misma_hora_7d": d.promedio_7d_misma_hora, f"{prefijo}_dia_anterior": d.total_dia_anterior,
                f"{prefijo}_media_28d": d.promedio_diario_28d / 24}  # el modelo usa el promedio por hora

    return {"hora": caso.hora, "dia_semana": caso.fecha.weekday(), "mes": caso.fecha.month,
            "es_fin_de_semana": int(fin_de_semana), "es_feriado": int(caso.es_feriado),
            "dia_habil": int(not fin_de_semana and not caso.es_feriado),
            **asdict(caso.clima), "llueve": int(caso.clima.precipitacion_mm > 0), "lat": caso.lat, "lon": caso.lon,
            **rezagos("ret", caso.retiros), **rezagos("arr", caso.arribos),
            **{f"{nombre}_dia_ant": np.nan if valor is None else valor for nombre, valor in asdict(caso.estado).items()}}


def predecir_casos(p: Predictor, casos: list[Caso]) -> pd.DataFrame:
    datos = pd.DataFrame([_fila(caso) for caso in casos])[p.meta["variables"]].astype(float)
    return pd.DataFrame({"pred_retiros": p.modelo_retiros.predict(datos), "pred_arribos": p.modelo_arribos.predict(datos)})


def caso_registrado(p: Predictor, station_id: int, fecha: date, hora: int) -> Caso:
    """El caso tal como quedó registrado en el dataset, para usarlo de punto de partida."""
    _verificar_rango(p, fecha)
    datos = _variables(p, fecha)
    datos = datos[(datos.station_id == station_id) & (datos.hora == hora)]
    if datos.empty:
        raise SinDatos(f"La estación {station_id} no tiene datos el {fecha:%d/%m/%Y} a las {hora} h.")
    fila = {k: None if pd.isna(v) else float(v) for k, v in datos[p.meta["variables"]].iloc[0].items()}

    def demanda(prefijo: str) -> DemandaReciente:
        valores = {"ayer_misma_hora": fila[f"{prefijo}_lag24"], "semana_anterior_misma_hora": fila[f"{prefijo}_lag168"],
                   "promedio_7d_misma_hora": fila[f"{prefijo}_misma_hora_7d"],
                   "total_dia_anterior": fila[f"{prefijo}_dia_anterior"], "promedio_diario_28d": fila[f"{prefijo}_media_28d"]}
        if None in valores.values():
            raise SinDatos(f"A la estación {station_id} le falta demanda previa para el {fecha:%d/%m/%Y} a las {hora} h.")
        return DemandaReciente(**valores | {"promedio_diario_28d": 24 * valores["promedio_diario_28d"]})

    return Caso(fecha=fecha, hora=hora, es_feriado=bool(fila["es_feriado"]), lat=fila["lat"], lon=fila["lon"],
                clima=ClimaDia(fila["temperatura_c"], fila["precipitacion_mm"], fila["viento_kmh"]),
                retiros=demanda("ret"), arribos=demanda("arr"),
                estado=EstadoDiaAnterior(**{c.name: fila[f"{c.name}_dia_ant"] for c in fields(EstadoDiaAnterior)}))


def flujo_neto(pred: pd.DataFrame, franja: Franja) -> pd.DataFrame:
    return (pred[pred.hora.isin(franja.horas)]
            .assign(flujo_predicho=lambda d: d.pred_arribos - d.pred_retiros,
                    flujo_real=lambda d: d.arribos - d.retiros)
            .groupby("station_id")
            .agg(flujo_predicho=("flujo_predicho", "sum"),
                 flujo_real=("flujo_real", lambda s: s.sum(min_count=1))))


def precision_en_dia(flujo: pd.DataFrame, top: int) -> dict[str, float]:
    """Parte de cada lista predicha que está entre las `top` estaciones con el flujo real más extremo."""
    return {lado: len(set(elegir(flujo.flujo_predicho).index) & set(elegir(flujo.flujo_real).index)) / top
            for lado, elegir in [("abastecer", lambda s: s.nsmallest(top)), ("descargar", lambda s: s.nlargest(top))]}


def rebalanceo(p: Predictor, pred: pd.DataFrame, franja: Franja, top: int) -> Rebalanceo:
    flujo = flujo_neto(pred, franja).join(p.estaciones)
    precision = precision_en_dia(flujo, top) if flujo.flujo_real.notna().all() else {"abastecer": None, "descargar": None}
    promedio = p.precision_promedio[franja] if p.precision_promedio else {}
    return Rebalanceo(abastecer=flujo.nsmallest(top, "flujo_predicho"), descargar=flujo.nlargest(top, "flujo_predicho"),
                      precision_abastecer=precision["abastecer"], precision_descargar=precision["descargar"],
                      promedio_abastecer=promedio.get("abastecer", {}).get(str(top)),
                      promedio_descargar=promedio.get("descargar", {}).get(str(top)))
