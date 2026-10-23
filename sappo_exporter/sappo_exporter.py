"""Exportador Prometheus para SAP PO (Adapter Engine).

Consulta periódicamente el servicio SOAP AdapterMessageMonitoringVi y publica en
/metrics (protegido con usuario y contraseña) cuántos mensajes hay en cada estado,
cuál es el más antiguo y cuántos errores nuevos han aparecido.

La consulta a PO se hace en segundo plano cada SAPPO_INTERVALO segundos; /metrics
devuelve el último resultado, así un scrape nunca carga PO.

Modos (SAPPO_MODO):
  soap      consulta el SAP PO real (por defecto)
  simulado  genera mensajes ficticios para probar el monitor sin PO
"""

import base64
import hmac
import logging
import os
import random
import signal
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest

log = logging.getLogger("sappo_exporter")

# Estados del Adapter Engine. "toBeDelivered" es lo que el monitor de PO muestra como "Scheduled".
ESTADOS_POR_DEFECTO = "systemError,toBeDelivered,holding,waiting"
ESTADO_ERROR = "systemError"


# ───────────────────────────── Configuración ─────────────────────────────

def _env(nombre, defecto=None, obligatoria=False):
    valor = os.environ.get(nombre, defecto)
    if obligatoria and not valor:
        raise SystemExit(f"Falta la variable de entorno {nombre}")
    return valor


def _env_bool(nombre, defecto):
    return os.environ.get(nombre, str(defecto)).strip().lower() in ("1", "true", "si", "sí", "yes")


@dataclass
class Config:
    modo: str
    url: str
    usuario: str
    password: str
    verificar_tls: bool
    estados: list
    ventana_dias: float
    errores_nuevos_min: float
    max_mensajes: int
    intervalo: float
    timeout: float
    metrics_usuario: str
    metrics_password: str
    puerto: int
    tls_cert: str
    tls_key: str

    @classmethod
    def desde_entorno(cls):
        modo = _env("SAPPO_MODO", "soap").lower()
        if modo not in ("soap", "simulado"):
            raise SystemExit("SAPPO_MODO debe ser 'soap' o 'simulado'")
        soap = modo == "soap"
        return cls(
            modo=modo,
            url=(_env("SAPPO_URL", obligatoria=soap) or "").rstrip("/"),
            usuario=_env("SAPPO_USUARIO", obligatoria=soap),
            password=_env("SAPPO_PASSWORD", obligatoria=soap),
            verificar_tls=_env_bool("SAPPO_VERIFICAR_TLS", True),
            estados=[e.strip() for e in _env("SAPPO_ESTADOS", ESTADOS_POR_DEFECTO).split(",") if e.strip()],
            ventana_dias=float(_env("SAPPO_VENTANA_DIAS", "7")),
            errores_nuevos_min=float(_env("SAPPO_ERRORES_NUEVOS_MIN", "15")),
            max_mensajes=int(_env("SAPPO_MAX_MENSAJES", "5000")),
            intervalo=float(_env("SAPPO_INTERVALO", "60")),
            timeout=float(_env("SAPPO_TIMEOUT", "30")),
            metrics_usuario=_env("METRICS_USUARIO", "monitor"),
            metrics_password=_env("METRICS_PASSWORD", obligatoria=True),
            puerto=int(_env("METRICS_PUERTO", "9105")),
            tls_cert=_env("METRICS_TLS_CERT", ""),
            tls_key=_env("METRICS_TLS_KEY", ""),
        )


# ───────────────────────────── Fuentes de datos ─────────────────────────────

@dataclass
class Mensaje:
    estado: str
    inicio: datetime  # siempre con zona horaria
    interfaz: str


def _fecha(valor):
    """Convierte la fecha que devuelve PO (datetime, texto ISO o {'date': ...}) a datetime con zona."""
    if isinstance(valor, dict):
        return _fecha(valor.get("date"))
    if isinstance(valor, str):
        try:
            valor = datetime.fromisoformat(valor)
        except ValueError:
            return None
    if isinstance(valor, datetime):
        return valor if valor.tzinfo else valor.replace(tzinfo=timezone.utc)
    return None


def _interfaz(datos):
    interfaz = datos.get("interface")
    if isinstance(interfaz, dict):
        interfaz = interfaz.get("name")
    return interfaz or "desconocida"


def _buscar_mensajes(nodo):
    """Recorre la respuesta SOAP y devuelve los AdapterFrameworkData, estén donde estén.

    La forma exacta de la respuesta cambia entre versiones de PO, así que se busca
    cualquier objeto con messageID o messageKey.
    """
    if isinstance(nodo, dict):
        if "messageID" in nodo or "messageKey" in nodo:
            yield nodo
            return
        for valor in nodo.values():
            yield from _buscar_mensajes(valor)
    elif isinstance(nodo, (list, tuple)):
        for valor in nodo:
            yield from _buscar_mensajes(valor)


class FuenteSoap:
    """Lee mensajes de SAP PO con AdapterMessageMonitoringVi.getMessageList."""

    RUTA_WSDL = "/AdapterMessageMonitoring/basic?wsdl&mode=ws_policy&style=document"

    def __init__(self, cfg):
        import requests
        from zeep import Client, Settings
        from zeep.transports import Transport

        sesion = requests.Session()
        sesion.auth = (cfg.usuario, cfg.password)
        sesion.verify = cfg.verificar_tls
        self.cfg = cfg
        self.client = Client(
            cfg.url + self.RUTA_WSDL,
            transport=Transport(session=sesion, timeout=cfg.timeout, operation_timeout=cfg.timeout),
            settings=Settings(strict=False, xml_huge_tree=True),
        )
        self.servicio, self.campos_filtro = self._servicio()
        log.info("Conectado a SAP PO en %s", cfg.url)

    def _servicio(self):
        # Se usa el puerto del WSDL que tenga getMessageList, pero apuntando a SAPPO_URL:
        # la dirección del WSDL suele ser el nombre interno del servidor.
        for servicio in self.client.wsdl.services.values():
            for puerto in servicio.ports.values():
                try:
                    operacion = puerto.binding.get("getMessageList")
                except ValueError:
                    continue
                ruta = urlsplit(puerto.binding_options["address"]).path
                proxy = self.client.create_service(str(puerto.binding.name), self.cfg.url + ruta)
                return proxy, self._campos_filtro(operacion)
        raise RuntimeError("El WSDL de PO no tiene la operación getMessageList")

    @staticmethod
    def _campos_filtro(operacion):
        try:
            elementos = dict(operacion.input.body.type.elements)
            return {nombre for nombre, _ in elementos["filter"].type.elements}
        except Exception:
            return None  # no se pudo leer el tipo: se envía el filtro completo

    def mensajes(self, estado, desde, hasta):
        from zeep.helpers import serialize_object

        filtro = {
            "status": estado,
            "fromTime": desde,
            "toTime": hasta,
            "archive": False,
            "dateType": 0,
            "nodeId": 0,
            "onlyFaultyMessages": False,
            "timesFailed": 0,
            "wasEdited": False,
        }
        if self.campos_filtro:
            filtro = {k: v for k, v in filtro.items() if k in self.campos_filtro}
        respuesta = self.servicio.getMessageList(filter=filtro, maxMessages=self.cfg.max_mensajes)
        datos = serialize_object(respuesta, dict)
        mensajes = []
        for d in _buscar_mensajes(datos):
            inicio = _fecha(d.get("startTime"))
            if inicio:
                mensajes.append(Mensaje(estado, inicio, _interfaz(d)))
        return mensajes


class FuenteSimulada:
    """Mensajes ficticios que cambian en cada ciclo, para probar dashboards y alertas."""

    INTERFACES = ["SI_Pedidos_Out", "SI_Facturas_Out", "SI_Stock_In", "SI_Clientes_Sync"]

    def __init__(self, cfg=None, semilla=None):
        self.azar = random.Random(semilla)
        ahora = datetime.now(timezone.utc)
        hace = lambda minutos: ahora - timedelta(minutes=minutos)
        self.lista = [
            # Dos errores, uno reciente: dispara "errores nuevos"
            Mensaje("systemError", hace(3), "SI_Facturas_Out"),
            Mensaje("systemError", hace(130), "SI_Pedidos_Out"),
            # Un scheduled atascado de hace 70 min: dispara "scheduled antiguo"
            Mensaje("toBeDelivered", hace(70), "SI_Stock_In"),
            Mensaje("holding", hace(25), "SI_Clientes_Sync"),
        ] + [Mensaje("toBeDelivered", hace(self.azar.randint(0, 20)), self._interfaz()) for _ in range(10)]
        self.atascado = self.lista[2]

    def _interfaz(self):
        return self.azar.choice(self.INTERFACES)

    def avanzar(self):
        ahora = datetime.now(timezone.utc)
        if self.azar.random() < 0.15:
            self.lista.append(Mensaje("systemError", ahora, self._interfaz()))
        for _ in range(self.azar.randint(0, 6)):
            self.lista.append(Mensaje("toBeDelivered", ahora, self._interfaz()))
        # Se entregan algunos scheduled, salvo el atascado
        entregables = [m for m in self.lista if m.estado == "toBeDelivered" and m is not self.atascado]
        for m in self.azar.sample(entregables, k=min(len(entregables), self.azar.randint(0, 6))):
            self.lista.remove(m)

    def mensajes(self, estado, desde, hasta):
        return [m for m in self.lista if m.estado == estado and desde <= m.inicio <= hasta]


# ───────────────────────────── Métricas ─────────────────────────────

class Recolector:
    def __init__(self, cfg, crear_fuente, registro):
        self.cfg = cfg
        self.crear_fuente = crear_fuente
        self.fuente = None
        g = lambda nombre, ayuda, etiquetas=(): Gauge(nombre, ayuda, etiquetas, registry=registro)
        self.m_mensajes = g("sappo_mensajes", "Mensajes en cada estado dentro de la ventana consultada", ["estado"])
        self.m_antiguo = g("sappo_mensaje_mas_antiguo_segundos",
                           "Antigüedad del mensaje más antiguo en cada estado (0 si no hay)", ["estado"])
        self.m_interfaz = g("sappo_mensajes_por_interfaz", "Mensajes por estado e interfaz", ["estado", "interfaz"])
        self.m_nuevos = g("sappo_errores_nuevos",
                          "Mensajes en systemError que empezaron en los últimos SAPPO_ERRORES_NUEVOS_MIN minutos")
        self.m_truncado = g("sappo_resultado_truncado",
                            "1 si PO devolvió SAPPO_MAX_MENSAJES mensajes: el recuento real puede ser mayor", ["estado"])
        self.m_ok = g("sappo_consulta_ok", "1 si la última consulta a SAP PO funcionó")
        self.m_duracion = g("sappo_consulta_duracion_segundos", "Duración de la última consulta a SAP PO")
        self.m_timestamp = g("sappo_consulta_timestamp_seconds", "Momento de la última consulta correcta a SAP PO")

    def ciclo(self):
        inicio = time.monotonic()
        ahora = datetime.now(timezone.utc)
        desde = ahora - timedelta(days=self.cfg.ventana_dias)
        try:
            if self.fuente is None:
                self.fuente = self.crear_fuente(self.cfg)
            if hasattr(self.fuente, "avanzar"):
                self.fuente.avanzar()
            resultados = {e: self.fuente.mensajes(e, desde, ahora) for e in self.cfg.estados}
            self.publicar(resultados, ahora)
            self.m_ok.set(1)
            self.m_timestamp.set(ahora.timestamp())
        except Exception:
            log.exception("Error consultando SAP PO")
            self.m_ok.set(0)
            self.fuente = None  # reconectar en el siguiente ciclo
        finally:
            self.m_duracion.set(time.monotonic() - inicio)

    def publicar(self, resultados, ahora):
        self.m_interfaz.clear()  # las interfaces que ya no tienen mensajes desaparecen
        for estado, mensajes in resultados.items():
            self.m_mensajes.labels(estado).set(len(mensajes))
            antiguo = min((m.inicio for m in mensajes), default=None)
            self.m_antiguo.labels(estado).set((ahora - antiguo).total_seconds() if antiguo else 0)
            self.m_truncado.labels(estado).set(1 if len(mensajes) >= self.cfg.max_mensajes else 0)
            por_interfaz = {}
            for m in mensajes:
                por_interfaz[m.interfaz] = por_interfaz.get(m.interfaz, 0) + 1
            for interfaz, n in por_interfaz.items():
                self.m_interfaz.labels(estado, interfaz).set(n)
        limite = ahora - timedelta(minutes=self.cfg.errores_nuevos_min)
        self.m_nuevos.set(sum(1 for m in resultados.get(ESTADO_ERROR, []) if m.inicio >= limite))


# ───────────────────────────── Servidor HTTP ─────────────────────────────

def crear_servidor(cfg, registro):
    esperado_usuario = cfg.metrics_usuario.encode()
    esperado_password = cfg.metrics_password.encode()

    class Manejador(BaseHTTPRequestHandler):
        def do_GET(self):
            ruta = self.path.split("?", 1)[0]
            if ruta == "/health":
                return self._responder(200, b"ok\n", "text/plain")
            if ruta != "/metrics":
                return self._responder(404, b"no encontrado\n", "text/plain")
            if not self._autorizado():
                return self._responder(401, b"no autorizado\n", "text/plain",
                                       {"WWW-Authenticate": 'Basic realm="sappo_exporter"'})
            return self._responder(200, generate_latest(registro), CONTENT_TYPE_LATEST)

        def _autorizado(self):
            cabecera = self.headers.get("Authorization", "")
            if not cabecera.startswith("Basic "):
                return False
            try:
                usuario, _, password = base64.b64decode(cabecera[6:]).partition(b":")
            except ValueError:
                return False
            # compare_digest en los dos campos, sin cortocircuito, para no filtrar tiempos
            ok_usuario = hmac.compare_digest(usuario, esperado_usuario)
            ok_password = hmac.compare_digest(password, esperado_password)
            return ok_usuario and ok_password

        def _responder(self, codigo, cuerpo, tipo, cabeceras=None):
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(cuerpo)))
            for k, v in (cabeceras or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(cuerpo)

        def log_message(self, formato, *args):
            log.debug("%s %s", self.address_string(), formato % args)

    servidor = ThreadingHTTPServer(("0.0.0.0", cfg.puerto), Manejador)
    if cfg.tls_cert:
        contexto = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        contexto.minimum_version = ssl.TLSVersion.TLSv1_2
        contexto.load_cert_chain(cfg.tls_cert, cfg.tls_key or None)
        servidor.socket = contexto.wrap_socket(servidor.socket, server_side=True)
    return servidor


# ───────────────────────────── Arranque ─────────────────────────────

def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.desde_entorno()
    registro = CollectorRegistry()
    crear_fuente = FuenteSimulada if cfg.modo == "simulado" else FuenteSoap
    recolector = Recolector(cfg, crear_fuente, registro)
    parar = threading.Event()

    def bucle():
        while not parar.is_set():
            recolector.ciclo()
            parar.wait(cfg.intervalo)

    threading.Thread(target=bucle, name="recolector", daemon=True).start()
    servidor = crear_servidor(cfg, registro)

    def terminar(*_):
        parar.set()
        threading.Thread(target=servidor.shutdown).start()

    signal.signal(signal.SIGTERM, terminar)
    signal.signal(signal.SIGINT, terminar)
    log.info("Modo %s · estados %s · /metrics en el puerto %s (%s)", cfg.modo, ",".join(cfg.estados),
             cfg.puerto, "https" if cfg.tls_cert else "http")
    servidor.serve_forever()


if __name__ == "__main__":
    main()
