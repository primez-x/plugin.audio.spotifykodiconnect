import sys
sys.path.append(r"c:\Git repos\Kodi addons\Custom addons\plugin.audio.spotifykodiconnect\resources\lib\deps")
import bottle
from utils import *
import threading
import time
from spotty import Spotty
from spotty_helper import SpottyHelper
from http_spotty_audio_streamer import HTTPSpottyAudioStreamer

spotty_helper = SpottyHelper()
spotty = Spotty()
spotty.set_spotty_path(spotty_helper.spotty_binary_path)

streamer = HTTPSpottyAudioStreamer(spotty)
app = bottle.Bottle()

for kw in dir(streamer):
    attr = getattr(streamer, kw)
    if hasattr(attr, "route"):
        app.route(attr.route)(attr)

import socketserver
from wsgiref.simple_server import WSGIServer, WSGIRequestHandler, make_server

class ThreadedWSGIServer(socketserver.ThreadingMixIn, WSGIServer):
    pass

class MyWSGIRefServer(bottle.WSGIRefServer):
    def run(self, app):
        srv = make_server(self.host, self.port, app, ThreadedWSGIServer, WSGIRequestHandler)
        srv.serve_forever()

def run_server():
    bottle.run(app=app, server=MyWSGIRefServer(host="localhost", port=52319), quiet=True)

threading.Thread(target=run_server, daemon=True).start()
time.sleep(1)

import requests
t0 = time.time()
r = requests.get("http://localhost:52319/track/11dFghVXANMlKmJXsNCbNl/178.795", stream=True)
bytes_read = 0
for chunk in r.iter_content(chunk_size=524288):
    if chunk:
        bytes_read += len(chunk)
        print(f"Downloaded {bytes_read} bytes in {time.time()-t0:.2f} seconds")
print(f"Total downloaded: {bytes_read} bytes in {time.time()-t0:.2f} seconds")
