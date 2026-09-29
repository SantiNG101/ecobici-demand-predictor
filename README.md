# Ecobici: predicción de demanda por estación y hora

Segundo entregable de Ciencia de Datos Aplicada (ITBA): adquisición, análisis exploratorio, diagnóstico de calidad y transformación de los datos de Ecobici (Buenos Aires).

## Notebooks

Se corren en orden; cada uno usa lo que deja el anterior.

| Notebook | Contenido | Tiempo aprox. |
|---|---|---|
| [`01_adquisicion.ipynb`](notebooks/01_adquisicion.ipynb) | Descripción de las fuentes, carga con esquema unificado, clima y feriados por API. Genera `data/interim/` y `data/feriados.csv` | 1 min (más la descompresión inicial) |
| [`02_eda_calidad.ipynb`](notebooks/02_eda_calidad.ipynb) | Análisis exploratorio, faltantes, duplicados, outliers, visualizaciones e inventario de calidad | 2 min |
| [`03_transformaciones.ipynb`](notebooks/03_transformaciones.ipynb) | Limpieza, integración, nuevas variables, codificación, partición temporal, normalización y reflexión final. Genera `data/processed/` | 1 min |

Los tiempos son de una corrida en una Mac con 24 GB de RAM. El notebook 02 carga las 21 M de fotos en memoria: se recomiendan al menos 16 GB.

## Cómo correrlo

### 1. Datos

Descargar los datos desde **<LINK_A_LOS_DATOS>** y dejarlos en `datasets/`, en la raíz del repositorio, con esta estructura:

```
datasets/
├── buenos-aires.zip                     # fotos del feed GBFS y pronósticos de clima
└── ba-ecobici-datasets/
    ├── recorridos-realizados-2024.zip   # viajes de BA Data, un ZIP por año
    ├── recorridos-realizados-2025.zip
    ├── recorridos-realizados-2026.zip
    └── ciclovias.csv
```

No hace falta descomprimir nada: el notebook 01 descomprime los ZIP la primera vez que se corre (unos 2 GB en disco) y las siguientes corridas reutilizan lo extraído. Si los datos están en otra carpeta, se puede indicar con la variable de entorno `ECOBICI_DATA_DIR`.

### 2. Entorno

Con Python 3.11 a 3.13:

```bash
python -m venv .venv
source .venv/bin/activate        # en Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Ejecución

```bash
jupyter lab
```

Abrir los notebooks de `notebooks/` y ejecutarlos en orden (01 → 02 → 03) con *Run All*. O bien, desde la terminal:

```bash
cd notebooks
jupyter nbconvert --to notebook --execute --inplace 01_adquisicion.ipynb 02_eda_calidad.ipynb 03_transformaciones.ipynb
```

El notebook 01 necesita conexión a internet para consultar las APIs de Open-Meteo (clima) y ArgentinaDatos (feriados). Si la API de feriados no responde, `data/feriados.csv` ya está versionado en el repositorio.

## Fuentes

- Fotos del estado de las estaciones (GBFS): [MaxHalford/bike-sharing-history](https://github.com/MaxHalford/bike-sharing-history), licencia MIT.
- Viajes realizados y ciclovías: [BA Data](https://data.buenosaires.gob.ar), Gobierno de la Ciudad de Buenos Aires.
- Clima observado: [Open-Meteo Historical Weather API](https://open-meteo.com), licencia CC BY 4.0.
- Feriados: [ArgentinaDatos](https://api.argentinadatos.com).
