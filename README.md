# Modelo predictivo de demanda de Ecobici (CABA)

Ciencia de Datos Aplicada (ITBA) · Segundo entregable: *Recopilación y preparación de datos*.

## Datos

1. Descargar **[datasets.zip](https://drive.google.com/file/d/1BlxhQnRVjPnbiqkNEjbbYYHRum74GdXF/view?usp=drive_link)**
2. Descomprimirlo en la raíz del repositorio. Tiene que quedar la carpeta `datasets/` al lado de `notebooks/`, con esta estructura:

```
datasets/
├── buenos-aires.zip                     # fotos del feed GBFS y pronósticos (o ya descomprimido como archive/)
└── ba-ecobici-datasets/
    ├── recorridos-realizados-2024.zip   # viajes de BA Data, un ZIP por año
    ├── recorridos-realizados-2025.zip
    ├── recorridos-realizados-2026.zip
    ├── ciclovias.csv
    └── usuarios_ecobici_AAAA.csv        # no se usan en los notebooks
```

No hace falta descomprimir los ZIP de adentro: el notebook 01 lo hace la primera vez que se corre (unos 2 GB en disco) y las siguientes corridas reutilizan lo extraído. Si los datos están en otra carpeta, se puede indicar con la variable de entorno `ECOBICI_DATA_DIR`.

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

Se pueden abrir con `jupyter lab` y ejecutar cada uno con *Run All*, o desde la terminal:

```bash
cd notebooks
jupyter nbconvert --to notebook --execute --inplace 01_adquisicion.ipynb 02_eda_calidad.ipynb 03_transformaciones.ipynb
```

Los tiempos son de una corrida en una Mac con 24 GB de RAM. El notebook 02 carga las 21 M de fotos en memoria: se recomiendan al menos 16 GB.

El 01 necesita conexión a internet para las APIs. Si la API de feriados no responde, `data/feriados.csv` ya está versionado en el repositorio.

## Fuentes

- Fotos del estado de las estaciones (GBFS): [MaxHalford/bike-sharing-history](https://github.com/MaxHalford/bike-sharing-history), licencia MIT.
- Viajes realizados y ciclovías: [BA Data](https://data.buenosaires.gob.ar), Gobierno de la Ciudad de Buenos Aires.
- Clima observado: [Open-Meteo Historical Weather API](https://open-meteo.com), licencia CC BY 4.0.
- Feriados: [ArgentinaDatos](https://api.argentinadatos.com).
