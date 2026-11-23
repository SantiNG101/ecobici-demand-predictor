import json
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import date, datetime
from typing import Annotated

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from api import registro
from src.ecobici.modelo import (ARCHIVO_MONITOREO, RUTA_MODELOS, Caso, ClimaDia, DemandaReciente, EstadoDiaAnterior,
                                FechaFueraDeRango, Franja, Predictor, SinDatos, cargar, caso_registrado, predecir_casos, predecir_dia, rebalanceo)


@asynccontextmanager
async def lifespan(app: FastAPI):
    registro.preparar()
    app.state.predictor = cargar()
    yield


app = FastAPI(
    title="Ecobici: demanda por estación",
    description="Predice retiros y arribos por estación y hora para un día, y arma las listas de "
                "estaciones a abastecer o descargar antes de una franja pico.",
    version="1.2.0",
    lifespan=lifespan,
)


app.middleware("http")(registro.middleware)


@app.exception_handler(HTTPException)
async def error_http(request: Request, error: HTTPException):
    registro.anotar(request, error=str(error.detail))
    return await http_exception_handler(request, error)


@app.exception_handler(RequestValidationError)
async def pedido_invalido(request: Request, error: RequestValidationError):
    registro.anotar(request, error="; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors()))
    return await request_validation_exception_handler(request, error)


@app.exception_handler(FechaFueraDeRango)
async def fecha_fuera_de_rango(request: Request, error: FechaFueraDeRango):
    registro.anotar(request, error=str(error))
    return JSONResponse(status_code=404, content={"detail": str(error)})


@app.exception_handler(SinDatos)
async def sin_datos(request: Request, error: SinDatos):
    registro.anotar(request, error=str(error))
    return JSONResponse(status_code=404, content={"detail": str(error)})


def predictor(request: Request) -> Predictor:
    return request.app.state.predictor


def verificar_estaciones(p: Predictor, station_ids: list[int]) -> None:
    desconocidas = sorted(set(station_ids) - set(p.estaciones.index))
    if desconocidas:
        raise HTTPException(404, f"Estaciones desconocidas: {desconocidas}")


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
    fin_entrenamiento: date = Field(description="Hasta este día el modelo ya vio los viajes: ahí los aciertos no miden nada")


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
    en_entrenamiento: bool = Field(description="Si el modelo se entrenó con este día")
    totales: Totales = Field(description="Suma del día sobre las estaciones devueltas")
    filas: list[FilaPrediccion] = Field(description="Una fila por estación y hora")


class Demanda(BaseModel):
    """Viajes recientes de la estación, conocidos al cierre del día anterior."""
    ayer_misma_hora: float = Field(ge=0, le=200, description="Viajes a la misma hora del día anterior", examples=[3])
    semana_anterior_misma_hora: float = Field(ge=0, le=200, description="Viajes a la misma hora, 7 días antes", examples=[6])
    promedio_7d_misma_hora: float = Field(ge=0, le=200, description="Promedio de esa hora en los últimos 7 días", examples=[5.4])
    total_dia_anterior: float = Field(ge=0, le=2000, description="Total de las 24 horas que terminan a la misma hora del día "
                                                                 "anterior", examples=[60])
    promedio_diario_28d: float = Field(ge=0, le=2000, description="Viajes por día, promedio de 28 días", examples=[50])


class Estado(BaseModel):
    """Estado de la estación el día anterior (promedio de las fotos). Todo es opcional."""
    capacidad_util: float | None = Field(None, ge=0, le=200, description="Bicis disponibles + anclajes libres")
    bicis_rotas: float | None = Field(None, ge=0, le=200)
    pct_capacidad_rota: float | None = Field(None, ge=0, le=1, description="Parte de la capacidad que está rota")
    prop_vacia: float | None = Field(None, ge=0, le=1, description="Parte del día que estuvo vacía")
    prop_llena: float | None = Field(None, ge=0, le=1, description="Parte del día que estuvo llena")


class CasoNuevo(BaseModel):
    """Una estación en una hora. La fecha puede ser futura y la estación, una que todavía no existe."""
    station_id: int | None = Field(None, description="Estación conocida: se usa su ubicación", examples=[175])
    lat: float | None = Field(None, ge=-34.75, le=-34.50, description="Latitud, si no se manda `station_id`")
    lon: float | None = Field(None, ge=-58.55, le=-58.33, description="Longitud, si no se manda `station_id`")
    fecha: date = Field(description="Día a predecir (AAAA-MM-DD)", examples=["2026-12-01"])
    hora: int = Field(ge=0, le=23, description="Hora de inicio", examples=[8])
    es_feriado: bool = Field(False, description="Si el día es feriado")
    clima: Clima = Field(description="Clima esperado en esa hora")
    retiros: Demanda = Field(description="Retiros recientes de la estación")
    arribos: Demanda = Field(description="Arribos recientes de la estación")
    estado: Estado = Field(default_factory=Estado)

    @model_validator(mode="after")
    def ubicacion(self):
        if self.station_id is not None and (self.lat is not None or self.lon is not None):
            raise ValueError("Mandar `station_id` o `lat` y `lon`, no los dos.")
        if self.station_id is None and (self.lat is None or self.lon is None):
            raise ValueError("Falta la ubicación: mandar `station_id`, o `lat` y `lon`.")
        return self

    def dominio(self, p: Predictor) -> Caso:
        lat, lon = (self.lat, self.lon) if self.station_id is None else p.estaciones.loc[self.station_id, ["lat", "lon"]]
        return Caso(fecha=self.fecha, hora=self.hora, es_feriado=self.es_feriado, lat=float(lat), lon=float(lon),
                    clima=self.clima.dominio(), retiros=DemandaReciente(**self.retiros.model_dump()),
                    arribos=DemandaReciente(**self.arribos.model_dump()),
                    estado=EstadoDiaAnterior(**self.estado.model_dump()))


class PedidoCasos(BaseModel):
    casos: list[CasoNuevo] = Field(min_length=1, max_length=1000)


class ResultadoCaso(BaseModel):
    station_id: int | None
    fecha: date
    hora: int
    pred_retiros: float = Field(description="Retiros esperados en la hora")
    pred_arribos: float = Field(description="Arribos esperados en la hora")
    flujo_neto: float = Field(description="Arribos − retiros esperados: negativo si la estación se vacía")


class RespuestaCasos(BaseModel):
    resultados: list[ResultadoCaso] = Field(description="En el mismo orden que los casos")


class CambioVariable(BaseModel):
    variable: str
    psi: float = Field(description="Population Stability Index contra los mismos meses del entrenamiento")
    nivel: str = Field(description="estable (< 0,10), leve o alto (≥ 0,25)")
    es_clima: bool
    faltantes_antes: float = Field(description="% sin dato en los mismos meses del entrenamiento")
    faltantes: float = Field(description="% sin dato en el mes")


class ErrorPeriodo(BaseModel):
    dias: int
    estaciones: int
    retiros_por_dia: float
    wape: float = Field(description="WAPE diario por estación del modelo, en %")
    wape_semana_anterior: float = Field(description="El mismo error repitiendo la semana anterior")
    sesgo: float = Field(description="Total predicho contra el real, en %")


class ErrorMes(ErrorPeriodo):
    mes: str
    referencia: bool = Field(description="Si el mes es del test con el que se aprobó el modelo")
    estaciones_nuevas: int = Field(description="Estaciones que no estaban en el entrenamiento")
    cambio: list[CambioVariable]
    avisos: list[str]


class ErrorSemana(ErrorPeriodo):
    semana: date


class MonitoreoModelo(BaseModel):
    calculado: datetime
    fin_entrenamiento: date
    ultima_fecha: date
    peor_wape_referencia: float
    modelo_vigente: bool = Field(description="Si el monitoreo corresponde al modelo cargado en la API")
    meses: list[ErrorMes]
    semanas: list[ErrorSemana]


class Uso(BaseModel):
    pedidos: int
    errores_cliente: int = Field(description="Respuestas 4xx")
    errores_servidor: int = Field(description="Respuestas 5xx")
    ms_mediana: float | None
    ms_p95: float | None


class UsoRuta(Uso):
    ruta: str


class MonitoreoServicio(BaseModel):
    desde: datetime
    total: Uso
    por_ruta: list[UsoRuta]
    ultimos: list[dict] = Field(description="Últimos pedidos, del más nuevo al más viejo")


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
    promedio_abastecer: float | None = Field(description="Precisión media de la franja en los días hábiles del test, con el mismo `top`")
    promedio_descargar: float | None = Field(description="Precisión media de la franja en los días hábiles del test, con el mismo `top`")
    en_entrenamiento: bool = Field(description="Si el modelo se entrenó con este día: la precisión sale inflada")


@app.get("/salud", response_model=Salud, summary="Estado del servicio y rango de fechas")
def salud(request: Request):
    p = predictor(request)
    return Salud(estado="ok", modelo_entrenado=p.meta["entrenado"],
                 fecha_minima=p.primera_fecha, fecha_maxima=p.ultima_fecha, fin_entrenamiento=p.fin_entrenamiento)


@app.get("/modelo", response_model=Modelo, summary="Descripción, variables y métricas del modelo")
def modelo(request: Request):
    return predictor(request).meta


@app.get("/estaciones", response_model=list[Estacion], summary="Estaciones con ubicación y grupo")
def estaciones(request: Request):
    return registros(predictor(request).estaciones.reset_index())


@app.post("/prediccion", response_model=RespuestaPrediccion, summary="Demanda por estación y hora de un día")
def prediccion(pedido: PedidoPrediccion, request: Request):
    p = predictor(request)
    registro.anotar(request, pedido=pedido.model_dump(mode="json"))
    verificar_estaciones(p, pedido.station_ids or [])
    pred = predecir_dia(p, pedido.fecha, pedido.clima.dominio() if pedido.clima else None)
    if pedido.station_ids:
        pred = pred[pred.station_id.isin(pedido.station_ids)]
    columnas = ["pred_retiros", "pred_arribos", "pred_glm", "retiros", "arribos"]
    totales = pred[columnas].sum(min_count=1)
    registro.anotar(request, respuesta=registros(totales.to_frame().T)[0])
    return RespuestaPrediccion(fecha=pedido.fecha, clima=pedido.clima, en_entrenamiento=p.en_entrenamiento(pedido.fecha),
                               totales=Totales(**registros(totales.to_frame().T)[0]),
                               filas=registros(pred))


@app.post("/prediccion/casos", response_model=RespuestaCasos,
          summary="Demanda de casos nuevos, con los datos que manda el usuario")
def prediccion_casos(pedido: PedidoCasos, request: Request):
    p = predictor(request)
    registro.anotar(request, pedido={"casos": len(pedido.casos), "primero": pedido.casos[0].model_dump(mode="json")})
    verificar_estaciones(p, [c.station_id for c in pedido.casos if c.station_id is not None])
    pred = predecir_casos(p, [c.dominio(p) for c in pedido.casos])
    registro.anotar(request, respuesta=pred.sum().round(3).to_dict())
    return RespuestaCasos(resultados=[
        ResultadoCaso(station_id=c.station_id, fecha=c.fecha, hora=c.hora, pred_retiros=retiros, pred_arribos=arribos,
                      flujo_neto=arribos - retiros)
        for c, retiros, arribos in zip(pedido.casos, pred.pred_retiros, pred.pred_arribos)])


@app.get("/estaciones/{station_id}/caso", response_model=CasoNuevo,
         summary="Un caso registrado en el dataset, listo para editar y mandar a /prediccion/casos")
def caso(station_id: int, fecha: date, request: Request, hora: Annotated[int, Query(ge=0, le=23)]):
    p = predictor(request)
    verificar_estaciones(p, [station_id])
    registrado = asdict(caso_registrado(p, station_id, fecha, hora))
    del registrado["lat"], registrado["lon"]
    return CasoNuevo(station_id=station_id, **registrado)


@app.post("/rebalanceo", response_model=RespuestaRebalanceo,
          summary="Estaciones a abastecer y a descargar antes de una franja pico")
def rebalancear(pedido: PedidoRebalanceo, request: Request):
    p = predictor(request)
    registro.anotar(request, pedido=pedido.model_dump(mode="json"))
    pred = predecir_dia(p, pedido.fecha, pedido.clima.dominio() if pedido.clima else None)
    r = rebalanceo(p, pred, pedido.franja, pedido.top)
    registro.anotar(request, respuesta={"precision_abastecer": r.precision_abastecer, "precision_descargar": r.precision_descargar,
                                        "abastecer": r.abastecer.index.tolist(), "descargar": r.descargar.index.tolist()})
    return RespuestaRebalanceo(fecha=pedido.fecha, franja=pedido.franja, horas=list(pedido.franja.horas),
                               abastecer=registros(r.abastecer.reset_index()),
                               descargar=registros(r.descargar.reset_index()),
                               precision_abastecer=r.precision_abastecer,
                               precision_descargar=r.precision_descargar,
                               promedio_abastecer=r.promedio_abastecer, promedio_descargar=r.promedio_descargar,
                               en_entrenamiento=p.en_entrenamiento(pedido.fecha))


@app.get("/monitoreo/servicio", response_model=MonitoreoServicio, summary="Pedidos, errores y latencia de los últimos días")
def monitoreo_servicio(dias: Annotated[int, Query(ge=1, le=registro.DIAS_A_CONSERVAR)] = 7,
                       ultimos: Annotated[int, Query(ge=0, le=200)] = 20):
    return registro.resumen(dias, ultimos)


@app.get("/monitoreo/modelo", response_model=MonitoreoModelo, summary="Error por mes y cambio en los datos desde el entrenamiento")
def monitoreo_modelo(request: Request):
    ruta = RUTA_MODELOS / ARCHIVO_MONITOREO
    if not ruta.exists():
        raise HTTPException(404, "Todavía no se calculó el monitoreo: correr scripts/monitorear_modelo.py.")
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    return datos | {"modelo_vigente": datos["modelo_entrenado"] == predictor(request).meta["entrenado"]}
