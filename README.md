# Modelo predictivo de demanda de Ecobici (CABA)

Ciencia de Datos Aplicada (ITBA) · Segundo entregable: *Recopilación y preparación de datos*.

## Datos

1. Descargar **[datasets.zip](https://drive.google.com/file/d/1BlxhQnRVjPnbiqkNEjbbYYHRum74GdXF/view?usp=drive_link)**
2. Descomprimirlo en la raíz del repositorio. Tiene que quedar la carpeta `datasets/` al lado de `notebooks/`.

## Instalación

```bash
python -m venv .venv
.venv\Scripts\activate          # en Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt
```

## Notebooks

Correrlos en orden, porque cada uno usa lo que genera el anterior.

| Notebook | Qué hace | Genera |
|---|---|---|
| `01_adquisicion` | Describe cada fuente (origen, formato y variables) y la carga: fotos de estaciones, viajes 2024–2026, clima (API de Open-Meteo) y feriados (API de ArgentinaDatos). | `data/interim/` y `data/feriados.csv` |
| `02_eda_calidad` | Análisis exploratorio (tipos, faltantes, duplicados, outliers y gráficos), inventario de calidad y confiabilidad del target. | Sólo gráficos y tablas |
| `03_transformaciones` | Limpieza, integración, nuevas variables, codificación, partición, normalización y reflexión final. | `data/processed/dataset_estacion_hora.parquet` |

El 01 necesita conexión a internet para las APIs.

