# Cassandra's Hoard

Observabilidad de todo el stack de IA local de tu PC. Vigila cada servicio local que usas — el espacio de trabajo Faustus y sus instancias de prueba, llama-server, Ollama, ComfyUI, el lanzador Hoard Hub y cada app Hoard — y recuerda **qué está activo, qué se cayó y cuándo, qué más cambió en ese mismo momento, qué decían los logs y qué hacían las GPU**. Cuando algo se para a las 04:00 y nadie sabe por qué, Cassandra tiene la respuesta (o la más probable). Un asistente accede a los mismos datos por MCP, así que basta con preguntar "¿por qué se paró Borges anoche?".

Todo se queda en el equipo: un único archivo SQLite, sin cuentas, sin telemetría, sin más red que las comprobaciones de salud en loopback de tus propios servicios.

Forma parte de la familia Hoard (ver `faustus-plugin.json`).

## Qué hace

- **Registro de servicios**, unión de tres fuentes:
  - **apps descubiertas**: cada `<raíz>/*/faustus-plugin.json` (el mismo manifiesto que lee el lanzador). `app.health` da la ruta de salud y el `service` esperado; `data/url` en la carpeta de la app manda sobre el puerto del manifiesto;
  - **externos conocidos**: Faustus (`7000` principal, `7001-7003` pruebas, `/api/health`), llama-server (`8081`, ayudante `8082`, `/health`), Ollama (`11434`, `/api/tags`), ComfyUI (`8188-8191`, `/system_stats`) y Hoard Hub (`8810`). Un externo que nunca respondió aparece como **nunca visto**, nunca como incidencia;
  - **tus servicios** en `data/services.json` (id, nombre, url, ruta de salud, JSON esperado, rutas de log, política de reinicio). Una entrada con el id de una app descubierta o de un externo la edita.
- **Sondeo**: cada `CASSANDRA_POLL_S` segundos (20 por defecto) se comprueban todos los servicios en paralelo con tiempos cortos. Primero se leen los puertos en escucha (psutil), así un puerto cerrado es `down` al instante (en Windows conectar a un puerto loopback cerrado tarda ~1,5 s en fallar). Estados: `up`, `degraded` (error HTTP o más lento que `CASSANDRA_SLOW_MS`), `foreign` (otro programa responde en ese puerto), `down`. Se guarda el pid de cada puerto, su hora de arranque y un hash de su línea de órdenes: un pid nuevo en un servicio que siguió activo es un **evento de reinicio**.
- **Incidencias**: se abren cuando un servicio pasa de activo a caído/otro programa (o se sustituye su proceso) y se cierran cuando vuelve a responder. Al abrirse se captura un **contexto**: los demás servicios que cambiaron en ±3 min (lo completan los sondeos siguientes), la memoria de GPU justo antes y el pico de los últimos 3 min, las últimas líneas de los logs del servicio, el proceso (pid, línea de órdenes, vivo o desaparecido) y la hora de arranque del equipo. Con reglas heurísticas se redacta una **causa probable**, por ejemplo:
  - "The machine restarted (boot at 03:58…): a reboot stops every service."
  - "Cassandra itself was not running between 01:10 and 07:30 (sleep, hibernation or shutdown?)…"
  - "3 services fell in the same minute (…) → machine-wide event (sleep, shutdown, GPU driver reset?)."
  - "GPU 1 memory jumped to 98% 40 s before → VRAM contention (another model loading?)."
  - "The log ends with a Traceback: “RuntimeError: …”."
  - "The process (pid 1234 python.exe) is still alive but does not answer: hung, overloaded or still loading."
- **Pseudo-servicio del sistema**: hora de arranque y tiempo encendido; un cambio de la hora de arranque se registra como `reboot`, y una pausa larga del propio bucle de Cassandra (suspensión, hibernación, app cerrada) como `gap`, que se ve rayado en las franjas.
- **GPU**: `nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu` en cada sondeo (se omite en silencio si no hay driver de NVIDIA).
- **Logs**: lectura incremental de `data/logs/*.log` de cada app, de `data/logs/<app>.log` del lanzador (la salida de las apps que arrancó), de `%LOCALAPPDATA%\Hoards\*.log`, de `data/logs/<id>.log` de Cassandra (apps que reinició), de los `log_paths` de tus servicios y de `CASSANDRA_LOG_GLOBS`. Un archivo nuevo se lee desde sus últimos 64 KB; se detecta la rotación (un archivo que encoge). Cada línea recibe una hora (la suya si la trae) y un nivel (error/warning/info/debug). Retención: `CASSANDRA_RETENTION_DAYS` (14) y `CASSANDRA_LOG_MAX_LINES`.
- **Auditoría** (Hoard Link 0.4): el Hoard Hub mantiene un bus de eventos al que escriben todas las apps: un `agent.call` por cada herramienta que ejecuta el asistente, hitos de cada app (`scribe.transcript.done`, `links.watch.new`…) y acciones del hub (`hub.backup.done`, `hub.rule.ran`, `hub.app.started`). Cassandra lo espeja para siempre en `bus_events` (`GET <hub>/api/events?since_id=` cada 10 s; `CASSANDRA_BUS=0` lo apaga, `CASSANDRA_HUB_URL` apunta al hub), así que `svc_why_down` enseña también qué hizo el asistente en los tres minutos alrededor de una incidencia, y `audit_search` / `audit_stats` responden «quién llamó a qué, cuándo y qué falló». Cassandra publica sus propias incidencias en el bus (`cassandra.incident.opened` / `closed`) para que una regla del hub pueda reaccionar a una caída.
- **Auditoría de secretos**: `secrets_audit` recorre cada carpeta de app descubierta: el fichero del token (existe, permisos), si `data/` está en `.gitignore`, ficheros con pinta de secreto rastreados por git (`mcp-token`, `.env`, `*.key`, bases de datos), ficheros `.env`. Solo lectura, sin red.
- **Políticas de reinicio** (opcionales, por servicio): el comando de la política; o, para una app descubierta con el lanzador activo, `POST http://127.0.0.1:8810/api/apps/<id>/start` (`/restart` si sigue viva); o el `launch_hint` de su manifiesto (`{X_DIR}` y `{FAUSTUS_PYTHON}` resueltos como hace el lanzador). Nunca más de `max_per_hour` reinicios automáticos por servicio; nunca sobre un puerto ocupado por otro programa; cada intento queda registrado en la incidencia. El reinicio manual (botón o `svc_restart`) siempre se permite si el servicio tiene forma de arrancar.
- **Interfaz** (español o inglés, automático y conmutable; tema oscuro): **Panel** (estado agrupado por tipo con una franja de 24 h por servicio, resumen de equipo y GPU, comprobar ahora, reiniciar), **Incidencias** (filtros por servicio, intervalo o "alrededor de las 04:00", solo abiertas; cada una se despliega con la explicación, qué más cambió, GPU, proceso, final del log y acciones), **GPU** (gráfico SVG de memoria y uso, picos, memoria libre), **Logs** (búsqueda por palabras, servicio, nivel y hora), **Servicios** (interruptor de reinicio automático, máx./hora y comando por servicio, añadir o quitar tus servicios, configuración efectiva). Instalable como PWA.

## Requisitos

- Windows 10/11 (también Linux/macOS), Python 3.11+ (3.13 sin problema), Node 22 solo para compilar la interfaz.
- `psutil` (incluido en requirements) para puertos, pids y hora de arranque. En macOS listar sockets de otros procesos puede requerir permisos; Cassandra pasa entonces a comprobar solo por HTTP.
- El driver de NVIDIA (`nvidia-smi`) es opcional: sin él las partes de GPU quedan vacías.

## Instalar y arrancar

Windows:

```bat
git clone <este repo> CassandraHoard
cd CassandraHoard
python -m venv venv
venv\Scripts\pip install -r requirements.txt
npm.cmd install --include=dev
npx.cmd vite build
venv\Scripts\python -m cassandra_hoard
```

Linux / macOS:

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
npm install --include=dev && npx vite build
.venv/bin/python -m cassandra_hoard
```

Abre http://127.0.0.1:5190. Pon el repositorio junto a las demás apps Hoard (su carpeta padre es la raíz de descubrimiento por defecto) o define `CASSANDRA_ROOTS`.

- `python scripts/launch.py` arranca la app en un puerto libre y abre el navegador.
- `python scripts/dev.py` levanta la API con recarga y el servidor de Vite (que redirige `/api`).
- `python scripts/make_icon.py --src <Icons>/dragon-src.png` regenera el icono a partir del dragón de la familia (necesita `requirements-icon.txt`).

## Configuración (entorno)

| Variable | Por defecto | Significado |
|---|---|---|
| `CASSANDRA_PORT` / `PORT` | `5190` | Puerto (el siguiente libre salvo `PORT_STRICT=1`). |
| `PORT_STRICT` | — | `1` = fallar en vez de cambiar de puerto. |
| `CASSANDRA_DATA_DIR` | `./data` | Base de datos, token, `url`, `services.json`, logs de reinicios. |
| `CASSANDRA_POLL_S` | `20` | Segundos entre comprobaciones. |
| `CASSANDRA_ROOTS` | carpeta padre del repo | Carpetas cuyas subcarpetas se exploran buscando `faustus-plugin.json` (separadas por `;`). |
| `CASSANDRA_EXTERNALS` | `1` | `0` = no vigilar los externos conocidos. |
| `CASSANDRA_LOG_GLOBS` | — | Logs extra separados por `;`: `glob` (etiquetado por nombre de archivo) o `servicio=glob`. |
| `CASSANDRA_DEFAULT_LOGS` | `1` | `0` = no leer la carpeta de logs de Cassandra ni `%LOCALAPPDATA%\Hoards`. |
| `CASSANDRA_RETENTION_DAYS` | `14` | Historial conservado (muestras, eventos, incidencias, GPU, logs). |
| `CASSANDRA_LOG_MAX_LINES` | `500000` | Tope de líneas de log guardadas. |
| `CASSANDRA_SAMPLE_EVERY_S` | `60` | Un estado activo sin cambios se guarda como mucho con esta frecuencia (historial de latencia). |
| `CASSANDRA_SLOW_MS` | `3000` | Respuestas más lentas cuentan como `degraded`. |
| `CASSANDRA_AUTO_RESTART` | `1` | `0` = interruptor general de reinicios automáticos apagado (los manuales siguen). |
| `CASSANDRA_AGENT_COMMANDS` | `0` | `1` = el asistente puede fijar comandos de reinicio con `svc_watch`. |
| `CASSANDRA_HUB_URL` | `http://127.0.0.1:8810` | El lanzador usado para arrancar apps descubiertas. |
| `CASSANDRA_FAUSTUS_PYTHON` | este intérprete | Rellena `{FAUSTUS_PYTHON}` en los launch hints. |
| `CASSANDRA_GPU` | `1` | `0` = no ejecutar `nvidia-smi`. |
| `CASSANDRA_PORT_CHECK` | `1` | `0` = sin atajo de puertos en escucha (solo HTTP). |
| `CASSANDRA_AUTOSTART` | `1` | `0` = no arrancar el sondeo con la app. |
| `CASSANDRA_ALLOWED_HOSTS` | — | Nombres de host extra (exactos o `*.sufijo`) para acceso por túnel. |

Ejemplo de `data/services.json`:

```json
{
  "services": [
    {"id": "whisper", "name": "Servidor Whisper", "url": "http://127.0.0.1:9000", "health_path": "/health",
     "expect": {"status": "ok"}, "log_paths": ["C:/tools/whisper/logs/*.log"],
     "restart": {"enabled": true, "cmd": ["C:/tools/whisper/run.bat"], "cwd": "C:/tools/whisper", "max_per_hour": 3}}
  ],
  "policies": {"ollama": {"enabled": true, "cmd": "ollama serve", "max_per_hour": 2}}
}
```

## API

`GET /api/health` (`{"service": "cassandra-hoard", ...}`), `/api/status`, `/api/services`, `/api/services/{id}`, `/api/services/{id}/history`, `/api/lanes?hours=24`, `/api/incidents` (`since`, `until`, `at`, `window_min`, `service`, `open_only`), `/api/incidents/{id}`, `/api/logs` (`q`, `service`, `level`, intervalo), `/api/logs/sources`, `/api/gpu`, `/api/settings`; `POST /api/poll`, `POST /api/services` (añadir/editar), `DELETE /api/services/{id}`, `PUT /api/services/{id}/policy`, `POST /api/services/{id}/restart`; `GET /api/agent/tools`, `POST /api/agent/call` (token Bearer de `data/mcp-token`). Las horas aceptan ISO (`2026-09-24T04:00`), una hora del día (`04:00` = las últimas 04:00) o una antigüedad (`2h`, `30m`, `1d`).

## Herramientas MCP

`mcp_server.py` es un puente stdio: pide el catálogo a la app, reenvía cada llamada con el token, nunca abre la base de datos y arranca la app si no responde (`CASSANDRA_BRIDGE_AUTOSTART=0` lo desactiva).

| Herramienta | Qué responde | Escribe |
|---|---|---|
| `svc_status` | ¿Está X caído? Estado actual de todos (o uno), desde cuándo, latencia, pid, incidencia abierta, política de reinicio, arranque del equipo, GPU. | no |
| `svc_incidents` | ¿Qué pasó a las 04:00? Incidencias en un intervalo (`at` ± `window_min`), con causa probable, reinicios del equipo y pausas de Cassandra. | no |
| `svc_why_down` | ¿Por qué se paró Y? La última incidencia en frases: causa, GPU, final del log, reinicios. | no |
| `logs_search` | ¿Qué decían los logs? Palabras, servicio, nivel, intervalo. | no |
| `gpu_timeline` | ¿Qué GPU está libre? Series de memoria/uso, picos, memoria libre, la GPU más libre. | no |
| `svc_history` | Cronología de estados de un servicio, con % de disponibilidad. | no |
| `audit_search` | ¿Qué hizo el asistente? El bus de la familia espejado desde el hub: cada llamada a una herramienta (app, tool, ok, ms, quién), hitos de las apps, acciones del hub — por palabras, tipo, app, herramienta, fallos, hora. | no |
| `audit_stats` | Llamadas del agente por app y herramienta, fallos, las más lentas, quién llama más, en un intervalo. | no |
| `secrets_audit` | Secretos expuestos en las carpetas de las apps: token presente, `data/` ignorado por git, ficheros con pinta de secreto rastreados por git, ficheros `.env`. | no |
| `svc_restart` | Reiniciar o arrancar un servicio (solo si el usuario lo pide). | sí |
| `svc_watch` | Añadir o editar un servicio vigilado, sus logs y su política (solo si el usuario lo pide). | sí |

## Pruebas

```sh
python -m pytest -q      # servicios falsos (apps ASGI que suben y caen), reloj, GPU y procesos simulados
npx vite build
```

## Privacidad

Todo es local: las comprobaciones de salud solo van a las URL del registro (loopback por defecto), los logs se leen de tu disco y se guardan en `data/cassandra.db`, nada se envía a ningún sitio y no hay telemetría. La API solo acepta orígenes locales (más `CASSANDRA_ALLOWED_HOSTS`) y las rutas del agente exigen el token de `data/mcp-token`.

## Límites (v1)

- Las causas probables son heurísticas sobre lo que Cassandra vio; si no estaba en marcha, solo puede decir que ocurrió dentro de esa pausa.
- El código de salida de un proceso que Cassandra no arrancó no está disponible; indica si el proceso desapareció o sigue vivo.
- El muestreo de GPU es solo NVIDIA (`nvidia-smi`).

## Licencia

MIT — ver `LICENSE`.
