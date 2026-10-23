"""Tests del exportador. Ejecutar: python -m unittest -v"""

import base64
import logging
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import CollectorRegistry

import sappo_exporter as sx

AHORA = datetime.now(timezone.utc)

# Los tests de fallos generan trazas de error a propósito
logging.disable(logging.CRITICAL)


def config(**cambios):
    base = dict(modo="simulado", url="", usuario="", password="", verificar_tls=True,
                estados=["systemError", "toBeDelivered"], ventana_dias=7, errores_nuevos_min=15,
                max_mensajes=5000, intervalo=60, timeout=5, metrics_usuario="monitor",
                metrics_password="secreto", puerto=0, tls_cert="", tls_key="")
    base.update(cambios)
    return sx.Config(**base)


def valor(registro, nombre, **etiquetas):
    return registro.get_sample_value(nombre, etiquetas or None)


class TestUtilidades(unittest.TestCase):
    def test_fecha_acepta_los_formatos_de_po(self):
        esperado = datetime(2026, 10, 23, 8, 0, tzinfo=timezone.utc)
        self.assertEqual(sx._fecha(esperado), esperado)
        self.assertEqual(sx._fecha("2026-10-23T10:00:00+02:00"), esperado)
        self.assertEqual(sx._fecha({"date": "2026-10-23T08:00:00Z", "timezone": "UTC"}), esperado)
        self.assertEqual(sx._fecha(datetime(2026, 10, 23, 8, 0)), esperado)  # sin zona → UTC
        self.assertIsNone(sx._fecha("no es fecha"))
        self.assertIsNone(sx._fecha(None))

    def test_buscar_mensajes_en_cualquier_nivel(self):
        respuesta = {"Response": {"list": {"AdapterFrameworkData": [
            {"messageID": "a", "interface": {"name": "SI_A"}},
            {"messageKey": "b", "interface": "SI_B"},
        ]}, "number": 2}}
        encontrados = list(sx._buscar_mensajes(respuesta))
        self.assertEqual([sx._interfaz(m) for m in encontrados], ["SI_A", "SI_B"])
        self.assertEqual(sx._interfaz({}), "desconocida")


class FuenteFija:
    def __init__(self, mensajes):
        self.lista = mensajes

    def mensajes(self, estado, desde, hasta):
        return [m for m in self.lista if m.estado == estado and desde <= m.inicio <= hasta]


class TestRecolector(unittest.TestCase):
    def test_metricas_por_estado_e_interfaz(self):
        lista = [
            sx.Mensaje("systemError", AHORA - timedelta(minutes=5), "SI_A"),
            sx.Mensaje("systemError", AHORA - timedelta(hours=3), "SI_A"),
            sx.Mensaje("toBeDelivered", AHORA - timedelta(minutes=40), "SI_B"),
            sx.Mensaje("toBeDelivered", AHORA - timedelta(days=30), "SI_B"),  # fuera de la ventana
        ]
        registro = CollectorRegistry()
        r = sx.Recolector(config(), lambda cfg: FuenteFija(lista), registro)
        r.ciclo()

        self.assertEqual(valor(registro, "sappo_consulta_ok"), 1)
        self.assertEqual(valor(registro, "sappo_mensajes", estado="systemError"), 2)
        self.assertEqual(valor(registro, "sappo_mensajes", estado="toBeDelivered"), 1)
        self.assertEqual(valor(registro, "sappo_errores_nuevos"), 1)
        self.assertAlmostEqual(valor(registro, "sappo_mensaje_mas_antiguo_segundos", estado="toBeDelivered"),
                               2400, delta=30)
        self.assertEqual(valor(registro, "sappo_mensajes_por_interfaz", estado="systemError", interfaz="SI_A"), 2)

        # Si una interfaz deja de tener mensajes, su serie desaparece
        lista[:] = [sx.Mensaje("toBeDelivered", AHORA, "SI_C")]
        r.ciclo()
        self.assertIsNone(valor(registro, "sappo_mensajes_por_interfaz", estado="systemError", interfaz="SI_A"))
        self.assertEqual(valor(registro, "sappo_mensajes", estado="systemError"), 0)
        self.assertEqual(valor(registro, "sappo_mensaje_mas_antiguo_segundos", estado="systemError"), 0)

    def test_resultado_truncado(self):
        lista = [sx.Mensaje("systemError", AHORA, "SI_A") for _ in range(3)]
        registro = CollectorRegistry()
        sx.Recolector(config(max_mensajes=3), lambda cfg: FuenteFija(lista), registro).ciclo()
        self.assertEqual(valor(registro, "sappo_resultado_truncado", estado="systemError"), 1)
        self.assertEqual(valor(registro, "sappo_resultado_truncado", estado="toBeDelivered"), 0)

    def test_error_de_po_marca_consulta_fallida_y_reconecta(self):
        intentos = []

        def fuente_rota(cfg):
            intentos.append(1)
            raise ConnectionError("PO no responde")

        registro = CollectorRegistry()
        r = sx.Recolector(config(), fuente_rota, registro)
        r.ciclo()
        r.ciclo()
        self.assertEqual(valor(registro, "sappo_consulta_ok"), 0)
        self.assertEqual(len(intentos), 2)

    def test_modo_simulado_genera_datos(self):
        registro = CollectorRegistry()
        sx.Recolector(config(), lambda cfg: sx.FuenteSimulada(semilla=1), registro).ciclo()
        self.assertGreaterEqual(valor(registro, "sappo_errores_nuevos"), 1)
        self.assertGreater(valor(registro, "sappo_mensaje_mas_antiguo_segundos", estado="toBeDelivered"), 3600)


class TestServidorHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        registro = CollectorRegistry()
        sx.Recolector(config(), lambda cfg: FuenteFija([]), registro).ciclo()
        cls.servidor = sx.crear_servidor(config(), registro)
        cls.url = f"http://127.0.0.1:{cls.servidor.server_address[1]}"
        threading.Thread(target=cls.servidor.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.servidor.shutdown()
        cls.servidor.server_close()

    def pedir(self, ruta, usuario=None, password=None):
        peticion = urllib.request.Request(self.url + ruta)
        if usuario is not None:
            token = base64.b64encode(f"{usuario}:{password}".encode()).decode()
            peticion.add_header("Authorization", "Basic " + token)
        try:
            with urllib.request.urlopen(peticion, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, ""

    def test_metrics_exige_credenciales(self):
        self.assertEqual(self.pedir("/metrics")[0], 401)
        self.assertEqual(self.pedir("/metrics", "monitor", "mala")[0], 401)
        self.assertEqual(self.pedir("/metrics", "otro", "secreto")[0], 401)
        codigo, cuerpo = self.pedir("/metrics", "monitor", "secreto")
        self.assertEqual(codigo, 200)
        self.assertIn("sappo_consulta_ok 1.0", cuerpo)

    def test_health_sin_credenciales(self):
        self.assertEqual(self.pedir("/health"), (200, "ok\n"))
        self.assertEqual(self.pedir("/otra")[0], 404)


# ─────────────── SOAP: servidor falso con un WSDL similar al de PO ───────────────

WSDL = """<?xml version="1.0" encoding="UTF-8"?>
<wsdl:definitions xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/"
    xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/"
    xmlns:xs="http://www.w3.org/2001/XMLSchema"
    xmlns:tns="urn:AdapterMessageMonitoringVi"
    xmlns:ws="urn:com.sap.aii.mdt.server.adapterframework.ws"
    xmlns:api="urn:com.sap.aii.mdt.api.data"
    targetNamespace="urn:AdapterMessageMonitoringVi">
  <wsdl:types>
    <xs:schema targetNamespace="urn:com.sap.aii.mdt.api.data" elementFormDefault="qualified">
      <xs:complexType name="BusinessCalendar">
        <xs:sequence>
          <xs:element name="date" type="xs:dateTime" minOccurs="0"/>
          <xs:element name="timezone" type="xs:string" minOccurs="0"/>
        </xs:sequence>
      </xs:complexType>
      <xs:complexType name="ConnectionData">
        <xs:sequence>
          <xs:element name="name" type="xs:string" minOccurs="0"/>
          <xs:element name="namespace" type="xs:string" minOccurs="0"/>
        </xs:sequence>
      </xs:complexType>
    </xs:schema>
    <xs:schema targetNamespace="urn:com.sap.aii.mdt.server.adapterframework.ws" elementFormDefault="qualified">
      <xs:import namespace="urn:com.sap.aii.mdt.api.data"/>
      <xs:complexType name="AdapterFilter">
        <xs:sequence>
          <xs:element name="archive" type="xs:boolean"/>
          <xs:element name="dateType" type="xs:int"/>
          <xs:element name="fromTime" type="xs:dateTime" minOccurs="0"/>
          <xs:element name="nodeId" type="xs:int"/>
          <xs:element name="onlyFaultyMessages" type="xs:boolean"/>
          <xs:element name="status" type="xs:string" minOccurs="0"/>
          <xs:element name="toTime" type="xs:dateTime" minOccurs="0"/>
          <xs:element name="wasEdited" type="xs:boolean"/>
        </xs:sequence>
      </xs:complexType>
      <xs:complexType name="AdapterFrameworkData">
        <xs:sequence>
          <xs:element name="messageID" type="xs:string" minOccurs="0"/>
          <xs:element name="status" type="xs:string" minOccurs="0"/>
          <xs:element name="startTime" type="api:BusinessCalendar" minOccurs="0"/>
          <xs:element name="interface" type="api:ConnectionData" minOccurs="0"/>
        </xs:sequence>
      </xs:complexType>
      <xs:complexType name="ArrayOfAdapterFrameworkData">
        <xs:sequence>
          <xs:element name="AdapterFrameworkData" type="ws:AdapterFrameworkData" minOccurs="0" maxOccurs="unbounded"/>
        </xs:sequence>
      </xs:complexType>
      <xs:complexType name="AdapterFrameworkDataList">
        <xs:sequence>
          <xs:element name="list" type="ws:ArrayOfAdapterFrameworkData" minOccurs="0"/>
        </xs:sequence>
      </xs:complexType>
    </xs:schema>
    <xs:schema targetNamespace="urn:AdapterMessageMonitoringVi" elementFormDefault="qualified">
      <xs:import namespace="urn:com.sap.aii.mdt.server.adapterframework.ws"/>
      <xs:element name="getMessageList">
        <xs:complexType>
          <xs:sequence>
            <xs:element name="filter" type="ws:AdapterFilter"/>
            <xs:element name="maxMessages" type="xs:int" minOccurs="0"/>
          </xs:sequence>
        </xs:complexType>
      </xs:element>
      <xs:element name="getMessageListResponse">
        <xs:complexType>
          <xs:sequence>
            <xs:element name="Response" type="ws:AdapterFrameworkDataList" minOccurs="0"/>
          </xs:sequence>
        </xs:complexType>
      </xs:element>
    </xs:schema>
  </wsdl:types>
  <wsdl:message name="getMessageListIn"><wsdl:part name="parameters" element="tns:getMessageList"/></wsdl:message>
  <wsdl:message name="getMessageListOut"><wsdl:part name="parameters" element="tns:getMessageListResponse"/></wsdl:message>
  <wsdl:portType name="AdapterMessageMonitoringVi">
    <wsdl:operation name="getMessageList">
      <wsdl:input message="tns:getMessageListIn"/>
      <wsdl:output message="tns:getMessageListOut"/>
    </wsdl:operation>
  </wsdl:portType>
  <wsdl:binding name="AdapterMessageMonitoringViBinding" type="tns:AdapterMessageMonitoringVi">
    <soap:binding transport="http://schemas.xmlsoap.org/soap/http" style="document"/>
    <wsdl:operation name="getMessageList">
      <soap:operation soapAction=""/>
      <wsdl:input><soap:body use="literal"/></wsdl:input>
      <wsdl:output><soap:body use="literal"/></wsdl:output>
    </wsdl:operation>
  </wsdl:binding>
  <wsdl:service name="AdapterMessageMonitoring">
    <wsdl:port name="AdapterMessageMonitoringViPort" binding="tns:AdapterMessageMonitoringViBinding">
      <soap:address location="http://po-nombre-interno:50000/AdapterMessageMonitoring/basic"/>
    </wsdl:port>
  </wsdl:service>
</wsdl:definitions>"""

RESPUESTA = """<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">
  <SOAP-ENV:Body>
    <rpl:getMessageListResponse xmlns:rpl="urn:AdapterMessageMonitoringVi">
      <rpl:Response xmlns:rn1="urn:com.sap.aii.mdt.server.adapterframework.ws" xmlns:rn2="urn:com.sap.aii.mdt.api.data">
        <rn1:list>
          <rn1:AdapterFrameworkData>
            <rn1:messageID>1111</rn1:messageID>
            <rn1:status>systemError</rn1:status>
            <rn1:startTime><rn2:date>{reciente}</rn2:date><rn2:timezone>UTC</rn2:timezone></rn1:startTime>
            <rn1:interface><rn2:name>SI_Pedidos_Out</rn2:name><rn2:namespace>urn:empresa</rn2:namespace></rn1:interface>
          </rn1:AdapterFrameworkData>
          <rn1:AdapterFrameworkData>
            <rn1:messageID>2222</rn1:messageID>
            <rn1:status>systemError</rn1:status>
            <rn1:startTime><rn2:date>{antiguo}</rn2:date><rn2:timezone>UTC</rn2:timezone></rn1:startTime>
            <rn1:interface><rn2:name>SI_Pedidos_Out</rn2:name><rn2:namespace>urn:empresa</rn2:namespace></rn1:interface>
          </rn1:AdapterFrameworkData>
        </rn1:list>
      </rpl:Response>
    </rpl:getMessageListResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""


class PoFalso(BaseHTTPRequestHandler):
    peticiones = []
    AUTH = "Basic " + base64.b64encode(b"PIMONITOR:clave").decode()

    def _auth(self):
        if self.headers.get("Authorization") != self.AUTH:
            self.send_response(401)
            self.end_headers()
            return False
        return True

    def do_GET(self):
        if self._auth():
            self._enviar(WSDL)

    def do_POST(self):
        if not self._auth():
            return
        cuerpo = self.rfile.read(int(self.headers["Content-Length"])).decode()
        PoFalso.peticiones.append((self.path, cuerpo))
        fmt = "%Y-%m-%dT%H:%M:%S+00:00"
        reciente = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime(fmt)
        antiguo = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(fmt)
        self._enviar(RESPUESTA.format(reciente=reciente, antiguo=antiguo))

    def _enviar(self, texto):
        datos = texto.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(datos)))
        self.end_headers()
        self.wfile.write(datos)

    def log_message(self, *args):
        pass


class TestFuenteSoap(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.po = ThreadingHTTPServer(("127.0.0.1", 0), PoFalso)
        threading.Thread(target=cls.po.serve_forever, daemon=True).start()
        cls.cfg = config(modo="soap", url=f"http://127.0.0.1:{cls.po.server_address[1]}",
                         usuario="PIMONITOR", password="clave", estados=["systemError"])

    @classmethod
    def tearDownClass(cls):
        cls.po.shutdown()
        cls.po.server_close()

    def test_consulta_completa_contra_po_falso(self):
        registro = CollectorRegistry()
        sx.Recolector(self.cfg, sx.FuenteSoap, registro).ciclo()

        self.assertEqual(valor(registro, "sappo_consulta_ok"), 1)
        self.assertEqual(valor(registro, "sappo_mensajes", estado="systemError"), 2)
        self.assertEqual(valor(registro, "sappo_errores_nuevos"), 1)
        self.assertEqual(valor(registro, "sappo_mensajes_por_interfaz",
                               estado="systemError", interfaz="SI_Pedidos_Out"), 2)

        ruta, cuerpo = PoFalso.peticiones[-1]
        # Se llama a SAPPO_URL, no al nombre interno que trae el WSDL
        self.assertEqual(ruta, "/AdapterMessageMonitoring/basic")
        self.assertIn("systemError", cuerpo)
        self.assertIn("5000", cuerpo)
        # Campos que no existen en este WSDL no se envían
        self.assertNotIn("timesFailed", cuerpo)

    def test_credenciales_incorrectas(self):
        cfg = config(**{**self.cfg.__dict__, "password": "mala"})
        registro = CollectorRegistry()
        sx.Recolector(cfg, sx.FuenteSoap, registro).ciclo()
        self.assertEqual(valor(registro, "sappo_consulta_ok"), 0)


if __name__ == "__main__":
    unittest.main()
