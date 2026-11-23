"""Seguimiento del modelo después del entrenamiento: error por mes y semana, y cambio en los datos de entrada."""
from datetime import date

import numpy as np
import pandas as pd

from src.ecobici.modelo import Predictor, conexion

# El calendario y la ubicación no se siguen: no dependen de cómo se usa el sistema
VARIABLES_SEGUIDAS = ["temperatura_c", "precipitacion_mm", "viento_kmh", "ret_misma_hora_7d", "ret_dia_anterior", "ret_media_28d",
                      "arr_misma_hora_7d", "arr_dia_anterior", "arr_media_28d", "capacidad_util_dia_ant", "bicis_rotas_dia_ant",
                      "pct_capacidad_rota_dia_ant", "prop_vacia_dia_ant", "prop_llena_dia_ant"]
VARIABLES_CLIMA = {"temperatura_c", "precipitacion_mm", "viento_kmh"}

UMBRAL_SESGO = 10          # % de diferencia en el total de viajes del mes
UMBRAL_EMPEORA = 10        # % de aumento del error contra el peor mes de referencia
UMBRAL_PSI_LEVE, UMBRAL_PSI = 0.10, 0.25  # valores habituales del PSI
MIN_DIAS = 7               # con menos días no se avisa


def psi(referencia: pd.Series, actual: pd.Series, tramos: int = 10) -> float:
    """Population Stability Index: cuánto cambió una distribución. Los faltantes cuentan como un tramo más."""
    cortes = np.unique(np.quantile(referencia.dropna(), np.linspace(0, 1, tramos + 1)[1:-1])) if referencia.notna().any() else []

    def partes(serie: pd.Series) -> np.ndarray:
        valores = serie.to_numpy(float)
        sin_dato = np.isnan(valores)
        cuentas = np.bincount(np.searchsorted(cortes, valores[~sin_dato], side="left"), minlength=len(cortes) + 1)
        return np.clip(np.append(cuentas, sin_dato.sum()) / len(valores), 1e-4, None)

    esperado, observado = partes(referencia), partes(actual)
    return float(((observado - esperado) * np.log(observado / esperado)).sum())


def nivel_psi(valor: float) -> str:
    return "alto" if valor >= UMBRAL_PSI else "leve" if valor >= UMBRAL_PSI_LEVE else "estable"


def errores(diario: pd.DataFrame, periodo: pd.Series) -> pd.DataFrame:
    """diario: una fila por estación y día con retiros, pred (modelo) y base (semana anterior)."""
    d = diario.assign(periodo=periodo.to_numpy(), error=(diario.pred - diario.retiros).abs(), error_base=(diario.base - diario.retiros).abs())
    t = d.groupby("periodo").agg(dias=("fecha", "nunique"), estaciones=("station_id", "nunique"), retiros=("retiros", "sum"),
                                 pred=("pred", "sum"), error=("error", "sum"), error_base=("error_base", "sum"))
    return pd.DataFrame({"dias": t.dias, "estaciones": t.estaciones, "retiros": t.retiros, "retiros_por_dia": t.retiros / t.dias,
                         "wape": 100 * t.error / t.retiros, "wape_semana_anterior": 100 * t.error_base / t.retiros,
                         "sesgo": 100 * (t.pred / t.retiros - 1)})


def avisos(mes: dict, peor_referencia: float) -> list[str]:
    if mes["dias"] < MIN_DIAS:
        return []
    lista = []
    if mes["wape"] > peor_referencia * (1 + UMBRAL_EMPEORA / 100):
        lista.append(f"El error ({mes['wape']:.1f} %) supera en más de {UMBRAL_EMPEORA} % al peor mes de referencia ({peor_referencia:.1f} %).")
    if abs(mes["sesgo"]) > UMBRAL_SESGO:
        lista.append(f"El total predicho se aparta {mes['sesgo']:+.1f} % del real.")
    if mes["wape"] >= mes["wape_semana_anterior"]:
        lista.append("El modelo no le gana a repetir la semana anterior.")
    return lista


def variables(p: Predictor, desde: date, hasta: date) -> pd.DataFrame:
    """Variables del modelo de las filas con desde <= día < hasta."""
    margen = pd.Timestamp(desde) - pd.Timedelta(days=29)
    consulta = p.meta["consulta_variables"].format(fuente=p.dataset.as_posix(), desde=f"{margen:%Y-%m-%d}")
    with conexion() as con:
        return con.execute(f"SELECT * FROM ({consulta}) WHERE ts >= ? AND ts < ?",
                           [pd.Timestamp(desde).to_pydatetime(), pd.Timestamp(hasta).to_pydatetime()]).df()


def monitorear(p: Predictor, fin_referencia: date) -> dict:
    """Error desde que terminó el entrenamiento. Los meses hasta fin_referencia son la vara; los siguientes se vigilan.

    El cambio en los datos compara cada mes con los mismos meses del entrenamiento, para no confundirlo con la estacionalidad.
    """
    desde = (pd.Timestamp(p.fin_entrenamiento) + pd.Timedelta(days=1)).date()
    hasta = (pd.Timestamp(p.ultima_fecha) + pd.Timedelta(days=1)).date()
    datos = variables(p, desde, hasta)
    datos["pred"] = p.modelo_retiros.predict(datos[p.meta["variables"]])
    datos["base"] = datos.ret_lag168.fillna(datos.ret_misma_hora_7d).fillna(0)
    datos["fecha"] = datos.ts.dt.floor("D")
    datos["mes"] = datos.ts.dt.strftime("%Y-%m")
    diario = datos.groupby(["station_id", "fecha"], as_index=False)[["retiros", "pred", "base"]].sum()

    por_mes = errores(diario, diario.fecha.dt.strftime("%Y-%m"))
    por_semana = errores(diario, diario.fecha.dt.to_period("W-SUN").dt.start_time)
    mes_referencia = f"{fin_referencia:%Y-%m}"
    peor = float(por_mes.loc[:mes_referencia, "wape"].max())

    with conexion() as con:
        conocidas = {e for (e,) in con.execute("SELECT DISTINCT station_id FROM read_parquet(?) WHERE split = 'train'",
                                               [str(p.dataset)]).fetchall()}
    meses = []
    for mes, fila in por_mes.iterrows():
        actual = datos[datos.mes == mes]
        inicio = pd.Timestamp(f"{mes}-01")
        anteriores = [variables(p, a.date(), (a + pd.offsets.MonthBegin(1)).date())
                      for a in (inicio - pd.DateOffset(years=n) for n in (1, 2)) if a.date() >= p.primera_fecha]
        antes = pd.concat(anteriores) if anteriores else None
        cambio = {v: round(psi(antes[v], actual[v]), 3) for v in VARIABLES_SEGUIDAS} if anteriores else {}
        resumen = {"mes": mes, "referencia": mes <= mes_referencia, **{k: round(float(v), 2) for k, v in fila.items()},
                   "estaciones_nuevas": int((~actual.station_id.drop_duplicates().isin(conocidas)).sum()),
                   "cambio": [{"variable": v, "psi": valor, "nivel": nivel_psi(valor), "es_clima": v in VARIABLES_CLIMA,
                               "faltantes_antes": round(100 * float(antes[v].isna().mean()), 1),
                               "faltantes": round(100 * float(actual[v].isna().mean()), 1)}
                              for v, valor in sorted(cambio.items(), key=lambda x: -x[1])]}
        resumen["avisos"] = [] if resumen["referencia"] else avisos(resumen, peor)
        meses.append(resumen)

    return {"calculado": pd.Timestamp.now().isoformat(timespec="seconds"), "fin_entrenamiento": str(p.fin_entrenamiento),
            "ultima_fecha": str(p.ultima_fecha), "modelo_entrenado": p.meta["entrenado"],
            "peor_wape_referencia": round(peor, 2), "meses": meses,
            "semanas": [{"semana": f"{s:%Y-%m-%d}", **{k: round(float(v), 2) for k, v in f.items()}} for s, f in por_semana.iterrows()]}
