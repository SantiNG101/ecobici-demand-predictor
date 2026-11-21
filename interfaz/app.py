"""Tablero de rebalanceo de Ecobici. Sólo habla con la API por HTTP."""
import os
from datetime import date

import pandas as pd
import pydeck as pdk
import requests
import streamlit as st

API = os.environ.get("ECOBICI_API_URL", "http://localhost:8000").rstrip("/")
FRANJAS = {"manana": "Mañana (6 a 9 h)", "tarde": "Tarde (16 a 19 h)"}
COLOR_ABASTECER = [227, 73, 72]   # se vacía
COLOR_DESCARGAR = [42, 120, 214]  # se llena
COLOR_RESTO = [137, 135, 129, 90]
COLORES_CURVA = ["#2a78d6", "#eb6834"]  # predicho, real
FECHA_EJEMPLO = date(2026, 6, 2)


class ErrorApi(Exception):
    pass


@st.cache_data(ttl=600, show_spinner=False)
def api(ruta: str, cuerpo: dict | None = None) -> dict | list:
    try:
        respuesta = requests.request("POST" if cuerpo else "GET", f"{API}{ruta}", json=cuerpo, timeout=60)
    except requests.ConnectionError:
        raise ErrorApi(f"No se pudo conectar con la API en {API}. Revisar que esté corriendo "
                       "(`uvicorn api.main:app`) o la variable ECOBICI_API_URL.")
    except requests.Timeout:
        raise ErrorApi(f"La API en {API} no respondió a tiempo.")
    if respuesta.status_code != 200:
        try:
            detalle = respuesta.json().get("detail")
        except ValueError:
            detalle = respuesta.text[:200]
        raise ErrorApi(f"La API respondió {respuesta.status_code}: {detalle}")
    return respuesta.json()


def tabla(estaciones: list[dict]) -> pd.DataFrame:
    return (pd.DataFrame(estaciones)
              .rename(columns={"nombre": "Estación", "nombre_grupo": "Grupo",
                               "flujo_predicho": "Predicho", "flujo_real": "Real"})
              [["Estación", "Grupo", "Predicho", "Real"]])


def mapa(abastecer: list[dict], descargar: list[dict], todas: list[dict]) -> pdk.Deck:
    marcadas = {e["station_id"] for e in abastecer + descargar}
    resto = pd.DataFrame([e | {"flujo": ""} for e in todas if e["station_id"] not in marcadas])
    elegidas = pd.DataFrame(
        [e | {"color": COLOR_ABASTECER, "accion": "Abastecer"} for e in abastecer]
        + [e | {"color": COLOR_DESCARGAR, "accion": "Descargar"} for e in descargar])
    elegidas["radio"] = 60 + 12 * elegidas.flujo_predicho.abs()
    elegidas["flujo"] = elegidas.apply(lambda e: f"{e.accion} · flujo predicho {e.flujo_predicho:+.1f}", axis=1)
    capas = [
        pdk.Layer("ScatterplotLayer", resto, get_position=["lon", "lat"], get_radius=40,
                  get_fill_color=COLOR_RESTO, pickable=True),
        pdk.Layer("ScatterplotLayer", elegidas, get_position=["lon", "lat"], get_radius="radio",
                  get_fill_color="color", get_line_color=[255, 255, 255], line_width_min_pixels=1,
                  stroked=True, pickable=True),
    ]
    vista = pdk.ViewState(latitude=-34.605, longitude=-58.43, zoom=11.3)
    return pdk.Deck(layers=capas, initial_view_state=vista, map_style=None,
                    tooltip={"text": "{nombre}\n{flujo}"})


def curva(filas: pd.DataFrame, target: str) -> pd.DataFrame:
    return filas.set_index("hora")[[f"pred_{target}", target]].rename(
        columns={f"pred_{target}": "Predicho", target: "Real"})


st.set_page_config(page_title="Rebalanceo Ecobici", layout="wide")
st.title("Rebalanceo de Ecobici")
st.caption("Qué estaciones abastecer o descargar antes de la franja pico, según la demanda "
           "predicha para el día (arribos − retiros sumados en la franja).")

try:
    salud = api("/salud")
    modelo = api("/modelo")
    todas = api("/estaciones")
except ErrorApi as error:
    st.error(str(error))
    st.stop()

fecha_minima, fecha_maxima = date.fromisoformat(salud["fecha_minima"]), date.fromisoformat(salud["fecha_maxima"])
with st.sidebar:
    st.header("Planificación")
    fecha = st.date_input("Día", value=min(max(FECHA_EJEMPLO, fecha_minima), fecha_maxima),
                          min_value=fecha_minima, max_value=fecha_maxima, format="DD/MM/YYYY")
    franja = st.radio("Franja", list(FRANJAS), format_func=FRANJAS.get)
    top = st.slider("Estaciones por lista", 5, 50, 15)
    clima = None
    if st.toggle("Usar un pronóstico propio",
                 help="Reemplaza el clima registrado ese día en las 24 horas, por ejemplo con el pronóstico."):
        clima = {"temperatura_c": st.slider("Temperatura (°C)", -5.0, 40.0, 15.0, 0.5),
                 "precipitacion_mm": st.slider("Lluvia por hora (mm)", 0.0, 20.0, 0.0, 0.5),
                 "viento_kmh": st.slider("Viento (km/h)", 0.0, 80.0, 15.0, 1.0)}
    st.caption(f"Datos del {fecha_minima:%d/%m/%Y} al {fecha_maxima:%d/%m/%Y}. API: {API}")

pedido = {"fecha": fecha.isoformat(), "franja": franja, "top": top, "clima": clima}
try:
    with st.spinner("Prediciendo el día..."):
        plan = api("/rebalanceo", pedido)
except ErrorApi as error:
    st.error(str(error))
    st.stop()

if fecha.weekday() >= 5:
    st.info("Es fin de semana: las franjas pico y la precisión del modelo se evaluaron en días hábiles.")

abastecer, descargar = plan["abastecer"], plan["descargar"]
columnas = st.columns(4)
columnas[0].metric("Bicis que faltarían", f"{-sum(e['flujo_predicho'] for e in abastecer):.0f}",
                   help=f"Suma del flujo predicho de las {top} estaciones a abastecer")
columnas[1].metric("Bicis que sobrarían", f"{sum(e['flujo_predicho'] for e in descargar):.0f}",
                   help=f"Suma del flujo predicho de las {top} estaciones a descargar")
for columna, lado in zip(columnas[2:], ["abastecer", "descargar"]):
    precision = plan[f"precision_{lado}"]
    columna.metric(f"Acierto al {lado}", "sin datos" if precision is None else f"{precision:.0%}",
                   help=f"Parte de la lista que estuvo entre las {top} estaciones con el flujo real más extremo")

st.pydeck_chart(mapa(abastecer, descargar, todas), height=460)
st.caption("Rojo: abastecer (se vacían). Azul: descargar (se llenan). El tamaño indica cuántas bicis. Gris: resto de la red. "
           "En las tablas, el flujo es arribos − retiros en la franja: predicho por el modelo y el real registrado ese día.")

izquierda, derecha = st.columns(2)
formato = {"Estación": st.column_config.TextColumn(width="medium"),
           "Grupo": st.column_config.TextColumn(width="small"),
           "Predicho": st.column_config.NumberColumn(format="%.1f", width="small"),
           "Real": st.column_config.NumberColumn(width="small")}
izquierda.subheader("Abastecer (se vacían)")
izquierda.dataframe(tabla(abastecer), hide_index=True, column_config=formato)
derecha.subheader("Descargar (se llenan)")
derecha.dataframe(tabla(descargar), hide_index=True, column_config=formato)

st.subheader("Demanda de una estación en el día")
nombres = {e["station_id"]: e["nombre"] for e in todas}
sugeridas = [e["station_id"] for e in abastecer + descargar]
opciones = sugeridas + sorted(set(nombres) - set(sugeridas), key=nombres.get)
estacion = st.selectbox("Estación", opciones, format_func=nombres.get,
                        help="Primero las de las listas, después el resto de la red")
try:
    detalle = api("/prediccion", {"fecha": fecha.isoformat(), "clima": clima, "station_ids": [estacion]})
except ErrorApi as error:
    st.error(str(error))
else:
    filas = pd.DataFrame(detalle["filas"])
    if filas.empty:
        st.warning("La estación no tuvo actividad registrada ese día.")
    else:
        totales = detalle["totales"]
        retiros, arribos = st.columns(2)
        retiros.markdown(f"**Retiros por hora** · predichos {totales['pred_retiros']:.0f}, reales {totales['retiros']}")
        retiros.line_chart(curva(filas, "retiros"), color=COLORES_CURVA, x_label="Hora", y_label="Retiros")
        arribos.markdown(f"**Arribos por hora** · predichos {totales['pred_arribos']:.0f}, reales {totales['arribos']}")
        arribos.line_chart(curva(filas, "arribos"), color=COLORES_CURVA, x_label="Hora", y_label="Arribos")

with st.expander("Sobre el modelo"):
    st.markdown(f"**{modelo['descripcion']}.** Horizonte: {modelo['horizonte']}.  \n"
                f"Entrenado el {modelo['entrenado'][:10]} con datos del {modelo['periodo_entrenamiento'][0][:10]} "
                f"al {modelo['periodo_entrenamiento'][1][:10]}. Clima al entrenar: {modelo['clima']['entrenamiento']}; "
                f"en producción: {modelo['clima']['produccion']}.")
    st.markdown("Métricas en test (junio a agosto de 2026), gradient boosting con pérdida de Poisson:")
    st.dataframe(pd.DataFrame(modelo["metricas_test"]).round(3))
    st.caption(f"{len(modelo['variables'])} variables: " + ", ".join(modelo["variables"]))
