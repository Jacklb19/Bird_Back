# BirdNet Local — API

API de BirdNet Local: FastAPI desplegada como función Python en Vercel, con PostgreSQL/PostGIS de Supabase y seguridad a nivel de fila. La aplicación web vive en el repositorio [BirdNet](https://github.com/Jacklb19/BirdNet) y reenvía `/api/*` a este servicio.

## Endpoints

| Método y ruta | Propósito | Sesión |
|---|---|---|
| `GET /v1/health` | Estado del servicio | No |
| `GET /v1/model/latest` | Manifiesto vigente del modelo acústico | No |
| `POST /v1/detections/batch` | Sincronización idempotente de la cola local | Sí |
| `POST /v1/detections/{id}/audio-url` | URL firmada para subir audio dudoso a Storage | Sí |
| `GET /v1/detections` | Detecciones del mapa por área, especie y periodo | Sí |

## Estructura

- `birdnet_api/`: aplicación (rutas, autenticación JWT, contratos, repositorio y Storage).
- `api/index.py`: punto de entrada de la función de Vercel.
- `supabase/migrations/`: esquema de la base de datos (ejecutar en orden en el SQL Editor de Supabase).
- `tests/`: pruebas con pytest; las de PostGIS usan una base local en Docker.
- `scripts/lock-python.py`: regenera `requirements.txt` y `requirements-dev.lock.txt` desde `requirements.in`.

## Desarrollo local

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest
```

Pruebas contra PostGIS real (opcional):

```bash
docker run --name birdnet-test --rm -d -e POSTGRES_HOST_AUTH_METHOD=trust -p 127.0.0.1:55434:5432 postgis/postgis@sha256:94146ac37bc61e2322f88016056c5920729cb8c64c8542ed590af8fc2abdac07
.venv/Scripts/python tests/prepare_database.py
BIRDNET_TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:55434/birdnet_s4_test .venv/Scripts/python -m pytest
```

## Despliegue

Proyecto de Vercel con este repositorio como raíz. Variables necesarias: ver `.env.example` (`DATABASE_URL`, `SUPABASE_AUTH_ISSUER`, `MODEL_RESOURCE_BASE_URL` como mínimo).
