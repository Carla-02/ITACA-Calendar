#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pruebas del actualizador. Ejecutar:  python -m unittest discover -s tests -v"""
import io
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from urllib import error as urlerror

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import update  # noqa: E402

FIXTURE = (ROOT / "tests" / "fixtures" / "export_2026-10-13.txt").read_text(encoding="utf-8")
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
NOW = datetime.fromisoformat("2026-10-13T20:35:00+02:00")
J3 = "3 18/10/2026 16:00 CV ITACA - A - CV STELLA MARIS NAUTILUS META CLUB DEPORTIVO (MASSANASSA)"
J1 = "1 04/10/2026 20:00 CV ITACA - A 3 - 0 (25-7/25-14/25-16) CV ITACA - B PAB. POL. SARA MORENO Y VICENTE LLI (MONCADA)"


def sandbox():
    """Copia de config + datos iniciales reales en un directorio temporal."""
    tmp = Path(tempfile.mkdtemp())
    shutil.copy(ROOT / "config.json", tmp / "config.json")
    shutil.copytree(ROOT / "data", tmp / "data")
    return tmp


def run(tmp, text=None, extra=None, now="2026-10-19T10:00:00+02:00"):
    """Ejecuta main() sobre el sandbox usando un texto dado."""
    args = ["--config", str(tmp / "config.json"), "--data-dir", str(tmp / "data"),
            "--ics", str(tmp / "calendar.ics"), "--diag-dir", str(tmp / "diag"), "--now", now]
    if text is not None:
        (tmp / "export.txt").write_text(text, encoding="utf-8")
        args += ["--text-file", str(tmp / "export.txt")]
    with mock.patch("sys.stderr", new_callable=io.StringIO), mock.patch("sys.stdout", new_callable=io.StringIO):
        rc = update.main(args + (extra or []))
    return rc


def load(tmp, name):
    return json.loads((tmp / "data" / name).read_text(encoding="utf-8"))


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.parsed = update.parse_export(FIXTURE, "CV ITACA - A", 1)

    def test_26_filas_del_equipo_y_24_partidos(self):
        ms = self.parsed["matches"]
        self.assertEqual([m["jornada"] for m in ms], list(range(1, 27)))
        self.assertEqual(sum(1 for m in ms if m["bye"]), 2)

    def test_resultado_local_y_visitante(self):
        j1, j2 = self.parsed["matches"][0], self.parsed["matches"][1]
        self.assertEqual(j1["result"], {"home_sets": 3, "away_sets": 0, "sets": [[25, 7], [25, 14], [25, 16]]})
        self.assertTrue(j1["team_is_home"])
        self.assertEqual((j2["result"]["home_sets"], j2["result"]["away_sets"]), (3, 2))
        self.assertFalse(j2["team_is_home"])

    def test_nombres_con_guiones_y_texto_pegado(self):
        by_j = {m["jornada"]: m for m in self.parsed["matches"]}
        self.assertEqual(by_j[5]["opponent"], "CV MEDITERRÁNEO - C")
        self.assertEqual(by_j[14]["opponent"], "CV ITACA - B")
        self.assertFalse(by_j[14]["team_is_home"])
        self.assertEqual(by_j[6]["venue"], "PISTA POLID. MUN. CANET D'EN BERENGUER")  # entidad HTML decodificada

    def test_hora_00_00_es_hora_por_confirmar(self):
        by_j = {m["jornada"]: m for m in self.parsed["matches"]}
        self.assertIsNone(by_j[4]["time"])
        self.assertEqual(by_j[3]["time"], "16:00")

    def test_descanso_sin_pabellon(self):
        j26 = self.parsed["matches"][-1]
        self.assertTrue(j26["bye"])
        self.assertEqual(j26["venue"], "")

    def test_clasificacion(self):
        rows = self.parsed["standings"]
        self.assertEqual(len(rows), 13)  # sin la fila DESCANSA
        mine = next(r for r in rows if r["is_team"])
        self.assertEqual((mine["pos"], mine["pts"], mine["pj"]), (2, 4, 2))

    def test_fecha_de_impresion(self):
        self.assertEqual(self.parsed["printed_at"].strftime("%Y-%m-%d %H:%M"), "2026-10-13 20:29")

    def test_pretty(self):
        self.assertEqual(update.pretty("CV MEDITERRÁNEO - C"), "Mediterráneo C")
        self.assertEqual(update.pretty("CV CAMP DE TURIA - B"), "Camp de Turia B")
        self.assertEqual(update.pretty("EL PILAR (VALENCIA)"), "El Pilar (Valencia)")


class DatosIncompletosTests(unittest.TestCase):
    def test_texto_vacio(self):
        with self.assertRaises(update.ParseError):
            update.parse_export("", "CV ITACA - A")

    def test_sin_clasificacion(self):
        with self.assertRaises(update.ParseError):
            update.parse_export(FIXTURE.split("CLASIFICACION")[0], "CV ITACA - A")

    def test_fila_del_equipo_sin_hora(self):
        bad = FIXTURE.replace(J3, "3 18/10/2026 CV ITACA - A - CV STELLA MARIS NAUTILUS META CLUB DEPORTIVO (MASSANASSA)")
        with self.assertRaises(update.ParseError):
            update.parse_export(bad, "CV ITACA - A")

    def test_falta_una_jornada(self):
        parsed = update.parse_export("\n".join(l for l in FIXTURE.splitlines() if not l.startswith("26 ")), "CV ITACA - A")
        with self.assertRaises(update.ValidationError) as ctx:
            update.validate(parsed, CFG, [])
        self.assertIn("26", str(ctx.exception))

    def test_resultado_incoherente(self):
        bad = FIXTURE.replace("3 - 0 (25-7/25-14/25-16)", "3 - 3 (25-7/25-14/25-16)")
        parsed = update.parse_export(bad, "CV ITACA - A")
        with self.assertRaises(update.ValidationError):
            update.validate(parsed, CFG, [])


class DuplicadosTests(unittest.TestCase):
    def test_fila_repetida_se_deduplica(self):
        parsed = update.parse_export(FIXTURE.replace(J3, J3 + "\n" + J3), "CV ITACA - A")
        self.assertEqual(len(parsed["matches"]), 26)

    def test_duplicado_con_datos_distintos_se_rechaza(self):
        other = J3.replace("16:00", "18:00")
        with self.assertRaises(update.ValidationError):
            update.parse_export(FIXTURE.replace(J3, J3 + "\n" + other), "CV ITACA - A")


class FusionTests(unittest.TestCase):
    def test_registros_nuevos_cambios_y_resultados(self):
        tmp = sandbox()
        # 1) sin cambios
        self.assertEqual(run(tmp, FIXTURE), 0)
        self.assertEqual(load(tmp, "status.json")["last_outcome"], "sin_cambios")
        # 2) cambio de hora + resultado nuevo en J1 ya existente y J3 jugado
        text = FIXTURE.replace(J3, "3 18/10/2026 18:30 CV ITACA - A 3 - 1 (25-20/20-25/25-18/25-22) CV STELLA MARIS NAUTILUS META CLUB DEPORTIVO (MASSANASSA)")
        self.assertEqual(run(tmp, text, now="2026-10-26T10:00:00+01:00"), 0)
        j3 = next(m for m in load(tmp, "matches.json")["matches"] if m["jornada"] == 3)
        self.assertEqual(j3["time"], "18:30")
        self.assertEqual(j3["status"], "jugado")
        self.assertEqual(j3["revision"], 2)
        self.assertEqual(j3["first_seen"], "2026-10-13T20:35:00+02:00")
        kinds = {(c["type"], c.get("field")) for c in load(tmp, "changelog.json")}
        self.assertIn(("modificado", "time"), kinds)
        self.assertIn(("resultado", "result"), kinds)
        shutil.rmtree(tmp)

    def test_el_historico_nunca_se_borra(self):
        prev = [dict(m, active=True, revision=1, first_seen="x", last_seen="x", last_changed="x")
                for m in update.parse_export(FIXTURE, "CV ITACA - A")["matches"]]
        merged, changes = update.merge(prev, prev[1:], NOW)
        self.assertEqual(len(merged), 26)
        self.assertFalse(next(m for m in merged if m["jornada"] == 1)["active"])
        self.assertEqual(changes[0]["type"], "desaparecido")

    def test_resultados_no_retroceden(self):
        tmp = sandbox()
        before = (tmp / "data" / "matches.json").read_bytes()
        sin_resultado = FIXTURE.replace(J1, J1.replace("3 - 0 (25-7/25-14/25-16) ", ""))
        self.assertEqual(run(tmp, sin_resultado), 1)
        self.assertEqual((tmp / "data" / "matches.json").read_bytes(), before)
        self.assertEqual(load(tmp, "status.json")["last_outcome"], "error")
        shutil.rmtree(tmp)


class ErroresTests(unittest.TestCase):
    def test_reintentos_y_error_de_conexion(self):
        calls = []

        def boom(*a, **k):
            calls.append(1)
            raise urlerror.URLError("sin red")

        with mock.patch("update.urlrequest.urlopen", boom):
            with self.assertRaises(update.FetchError):
                update.fetch_export("https://x.invalid/f.pdf", 5, 3, "test", sleep=lambda s: None)
        self.assertEqual(len(calls), 3)

    def test_respuesta_que_no_es_pdf(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = b"<html>Mantenimiento</html>"
        with mock.patch("update.urlrequest.urlopen", return_value=resp):
            with self.assertRaises(update.FetchError):
                update.fetch_export("https://x.invalid/f.pdf", 5, 2, "test", sleep=lambda s: None)

    def test_fallo_de_red_no_toca_los_datos_y_registra_el_error(self):
        tmp = sandbox()
        before = {n: (tmp / "data" / n).read_bytes() for n in ("matches.json", "standings.json")}
        with mock.patch("update.fetch_export", side_effect=update.FetchError("sin red")):
            self.assertEqual(run(tmp), 1)
        for n, b in before.items():
            self.assertEqual((tmp / "data" / n).read_bytes(), b)
        st = load(tmp, "status.json")
        self.assertEqual(st["last_outcome"], "error")
        self.assertEqual(st["last_error"]["kind"], "conexion")
        self.assertEqual(st["consecutive_failures"], 1)
        self.assertEqual(st["last_success"], "2026-10-13T20:35:00+02:00")  # se conserva la última correcta
        shutil.rmtree(tmp)

    def test_estructura_rota_guarda_diagnostico_y_no_toca_datos(self):
        tmp = sandbox()
        before = (tmp / "data" / "matches.json").read_bytes()
        self.assertEqual(run(tmp, "texto\n" * 30), 1)
        self.assertEqual((tmp / "data" / "matches.json").read_bytes(), before)
        self.assertTrue((tmp / "diag" / "last_failed_export.txt").exists())
        shutil.rmtree(tmp)


class ProgramacionTests(unittest.TestCase):
    def test_fuera_de_la_hora_no_hace_nada(self):
        tmp = sandbox()
        before = (tmp / "data" / "status.json").read_bytes()
        self.assertEqual(run(tmp, FIXTURE, extra=["--only-if-madrid-hour", "10"], now="2026-12-07T09:00:00+01:00"), 0)
        self.assertEqual((tmp / "data" / "status.json").read_bytes(), before)
        shutil.rmtree(tmp)

    def test_a_la_hora_correcta_si_actualiza(self):
        tmp = sandbox()
        self.assertEqual(run(tmp, FIXTURE, extra=["--only-if-madrid-hour", "10"], now="2026-12-07T10:00:00+01:00"), 0)
        self.assertEqual(load(tmp, "status.json")["last_attempt"], "2026-12-07T10:00:00+01:00")
        shutil.rmtree(tmp)

    def test_fuera_de_temporada(self):
        tmp = sandbox()
        before = (tmp / "data" / "status.json").read_bytes()
        self.assertEqual(run(tmp, FIXTURE, now="2027-08-01T10:00:00+02:00"), 0)
        self.assertEqual((tmp / "data" / "status.json").read_bytes(), before)
        shutil.rmtree(tmp)


class CalendarioTests(unittest.TestCase):
    def test_ics(self):
        tmp = sandbox()
        run(tmp, FIXTURE)
        ics = (tmp / "calendar.ics").read_bytes().decode("utf-8")  # bytes: conserva los CRLF
        self.assertEqual(ics.count("BEGIN:VEVENT"), 24)
        self.assertIn("SUMMARY:J1: Itaca A vs Itaca B", ics)
        self.assertIn("DTSTART;VALUE=DATE:20261024", ics)  # J4, hora por confirmar
        self.assertIn("DTSTART;TZID=Europe/Madrid:20261018T160000", ics)
        self.assertTrue(all(len(l.encode("utf-8")) <= 75 for l in ics.split("\r\n")))
        shutil.rmtree(tmp)


class PdfTests(unittest.TestCase):
    """Genera un PDF con el texto de prueba y comprueba la cadena PDF -> texto -> datos."""

    def test_ida_y_vuelta_por_pdf(self):
        try:
            import pdfplumber  # noqa: F401
            from reportlab.lib.pagesizes import A4, landscape
            from reportlab.pdfgen import canvas
        except ImportError:
            self.skipTest("faltan reportlab o pdfplumber")
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=landscape(A4))
        c.setFont("Courier", 6.2)
        y = 580
        for line in FIXTURE.replace("&#39;", "'").splitlines():
            c.drawString(20, y, line)
            y -= 7.4
        c.save()
        parsed = update.parse_export(update.pdf_to_text(buf.getvalue()), "CV ITACA - A", 1)
        update.validate(parsed, CFG, [])
        self.assertEqual(len(parsed["matches"]), 26)
        self.assertEqual(parsed["matches"][0]["result"]["home_sets"], 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
