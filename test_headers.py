import sys
sys.path.append(r"c:\Git repos\Kodi addons\Custom addons\plugin.audio.spotifykodiconnect\resources\lib\deps")
import bottle
import threading
import time
import requests

app = bottle.Bottle()

@app.route("/test1")
def test1():
    bottle.response.status = 200
    bottle.response.content_type = "audio/x-wav"
    bottle.response.content_length = 1000000
    bottle.response.headers["X-Custom"] = "Test"
    
    def generate():
        yield b"hello"
    return bottle.Response(generate())

@app.route("/test2")
def test2():
    bottle.response.status = 200
    bottle.response.content_type = "audio/x-wav"
    bottle.response.content_length = 1000000
    bottle.response.headers["X-Custom"] = "Test"
    
    def generate():
        yield b"hello"
    return generate()

def run_server():
    bottle.run(app=app, port=52319, quiet=True)

threading.Thread(target=run_server, daemon=True).start()
time.sleep(1)

r1 = requests.get("http://localhost:52319/test1", stream=True)
print("test1 Headers:", r1.headers)

r2 = requests.get("http://localhost:52319/test2", stream=True)
print("test2 Headers:", r2.headers)
