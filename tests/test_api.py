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
    assert respuesta.json() | {"modelo_entrenado": None} == {
        "estado": "ok", "modelo_entrenado": None, "fecha_minima": "2024-01-30", "fecha_maxima": "2026-08-31"}


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


@pytest.mark.parametrize("fecha", ["2024-01-29", "2026-09-01"])
def test_fecha_fuera_del_rango_es_404(cliente, fecha):
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
