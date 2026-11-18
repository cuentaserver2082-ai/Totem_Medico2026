"""Pruebas rápidas de los módulos puros (sin FastAPI ni base de datos).
Ejecutar desde la carpeta del proyecto:  python -m unittest discover tests -v
"""
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from servicios.filtro_texto import validar_mensaje
from servicios.triage import triage_por_reglas

VITALES_OK = dict(temp_c=36.8, spo2=98, fc_lpm=80, pa_sis=120, pa_dia=80)


class TestFiltro(unittest.TestCase):
    def test_rechaza(self):
        casos = {"": "vacio", "   ": "vacio", "12345": "sinsentido", "asdfgh": "sinsentido",
                 "aaaaaa": "sinsentido", "qwerty": "sinsentido", "bcdfgh": "sinsentido",
                 "hola": "irrelevante", "ok": "irrelevante", "weeeon": "groseria", "m1erda": "groseria"}
        for texto, codigo in casos.items():
            r = validar_mensaje(texto)
            self.assertFalse(r.valido, texto)
            self.assertEqual(r.codigo, codigo, texto)

    def test_acepta(self):
        for texto in ["Me duele la cabeza", "3 días", "si", "no", "38.5 de fiebre", "Me duele el culo",
                      "buenas tardes, tengo tos", "desde ayer"]:
            self.assertTrue(validar_mensaje(texto).valido, texto)


class TestTriage(unittest.TestCase):
    def test_leve_sin_alertas(self):
        self.assertEqual(triage_por_reglas(["Me duele la cabeza"], VITALES_OK).prioridad, 4)

    def test_pecho_es_urgente(self):
        self.assertEqual(triage_por_reglas(["Siento molestia en el pecho"], VITALES_OK).prioridad, 2)

    def test_emergencia_por_texto(self):
        self.assertEqual(triage_por_reglas(["no puedo respirar"], VITALES_OK).prioridad, 1)

    def test_signos_vitales_suben_prioridad(self):
        self.assertEqual(triage_por_reglas(["tos"], {**VITALES_OK, "spo2": 91}).prioridad, 2)
        self.assertEqual(triage_por_reglas(["nada"], {**VITALES_OK, "pa_sis": 185, "pa_dia": 100}).prioridad, 1)

    def test_combinacion_moderada_sube_a_urgente(self):
        self.assertEqual(triage_por_reglas(["tengo fiebre"], {**VITALES_OK, "temp_c": 38.6, "fc_lpm": 105}).prioridad, 2)

    def test_sin_signos_vitales(self):
        self.assertEqual(triage_por_reglas(["control de receta"], None).prioridad, 4)


if __name__ == "__main__":
    unittest.main()
