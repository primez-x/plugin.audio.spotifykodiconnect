import threading
import time
import requests
import sys
sys.path.append(r"c:\Git repos\Kodi addons\Custom addons\plugin.audio.spotifykodiconnect\resources\lib\deps")
import bottle

app = bottle.Bottle()

@app.route("/test")
def test():
    bottle.response.status = 200
    bottle.response.content_type = "audio/x-wav"
    bottle.response.content_length = 1000000

    def generate():
        yield b"hello" * 1000
    
    return bottle.Response(generate())

def run_server():
    bottle.run(app=app, port=52319, quiet=True)

threading.Thread(target=run_server, daemon=True).start()
time.sleep(1)

r = requests.get("http://localhost:52319/test")
print("Headers:", r.headers)
print("Content:", len(r.content))
