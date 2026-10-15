# Trino — API

API de Trino (antes BirdNet Local): FastAPI desplegada como función Python en Vercel, con PostgreSQL/PostGIS de Supabase y seguridad a nivel de fila. La aplicación web vive en el repositorio [BirdNet](https://github.com/Jacklb19/BirdNet) y reenvía `/api/*` a este servicio.

## Endpoints

| Método y ruta | Propósito | Sesión |
|---|---|---|
| `GET /v1/health` | Estado del servicio | No |
| `GET /v1/model/latest` | Manifiesto vigente del modelo acústico | No |
| `POST /v1/detections/batch` | Sincronización idempotente de la cola local; `shared` (por defecto `true`) indica si el canto se comparte en el mapa de todos | Sí |
| `PATCH /v1/detections/{id}` | Revisión de una detección propia: `{"action": "confirm" \| "correct" \| "discard", "species"?}` (la especie, solo al corregir). Responde la especie vigente, la que dio el modelo si se corrigió, el estado y quién revisó | Sí |
| `POST /v1/detections/{id}/recording-url` | URL firmada para subir la grabación de una detección propia (WebM, máx. 100 000 bytes) al bucket privado; 507 si la cuenta ya llenó su tope | Sí |
| `PUT /v1/detections/{id}/recording` | Enlaza la grabación subida tras verificar su ruta y contenido (`{"recording_path"}`); responde el uso de la cuenta | Sí |
| `GET /v1/detections/{id}/recording` | URL firmada temporal (10 min) para oír la grabación; solo su autor | Sí |
| `DELETE /v1/detections/{id}/recording` | Borra la grabación de Storage y su enlace | Sí |
| `GET /v1/me/recordings`, `DELETE /v1/me/recordings` | Uso (`recordings`, `bytes`, `limit_bytes`) y borrado de todas las grabaciones propias | Sí |
| `GET /v1/detections` | Detecciones del mapa por área, especie y periodo: las compartidas por otras personas y todas las propias; `own` marca las propias y `site_name` solo se llena en ellas | Sí |
| `GET /v1/me` | Perfil propio: alias, URL firmada temporal de la foto y fecha de creación | Sí |
| `PATCH /v1/me` | Cambia alias o foto (campos omitidos se conservan; `null` borra) | Sí |
| `POST /v1/me/avatar-url` | URL firmada para subir la foto (WebP, máx. 200 000 bytes) al bucket privado | Sí |
| `POST /v1/me/sharing` | Comparte o deja de compartir todas las detecciones propias ya subidas (`{"shared": bool}` → `{"updated": n}`, las que cambiaron) | Sí |
| `GET /v1/me/summary?tz=` | Totales propios sin descartadas: detecciones, especies, sitios, primera y última, días activos | Sí |
| `GET /v1/me/species` | Especies propias con detecciones, mejor confianza, primera y última y sitios | Sí |
| `GET /v1/me/species/{species}?tz=` | Ficha propia de una especie: horas, sitios, celdas de ~10 m y últimas detecciones (vacía si no hay) | Sí |
| `GET /v1/sites`, `POST /v1/sites` | Sitios de monitoreo propios | Sí |
| `GET /v1/sites/{id}/stats?period=&tz=` | Estadísticas deterministas de un sitio | Sí |
| `PUT /v1/walks/{id}` | Guarda una caminata terminada (`name` opcional, `started_at`, `ended_at`, `track` con celdas `[lat, lon]` o `[lat, lon, segundos]`, hasta 2000). Idempotente con el UUID del teléfono; responde el resumen. 409 si el identificador es de otra cuenta, 507 si la cuenta llenó su tope | Sí |
| `PATCH /v1/walks/{id}` | Cambia el nombre (`{"name"}`; `null` vuelve al que la app arma con la fecha) | Sí |
| `GET /v1/walks?before=&limit=` | Caminatas propias, de la más reciente a la más antigua, con distancia, puntos y especies; `has_more` indica si hay otra página | Sí |
| `GET /v1/walks/{id}` | Una caminata con su recorrido y los lugares donde se oyó cada especie (celdas de ~10 m) | Sí |
| `DELETE /v1/walks/{id}` | Borra la caminata (sus cantos quedan); repetirlo no falla | Sí |
| `GET /v1/me/walks`, `DELETE /v1/me/walks` | Uso (`walks`, `limit`) y borrado de todas las caminatas propias | Sí |
| `GET /v1/export?site_id=` o `?walk_id=` | Exportación CSV de un sitio, de una caminata o, sin ninguno de los dos, de todo el registro propio | Sí |

### Ubicación y mapa compartido

- Las coordenadas llegan ya redondeadas a una cuadrícula de 4 decimales (~10 m; `LOCATION_GRID_DECIMALS`); con más decimales la API responde 422 y la base de datos vuelve a redondear por si acaso. Los valores de 3 decimales (~100 m) de clientes y filas anteriores siguen siendo válidos.
- Cada persona decide si sus cantos aparecen en el mapa de todos (`detections.compartida`, por defecto sí). Una detección no compartida solo la lee su autor: lo imponen la consulta del mapa y la política RLS `detections_select_shared_or_own`. Su registro personal, las estadísticas del sitio y la exportación no cambian.
- Reintentar la sincronización no modifica una detección ya guardada; para cambiar lo ya subido se usa `POST /v1/me/sharing`.

### Revisión y grabaciones

- Confirmar deja la detección como `verified`; corregir, como `corrected` con la especie nueva en `especie` y la del modelo en `especie_modelo`; descartar, como `discarded`. Corregir de vuelta a la especie del modelo equivale a confirmar. `revisada_por` registra quién revisó: hoy, siempre la persona. Reintentar la sincronización tras una corrección no es un conflicto: el lote se compara con la especie del modelo.
- Las grabaciones son privadas: se suben y se oyen solo con URLs firmadas para su autor, y nunca aparecen en el mapa. El tope por cuenta (`RECORDING_QUOTA_BYTES`) se comprueba al pedir la subida y otra vez al enlazarla.
- El CSV añade al final `model_species` y `reviewed_by`; las descartadas se exportan con su estado y las estadísticas del sitio las excluyen.

### Caminatas

- Solo se suben si la persona eligió guardarlas en su cuenta (ADR-35). Son privadas: RLS solo del dueño y ninguna consulta del mapa las lee.
- El recorrido solo admite celdas de la cuadrícula de ~10 m; la distancia la calcula la API con la misma fórmula que el teléfono.
- Las especies no se copian: son las detecciones propias entre el inicio y el fin, sin las descartadas y con las correcciones aplicadas, así que una revisión posterior cambia también la caminata. El tope por cuenta (`WALK_QUOTA`) solo rechaza caminatas nuevas.

## Estructura

- `birdnet_api/`: aplicación (rutas, autenticación JWT, contratos, repositorios y Storage).
  - `profiles.py`: perfil propio; `records.py`: registro personal (resumen y especies); `recordings.py`: grabaciones guardadas y su tope; `sites.py`: sitios y estadísticas; `walks.py`: caminatas guardadas; `exports.py`: CSV.
  - `storage.py`: URLs firmadas de subida y descarga con la clave de servicio y verificación del contenido subido (foto WebP, grabación WebM) y borrado de grabaciones.
  - `settings.py`: configuración leída del entorno una sola vez y validada.
  - `domain.py`: reglas de negocio (umbrales, cuadrícula, límites, estados, periodos, ventana de captura, columnas del CSV); el cliente las replica en `src/config/contract.ts`.
  - `errors.py`: catálogo de errores con su código HTTP y mensaje.
  - `manifest.py`: carga del manifiesto del modelo (local o remoto desde los hosts permitidos).
- `api/index.py`: punto de entrada de la función de Vercel.
- `supabase/migrations/`: esquema de la base de datos (ejecutar en orden en el SQL Editor de Supabase).
- `tests/`: pruebas con pytest; las de PostGIS usan una base local en Docker.
- `scripts/lock-python.py`: regenera `requirements.txt` y `requirements-dev.lock.txt` desde `requirements.in` y `requirements-dev.txt`.

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

## Configuración

`birdnet_api/settings.py` lee las variables una vez por proceso. Las de una función opcional (base de datos, autenticación, Storage, manifiesto remoto) pueden faltar: el endpoint que las necesita responde 503 «no configurado». Un valor mal formado detiene el arranque con un mensaje que nombra la variable y, si llega a leerse en una petición, se responde 503 y se registra el mensaje.

| Variable | Uso | Por defecto |
|---|---|---|
| `DATABASE_URL` | Cadena del Transaction pooler de Supabase | — (503 al usar la base) |
| `DATABASE_CONNECT_TIMEOUT_SECONDS` | Espera al conectar con la base (entero) | `10` |
| `SUPABASE_AUTH_ISSUER` | Emisor de los tokens de sesión | — (503 al autenticar) |
| `SUPABASE_JWT_SECRET` | Secreto de tokens HS256 heredados | — (solo JWKS) |
| `JWKS_TIMEOUT_SECONDS` | Espera al descargar las claves públicas del emisor | `10` |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` | Acceso a Storage con la clave de servicio (fotos de perfil y grabaciones guardadas) | — (503 al subir; perfil sin foto) |
| `SUPABASE_AVATAR_BUCKET` | Bucket privado de las fotos de perfil (lo crea la migración `20261008000000`) | `avatars` |
| `SUPABASE_RECORDING_BUCKET` | Bucket privado de las grabaciones guardadas (lo crea la migración `20261011000000`) | `recordings` |
| `RECORDING_QUOTA_BYTES` | Tope de grabaciones por cuenta | `52428800` (50 MiB) |
| `WALK_QUOTA` | Tope de caminatas guardadas por cuenta | `200` |
| `STORAGE_TIMEOUT_SECONDS` | Espera de cada petición a Storage | `20` |
| `MODEL_MANIFEST_PATH` | Manifiesto local del modelo | `birdnet_api/model_manifest.json` |
| `MODEL_MANIFEST_URL` | Manifiesto remoto (solo `https`); sustituye al local | — |
| `MODEL_MANIFEST_ALLOWED_HOSTS` | Hosts permitidos para `MODEL_MANIFEST_URL`, separados por comas | Host de `SUPABASE_URL` o, si falta, el de `MODEL_MANIFEST_URL` |
| `MODEL_MANIFEST_TIMEOUT_SECONDS` | Espera al descargar el manifiesto remoto | `10` |
| `MAX_MANIFEST_BYTES` | Tamaño máximo del manifiesto remoto | `131072` |
| `MODEL_RESOURCE_BASE_URL` | Base de `model_file` y `labels_file` relativos | `/models/` |
| `MAX_METADATA_BODY_BYTES` | Tamaño máximo del cuerpo de un `POST`, `PATCH` o `PUT` (413 si se supera); como máximo 4 500 000, el límite de Vercel | `131072` |
| `MAP_RESULT_LIMIT` | Detecciones por respuesta del mapa (`truncated` indica el corte) | `2000` |
| `EXPORT_ROW_LIMIT` | Filas por exportación CSV (cabecera `X-Truncated` indica el corte) | `20000` |
| `DEFAULT_TIME_ZONE` | Zona horaria IANA de las estadísticas y del registro personal si el cliente no envía `tz` | `America/Bogota` |
