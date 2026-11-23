"""Tablero de rebalanceo de Ecobici. Sólo habla con la API por HTTP."""
import os
from datetime import date, timedelta

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
ESTACION_EJEMPLO = 175  # 147 - Constitución
FECHA_EJEMPLO = date(2026, 7, 14)  # el día hábil del test más parecido al promedio (scripts/evaluar_rebalanceo.py)


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


def api_o_frenar(ruta: str, cuerpo: dict | None = None) -> dict | list:
    try:
        return api(ruta, cuerpo)
    except ErrorApi as error:
        st.error(str(error))
        st.stop()


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


def pestania_rebalanceo(salud: dict, todas: list[dict], fecha: date, franja: str, top: int, clima: dict | None) -> None:
    with st.spinner("Prediciendo el día..."):
        plan = api_o_frenar("/rebalanceo", {"fecha": fecha.isoformat(), "franja": franja, "top": top, "clima": clima})

    if plan["en_entrenamiento"]:
        fin = date.fromisoformat(salud["fin_entrenamiento"])
        st.warning(f"El modelo se entrenó con datos hasta el {fin:%d/%m/%Y}: ya vio los viajes de este día, así que su acierto "
                   f"no dice cómo predice. Para evaluarlo, elegir un día desde el {fin + timedelta(days=1):%d/%m/%Y}.")
    elif fecha.weekday() >= 5:
        st.info("Es fin de semana: las franjas pico y la precisión del modelo se evaluaron en días hábiles.")

    abastecer, descargar = plan["abastecer"], plan["descargar"]
    columnas = st.columns(4)
    columnas[0].metric("Bicis que faltan", f"{-sum(e['flujo_predicho'] for e in abastecer):.0f}",
                       help=f"Suma del flujo predicho de las {top} estaciones a abastecer")
    columnas[1].metric("Bicis que sobran", f"{sum(e['flujo_predicho'] for e in descargar):.0f}",
                       help=f"Suma del flujo predicho de las {top} estaciones a descargar")
    for columna, lado in zip(columnas[2:], ["abastecer", "descargar"]):
        precision, promedio = plan[f"precision_{lado}"], plan[f"promedio_{lado}"]
        sin_acierto = precision is None or plan["en_entrenamiento"]
        columna.metric(f"Acierto {lado}", "–" if sin_acierto else f"{precision:.0%}",
                       delta=None if promedio is None or sin_acierto or fecha.weekday() >= 5 else f"promedio {promedio:.0%}",
                       delta_color="off", delta_arrow="off",
                       help=f"Parte de la lista que estuvo entre las {top} estaciones con el flujo real más extremo. El promedio "
                            "es el de los días hábiles de junio a agosto de 2026, que el modelo no vio al entrenar. Al azar: "
                            f"{top / len(todas):.0%}.")

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
        return
    filas = pd.DataFrame(detalle["filas"])
    if filas.empty:
        st.warning("La estación no tuvo actividad registrada ese día.")
        return
    totales = detalle["totales"]
    retiros, arribos = st.columns(2)
    retiros.markdown(f"**Retiros por hora** · predichos {totales['pred_retiros']:.0f}, reales {totales['retiros']}")
    retiros.line_chart(curva(filas, "retiros"), color=COLORES_CURVA, x_label="Hora", y_label="Retiros")
    arribos.markdown(f"**Arribos por hora** · predichos {totales['pred_arribos']:.0f}, reales {totales['arribos']}")
    arribos.line_chart(curva(filas, "arribos"), color=COLORES_CURVA, x_label="Hora", y_label="Arribos")


def pedir_demanda(contenedor, titulo: str, tipo: str, registrada: dict, clave: str) -> dict:
    contenedor.markdown(f"**{titulo} recientes**")
    etiquetas = {"ayer_misma_hora": "Ayer, a esta hora", "semana_anterior_misma_hora": "Hace 7 días, a esta hora",
                 "promedio_7d_misma_hora": "Promedio de 7 días, a esta hora", "total_dia_anterior": "Total del día anterior",
                 "promedio_diario_28d": "Por día, promedio de 28 días"}
    return {campo: contenedor.number_input(etiqueta, 0.0, 2000.0 if "dia" in campo and "hora" not in campo else 200.0,
                                           float(registrada[campo]), step=1.0, format="%.1f", key=f"{clave}-{tipo}-{campo}")
            for campo, etiqueta in etiquetas.items()}


def pestania_datos_nuevos(salud: dict, todas: list[dict], fecha_base: date) -> None:
    st.markdown("Predice una estación y una hora **con los datos que se cargan acá**, sin que estén en el dataset: un día futuro, "
                "otro clima u otra demanda. Parte de un caso registrado, que se puede editar.")
    nombres = {e["station_id"]: e["nombre"] for e in todas}
    ordenadas = sorted(nombres, key=nombres.get)
    a, b, c = st.columns([2, 1, 1])
    estacion = a.selectbox("Estación", ordenadas, index=ordenadas.index(ESTACION_EJEMPLO) if ESTACION_EJEMPLO in nombres else 0,
                           format_func=nombres.get, key="nuevo-estacion")
    base = b.date_input("Día de partida", fecha_base, min_value=date.fromisoformat(salud["fecha_minima"]),
                        max_value=date.fromisoformat(salud["fecha_maxima"]), format="DD/MM/YYYY", key="nuevo-base")
    hora = c.selectbox("Hora", range(24), index=8, format_func="{} h".format, key="nuevo-hora")
    try:
        registrado = api(f"/estaciones/{estacion}/caso?fecha={base.isoformat()}&hora={hora}")
    except ErrorApi as error:
        st.warning(str(error))
        return

    # la clave cambia con el caso de partida: al elegir otro, los campos vuelven a sus valores registrados
    clave = f"{estacion}-{base}-{hora}"
    with st.form(f"caso-{clave}"):
        columnas = st.columns(3)
        columnas[0].markdown("**Cuándo y con qué clima**")
        fecha = columnas[0].date_input("Día a predecir", base, format="DD/MM/YYYY", key=f"{clave}-fecha")
        es_feriado = columnas[0].checkbox("Es feriado", registrado["es_feriado"], key=f"{clave}-feriado")
        clima = {"temperatura_c": columnas[0].number_input("Temperatura (°C)", -15.0, 50.0, registrado["clima"]["temperatura_c"],
                                                           step=1.0, format="%.1f", key=f"{clave}-temp"),
                 "precipitacion_mm": columnas[0].number_input("Lluvia en la hora (mm)", 0.0, 60.0, registrado["clima"]["precipitacion_mm"],
                                                              step=0.5, format="%.1f", key=f"{clave}-lluvia"),
                 "viento_kmh": columnas[0].number_input("Viento (km/h)", 0.0, 150.0, registrado["clima"]["viento_kmh"],
                                                        step=1.0, format="%.1f", key=f"{clave}-viento")}
        retiros = pedir_demanda(columnas[1], "Retiros", "retiros", registrado["retiros"], clave)
        arribos = pedir_demanda(columnas[2], "Arribos", "arribos", registrado["arribos"], clave)
        st.form_submit_button("Predecir", type="primary")

    nuevo = registrado | {"fecha": fecha.isoformat(), "es_feriado": es_feriado, "clima": clima, "retiros": retiros, "arribos": arribos}
    try:
        original, resultado = api("/prediccion/casos", {"casos": [registrado, nuevo]})["resultados"]
    except ErrorApi as error:
        st.error(str(error))
        return
    for columna, etiqueta, campo, signo in zip(st.columns(3), ["Retiros esperados", "Arribos esperados", "Flujo neto"],
                                               ["pred_retiros", "pred_arribos", "flujo_neto"], ["", "", "+"]):
        cambio = resultado[campo] - original[campo]
        columna.metric(etiqueta, f"{resultado[campo]:{signo}.1f}",
                       delta=f"{cambio:+.1f} por los cambios" if abs(cambio) >= 0.05 else "sin cambios",
                       delta_color="off", delta_arrow="off", help="Debajo, la diferencia contra el caso registrado")
    st.caption("Flujo neto: arribos − retiros. Negativo, la estación se vacía; positivo, se llena. "
               "El estado de la estación el día anterior (bicis rotas, tiempo vacía o llena) se toma del caso registrado.")
    with st.expander("Pedido que se manda a la API (POST /prediccion/casos)"):
        st.json({"casos": [nuevo]})


st.set_page_config(page_title="Rebalanceo Ecobici", layout="wide")
st.title("Rebalanceo de Ecobici")
st.caption("Qué estaciones abastecer o descargar antes de la franja pico, según la demanda "
           "predicha para el día (arribos − retiros sumados en la franja).")

salud, modelo, todas = api_o_frenar("/salud"), api_o_frenar("/modelo"), api_o_frenar("/estaciones")
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

planificar, datos_nuevos = st.tabs(["Planificar el rebalanceo", "Probar con datos nuevos"])
with planificar:
    pestania_rebalanceo(salud, todas, fecha, franja, top, clima)
with datos_nuevos:
    pestania_datos_nuevos(salud, todas, fecha)

with st.expander("Sobre el modelo"):
    st.markdown(f"**{modelo['descripcion']}.** Horizonte: {modelo['horizonte']}.  \n"
                f"Entrenado el {modelo['entrenado'][:10]} con datos del {modelo['periodo_entrenamiento'][0][:10]} "
                f"al {modelo['periodo_entrenamiento'][1][:10]}. Clima al entrenar: {modelo['clima']['entrenamiento']}; "
                f"en producción: {modelo['clima']['produccion']}.")
    st.markdown("Métricas en test (junio a agosto de 2026), gradient boosting con pérdida de Poisson:")
    st.dataframe(pd.DataFrame(modelo["metricas_test"]).round(3))
    st.caption(f"{len(modelo['variables'])} variables: " + ", ".join(modelo["variables"]))
