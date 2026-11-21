from contextlib import asynccontextmanager
from datetime import date, datetime

import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.ecobici.modelo import ClimaDia, FechaFueraDeRango, Franja, Predictor, cargar, predecir_dia, rebalanceo


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.predictor = cargar()
    yield


app = FastAPI(
    title="Ecobici: demanda por estación",
    description="Predice retiros y arribos por estación y hora para un día, y arma las listas de "
                "estaciones a abastecer o descargar antes de una franja pico.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(FechaFueraDeRango)
async def fecha_fuera_de_rango(request: Request, error: FechaFueraDeRango):
    return JSONResponse(status_code=404, content={"detail": str(error)})


def predictor(request: Request) -> Predictor:
    return request.app.state.predictor


def registros(df: pd.DataFrame) -> list[dict]:
    return df.astype(object).where(df.notna(), None).to_dict("records")


class Clima(BaseModel):
    """Clima del día a predecir (por ejemplo, el pronóstico). Se aplica igual a las 24 horas."""
    temperatura_c: float = Field(ge=-15, le=50, description="Temperatura en °C", examples=[12.0])
    precipitacion_mm: float = Field(ge=0, le=60, description="Lluvia por hora en mm (0 = no llueve)", examples=[0.0])
    viento_kmh: float = Field(ge=0, le=150, description="Velocidad del viento en km/h", examples=[15.0])

    def dominio(self) -> ClimaDia:
        return ClimaDia(**self.model_dump())


class Salud(BaseModel):
    estado: str = Field(description="'ok' si los modelos y el dataset están cargados")
    modelo_entrenado: str = Field(description="Fecha y hora del entrenamiento (metadata.json)")
    fecha_minima: date = Field(description="Primer día que se puede predecir (hacen falta los 29 días previos)")
    fecha_maxima: date = Field(description="Último día del dataset")


class Modelo(BaseModel):
    descripcion: str
    horizonte: str
    entrenado: str
    periodo_entrenamiento: list[str]
    variables: list[str] = Field(description="Variables de los modelos de boosting")
    clima: dict = Field(description="Clima usado al entrenar y el esperado en producción")
    metricas_test: dict = Field(description="Métricas del boosting en test (jun–ago 2026) por target")
    grupos: dict[str, str] = Field(description="Grupos de estaciones (K-means sobre perfiles horarios)")
    versiones: dict[str, str]


class Estacion(BaseModel):
    station_id: int
    nombre: str
    lat: float
    lon: float
    grupo: int | None = Field(description="Id de grupo; null si la estación no tenía datos para agruparla")
    nombre_grupo: str | None


class PedidoPrediccion(BaseModel):
    fecha: date = Field(description="Día a predecir (AAAA-MM-DD)", examples=["2026-06-02"])
    clima: Clima | None = Field(None, description="Si se manda, reemplaza el clima del dataset en las 24 horas")
    station_ids: list[int] | None = Field(None, min_length=1, description="Estaciones a devolver; todas si se omite")


class FilaPrediccion(BaseModel):
    station_id: int
    ts: datetime = Field(description="Inicio de la hora, en hora local de Buenos Aires")
    hora: int
    pred_retiros: float = Field(description="Retiros esperados (boosting)")
    pred_arribos: float = Field(description="Arribos esperados (boosting)")
    pred_glm: float = Field(description="Retiros esperados según la regresión de Poisson (referencia)")
    retiros: int | None = Field(description="Retiros reales registrados, si los hay")
    arribos: int | None = Field(description="Arribos reales registrados, si los hay")


class Totales(BaseModel):
    pred_retiros: float
    pred_arribos: float
    pred_glm: float
    retiros: int | None
    arribos: int | None


class RespuestaPrediccion(BaseModel):
    fecha: date
    clima: Clima | None = Field(description="Clima aplicado; null si se usó el del dataset")
    totales: Totales = Field(description="Suma del día sobre las estaciones devueltas")
    filas: list[FilaPrediccion] = Field(description="Una fila por estación y hora")


class PedidoRebalanceo(BaseModel):
    fecha: date = Field(description="Día a planificar (AAAA-MM-DD)", examples=["2026-06-02"])
    franja: Franja = Field(description="manana: 6 a 9 h · tarde: 16 a 19 h")
    top: int = Field(15, ge=1, le=50, description="Cantidad de estaciones por lista")
    clima: Clima | None = Field(None, description="Si se manda, reemplaza el clima del dataset en las 24 horas")


class EstacionRebalanceo(BaseModel):
    station_id: int
    nombre: str
    nombre_grupo: str | None
    lat: float
    lon: float
    flujo_predicho: float = Field(description="Arribos − retiros predichos en la franja")
    flujo_real: int | None = Field(description="Arribos − retiros reales en la franja, si hay datos")


class RespuestaRebalanceo(BaseModel):
    fecha: date
    franja: Franja
    horas: list[int] = Field(description="Horas de inicio que suma la franja")
    abastecer: list[EstacionRebalanceo] = Field(description="Se vacían: flujo predicho más negativo primero")
    descargar: list[EstacionRebalanceo] = Field(description="Se llenan: flujo predicho más positivo primero")
    precision_abastecer: float | None = Field(description="Parte de la lista que está entre las `top` reales")
    precision_descargar: float | None = Field(description="Parte de la lista que está entre las `top` reales")


@app.get("/salud", response_model=Salud, summary="Estado del servicio y rango de fechas")
def salud(request: Request):
    p = predictor(request)
    return Salud(estado="ok", modelo_entrenado=p.meta["entrenado"],
                 fecha_minima=p.primera_fecha, fecha_maxima=p.ultima_fecha)


@app.get("/modelo", response_model=Modelo, summary="Descripción, variables y métricas del modelo")
def modelo(request: Request):
    return predictor(request).meta


@app.get("/estaciones", response_model=list[Estacion], summary="Estaciones con ubicación y grupo")
def estaciones(request: Request):
    return registros(predictor(request).estaciones.reset_index())


@app.post("/prediccion", response_model=RespuestaPrediccion, summary="Demanda por estación y hora de un día")
def prediccion(pedido: PedidoPrediccion, request: Request):
    p = predictor(request)
    if pedido.station_ids:
        desconocidas = sorted(set(pedido.station_ids) - set(p.estaciones.index))
        if desconocidas:
            raise HTTPException(404, f"Estaciones desconocidas: {desconocidas}")
    pred = predecir_dia(p, pedido.fecha, pedido.clima.dominio() if pedido.clima else None)
    if pedido.station_ids:
        pred = pred[pred.station_id.isin(pedido.station_ids)]
    columnas = ["pred_retiros", "pred_arribos", "pred_glm", "retiros", "arribos"]
    totales = pred[columnas].sum(min_count=1)
    return RespuestaPrediccion(fecha=pedido.fecha, clima=pedido.clima,
                               totales=Totales(**registros(totales.to_frame().T)[0]),
                               filas=registros(pred))


@app.post("/rebalanceo", response_model=RespuestaRebalanceo,
          summary="Estaciones a abastecer y a descargar antes de una franja pico")
def rebalancear(pedido: PedidoRebalanceo, request: Request):
    p = predictor(request)
    pred = predecir_dia(p, pedido.fecha, pedido.clima.dominio() if pedido.clima else None)
    r = rebalanceo(p, pred, pedido.franja, pedido.top)
    return RespuestaRebalanceo(fecha=pedido.fecha, franja=pedido.franja, horas=list(pedido.franja.horas),
                               abastecer=registros(r.abastecer.reset_index()),
                               descargar=registros(r.descargar.reset_index()),
                               precision_abastecer=r.precision_abastecer,
                               precision_descargar=r.precision_descargar)
