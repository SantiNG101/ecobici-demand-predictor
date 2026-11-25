import json

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api import main, registro
from src.ecobici import monitoreo

PEDIDO = {"fecha": "2026-06-02", "franja": "manana", "top": 5}


@pytest.fixture(scope="module")
def cliente():
    with TestClient(main.app, raise_server_exceptions=False) as cliente:
        yield cliente


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setenv("ECOBICI_LOGS_DIR", str(tmp_path))
    return tmp_path


def lineas(carpeta):
    return [json.loads(t) for a in carpeta.glob("*.jsonl") for t in a.read_text(encoding="utf-8").splitlines()]


def test_cada_pedido_queda_registrado(cliente, logs):
    respuesta = cliente.post("/rebalanceo", json=PEDIDO)
    (linea,) = lineas(logs)
    assert (linea["metodo"], linea["ruta"], linea["estado"]) == ("POST", "/rebalanceo", 200)
    assert linea["id"] == respuesta.headers["X-Pedido-Id"] and linea["ms"] > 0
    assert linea["pedido"]["fecha"] == "2026-06-02"
    assert linea["respuesta"]["abastecer"] == [e["station_id"] for e in respuesta.json()["abastecer"]]


def test_la_ruta_se_registra_sin_los_parametros(cliente, logs):
    cliente.get("/estaciones/175/caso", params={"fecha": "2026-06-02", "hora": 8})
    assert lineas(logs)[0]["ruta"] == "/estaciones/{station_id}/caso"


def test_el_healthcheck_no_se_registra(cliente, logs):
    assert cliente.get("/salud").status_code == 200
    assert lineas(logs) == []


@pytest.mark.parametrize("ruta,cuerpo,estado,texto", [
    ("/rebalanceo", PEDIDO | {"franja": "noche"}, 422, "franja"),
    ("/rebalanceo", PEDIDO | {"fecha": "2024-01-01"}, 404, "No se puede predecir"),
    ("/prediccion", {"fecha": "2026-06-02", "station_ids": [999999]}, 404, "999999"),
])
def test_los_errores_del_pedido_se_registran_con_el_motivo(cliente, logs, ruta, cuerpo, estado, texto):
    assert cliente.post(ruta, json=cuerpo).status_code == estado
    (linea,) = lineas(logs)
    assert linea["estado"] == estado and texto in linea["error"]


def test_un_error_no_previsto_responde_500_y_queda_registrado(cliente, logs, monkeypatch):
    def falla(*_):
        raise RuntimeError("se rompió")
    monkeypatch.setattr(main, "predecir_dia", falla)
    respuesta = cliente.post("/rebalanceo", json=PEDIDO)
    (linea,) = lineas(logs)
    assert respuesta.status_code == 500 and linea["id"] in respuesta.json()["detail"]
    assert "se rompió" in linea["error"] and "se rompió" not in respuesta.text


def test_si_no_se_puede_escribir_la_api_responde_igual(cliente, tmp_path, monkeypatch):
    archivo = tmp_path / "ocupado"
    archivo.write_text("")
    monkeypatch.setenv("ECOBICI_LOGS_DIR", str(archivo))
    registro.preparar()
    assert cliente.post("/rebalanceo", json=PEDIDO).status_code == 200


def test_el_resumen_del_servicio_cuenta_pedidos_y_errores(cliente, logs):
    cliente.post("/rebalanceo", json=PEDIDO)
    cliente.post("/rebalanceo", json=PEDIDO | {"top": 0})
    (logs / "api-2099-01-01.jsonl").write_text('{"cortada": ', encoding="utf-8")
    cuerpo = cliente.get("/monitoreo/servicio").json()
    assert cuerpo["total"]["pedidos"] == 2 and cuerpo["total"]["errores_cliente"] == 1 and cuerpo["total"]["errores_servidor"] == 0
    assert [r["ruta"] for r in cuerpo["por_ruta"]] == ["POST /rebalanceo"]
    assert [p["estado"] for p in cuerpo["ultimos"]] == [422, 200]


def test_se_borran_los_registros_viejos(logs):
    viejo, nuevo = logs / "api-2020-01-01.jsonl", logs / "api-2099-01-01.jsonl"
    viejo.touch(), nuevo.touch()
    registro.preparar()
    assert not viejo.exists() and nuevo.exists()


def test_monitoreo_del_modelo(cliente, tmp_path, monkeypatch):
    cuerpo = cliente.get("/monitoreo/modelo").json()
    assert cuerpo["modelo_vigente"] is True
    test = [m for m in cuerpo["meses"] if m["referencia"]]
    assert [m["mes"] for m in test] == ["2026-06", "2026-07", "2026-08"]
    # ponderado por los viajes de cada mes da el WAPE diario del test (notebook 04)
    pesos = [m["retiros_por_dia"] * m["dias"] for m in test]
    assert np.average([m["wape"] for m in test], weights=pesos) == pytest.approx(29.04, abs=0.05)
    assert all(m["avisos"] == [] for m in test)

    monkeypatch.setattr(main, "RUTA_MODELOS", tmp_path)
    assert cliente.get("/monitoreo/modelo").status_code == 404


def test_psi():
    rng = np.random.default_rng(0)
    base = pd.Series(rng.normal(0, 1, 20_000))
    assert monitoreo.psi(base, pd.Series(rng.normal(0, 1, 20_000))) < 0.01
    assert monitoreo.nivel_psi(monitoreo.psi(base, base + 1)) == "alto"
    sin_dato = base.copy()
    sin_dato[:10_000] = np.nan
    assert monitoreo.nivel_psi(monitoreo.psi(base, sin_dato)) == "alto"
    con_ceros = pd.Series(np.where(rng.random(20_000) < 0.7, 0.0, 1.0))
    assert monitoreo.psi(con_ceros, con_ceros) == pytest.approx(0)


def test_errores_por_periodo():
    diario = pd.DataFrame({"station_id": [1, 1], "fecha": pd.to_datetime(["2026-09-01", "2026-09-02"]),
                           "retiros": [10, 10], "pred": [12, 9], "base": [5, 5]})
    fila = monitoreo.errores(diario, diario.fecha.dt.strftime("%Y-%m")).loc["2026-09"]
    assert (fila.dias, fila.estaciones, fila.wape, fila.wape_semana_anterior) == (2, 1, 15, 50)
    assert fila.sesgo == pytest.approx(5)


@pytest.mark.parametrize("cambios,cantidad", [
    ({}, 0), ({"wape": 39}, 1), ({"sesgo": -12}, 1), ({"wape_semana_anterior": 20}, 1),
    ({"wape": 50, "sesgo": 20, "wape_semana_anterior": 30}, 3), ({"wape": 50, "dias": 3}, 0),
])
def test_avisos(cambios, cantidad):
    mes = {"dias": 30, "wape": 28, "wape_semana_anterior": 40, "sesgo": 2} | cambios
    assert len(monitoreo.avisos(mes, peor_referencia=31)) == cantidad
