# Modelo predictivo de demanda de Ecobici (CABA)

Ciencia de Datos Aplicada (ITBA) · Segundo entregable: *Recopilación y preparación de datos* · Tercer entregable: *Modelado de la solución* · Cuarto entregable: *Despliegue*.

## Datos

1. Descargar **[datasets.zip](https://drive.google.com/file/d/1BlxhQnRVjPnbiqkNEjbbYYHRum74GdXF/view?usp=drive_link)**
2. Descomprimirlo en la raíz del repositorio. Tiene que quedar la carpeta `datasets/` al lado de `notebooks/`, con esta estructura:

```
datasets/
├── archive/
│   ├── stations/buenos-aires/ecobici/2024, 2025, 2026/   # fotos del feed GBFS (parquet mensuales)
│   └── weather/buenos-aires/2024, 2025, 2026/            # pronósticos de clima (parquet mensuales)
└── ba_data/
    ├── recorridos-realizados-AAAA/                       # viajes de BA Data, un CSV por año (2024, 2025, 2026)
    └── ciclovias.csv
```

Si los viajes de algún año vienen como `recorridos-realizados-AAAA.zip`, no hace falta descomprimirlos: el notebook 01 lo hace la primera vez que se corre (unos 2 GB en disco) y las siguientes corridas reutilizan lo extraído. Si los datos están en otra carpeta, se puede indicar con la variable de entorno `ECOBICI_DATA_DIR`.

## Instalación

Con Python 3.11 a 3.13:

```bash
python -m venv .venv
.venv\Scripts\activate          # en Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt
```

## Notebooks

Correrlos en orden, porque cada uno usa lo que genera el anterior.

| Notebook | Qué hace | Genera | Tiempo aprox. |
|---|---|---|---|
| [`01_adquisicion`](notebooks/01_adquisicion.ipynb) | Describe cada fuente (origen, formato y variables) y la carga: fotos de estaciones, viajes 2024–2026, clima (API de Open-Meteo) y feriados (API de ArgentinaDatos). | `data/interim/` y `data/feriados.csv` | 1 min, más la descompresión inicial |
| [`02_eda_calidad`](notebooks/02_eda_calidad.ipynb) | Análisis exploratorio (tipos, faltantes, duplicados, outliers y gráficos), inventario de calidad y confiabilidad del target. | Sólo gráficos y tablas | 2 min |
| [`03_transformaciones`](notebooks/03_transformaciones.ipynb) | Limpieza, integración, nuevas variables, codificación, partición, normalización y reflexión final. | `data/processed/dataset_estacion_hora.parquet` | 1 min |
| [`04_modelado`](notebooks/04_modelado.ipynb) | Variables con rezago, baselines, regresión de Poisson, gradient boosting (búsqueda de hiperparámetros y validación cruzada temporal), evaluación en test, agrupamiento de estaciones, rebalanceo y ampliación, y persistencia. Incluye las respuestas a la devolución del segundo entregable: aporte de las variables de estado con buena cobertura de fotos, clima pronosticado y censura medida al inicio de la hora (lee también las fotos y los pronósticos crudos de `datasets/archive/`). | `models/` | 1 h 36 min con 16 GB de RAM (sin las secciones nuevas, 20 min en la Mac) |
| [`05_evaluacion`](notebooks/05_evaluacion.ipynb) | Responde la devolución del tercer entregable con los modelos guardados, sin reentrenar: simulación del impacto operativo en el test (estaciones vacías o llenas evitadas contra la media histórica), error por grupo, zona y tipo de día, e interpretación con SHAP. Lee también `data/interim/viajes.parquet` y las fotos crudas. | Sólo gráficos y tablas | 3 min |

Se pueden abrir con `jupyter lab` y ejecutar cada uno con *Run All*, o desde la terminal:

```bash
cd notebooks
jupyter nbconvert --to notebook --execute --inplace 01_adquisicion.ipynb 02_eda_calidad.ipynb 03_transformaciones.ipynb 04_modelado.ipynb 05_evaluacion.ipynb
```

## Modelos

El notebook 04 guarda en `models/` los modelos entrenados (`.joblib`), los grupos de estaciones y un `metadata.json` con las variables, los hiperparámetros, las métricas de test y la consulta SQL que arma las variables. La función `predecir_dia` del final del notebook muestra cómo cargarlos y predecir un día sin reentrenar.

Los `.joblib` de scikit-learn sólo se garantizan con la misma versión con la que se guardaron (está en `metadata.json`). Con otra versión, volver a correr el notebook 04.

Los tiempos de los notebooks 01 a 03 son de una corrida en una Mac con 24 GB de RAM. El notebook 02 carga las 21 M de fotos en memoria: se recomiendan al menos 16 GB. El 04 se corrió completo en una notebook con 16 GB de RAM, donde el sistema usa el disco como memoria y tarda bastante más.

El 01 necesita conexión a internet para las APIs. Si la API de feriados no responde, `data/feriados.csv` ya está versionado en el repositorio.

## Despliegue (cuarto entregable)

Caso de uso: el equipo de logística decide, antes de una franja pico, qué estaciones **abastecer** (se van a vaciar) y cuáles **descargar** (se van a llenar). Es la lista de la sección 12.1 del notebook 04, ahora servida por una API y un tablero.

```
interfaz/app.py (Streamlit) --HTTP--> api/main.py (FastAPI) --> src/ecobici/modelo.py --> models/ + data/processed/
```

- `src/ecobici/modelo.py`: carga los modelos y predice. No depende de FastAPI. Arma las variables con la consulta SQL guardada en `models/metadata.json`, la misma con la que se entrenó.
- `api/main.py`: valida las entradas, llama a `src/ecobici` y traduce los errores a códigos HTTP. Carga los modelos una sola vez, al arrancar.
- `interfaz/app.py`: sólo habla con la API, por HTTP.

### Cómo correrlo

Hacen falta `models/` (versionado) y `data/processed/` (lo genera el notebook 03).

En local, con las dependencias de `requirements.txt` instaladas y desde la raíz del repositorio:

```bash
uvicorn api.main:app --reload        # API en http://localhost:8000 (documentación en /docs)
streamlit run interfaz/app.py        # tablero en http://localhost:8501, en otra terminal
python -m pytest tests -q            # tests contra los modelos y el dataset reales
```

Con Docker, la misma imagen corre los dos servicios y monta `models/` y `data/processed/` como volúmenes de sólo lectura (el dataset pesa 434 MB y no entra en la imagen):

```bash
docker compose up --build
```

Variables de entorno: `ECOBICI_MODELS_DIR` (por defecto `models`), `ECOBICI_DATA_DIR` (por defecto `data/processed`, con `dataset_estacion_hora.parquet` y `estaciones.csv`), `ECOBICI_LOGS_DIR` (por defecto `logs`) y `ECOBICI_API_URL` para la interfaz (por defecto `http://localhost:8000`).

### Actualizar los datos

```bash
python scripts/actualizar_datos.py     # opciones: --solo-descargar, --sin-descargar, --fecha-fin AAAA-MM-DD
```

El script extiende el dataset sin reentrenar:

1. Baja los viajes de BA Data del año en curso y del anterior, y las fotos de los meses nuevos de bike-sharing-history. Sólo baja lo que cambió: guarda el tamaño y la fecha de cada archivo en `datasets/descargas.json`.
2. Toma como fin el último día completo de viajes.
3. Respalda `data/processed/` en `data/processed.anterior/` y ejecuta los notebooks 01 y 03 con `ECOBICI_FECHA_FIN`. Las copias ejecutadas quedan en `data/interim/notebooks_ejecutados/` y los notebooks del repositorio no cambian.
4. Verifica que el dataset termine en esa fecha, que tenga más filas y que los viajes del 02/06/2026 sigan iguales.

El 21/11/2026 llevó el dataset del 31/08 al 30/09/2026 en 4 min 20 s. Las descargas tardaron 3 min (355 MB, contando el zip de 2025, que se baja una sola vez) y los notebooks 74 s. Una segunda corrida no baja nada y termina en 3 s. La API lee el rango de fechas al arrancar, así que después hay que reiniciarla.

BA Data publica los viajes con demora: el zip de 2026 se actualizó el 07/10 con viajes hasta el 30/09. Con esta fuente no se llega a predecir el día siguiente. Para eso hace falta la grilla en vivo que se describe más abajo.

### Endpoints

| Método y ruta | Entrada | Salida |
|---|---|---|
| `GET /salud` | — | Estado, fecha de entrenamiento, rango de fechas que se pueden predecir y último día que vio el modelo al entrenar |
| `GET /modelo` | — | Descripción, horizonte, variables, métricas de test, clima esperado y versiones |
| `GET /estaciones` | — | Id, nombre, latitud, longitud y grupo de cada estación |
| `GET /monitoreo/servicio` | `dias` (1 a 30), `ultimos` | Pedidos, errores y latencia por ruta, y los últimos pedidos |
| `GET /monitoreo/modelo` | — | Error por mes y semana desde el entrenamiento, cambio en los datos y avisos |
| `POST /prediccion/casos` | `casos`: de 1 a 1000 estaciones-hora con la ubicación, la fecha, el clima y la demanda reciente | Retiros y arribos esperados y flujo neto de cada caso |
| `GET /estaciones/{id}/caso` | `fecha`, `hora` | Un caso registrado, listo para editar y mandar a `/prediccion/casos` |
| `POST /prediccion` | `fecha`, `clima` (opcional), `station_ids` (opcional) | Retiros y arribos predichos y reales por estación y hora, y los totales del día |
| `POST /rebalanceo` | `fecha`, `franja` (`manana` o `tarde`), `top` (1 a 50, por defecto 15), `clima` (opcional) | Las dos listas ordenadas con el flujo neto predicho y el real, la precisión contra lo que pasó, la precisión promedio de la franja y si el día es de entrenamiento |

Ejemplo:

```bash
curl -X POST localhost:8000/rebalanceo -H "content-type: application/json" \
  -d '{"fecha": "2026-06-02", "franja": "manana", "top": 15}'
```

```json
{
  "fecha": "2026-06-02", "franja": "manana", "horas": [6, 7, 8, 9],
  "abastecer": [{"station_id": 175, "nombre": "147 - Constitución", "nombre_grupo": "Residencial (origen a la mañana)",
                 "lat": -34.6269, "lon": -58.3807, "flujo_predicho": -18.97, "flujo_real": -37}, "..."],
  "descargar": [{"station_id": 26, "nombre": "026 - JUANA MANSO I", "flujo_predicho": 19.44, "flujo_real": 19, "...": "..."}],
  "precision_abastecer": 0.2, "precision_descargar": 0.533,
  "promedio_abastecer": 0.3763, "promedio_descargar": 0.4968, "en_entrenamiento": false
}
```

**Datos nuevos.** `POST /prediccion/casos` predice con los datos que manda el usuario y no lee el dataset: acepta una fecha futura, otro clima, otra demanda o una estación que todavía no existe (`lat` y `lon` en lugar de `station_id`). El estado de la estación el día anterior es opcional.

```bash
curl -X POST localhost:8000/prediccion/casos -H "content-type: application/json" -d '{"casos": [{
  "station_id": 175, "fecha": "2026-12-01", "hora": 8,
  "clima": {"temperatura_c": 22, "precipitacion_mm": 0, "viento_kmh": 10},
  "retiros": {"ayer_misma_hora": 6, "semana_anterior_misma_hora": 6, "promedio_7d_misma_hora": 5.4, "total_dia_anterior": 45, "promedio_diario_28d": 50},
  "arribos": {"ayer_misma_hora": 2, "semana_anterior_misma_hora": 3, "promedio_7d_misma_hora": 1.6, "total_dia_anterior": 40, "promedio_diario_28d": 53}}]}'
```

```json
{"resultados": [{"station_id": 175, "fecha": "2026-12-01", "hora": 8,
  "pred_retiros": 6.08, "pred_arribos": 1.89, "flujo_neto": -4.19}]}
```

`clima` tiene `temperatura_c`, `precipitacion_mm` (lluvia por hora) y `viento_kmh`, y reemplaza el clima registrado en las 24 horas del día. Sirve para cargar un pronóstico o probar escenarios. Errores: un cuerpo inválido devuelve 422, y una fecha fuera del rango, una estación desconocida o un caso sin datos devuelven 404 con el motivo. Si faltan los modelos o el dataset, la API no arranca y el error dice qué archivo falta.

### Registro y monitoreo

**Registro de pedidos.** La API guarda una línea JSON por pedido en `logs/api-AAAA-MM-DD.jsonl`: momento, ruta, datos del pedido, resumen de la respuesta, estado, duración y el motivo si falló. Cada respuesta lleva su identificador en la cabecera `X-Pedido-Id`. No guarda la IP ni las cabeceras, no registra `/salud`, y borra los archivos de más de 30 días. Si no puede escribir, la API responde igual. La carpeta se cambia con `ECOBICI_LOGS_DIR`.

**Monitoreo del modelo.** `python scripts/monitorear_modelo.py` (unos 100 s) compara las predicciones con los viajes reales desde que terminó el entrenamiento y guarda `models/monitoreo.json`, que se ve en la pestaña **Monitoreo**. Se corre después de actualizar los datos:

- **Error por mes y semana**, al lado del de repetir la semana anterior.
- **Cambio en los datos (PSI):** cada mes contra los mismos meses del entrenamiento, para no confundirlo con la estacionalidad.
- **Avisos** en los meses posteriores al test: si el error supera en más de 10 % al peor mes del test, si el total se aparta más de 10 %, o si no le gana a la semana anterior. Con avisos, el script termina con código 1.

Con los datos hasta el 30/09/2026:

| Mes | Error del modelo | Semana anterior | Sesgo |
|---|---|---|---|
| Junio a agosto (test) | 29,2 · 30,7 · 27,4 % | 44,4 · 50,4 · 43,4 % | −0,3 · +1,0 · +0,6 % |
| **Septiembre (no visto antes)** | **20,5 %** | 28,1 % | +3,7 % |

Septiembre es el primer mes fuera del invierno: el error baja con el aumento de los viajes y queda cerca del de la validación cruzada (23,0 %). El cambio alto de las variables de estado en junio a agosto es porque los inviernos anteriores tienen huecos de fotos (35 a 65 % sin dato), no porque cambiara la red. El viento medido baja de forma sostenida desde diciembre de 2024 (unos 15 a 11 km/h de promedio): puede ser un cambio de la fuente y no se verificó.

Los umbrales son una elección nuestra y son fáciles de cambiar en `src/ecobici/monitoreo.py`. El monitoreo usa clima observado, como el entrenamiento. Las rutas de monitoreo no piden contraseña: si la API se publica, conviene cerrarlas.

### Demo

```bash
python scripts/demo.py     # opciones: --local (sin Docker), --solo-verificar, --sin-navegador
```

Levanta la API y el tablero, espera que respondan, verifica cada paso del recorrido y muestra los números que se van a ver: el plan de un día, datos nuevos con lluvia o feriado, casos límite y monitoreo. Si algo falla, dice qué.

### Decisiones y límites

- **El dataset de la entrega termina el 31/08/2026**, así que el servicio predice días entre el 30/01/2024 y el 31/08/2026, o hasta donde lo extienda `scripts/actualizar_datos.py`. Cada día necesita sus 29 días previos. Como los días ya pasaron, la respuesta trae también lo que pasó, y el tablero muestra cuánto acertó el modelo.
- **En producción** la grilla del día siguiente se armaría con las fotos en vivo del feed GBFS, los viajes del día anterior y el pronóstico del clima. La consulta SQL y los modelos no cambian. Hoy ese pronóstico se puede cargar a mano con `clima`.
- **Las variables se arman con la misma consulta del entrenamiento**, leída de `metadata.json`. Se le pasa sólo el tramo del dataset que necesita (los 29 días previos y el día), lo que da las mismas variables y lleva una predicción de 24 s a menos de medio segundo.
- **Las listas coinciden con el ejemplo de la sección 12.1** para la mañana del 02/06/2026. Los tests lo verifican. Una predicción o un rebalanceo tarda 0,4 s.
- **Los aciertos se miden sólo en días que el modelo no vio.** Se entrenó hasta el 31/05/2026, así que hasta ese día el acierto sale inflado: la API lo marca con `en_entrenamiento` y el tablero avisa y no lo muestra.
- **El acierto de un día se compara con el promedio** de los 62 días hábiles de junio a agosto de 2026. Lo calcula `python scripts/evaluar_rebalanceo.py`, que guarda `models/precision_rebalanceo.json`. Si se reentrena el modelo, hay que volver a correrlo. Sin ese archivo la API funciona igual y el promedio queda en `null`.
- **Para ordenar estaciones, el modelo empata con la media histórica** (sección 12.1 del notebook 04). Su aporte está en estimar cuántas bicis mover.

## Fuentes

- Fotos del estado de las estaciones (GBFS): [MaxHalford/bike-sharing-history](https://github.com/MaxHalford/bike-sharing-history), licencia MIT.
- Viajes realizados y ciclovías: [BA Data](https://data.buenosaires.gob.ar), Gobierno de la Ciudad de Buenos Aires.
- Clima observado: [Open-Meteo Historical Weather API](https://open-meteo.com), licencia CC BY 4.0.
- Feriados: [ArgentinaDatos](https://api.argentinadatos.com).
