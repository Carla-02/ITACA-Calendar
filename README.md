# CV Itaca - A, calendario que se actualiza solo

Página web con todos los partidos del CV Itaca - A (2ª División Senior Masculino, grupo 1) y la clasificación de la liga. Cada lunes a las 10:00 (hora de Madrid) un script consulta la exportación oficial de la FVBCV, comprueba los datos, los guarda en este repositorio y vuelve a publicar la página.

No hace falta ninguna contraseña ni clave: la fuente es pública y el repositorio se actualiza con el permiso que GitHub da a sus propias tareas.

## Cómo funciona

```
FVBCV (PDF oficial)  ->  scripts/update.py  ->  data/*.json + calendar.ics  ->  GitHub Pages (index.html)
                          (cada lunes 10:00)     (se guardan en el repositorio)
```

- **Fuente:** `https://fvbcv.com/exportar_competiciones.php?idens=1823`. Es el PDF que genera la propia federación y trae calendario, resultados con sets y clasificación.
- **Validación:** antes de guardar se comprueba que hay 26 jornadas, que la clasificación se lee, que los marcadores son coherentes y que no bajan los resultados. Si algo falla, **no se toca ningún dato** y el error queda anotado.
- **Histórico:** los datos viven en el repositorio (cada actualización es un commit), así que sobreviven a las publicaciones y se pueden recuperar con el historial de GitHub. Los partidos nunca se borran: si desaparece uno de la fuente se marca como inactivo.
- **Aviso en la web:** la página muestra la fecha de la última actualización correcta y un aviso si el último intento falló o los datos tienen más de 9 días.

## Puesta en marcha (una sola vez)

1. En GitHub: **New repository**. Nombre, por ejemplo, `cv-itaca-a`. Público (Pages es gratis solo en repositorios públicos). Marca "Add a README" para que exista la rama `main`.
2. Sube el contenido de esta carpeta al repositorio: **Add file > Upload files**, arrastra todo (incluida la carpeta oculta `.github`) y pulsa **Commit changes**.
   - Si `.github/workflows/update.yml` no se ha subido: **Add file > Create new file**, escribe `.github/workflows/update.yml` como nombre y pega el contenido del archivo.
3. **Settings > Pages > Build and deployment > Source: GitHub Actions.**
4. **Settings > Actions > General > Workflow permissions: Read and write permissions.** Guarda.
5. Ve a **Actions > "Actualizar datos y publicar" > Run workflow**. Es la primera ejecución real: descarga el PDF, valida y publica.
6. Cuando termine en verde, tu dirección es `https://TU-USUARIO.github.io/cv-itaca-a/`.

### Cómo comprobar que funciona de verdad

- En **Actions** la ejecución debe salir en verde y el paso "Actualizar datos desde la FVBCV" debe imprimir `Actualización correcta`.
- En `data/status.json`: `last_outcome` es `ok` o `sin_cambios`, `last_error` es `null` y `last_success` tiene la fecha de hoy.
- En la web, la franja de arriba muestra esa fecha.
- El lunes siguiente, en la pestaña Actions debe aparecer una ejecución con origen "schedule". GitHub puede retrasar las tareas programadas unos minutos o más en horas de mucha carga.

## Cambiar el día o la hora

Edita `.github/workflows/update.yml`: las líneas `cron` (en UTC, `1` = lunes) y el número de `--scheduled-from-madrid-hour`. Con las líneas actuales (08:00 y 09:00 UTC, más un reintento a las 11:17 UTC) el script actúa una sola vez cada lunes, a partir de las 10 en Madrid, en verano y en invierno. Si GitHub retrasa la tarea, se actualiza igualmente ese día; si la primera falla, el reintento lo vuelve a intentar.

## Archivos

| Archivo | Para qué sirve |
|---|---|
| `index.html` | La página. Lee los JSON de `data/`. |
| `config.json` | Equipo, URL de la fuente, nº de jornadas esperadas, fechas de temporada, tiempos de espera y reintentos. |
| `scripts/update.py` | Descarga, valida, fusiona, guarda y genera el calendario. |
| `data/matches.json` | Partidos del equipo con histórico (cuándo se vio, cuándo cambió, nº de revisión). |
| `data/standings.json` | Clasificación del grupo en la última actualización. |
| `data/team_history.json` | Posición y puntos del equipo en cada cambio de la clasificación. |
| `data/changelog.json` | Registro de altas, cambios de hora o fecha, resultados y desapariciones. |
| `data/status.json` | Estado de la sincronización y últimas 30 ejecuciones. |
| `data/venues.json` | Nombres bonitos y búsquedas de mapa para cada pabellón (editable). |
| `calendar.ics` | Calendario para móvil. Los eventos tienen identificador fijo: al volver a descargarlo se actualizan. |
| `tests/` | Pruebas automáticas y un texto de ejemplo real de la federación. |

## "API" de datos

Los datos se sirven como archivos JSON de solo lectura en la misma dirección de la web. No hay servidor ni claves.

| Dirección | Contenido |
|---|---|
| `/data/matches.json` | `{team, competition, season, source, matches[]}`. Cada partido: `id`, `jornada`, `date` (AAAA-MM-DD), `time` (HH:MM o `null` si falta confirmar), `home`, `away`, `team_is_home`, `opponent_display`, `venue`, `venue_display`, `result` (`home_sets`, `away_sets`, `sets` como pares local-visitante) o `null`, `status` (`pendiente`, `jugado`, `descanso`), `bye`, `active`, `first_seen`, `last_seen`, `last_changed`, `revision`. |
| `/data/standings.json` | `{printed_at, rows[]}` con `pos`, `display`, `pj`, `pg3`, `pg2`, `pp1`, `pp0`, `sf`, `sc`, `pf`, `pc`, `pts`, `is_team`. |
| `/data/status.json` | `last_attempt`, `last_success`, `last_outcome` (`ok`, `sin_cambios`, `error`), `last_error` (`at`, `kind`, `message`), `consecutive_failures`, `source_printed_at`, `counts`, `recent_runs[]`. |
| `/data/changelog.json` | Lista de cambios con `at`, `type`, `id` y, si procede, `field`, `old`, `new`. |
| `/calendar.ics` | Calendario iCalendar. Se puede suscribir con `webcal://…/calendar.ics`. |

**Actualización manual:** Actions > "Actualizar datos y publicar" > Run workflow. Solo pueden lanzarla quienes tengan permiso de escritura en el repositorio. La casilla "Forzar" permite que bajen los resultados y ejecutar fuera de temporada.

**Errores posibles** (`last_error.kind`): `conexion` (no se pudo descargar tras 3 intentos o no llegó un PDF), `estructura` (el PDF cambió de formato o una fila del equipo no se entiende), `validacion` (los datos no pasan las comprobaciones). En los tres casos la ejecución sale en rojo, GitHub avisa por correo al propietario y los datos anteriores se conservan.

## Si falla una ejecución

1. Abre la ejecución en Actions y lee el error del paso "Actualizar datos".
2. Descarga el archivo **diagnostico** (aparece al final de la ejecución): contiene el texto exacto que se extrajo del PDF.
3. Si la federación ha cambiado el formato, el texto de diagnóstico es lo que hace falta para ajustar `parse_export` en `scripts/update.py`. Mientras tanto la web sigue mostrando los últimos datos buenos con un aviso.

## Probar en local

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v           # pruebas
python scripts/update.py --dry-run                # descarga y valida sin guardar
python scripts/update.py --text-file tests/fixtures/export_2026-10-13.txt --dry-run
python -m http.server 8000                        # y abrir http://localhost:8000
```

## Nueva temporada

1. En la web de la FVBCV abre el grupo del equipo, copia el enlace de "exportar" (el número `idens`) y cámbialo en `config.json` (`group_id` y `source.url`).
2. Ajusta `group_number`, `season_start`, `season_end` y, si cambia el número de jornadas, `expected.jornadas`.
3. Vacía `data/matches.json`, `standings.json`, `team_history.json` y `changelog.json` (o guarda una copia en otra carpeta) y lanza una ejecución manual.

## Añadir otras fuentes de datos

La interfaz solo conoce los JSON de `data/`, no la federación. Para añadir otra fuente (otro equipo, otra liga, una API oficial si algún día existe):

1. Escribe una función que devuelva partidos y clasificación con el mismo formato que `parse_export` (misma estructura de campos).
2. Llámala desde `main()` en lugar de, o además de, `fetch_export` + `parse_export`.
3. La validación, la fusión con el histórico, el registro de cambios y la generación del `.ics` se reutilizan tal cual.

## Seguridad y límites

- No se usan secretos. Si en el futuro hiciera falta una clave, guárdala en **Settings > Secrets and variables > Actions**, nunca en el código ni en el HTML.
- Una sola consulta por semana, con 30 s de espera máxima y 3 reintentos. El script se identifica con un `User-Agent` propio (editable en `config.json`).
- La web incluye `noindex` para que los buscadores no la indexen, pero **una página de GitHub Pages gratuita es accesible para cualquiera que tenga el enlace**. Los datos ya son públicos en la web de la federación.
- GitHub desactiva las tareas programadas de un repositorio tras 60 días sin actividad. Mientras haya actualizaciones con cambios, los commits cuentan como actividad. Si dejara de ejecutarse, se reactiva desde la pestaña Actions.
- Fuera de temporada (`season_end` en `config.json`) el script no hace nada.
