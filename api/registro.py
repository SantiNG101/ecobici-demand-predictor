"""Registro de los pedidos a la API: una línea JSON por pedido, en un archivo por día.

No guarda la IP ni las cabeceras de quien llama.
"""
import json
import logging
import os
import threading
import time
import traceback
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from fastapi import Request
from fastapi.responses import JSONResponse

REPO_DIR = Path(__file__).resolve().parents[1]
RUTAS_SIN_REGISTRO = {"/salud"}  # el healthcheck de Docker la llama cada 10 segundos
DIAS_A_CONSERVAR = 30

log = logging.getLogger("uvicorn.error")
_candado = threading.Lock()


def carpeta() -> Path:
    return REPO_DIR / os.environ.get("ECOBICI_LOGS_DIR", "logs")


def preparar() -> None:
    """Crea la carpeta y borra los registros viejos. Si no se puede escribir, la API funciona igual."""
    try:
        carpeta().mkdir(parents=True, exist_ok=True)
        limite = f"api-{datetime.now() - timedelta(days=DIAS_A_CONSERVAR):%Y-%m-%d}.jsonl"
        for viejo in carpeta().glob("api-????-??-??.jsonl"):
            if viejo.name < limite:
                viejo.unlink()
    except OSError as error:
        log.warning("No se puede escribir el registro de pedidos en %s: %s", carpeta(), error)


def anotar(request: Request, **datos) -> None:
    """Agrega datos del pedido o de la respuesta a la línea que se va a guardar."""
    if not hasattr(request.state, "registro"):
        request.state.registro = {}
    request.state.registro.update(datos)


def _escribir(linea: dict) -> None:
    try:
        with _candado, open(carpeta() / f"api-{linea['momento'][:10]}.jsonl", "a", encoding="utf-8") as archivo:
            archivo.write(json.dumps(linea, ensure_ascii=False, default=str) + "\n")
    except OSError as error:
        log.warning("No se pudo registrar un pedido: %s", error)


async def middleware(request: Request, call_next):
    id_pedido, inicio = uuid.uuid4().hex[:12], time.perf_counter()
    try:
        respuesta = await call_next(request)
    except Exception as error:
        log.exception("Error no previsto en el pedido %s", id_pedido)
        anotar(request, error=f"{type(error).__name__}: {error}", traza=traceback.format_exc(limit=-3))
        respuesta = JSONResponse(status_code=500, content={"detail": f"Error interno. Pedido {id_pedido}."})
    respuesta.headers["X-Pedido-Id"] = id_pedido
    ruta = getattr(request.scope.get("route"), "path", "(sin ruta)")
    if ruta not in RUTAS_SIN_REGISTRO:
        _escribir({"momento": datetime.now().astimezone().isoformat(timespec="milliseconds"), "id": id_pedido,
                   "metodo": request.method, "ruta": ruta, "estado": respuesta.status_code,
                   "ms": round(1000 * (time.perf_counter() - inicio), 1), **getattr(request.state, "registro", {})})
    return respuesta


def _leer(desde: datetime) -> list[dict]:
    lineas = []
    for archivo in sorted(carpeta().glob("api-????-??-??.jsonl")):
        if archivo.name >= f"api-{desde:%Y-%m-%d}.jsonl":
            for texto in archivo.read_text(encoding="utf-8").splitlines():
                try:
                    lineas.append(json.loads(texto))
                except json.JSONDecodeError:  # una línea cortada no invalida el resto
                    continue
    return [linea for linea in lineas if datetime.fromisoformat(linea["momento"]) >= desde]


def resumen(dias: int, ultimos: int) -> dict:
    desde = datetime.now().astimezone() - timedelta(days=dias)
    lineas = _leer(desde)

    def fila(grupo: list[dict]) -> dict:
        ms = [linea["ms"] for linea in grupo]
        return {"pedidos": len(grupo), "errores_cliente": sum(400 <= linea["estado"] < 500 for linea in grupo),
                "errores_servidor": sum(linea["estado"] >= 500 for linea in grupo),
                "ms_mediana": round(float(np.percentile(ms, 50)), 1) if ms else None,
                "ms_p95": round(float(np.percentile(ms, 95)), 1) if ms else None}

    por_ruta: dict[str, list[dict]] = {}
    for linea in lineas:
        por_ruta.setdefault(f"{linea['metodo']} {linea['ruta']}", []).append(linea)
    return {"desde": desde, "total": fila(lineas),
            "por_ruta": [{"ruta": ruta, **fila(grupo)} for ruta, grupo in sorted(por_ruta.items())],
            "ultimos": [{k: v for k, v in linea.items() if k != "traza"} for linea in lineas[::-1][:ultimos]]}
