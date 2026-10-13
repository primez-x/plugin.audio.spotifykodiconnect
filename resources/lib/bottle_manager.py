import socket
import socketserver
import threading
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer
from wsgiref.simple_server import make_server

import bottle
from bottle import Bottle
from xbmc import LOGWARNING
from utils import log_msg, log_exception, LOGDEBUG, PROXY_HOST

STREAMING_TIMEOUT_IN_SECS = 600


def __bottle_stderr(*args):
    log_msg(f"{args}", LOGDEBUG)


bottle._stderr = __bottle_stderr


class ThreadedWSGIServer(socketserver.ThreadingMixIn, WSGIServer):
    # A track stream can stay open for minutes; it must not keep the service
    # alive (or the port bound) after Kodi asks the service to stop.
    daemon_threads = True


# Need this copy of 'bottle.WSGIRefServer' to add a 'shutdown' method, so we can do a
# clean shutdown of the bottle app.
class MyWSGIRefServer(bottle.WSGIRefServer):
    def __init__(self, host: str = "", port: int = 0):
        super().__init__(host, port)
        self.__server = None
        self.__startup_ready = threading.Event()
        self.__startup_error = None

    def wait_for_startup(self, timeout: float) -> bool:
        if not self.__startup_ready.wait(timeout):
            return False
        return self.__server is not None and self.__startup_error is None

    def get_startup_error(self):
        return self.__startup_error

    def is_bound(self) -> bool:
        return self.__server is not None and self.__server.socket.fileno() >= 0

    def run(self, app) -> None:
        class FixedHandler(WSGIRequestHandler):
            # Prevent reverse DNS lookups.
            def address_string(self) -> str:
                return self.client_address[0]

            # wsgiref writes to stderr, which Kodi logs at error level. Log the
            # access line of every request at debug level instead...
            def log_request(handler, code="-", size="-"):
                if not self.quiet:
                    log_msg(f'{handler.address_string()} - "{handler.requestline}" {code} {size}', LOGDEBUG)

            # ...and keep real errors (log_error, e.g. "code 400" or "Request
            # timed out") visible at warning level.
            def log_message(handler, format, *args):
                log_msg(f"{handler.address_string()} - {format % args}", LOGWARNING)

        handler_cls = self.options.get("handler_class", FixedHandler)
        server_cls = self.options.get("server_class", ThreadedWSGIServer)

        if ":" in self.host:
            # Fix wsgiref for IPv6 addresses.
            if getattr(server_cls, "address_family") == socket.AF_INET:

                class AddressFamilyServerCls(server_cls):
                    address_family = socket.AF_INET6

                server_cls = AddressFamilyServerCls

        # CHANGE (1) TO THE ORIGINAL BOTTLE CLASS METHOD!
        server_cls.timeout = STREAMING_TIMEOUT_IN_SECS

        try:
            srv = make_server(self.host, self.port, app, server_cls, handler_cls)
        except Exception as exc:
            self.__startup_error = exc
            self.__startup_ready.set()
            raise
        # CHANGE (2) TO THE ORIGINAL BOTTLE CLASS METHOD!
        self.__server = srv
        self.__startup_ready.set()

        log_msg(f"Starting wsgiref web server, timeout = {self.__server.timeout}.")

        try:
            srv.serve_forever()
        finally:
            srv.server_close()

    # HERE'S THE ADDED SERVER SHUTDOWN METHOD.
    def shutdown(self) -> None:
        log_msg("Shutdown wsgiref server requested...")
        server = self.__server
        if server is None:
            return
        try:
            server.shutdown()
        finally:
            server.server_close()

    def close(self) -> None:
        server = self.__server
        if server is not None:
            server.server_close()


__server: MyWSGIRefServer = MyWSGIRefServer()
__bottle_manager: Bottle = Bottle()
__manager_thread = None


def route_all(app: object) -> None:
    for kw in dir(app):
        attr = getattr(app, kw)
        if hasattr(attr, "route"):
            __bottle_manager.route(attr.route)(attr)


def __begin_app() -> None:
    try:
        bottle.run(app=__bottle_manager, server=__server)
    except Exception as exc:
        log_exception(exc, "Bottle server stopped unexpectedly")


def start_thread(web_port: int) -> bool:
    global __manager_thread
    global __server
    if is_healthy():
        return True
    if __manager_thread is not None and __manager_thread.is_alive():
        return __server.wait_for_startup(3.0) and is_healthy()

    __server = MyWSGIRefServer(host=PROXY_HOST, port=web_port)
    __manager_thread = threading.Thread(target=__begin_app, daemon=True)
    __manager_thread.start()
    if __server.wait_for_startup(3.0) and is_healthy():
        return True
    __manager_thread.join(timeout=1.0)
    return False


def is_healthy() -> bool:
    return (
        __server is not None
        and __manager_thread is not None
        and __manager_thread.is_alive()
        and __server.is_bound()
    )


def get_startup_error():
    return __server.get_startup_error() if __server is not None else None


def stop_thread() -> None:
    log_msg("Closing bottle app and thread.", LOGDEBUG)
    try:
        __bottle_manager.close()
        if __server is not None:
            if __manager_thread is not None and __manager_thread.is_alive():
                __server.shutdown()
            else:
                __server.close()
        if __manager_thread is not None:
            __manager_thread.join(timeout=5.0)
    except Exception as exc:
        log_exception(exc, f"Bottle app closed with exception.")
