# Boop: transporte opcional de avisos de incidentes

Fuente: [chrisgreg/boop](https://github.com/chrisgreg/boop), revisión [881004d8ed6fb9be1a3812ff779119ba04b0d542](https://github.com/chrisgreg/boop/tree/881004d8ed6fb9be1a3812ff779119ba04b0d542), README archivado en el radar del 29-09-2026. Licencia observada: MIT. Implementación propia del contrato HTTP; no se ha incorporado código de Boop.

## Implementado

El emisor existente de Cassandra avisa a Boop cuando detecta apertura o cierre de incidentes. Usa `POST /api/v1/events` con una clave de proyecto en Authorization. Cada incidente comparte un `fingerprint` `cassandra:incident:<id>` entre apertura y cierre; `external_id` distingue ambas fases. Los sondeos repetidos sin cambios no envían nuevos avisos porque reutilizamos el seguimiento de BusMirror.

El título solo contiene número de incidente y fase. No se envían nombres de servicios, logs, comandos ni causas inferidas. El botón apunta a la ruta existente `/#/incidents/<id>` de Cassandra. El cierre usa nivel informativo; no afirma por sí mismo recuperación comprobada.

El transporte está **desactivado por defecto** y requiere configuración explícita del usuario antes de iniciar Cassandra:

| Variable | Valor |
|---|---|
| `CASSANDRA_BOOP_ENABLED` | `1` para activar |
| `CASSANDRA_BOOP_URL` | Base de un servidor Boop, p. ej. `https://boop.example.test` |
| `CASSANDRA_BOOP_API_KEY` | Clave de proyecto Boop, solo en servidor |
| `CASSANDRA_PUBLIC_URL` | Base de Cassandra accesible desde el dispositivo |
| `CASSANDRA_BUS` | Debe permanecer en `1` (valor predeterminado) |

Cada instalación de Cassandra debe usar su propio proyecto Boop para evitar colisiones entre números de incidente. Se acepta HTTP para Boop solo en loopback; otros destinos requieren HTTPS. No se siguen redirecciones ni se usan proxies del entorno. La clave no aparece en la representación de Config ni en `/api/status`; los fallos muestran únicamente un código HTTP o un mensaje fijo. Las URL no admiten credenciales, parámetros ni fragmentos.

Configurar acceso remoto a Cassandra, su lista de hosts admitidos y autenticación pertenece al despliegue existente. Esta entrega no publica el servicio. El enlace usa la vista actual, que conserva sus filtros y límites de lista: no garantiza abrir incidentes antiguos fuera de ese resultado.

## Comprobación

**74 pruebas correctas** de la suite completa (17 nuevas), ejecutadas con el Python del entorno de Faustus. MockTransport mantiene todas las peticiones dentro del proceso: apertura y cierre reales del arnés de incidentes, agrupación, ausencia de duplicados por sondeo, configuración, campos mínimos, exclusión de eventos ajenos, autenticación y fallos 401/500/307/timeout. No hubo envíos externos ni claves reales.

`/api/status.notifications` expone `enabled`, `accepted`, `failed`, `last_error`. `accepted` significa respuesta HTTP 2xx, **no entrega al teléfono**; los contadores son de esta ejecución y se reinician con el proceso.

## Límites pendientes

- Sin prueba contra servidor Boop real, iOS, dispositivo firmado o APNs. Esa validación requiere infraestructura del usuario; la entrega móvil sigue pendiente.
- `fingerprint` agrupa la visualización en Boop; Boop puede almacenar y enviar ambas ocurrencias. No es supresión de pushes.
- Envío de mejor esfuerzo, sin cola duradera ni reintentos. Un fallo queda visible en status, pero el emisor existente marca la transición observada. No hay garantía de entrega tras reinicio ni recuperación de avisos perdidos.
- Se heredan las ventanas del emisor: últimas 50 incidencias, apertura reciente de menos de una hora, no reinicios sin caída. Las incidencias previas al inicio no se reenvían como nuevas; un cierre posterior sí puede notificarse.
- Requiere el hilo BusMirror activo (y autostart para arranque automático). Una avería de Boop no detiene el sondeo de servicios; cada petición limita la espera a tres segundos dentro del hilo del bus.

Estado: transporte opcional implementado y comprobado de forma hermética; entrega móvil bloqueada por servidor y dispositivo todavía no aportados.
