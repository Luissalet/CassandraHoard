# Cassandra's Hoard

Observabilidad de todo el stack de IA local de tu PC. Vigila cada servicio local que usas — el espacio de trabajo Faustus y sus instancias de prueba, llama-server, Ollama, ComfyUI, el lanzador Hoard Hub y cada app Hoard — y recuerda **qué está activo, qué se cayó y cuándo, qué más cambió en ese mismo momento, qué decían los logs y qué hacían las GPU**. Cuando algo se para a las 04:00 y nadie sabe por qué, Cassandra tiene la respuesta (o la más probable). Un asistente accede a los mismos datos por MCP, así que basta con preguntar "¿por qué se paró Borges anoche?".

Todo se queda en el equipo: un único archivo SQLite, sin cuentas, sin telemetría, sin más red que las comprobaciones de salud en loopback de tus propios servicios y, solo si indicas webs públicas, una petición por web cada pocos minutos.

Forma parte de la familia Hoard (ver `faustus-plugin.json`).

## Qué hace

- **Registro de servicios**, unión de tres fuentes:
  - **apps descubiertas**: la lista del propio Hoard Hub (`GET http://127.0.0.1:8810/api/apps`, una sola fuente de verdad para toda la familia; `CASSANDRA_HUB_REGISTRY=0` lo desactiva, `/api/status` enseña `registry_source`) y, si el hub no está, cada `<raíz>/*/faustus-plugin.json` (el mismo manifiesto que lee el lanzador). `app.health` da la ruta de salud y el `service` esperado; `data/url` en la carpeta de la app manda sobre el puerto del manifiesto;
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
- **Incidencias de trabajos**: cada app cuenta su trabajo largo al hub como `<app>.job.queued|started|progress|done|failed` (y el hub añade `work.failed`). Cuando el bus reflejado trae un `*.job.failed` o un `work.failed`, Cassandra abre una incidencia de tipo **job** (trabajo): servicio = la app, causa probable = el texto del error, contexto = el trabajo (id, título, tipo, url) y sus propios eventos del bus. Queda abierta una incidencia por app y tipo de trabajo; otro fallo del mismo tipo se añade a ella y el error más reciente pasa a ser la causa. Un `*.job.done` posterior de la misma app y tipo la cierra, y también 24 horas sin un nuevo fallo. La página de incidencias la muestra con una etiqueta **trabajo**. Se anuncia como `cassandra.incident.opened` / `closed` con `kind: "job"`, `service_kind: "job"` y `to_state: "failed"`, de modo que las reglas del hub que reinician una app o avisan de una app caída no saltan por ella (el hub ya avisa del propio trabajo fallido). Un fallo de hace más de 24 horas en un historial largo del bus no abre nada. `CASSANDRA_JOB_INCIDENTS=0` lo desactiva.
- **Agenda de la familia**: `GET /api/family/agenda` (con el token de la app) responde a la lista Hoy y al calendario del hub con las incidencias que siguen abiertas (un servicio o web caído, un trabajo que falló y no ha tenido un éxito después) como elementos de tipo `incident`, prioridad `high`, desde que se abrieron hasta ahora. Las incidencias cerradas son historia y no aparecen.
- **Auditoría de secretos**: `secrets_audit` recorre cada carpeta de app descubierta: el fichero del token (existe, permisos), si `data/` está en `.gitignore`, ficheros con pinta de secreto rastreados por git (`mcp-token`, `.env`, `*.key`, bases de datos), ficheros `.env`. Solo lectura, sin red.
- **Políticas de reinicio** (opcionales, por servicio): el comando de la política; o, para una app descubierta con el lanzador activo, `POST http://127.0.0.1:8810/api/apps/<id>/start` (`/restart` si sigue viva); o el `launch_hint` de su manifiesto (`{X_DIR}` y `{FAUSTUS_PYTHON}` resueltos como hace el lanzador). Nunca más de `max_per_hour` reinicios automáticos por servicio; nunca sobre un puerto ocupado por otro programa; cada intento queda registrado en la incidencia. El reinicio manual (botón o `svc_restart`) siempre se permite si el servicio tiene forma de arrancar.
- **Webs públicas** (`data/sites.json`, nunca en el código): Cassandra vigila tus webs públicas desde fuera. Por web: HTTP (estado esperado, por defecto `2xx`/`3xx`; una palabra clave opcional que debe aparecer en los primeros 512 KB de la página, y entonces se siguen las redirecciones), latencia, **certificado TLS** (días que quedan, emisor, validez del nombre y de la cadena, releído cada hora), **DNS** (las direcciones; un cambio queda como evento, no como incidencia) y **caducidad del dominio** por RDAP (arranque IANA, una vez al día por dominio registrable; un TLD sin RDAP muestra `unknown`). Una web pasa a `down` tras 2 fallos seguidos (el primer fallo programa una comprobación de confirmación 60 s después) y a `up` con el primer acierto; la incidencia se abre y se cierra como la de cualquier servicio, con la causa en palabras (tiempo agotado, fallo de DNS, error TLS, estado inesperado, falta la palabra clave). Avisos, cada uno una sola vez: certificado a 21, 7 y 1 días, dominio a 30, 7 y 1 días; una renovación queda registrada. Eventos del bus `cassandra.site.down`, `.up`, `.cert_expiring` y `.domain_expiring`; con Boop configurado, las notificaciones de incidencia más los dos avisos de caducidad (sin nombres de web en el texto). Cada comprobación es un GET de la portada con el User-Agent `Cassandra's Hoard site check/<versión> (monitor run by the site owner)`, como mucho una vez por intervalo (5 min por defecto, de 1 a 1440); una comprobación manual de una web se separa al menos un minuto de la anterior. La lista se edita desde la tarjeta del Panel, con `sites_watch` o a mano (se recoge en la siguiente lectura). `CASSANDRA_SITES=0` lo apaga todo.
- **Qué está corriendo Faustus** (`faustus_farm`, la tarjeta **Agentes en marcha** del Panel): una llamada de sólo lectura a `GET /api/farm/state` de Faustus (se construye una vez por segundo, con ETag, así que una imagen sin cambios es un 304). Cada turno de chat en marcha es una fila (título, tipo, modelo, estado, cuánto lleva, progreso) con los subagentes que lanzó sangrados debajo; los trabajos despachados muestran sus workers, y también salen un turno de noche, ejecuciones de flujos de trabajo y tareas programadas en curso. Debajo, una línea de presupuesto: la ventana gastada frente a su línea de ritmo, segundos de GPU de hoy, si el cortacircuitos de fallos está abierto y los enfriamientos de proveedores. La tarjeta se refresca cada 4 s sólo mientras el Panel está visible, y dice por qué si no puede leer Faustus (sin token, rechazado, caído, demasiado antiguo). Sólo viajan nombres y estados, nunca lo que alguien escribió.
- **Interfaz** (español o inglés, automático y conmutable; tema oscuro): **Panel** (estado agrupado por tipo con una franja de 24 h por servicio, resumen de equipo y GPU, la tarjeta **Agentes en marcha**, la tarjeta **Webs públicas** con su editor de lista, comprobar ahora, reiniciar), **Incidencias** (filtros por servicio, intervalo o "alrededor de las 04:00", solo abiertas; cada una se despliega con la explicación, qué más cambió, GPU, proceso, final del log y acciones), **GPU** (gráfico SVG de memoria y uso, picos, memoria libre), **Logs** (búsqueda por palabras, servicio, nivel y hora), **Servicios** (interruptor de reinicio automático, máx./hora y comando por servicio, añadir o quitar tus servicios, configuración efectiva). Instalable como PWA.

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

Los avisos opcionales de incidentes mediante Boop están desactivados por defecto. Consulta [configuración, pruebas y límites de entrega](docs/adaptations/BOOP_TRANSPORT_2026-09-29.md) antes de activarlos.

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
| `CASSANDRA_FAUSTUS_URL` | `http://127.0.0.1:7000` | La instancia de Faustus que lee `faustus_attention` (sólo loopback). |
| `CASSANDRA_FAUSTUS_TOKEN` | `data/faustus-token` | Un token de API de Faustus con el ámbito `attention:read` (Ajustes → Tokens de API → Atención). Sólo lectura. |
| `CASSANDRA_FAUSTUS_FARM_TOKEN` | `data/faustus-farm-token` | Un token de API de Faustus con el ámbito `farm:read` (Ajustes → Tokens de API → Granja) para `faustus_farm` y la tarjeta **Agentes en marcha** del Panel. Si falta, se prueba el token de atención (sirve si lleva también `farm:read` o `sessions`). Sólo lectura. |
| `CASSANDRA_FAUSTUS_WAIT_MIN` | `15` | Con token: una aprobación o pregunta que lleve esto esperando en Faustus se anuncia una vez por el bus de la familia (`cassandra.faustus.waiting`) y, si Boop está configurado, como aviso sin contenido del chat; el Panel enseña la tarjeta «Faustus te espera». `0` = apagado. |
| `CASSANDRA_GPU` | `1` | `0` = no ejecutar `nvidia-smi`. |
| `CASSANDRA_PORT_CHECK` | `1` | `0` = sin atajo de puertos en escucha (solo HTTP). |
| `CASSANDRA_AUTOSTART` | `1` | `0` = no arrancar el sondeo con la app. |
| `CASSANDRA_ALLOWED_HOSTS` | — | Nombres de host extra (exactos o `*.sufijo`) para acceso por túnel. |
| `CASSANDRA_JOB_INCIDENTS` | `1` | `0` = no abrir incidencias por trabajos fallidos vistos en el bus reflejado. |
| `CASSANDRA_SITES` | `1` | `0` = no vigilar webs públicas (ninguna petición saliente). |
| `CASSANDRA_SITES_TICK_S` | `15` | Cada cuánto busca el vigilante de webs las que toca comprobar (cada web respeta su propio intervalo). |

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

Ejemplo de `data/sites.json` (las webs públicas; el `id` sale del host, `expect_status` por defecto es `["2xx","3xx"]` e `interval_min` 5):

```json
{
  "sites": [
    {"url": "https://example.com/", "name": "Tienda", "keyword": "Añadir al carrito", "interval_min": 5},
    {"url": "https://www.example.org/", "expect_status": ["200", "301-308"], "enabled": false}
  ]
}
```

## API

`GET /api/health` (`{"service": "cassandra-hoard", ...}`), `/api/status`, `/api/services`, `/api/services/{id}`, `/api/services/{id}/history`, `/api/lanes?hours=24`, `/api/incidents` (`since`, `until`, `at`, `window_min`, `service`, `open_only`), `/api/incidents/{id}`, `/api/logs` (`q`, `service`, `level`, intervalo), `/api/logs/sources`, `/api/gpu`, `/api/settings`; `POST /api/poll`, `POST /api/services` (añadir/editar), `DELETE /api/services/{id}`, `PUT /api/services/{id}/policy`, `POST /api/services/{id}/restart`; las webs: `GET /api/sites?hours=24`, `GET /api/sites/{id}/history`, `POST /api/sites` (añadir/editar), `POST /api/sites/check` (comprobar ahora), `DELETE /api/sites/{id}`; `GET /api/faustus/farm` (qué está corriendo Faustus ahora, o por qué no se puede leer), `GET /api/agent/tools`, `POST /api/agent/call` (token Bearer de `data/mcp-token`). Las horas aceptan ISO (`2026-09-24T04:00`), una hora del día (`04:00` = las últimas 04:00) o una antigüedad (`2h`, `30m`, `1d`).

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
| `faustus_attention` | ¿Me espera Faustus? Aprobaciones y preguntas que te esperan, ejecuciones que dejaron de mandar eventos, cuánto lleva cada una y las esperas largas (≥ `wait_min`). Lo lee de la lista de atención del propio Faustus con un token de sólo lectura; dice por qué cuando no puede (sin token, rechazado, Faustus caído). | no |
| `faustus_farm` | ¿Qué está haciendo Faustus ahora? Cada turno de chat en marcha con los subagentes que lanzó, los trabajos despachados y sus workers, un turno de noche, ejecuciones de flujos de trabajo y tareas programadas (título, tipo, modelo, estado, cuánto lleva, progreso), más el presupuesto del periodo: ventana gastada frente a su ritmo, segundos de GPU de hoy, cortacircuitos de fallos, enfriamientos de proveedores. Lo lee del estado de la granja de Faustus con un token `farm:read` de sólo lectura; dice por qué cuando no puede (sin token, rechazado, Faustus caído o antiguo). | no |
| `sites_status` | ¿Están mis webs arriba? Cada web pública (o una): estado, estado HTTP, latencia, días de certificado, fecha de caducidad del dominio, DNS, avisos, incidencia abierta. | no |
| `site_history` | La línea de tiempo arriba/abajo de una web, latencia (mín./media/p95/máx. y una serie), incidencias y eventos de certificado, DNS y dominio de un intervalo. | no |
| `svc_restart` | Reiniciar o arrancar un servicio (solo si el usuario lo pide). | sí |
| `svc_watch` | Añadir o editar un servicio vigilado, sus logs y su política (solo si el usuario lo pide). | sí |
| `sites_watch` | Añadir, editar o quitar una web pública vigilada (`add`, `edit`, `remove`; URL, estado esperado, palabra clave, intervalo; solo si el usuario lo pide). | sí |

## Pruebas

```sh
python -m pytest -q      # servicios falsos (apps ASGI que suben y caen), reloj, GPU y procesos simulados
npx vite build
```

## Privacidad

Todo es local: las comprobaciones de salud solo van a las URL del registro (loopback por defecto) y a las webs que pongas en `data/sites.json` (un GET de la portada por intervalo con un User-Agent que nombra a Cassandra's Hoard, un saludo TLS por hora, una consulta RDAP por dominio al día; `CASSANDRA_SITES=0` lo apaga todo), los logs se leen de tu disco y se guardan en `data/cassandra.db`, las dos tarjetas de Faustus (esperas, en marcha) leen rutas de sólo lectura del Faustus loopback que configures y muestran nombres y estados, nunca texto de mensajes, nada se envía a ningún sitio y no hay telemetría. La API solo acepta orígenes locales (más `CASSANDRA_ALLOWED_HOSTS`) y las rutas del agente exigen el token de `data/mcp-token`.

## Límites (v1)

- Las causas probables son heurísticas sobre lo que Cassandra vio; si no estaba en marcha, solo puede decir que ocurrió dentro de esa pausa.
- El código de salida de un proceso que Cassandra no arrancó no está disponible; indica si el proceso desapareció o sigue vivo.
- El muestreo de GPU es solo NVIDIA (`nvidia-smi`).
- Las webs se vigilan solo desde este equipo: un fallo de tu propia conexión puede parecer que todas están caídas (la causa probable dice cuándo falló el DNS o la red). La comprobación de confirmación tras un primer fallo es la única petición por encima del intervalo configurado.
- La caducidad del dominio depende de que el registro la publique por RDAP; algunos TLD no lo hacen y entonces la tarjeta no muestra días de dominio. Una consulta RDAP fallida se reintenta a las 6 horas.
- Una CDN que rota direcciones IP aparece como eventos de cambio de DNS; son informativos.

## Licencia

MIT — ver `LICENSE`.
