from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from api.main import app

FECHA = "2026-06-02"  # martes del test, el ejemplo de la sección 12.1 del notebook 04


@pytest.fixture(scope="module")
def cliente():
    with TestClient(app) as cliente:
        yield cliente


def test_salud_informa_el_rango_de_fechas(cliente):
    respuesta = cliente.get("/salud")
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["estado"] == "ok" and cuerpo["fecha_minima"] == "2024-01-30"
    # El último día crece con scripts/actualizar_datos.py, y siempre se puede predecir
    assert cuerpo["fecha_maxima"] >= "2026-08-31"
    assert cliente.post("/rebalanceo", json={"fecha": cuerpo["fecha_maxima"], "franja": "manana"}).status_code == 200


def test_prediccion_de_un_dia_completo(cliente):
    respuesta = cliente.post("/prediccion", json={"fecha": FECHA})
    assert respuesta.status_code == 200
    filas = respuesta.json()["filas"]
    assert len(filas) == 9480  # 395 estaciones activas × 24 horas, como en la sección 13 del notebook
    assert {f["hora"] for f in filas} == set(range(24))
    assert all(f["pred_retiros"] >= 0 and f["pred_arribos"] >= 0 and f["pred_glm"] >= 0 for f in filas)


def test_prediccion_filtrada_por_estacion(cliente):
    respuesta = cliente.post("/prediccion", json={"fecha": FECHA, "station_ids": [130]})
    cuerpo = respuesta.json()
    assert respuesta.status_code == 200
    assert len(cuerpo["filas"]) == 24
    assert round(cuerpo["totales"]["pred_retiros"], 1) == 97.3  # tabla de la sección 13 del notebook
    assert cuerpo["totales"]["retiros"] == 100


@pytest.mark.parametrize("fecha", ["2024-01-29", None])  # None: el día después del último
def test_fecha_fuera_del_rango_es_404(cliente, fecha):
    fecha = fecha or str(date.fromisoformat(cliente.get("/salud").json()["fecha_maxima"]) + timedelta(days=1))
    assert cliente.post("/prediccion", json={"fecha": fecha}).status_code == 404
    assert cliente.post("/rebalanceo", json={"fecha": fecha, "franja": "manana"}).status_code == 404


def test_estacion_desconocida_es_404(cliente):
    assert cliente.post("/prediccion", json={"fecha": FECHA, "station_ids": [999999]}).status_code == 404


@pytest.mark.parametrize("cuerpo", [
    {"fecha": "2 de junio"},
    {"fecha": FECHA, "clima": {"temperatura_c": 10}},
    {"fecha": FECHA, "clima": {"temperatura_c": 10, "precipitacion_mm": -1, "viento_kmh": 5}},
])
def test_prediccion_con_cuerpo_invalido_es_422(cliente, cuerpo):
    assert cliente.post("/prediccion", json=cuerpo).status_code == 422


@pytest.mark.parametrize("cuerpo", [
    {"fecha": FECHA, "franja": "noche"},
    {"fecha": FECHA, "franja": "manana", "top": 0},
    {"fecha": FECHA, "franja": "manana", "top": 51},
])
def test_rebalanceo_con_cuerpo_invalido_es_422(cliente, cuerpo):
    assert cliente.post("/rebalanceo", json=cuerpo).status_code == 422


def test_rebalanceo_de_la_manana(cliente):
    respuesta = cliente.post("/rebalanceo", json={"fecha": FECHA, "franja": "manana", "top": 10})
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    abastecer, descargar = cuerpo["abastecer"], cuerpo["descargar"]
    assert len(abastecer) == len(descargar) == 10
    assert max(e["flujo_predicho"] for e in abastecer) <= min(e["flujo_predicho"] for e in descargar)
    # el ejemplo de salida operativa de la sección 12.1 del notebook
    assert [e["nombre"] for e in abastecer[:3]] == ["147 - Constitución", "130 - RETIRO II", "002 - Retiro I"]
    assert [e["nombre"] for e in descargar[:3]] == ["026 - JUANA MANSO I", "035 - INGENIERO BUTTY", "237 - Madero Office"]
    assert abastecer[0]["flujo_real"] == -37
    assert 0 <= cuerpo["precision_abastecer"] <= 1 and 0 <= cuerpo["precision_descargar"] <= 1


def test_lluvia_fuerte_baja_los_retiros(cliente):
    sin_pronostico = cliente.post("/prediccion", json={"fecha": FECHA}).json()["totales"]["pred_retiros"]
    con_lluvia = cliente.post("/prediccion", json={
        "fecha": FECHA, "clima": {"temperatura_c": 10, "precipitacion_mm": 8, "viento_kmh": 30}}).json()
    assert con_lluvia["totales"]["pred_retiros"] < sin_pronostico
    assert con_lluvia["clima"]["precipitacion_mm"] == 8


def caso_registrado(cliente, station_id=175, hora=8):
    respuesta = cliente.get(f"/estaciones/{station_id}/caso", params={"fecha": FECHA, "hora": hora})
    assert respuesta.status_code == 200
    return respuesta.json()


@pytest.mark.parametrize("station_id,hora", [(175, 8), (26, 18), (130, 0)])
def test_un_caso_registrado_da_lo_mismo_que_la_prediccion_del_dia(cliente, station_id, hora):
    resultado = cliente.post("/prediccion/casos", json={"casos": [caso_registrado(cliente, station_id, hora)]}).json()["resultados"][0]
    filas = cliente.post("/prediccion", json={"fecha": FECHA, "station_ids": [station_id]}).json()["filas"]
    esperada = next(f for f in filas if f["hora"] == hora)
    assert resultado["pred_retiros"] == pytest.approx(esperada["pred_retiros"], rel=1e-6)
    assert resultado["pred_arribos"] == pytest.approx(esperada["pred_arribos"], rel=1e-6)
    assert resultado["flujo_neto"] == pytest.approx(resultado["pred_arribos"] - resultado["pred_retiros"])


def test_casos_con_fecha_futura_y_estacion_que_no_existe(cliente):
    base = caso_registrado(cliente)
    futuro = base | {"fecha": "2026-12-01"}
    nueva = {k: v for k, v in base.items() if k != "station_id"} | {"lat": -34.60, "lon": -58.42}
    lluvia = base | {"clima": {"temperatura_c": 10, "precipitacion_mm": 5, "viento_kmh": 20}}
    respuesta = cliente.post("/prediccion/casos", json={"casos": [base, futuro, nueva, lluvia]})
    assert respuesta.status_code == 200
    resultados = respuesta.json()["resultados"]
    assert len(resultados) == 4 and all(r["pred_retiros"] >= 0 and r["pred_arribos"] >= 0 for r in resultados)
    assert resultados[3]["pred_retiros"] < resultados[0]["pred_retiros"]


def test_un_caso_no_necesita_el_estado_de_la_estacion(cliente):
    sin_estado = {k: v for k, v in caso_registrado(cliente).items() if k != "estado"}
    assert cliente.post("/prediccion/casos", json={"casos": [sin_estado]}).status_code == 200


@pytest.mark.parametrize("cambio", [
    {"hora": 24},
    {"station_id": None},                  # sin ubicación
    {"lat": -34.6, "lon": -58.4},          # station_id y coordenadas a la vez
    {"retiros": {"ayer_misma_hora": 3}},   # demanda incompleta
])
def test_caso_invalido_es_422(cliente, cambio):
    caso = caso_registrado(cliente) | cambio
    assert cliente.post("/prediccion/casos", json={"casos": [caso]}).status_code == 422


def test_sin_casos_es_422(cliente):
    assert cliente.post("/prediccion/casos", json={"casos": []}).status_code == 422


def test_caso_de_estacion_desconocida_es_404(cliente):
    caso = caso_registrado(cliente) | {"station_id": 999999}
    assert cliente.post("/prediccion/casos", json={"casos": [caso]}).status_code == 404
    assert cliente.get("/estaciones/999999/caso", params={"fecha": FECHA, "hora": 8}).status_code == 404


def test_caso_sin_datos_o_fuera_de_rango_es_404(cliente):
    assert cliente.get("/estaciones/175/caso", params={"fecha": "2024-01-29", "hora": 8}).status_code == 404
    activas = {f["station_id"] for f in cliente.post("/prediccion", json={"fecha": FECHA}).json()["filas"]}
    inactiva = next(e["station_id"] for e in cliente.get("/estaciones").json() if e["station_id"] not in activas)
    assert cliente.get(f"/estaciones/{inactiva}/caso", params={"fecha": FECHA, "hora": 8}).status_code == 404


@pytest.mark.parametrize("fecha,esperado", [("2026-05-31", True), ("2026-06-01", False)])
def test_avisa_si_el_dia_es_de_entrenamiento(cliente, fecha, esperado):
    assert cliente.get("/salud").json()["fin_entrenamiento"] == "2026-05-31"
    assert cliente.post("/prediccion", json={"fecha": fecha, "station_ids": [130]}).json()["en_entrenamiento"] is esperado
    assert cliente.post("/rebalanceo", json={"fecha": fecha, "franja": "manana"}).json()["en_entrenamiento"] is esperado


def test_rebalanceo_informa_la_precision_promedio(cliente):
    cuerpo = cliente.post("/rebalanceo", json={"fecha": FECHA, "franja": "manana", "top": 15}).json()
    # promedios de la sección 12.1 del notebook 04
    assert round(cuerpo["promedio_abastecer"], 3) == 0.376
    assert round(cuerpo["promedio_descargar"], 3) == 0.497
