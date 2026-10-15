#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Actualiza los datos del equipo a partir de la exportación oficial de la FVBCV.

Flujo: descargar PDF -> extraer texto -> parsear -> validar -> fusionar con el
histórico -> guardar JSON y calendar.ics. Si algo falla, NO se tocan los datos
buenos: solo se anota el error en data/status.json y el proceso termina con
código 1 (GitHub avisa por correo al propietario del repositorio).

Uso habitual (lo hace el workflow):  python scripts/update.py
Prueba sin tocar nada:               python scripts/update.py --dry-run
Con un texto ya extraído:            python scripts/update.py --text-file ruta.txt
"""
from __future__ import annotations

import argparse
import html
import io
import json
import os
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
MADRID = ZoneInfo("Europe/Madrid")
BYE = "DESCANSA"
STANDING_KEYS = ["pj", "pg3", "pg2", "pp1", "pp0", "sa", "sf", "sc", "pf", "pc", "pts"]
TRACKED_FIELDS = ["date", "time", "venue", "result"]
MAX_RUNS_LOGGED = 30
MAX_CHANGELOG = 1000


# ----------------------------------------------------------------- errores
class UpdateError(Exception):
    kind = "error"


class FetchError(UpdateError):
    kind = "conexion"


class ParseError(UpdateError):
    kind = "estructura"


class ValidationError(UpdateError):
    kind = "validacion"


# ------------------------------------------------------------- utilidades
def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))
    os.replace(tmp, path)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


SMALL_WORDS = {"de", "del", "la", "y", "d'en", "en"}


def pretty(name: str, strip_cv: bool = True) -> str:
    """'CV MEDITERRÁNEO - C' -> 'Mediterráneo C'."""
    s = name.replace(" - ", " ")
    if strip_cv:
        s = re.sub(r"^CV ", "", s)
    out = []
    for i, w in enumerate(s.lower().split()):
        if w in SMALL_WORDS and i > 0:
            out.append(w)
        elif re.fullmatch(r"[a-z]", w):
            out.append(w.upper())
        elif "." in w:
            out.append(re.sub(r"(^|[.(])([a-z])", lambda m: m.group(1) + m.group(2).upper(), w))
        elif w.startswith("("):
            out.append("(" + w[1:2].upper() + w[2:])
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)


# ------------------------------------------------------------ descarga
def fetch_export(url: str, timeout: int, retries: int, user_agent: str, sleep=time.sleep) -> bytes:
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urlrequest.Request(url, headers={"User-Agent": user_agent, "Accept": "application/pdf,*/*"})
            with urlrequest.urlopen(req, timeout=timeout) as resp:
                data = resp.read(25_000_000)
            if not data.startswith(b"%PDF"):
                raise FetchError("La respuesta no es un PDF (la fuente puede haber cambiado)")
            return data
        except (urlerror.URLError, TimeoutError, ConnectionError, OSError, FetchError) as exc:
            last = exc
        if attempt < retries:
            sleep(5 * attempt * attempt)
    raise FetchError(f"No se pudo descargar la exportación tras {retries} intentos: {last}")


def pdf_to_text(data: bytes) -> str:
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover
        raise ParseError("Falta la dependencia pdfplumber (pip install -r requirements.txt)") from exc
    pages = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                pages.append(page.extract_text() or "")
    except Exception as exc:
        raise ParseError(f"No se pudo leer el PDF: {exc}") from exc
    text = "\n".join(pages)
    if not text.strip():
        raise ParseError("El PDF no contiene texto extraíble")
    return text


# ------------------------------------------------------------- parseo
ROW = re.compile(r"^(?P<j>\d{1,2}) (?P<date>\d{2}/\d{2}/\d{4}) (?P<time>\d{2}:\d{2}) (?P<rest>.+)$")
STAND = re.compile(r"^(?P<pos>\d{1,2}) (?P<team>[^\d].*?) (?P<nums>\d+(?: \d+){10})$")
PRINTED = re.compile(r"Fecha de impresi[oó]n:\s*(\d{2})-(\d{2})-(\d{4})\s+(\d{2}):(\d{2})")
RESULT = re.compile(r"^(\d) - (\d) \(([\d/\-]*)\)\s*")


def clean_lines(text: str):
    for raw in text.splitlines():
        line = html.unescape(raw).replace("\xa0", " ")
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            yield line


def split_teams(rest: str, teams_by_len: list):
    """Separa 'LOCAL [res] VISITANTE PABELLÓN' usando la lista de equipos conocidos."""
    for home in teams_by_len:
        if not rest.startswith(home + " "):
            continue
        tail = rest[len(home) + 1:]
        result = None
        if tail.startswith("- "):
            tail = tail[2:]
        else:
            m = RESULT.match(tail)
            if not m:
                continue
            sets = []
            if m.group(3):
                for part in m.group(3).split("/"):
                    a, _, b = part.partition("-")
                    if not (a.isdigit() and b.isdigit()):
                        raise ParseError(f"Resultado de set no válido: {part!r}")
                    sets.append([int(a), int(b)])
            result = {"home_sets": int(m.group(1)), "away_sets": int(m.group(2)), "sets": sets}
            tail = tail[m.end():]
        for away in teams_by_len:
            if tail.startswith(away):
                return home, result, away, tail[len(away):].strip()
    return None


def parse_export(text: str, team: str, group_id: int = 1, printed_tz: str = "UTC") -> dict:
    lines = list(clean_lines(text))
    if len(lines) < 10:
        raise ParseError("El texto extraído es demasiado corto")

    printed = None
    for line in lines:
        m = PRINTED.search(line)
        if m:
            d, mo, y, h, mi = map(int, m.groups())
            # El servidor de la FVBCV imprime esta hora en UTC (comprobado el 15/10/2026:
            # a las 20:07 de Madrid el PDF decía 18:07). Se convierte a hora de Madrid.
            printed = datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(printed_tz)).astimezone(MADRID)
            break
    if printed is None:
        raise ParseError("No se encontró la fecha de impresión del documento")

    idx = next((i for i, ln in enumerate(lines) if ln.upper().replace("Ó", "O") == "CLASIFICACION"), None)
    if idx is None:
        raise ParseError("No se encontró la sección CLASIFICACION")

    standings = []
    for line in lines[idx + 1:]:
        m = STAND.match(line)
        if m:
            nums = list(map(int, m.group("nums").split()))
            standings.append({"team": m.group("team"), **dict(zip(STANDING_KEYS, nums))})
    if not standings:
        raise ParseError("La clasificación está vacía o no se pudo leer")

    teams = [s["team"] for s in standings]
    teams_by_len = sorted(set(teams + [BYE]), key=len, reverse=True)

    competition = next((ln for ln in lines if "DIVISI" in ln.upper() and "FASE" in ln.upper()), None)
    season = next((ln for ln in lines if ln.lower().startswith("temporada")), None)

    problems, matches = [], []
    for line in lines[:idx]:
        m = ROW.match(line)
        if not m:
            if re.match(r"^\d{1,2} \d", line) and team in line:
                problems.append(line)
            continue
        split = split_teams(m.group("rest"), teams_by_len)
        if split is None:
            if team in m.group("rest"):
                problems.append(line)
            continue
        home, result, away, venue = split
        if team not in (home, away):
            continue
        try:
            matches.append(build_match(group_id, int(m.group("j")), m.group("date"), m.group("time"),
                                       home, result, away, venue, team, printed))
        except ValueError:
            problems.append(line)
    if problems:
        raise ParseError("Filas del equipo que no se pudieron interpretar: " + " | ".join(problems[:3]))

    # deduplicar: mismas filas repetidas (p. ej. cabeceras de página) se descartan
    unique, seen = [], {}
    for mt in matches:
        key = mt["id"]
        core = {k: mt[k] for k in ("date", "time", "venue", "result", "home", "away")}
        if key in seen:
            if seen[key] != core:
                raise ValidationError(f"Partido duplicado con datos distintos: {key}")
            continue
        seen[key] = core
        unique.append(mt)
    unique.sort(key=lambda x: x["jornada"])

    real = [s for s in standings if s["team"] != BYE]
    for i, row in enumerate(real, start=1):
        row["pos"] = i
        row["display"] = pretty(row["team"])
        row["is_team"] = row["team"] == team

    return {"printed_at": printed, "competition": competition, "season": season,
            "standings": real, "matches": unique}


def build_match(group_id, jornada, date_s, time_s, home, result, away, venue, team, printed) -> dict:
    d = datetime.strptime(date_s, "%d/%m/%Y").date()
    bye = BYE in (home, away)
    team_home = home == team
    opponent = away if team_home else home
    mid = f"g{group_id}-j{jornada:02d}-" + ("bye" if bye else f"{slug(home)}-{slug(away)}")
    return {
        "id": mid,
        "jornada": jornada,
        "date": d.isoformat(),
        "time": None if time_s == "00:00" else time_s,
        "bye": bye,
        "home": home,
        "away": away,
        "team_is_home": team_home,
        "opponent": opponent,
        "opponent_display": pretty(opponent) if not bye else "Descansa",
        "venue": venue,
        "venue_display": pretty(venue, strip_cv=False) if venue else "",
        "result": result,
        "status": "descanso" if bye else ("jugado" if result else "pendiente"),
        "source": {"printed_at": iso(printed)},
    }


# ---------------------------------------------------------- validación
def validate(parsed: dict, cfg: dict, previous: list, force: bool = False) -> None:
    exp, team = cfg["expected"], cfg["team"]["name"]
    standings, matches = parsed["standings"], parsed["matches"]

    if len(standings) < exp["min_teams"]:
        raise ValidationError(f"La clasificación tiene {len(standings)} equipos (mínimo {exp['min_teams']})")
    if team not in [s["team"] for s in standings]:
        raise ValidationError(f"El equipo {team} no aparece en la clasificación")
    for s in standings:
        if s["pj"] > exp["jornadas"] or s["pts"] < 0:
            raise ValidationError(f"Fila de clasificación incoherente: {s['team']}")

    found = sorted(m["jornada"] for m in matches)
    wanted = list(range(1, exp["jornadas"] + 1))
    if found != wanted:
        missing = sorted(set(wanted) - set(found))
        raise ValidationError(f"Se esperaban {exp['jornadas']} jornadas del equipo; faltan: {missing or '—'}; encontradas: {len(found)}")

    start, end = date.fromisoformat(cfg["season_start"]), date.fromisoformat(cfg["season_end"])
    for m in matches:
        if not (start <= date.fromisoformat(m["date"]) <= end):
            raise ValidationError(f"Fecha fuera de temporada en {m['id']}: {m['date']}")
        r = m["result"]
        if r:
            hs, as_ = r["home_sets"], r["away_sets"]
            if max(hs, as_) != 3 or min(hs, as_) > 2:
                raise ValidationError(f"Resultado incoherente en {m['id']}: {hs}-{as_}")
            if r["sets"] and len(r["sets"]) != hs + as_:
                raise ValidationError(f"Sets no coinciden con el marcador en {m['id']}")

    prev_results = sum(1 for m in previous if m.get("result") and m.get("active", True))
    new_results = sum(1 for m in matches if m["result"])
    if new_results < prev_results and not force:
        raise ValidationError(
            f"La fuente trae {new_results} resultados y ya teníamos {prev_results}: "
            "se descarta la actualización (usa --force si es intencionado)")


# ------------------------------------------------------------- fusión
def merge(previous: list, new: list, now: datetime) -> tuple:
    stamp = iso(now)
    prev_by_id = {m["id"]: m for m in previous}
    merged, changes = [], []
    for m in new:
        old = prev_by_id.get(m["id"])
        m = dict(m)
        if old is None:
            m.update(first_seen=stamp, last_seen=stamp, last_changed=stamp, revision=1, active=True)
            changes.append({"at": stamp, "type": "nuevo", "id": m["id"]})
        else:
            m["first_seen"] = old.get("first_seen", stamp)
            m["last_seen"] = stamp
            m["active"] = True
            diffs = []
            for field in TRACKED_FIELDS:
                if old.get(field) != m.get(field):
                    diffs.append(field)
                    changes.append({"at": stamp, "type": "resultado" if field == "result" else "modificado",
                                    "id": m["id"], "field": field, "old": old.get(field), "new": m.get(field)})
            m["revision"] = old.get("revision", 1) + (1 if diffs else 0)
            m["last_changed"] = stamp if diffs else old.get("last_changed", stamp)
        merged.append(m)
    new_ids = {m["id"] for m in new}
    for old in previous:
        if old["id"] not in new_ids:  # nunca se borra histórico
            old = dict(old)
            if old.get("active", True):
                changes.append({"at": stamp, "type": "desaparecido", "id": old["id"]})
            old["active"] = False
            merged.append(old)
    merged.sort(key=lambda x: (x["jornada"], not x.get("active", True)))
    return merged, changes


# ------------------------------------------------------------- calendario
def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    raw, out = line.encode("utf-8"), []
    while len(raw) > 74:
        cut = 74
        while raw[cut] & 0xC0 == 0x80:
            cut -= 1
        out.append(raw[:cut].decode("utf-8"))
        raw = b" " + raw[cut:]
    out.append(raw.decode("utf-8"))
    return "\r\n".join(out)


def build_ics(matches: list, cfg: dict, venues: dict, competition: str) -> str:
    team_disp = cfg["team"]["display"]
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//CV Itaca A//Calendario//ES", "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_esc(cfg['team']['calendar_name'])}", "X-WR-TIMEZONE:Europe/Madrid",
        "REFRESH-INTERVAL;VALUE=DURATION:P1D", "X-PUBLISHED-TTL:P1D",
        "BEGIN:VTIMEZONE", "TZID:Europe/Madrid",
        "BEGIN:DAYLIGHT", "TZOFFSETFROM:+0100", "TZOFFSETTO:+0200", "TZNAME:CEST", "DTSTART:19700329T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU", "END:DAYLIGHT",
        "BEGIN:STANDARD", "TZOFFSETFROM:+0200", "TZOFFSETTO:+0100", "TZNAME:CET", "DTSTART:19701025T030000",
        "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU", "END:STANDARD", "END:VTIMEZONE",
    ]
    for m in matches:
        if m.get("bye") or not m.get("active", True):
            continue
        home = team_disp if m["team_is_home"] else m["opponent_display"]
        away = m["opponent_display"] if m["team_is_home"] else team_disp
        title = f"J{m['jornada']}: {home} vs {away}"
        venue = venues.get(m["venue"], {}).get("name") or m["venue_display"]
        desc = [competition or "", "En casa" if m["team_is_home"] else "Fuera"]
        if m["time"] is None:
            title += " (hora por confirmar)"
            desc.append("Hora por confirmar")
        r = m.get("result")
        if r:
            mine, theirs = (r["home_sets"], r["away_sets"]) if m["team_is_home"] else (r["away_sets"], r["home_sets"])
            sets = " / ".join(f"{a}-{b}" if m["team_is_home"] else f"{b}-{a}" for a, b in r["sets"])
            desc.append(f"Resultado: {mine}-{theirs}" + (f" ({sets})" if sets else ""))
        stamp = datetime.fromisoformat(m["last_changed"]).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        d = date.fromisoformat(m["date"])
        lines += ["BEGIN:VEVENT", f"UID:{m['id']}@cv-itaca-a", f"DTSTAMP:{stamp}", f"LAST-MODIFIED:{stamp}",
                  f"SEQUENCE:{m.get('revision', 1)}"]
        if m["time"]:
            start = datetime.combine(d, datetime.strptime(m["time"], "%H:%M").time())
            lines += [f"DTSTART;TZID=Europe/Madrid:{start:%Y%m%dT%H%M%S}",
                      f"DTEND;TZID=Europe/Madrid:{start + timedelta(hours=2):%Y%m%dT%H%M%S}"]
        else:
            lines += [f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{d + timedelta(days=1):%Y%m%d}"]
        lines += [f"SUMMARY:{_esc(title)}", f"LOCATION:{_esc(venue)}", f"DESCRIPTION:{_esc(chr(10).join(x for x in desc if x))}",
                  "BEGIN:VALARM", "TRIGGER:-P1D", "ACTION:DISPLAY", f"DESCRIPTION:{_esc('Mañana juega el ' + team_disp)}",
                  "END:VALARM", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(x) for x in lines) + "\r\n"


# ----------------------------------------------------------------- estado
def record_run(status: dict, now: datetime, outcome: str, origin: str, detail: str = "",
               printed: str | None = None, counts: dict | None = None, kind: str | None = None) -> dict:
    stamp = iso(now)
    status["last_attempt"] = stamp
    status["last_outcome"] = outcome
    if outcome == "error":
        status["last_error"] = {"at": stamp, "kind": kind, "message": detail}
        status["consecutive_failures"] = status.get("consecutive_failures", 0) + 1
    else:
        status["last_success"] = stamp
        status["last_error"] = None
        status["consecutive_failures"] = 0
        if printed:
            status["source_printed_at"] = printed
        if counts:
            status["counts"] = counts
    runs = status.get("recent_runs", [])
    runs.insert(0, {"at": stamp, "outcome": outcome, "origin": origin, "detail": detail[:300]})
    status["recent_runs"] = runs[:MAX_RUNS_LOGGED]
    return status


def scheduled_skip_reason(now: datetime, from_hour: int, status: dict) -> str | None:
    """Motivo para no actuar en una ejecución programada, o None si toca actuar.

    GitHub puede retrasar las tareas programadas; por eso no se exige una hora exacta:
    basta con que en Madrid sean las `from_hour` o más y que hoy no haya ya una
    ejecución programada correcta (si la anterior falló, se reintenta)."""
    local = now.astimezone(MADRID)
    if local.hour < from_hour:
        return f"Aún no son las {from_hour}:00 en Madrid"
    for run in status.get("recent_runs", []):
        try:
            at = datetime.fromisoformat(run["at"]).astimezone(MADRID)
        except (KeyError, ValueError):
            continue
        if at.date() == local.date() and run.get("origin") == "programada" and run.get("outcome") != "error":
            return "Hoy ya hubo una actualización programada correcta"
    return None


def detect_origin(arg: str | None) -> str:
    if arg:
        return arg
    return {"schedule": "programada", "workflow_dispatch": "manual"}.get(os.environ.get("GITHUB_EVENT_NAME", ""), "local")


# ------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    ap.add_argument("--ics", default=str(ROOT / "calendar.ics"))
    ap.add_argument("--diag-dir", default=str(ROOT / "diag"))
    ap.add_argument("--text-file", help="usar este texto en vez de descargar el PDF")
    ap.add_argument("--dry-run", action="store_true", help="validar sin guardar nada")
    ap.add_argument("--force", action="store_true", help="permitir que bajen los resultados y saltar la fecha de fin de temporada")
    ap.add_argument("--only-if-madrid-hour", type=int, help="salir sin hacer nada si no es esa hora en Madrid")
    ap.add_argument("--scheduled-from-madrid-hour", type=int,
                    help="ejecución programada: actuar solo a partir de esa hora de Madrid y si hoy "
                         "no ha habido ya una ejecución programada correcta")
    ap.add_argument("--now", help="fecha y hora ISO a usar como 'ahora' (pruebas)")
    ap.add_argument("--origin", help="programada | manual | local | carga_inicial")
    args = ap.parse_args(argv)

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    data_dir = Path(args.data_dir)
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(MADRID)
    if now.tzinfo is None:
        now = now.replace(tzinfo=MADRID)

    if args.only_if_madrid_hour is not None and now.astimezone(MADRID).hour != args.only_if_madrid_hour:
        print("Fuera de la hora programada en Madrid; no se hace nada.")
        set_output("skipped", "true")
        return 0
    if args.scheduled_from_madrid_hour is not None:
        reason = scheduled_skip_reason(now, args.scheduled_from_madrid_hour,
                                       read_json(data_dir / "status.json", {}))
        if reason:
            print(reason + "; no se hace nada.")
            set_output("skipped", "true")
            return 0
    if not args.force and now.date() > date.fromisoformat(cfg["season_end"]):
        print("La temporada ha terminado; no se hace nada (usa --force para ejecutarlo igualmente).")
        set_output("skipped", "true")
        return 0

    origin = detect_origin(args.origin)
    status = read_json(data_dir / "status.json", {})
    text = None
    try:
        if args.text_file:
            text = Path(args.text_file).read_text(encoding="utf-8")
        else:
            http = cfg["http"]
            pdf = fetch_export(cfg["source"]["url"], http["timeout_seconds"], http["retries"], http["user_agent"])
            text = pdf_to_text(pdf)

        parsed = parse_export(text, cfg["team"]["name"], cfg["group_number"],
                              cfg["source"].get("printed_tz", "UTC"))
        matches_doc = read_json(data_dir / "matches.json", {"matches": []})
        previous = matches_doc.get("matches", [])
        validate(parsed, cfg, previous, force=args.force)

        merged, changes = merge(previous, parsed["matches"], now)
        counts = {"partidos": sum(1 for m in merged if not m["bye"] and m["active"]),
                  "jugados": sum(1 for m in merged if m["status"] == "jugado" and m["active"]),
                  "cambios": len(changes)}
        printed = iso(parsed["printed_at"])
        venues = read_json(data_dir / "venues.json", {})

        new_doc = {
            "team": cfg["team"], "competition": parsed["competition"], "season": parsed["season"],
            "source": {**cfg["source"], "printed_at": printed}, "matches": merged,
        }
        new_standings = {"printed_at": printed, "competition": parsed["competition"], "rows": parsed["standings"]}

        history = read_json(data_dir / "team_history.json", [])
        mine = next(r for r in parsed["standings"] if r["is_team"])
        entry = {"printed_at": printed, "pos": mine["pos"], "pj": mine["pj"], "pts": mine["pts"], "sf": mine["sf"], "sc": mine["sc"]}
        if not history or {k: history[-1].get(k) for k in entry if k != "printed_at"} != {k: entry[k] for k in entry if k != "printed_at"}:
            history.append(entry)
        changelog = (read_json(data_dir / "changelog.json", []) + changes)[-MAX_CHANGELOG:]

        outcome = "ok" if changes else "sin_cambios"
        record_run(status, now, outcome, origin, f"{len(changes)} cambios", printed, counts)
        print(f"Actualización correcta ({outcome}): {counts}")
        for c in changes:
            print("  -", c)

        if args.dry_run:
            print("--dry-run: no se guarda nada.")
            return 0
        write_json(data_dir / "matches.json", new_doc)
        write_json(data_dir / "standings.json", new_standings)
        write_json(data_dir / "team_history.json", history)
        write_json(data_dir / "changelog.json", changelog)
        write_json(data_dir / "status.json", status)
        write_text(Path(args.ics), build_ics(merged, cfg, venues, parsed["competition"]))
        return 0

    except UpdateError as exc:
        print(f"ERROR ({exc.kind}): {exc}", file=sys.stderr)
        if text:
            diag = Path(args.diag_dir)
            diag.mkdir(parents=True, exist_ok=True)
            (diag / "last_failed_export.txt").write_text(text, encoding="utf-8")
        if not args.dry_run:
            record_run(status, now, "error", origin, str(exc), kind=exc.kind)
            write_json(data_dir / "status.json", status)
        return 1


if __name__ == "__main__":
    sys.exit(main())
