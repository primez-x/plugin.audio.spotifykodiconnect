import time
import subprocess
import os

spotty_path = r"c:\Git repos\Kodi addons\Custom addons\plugin.audio.spotifykodiconnect\resources\lib\deps\spotty\windows\spotty.exe"
cache_path = r"C:\Users\Matt\AppData\Roaming\Kodi\userdata\addon_data\plugin.audio.spotifykodiconnect\spotty-cache"

args = [
    spotty_path,
    "--cache", cache_path,
    "--disable-audio-cache",
    "--disable-discovery",
    "--bitrate", "320",
    "--enable-volume-normalisation",
    "--normalisation-gain-type", "track",
    "--single-track", "spotify:track:11dFghVXANMlKmJXsNCbNl"
]

print("Running:", " ".join(args))
p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

t0 = time.time()
total_bytes = 0

while True:
    data = p.stdout.read(524288)
    if not data:
        break
    total_bytes += len(data)
    elapsed = time.time() - t0
    # Print progress every MB
    if total_bytes % (1024*1024) < 524288:
        print(f"Read {total_bytes} bytes in {elapsed:.2f} seconds")

print(f"Finished: {total_bytes} bytes in {time.time()-t0:.2f} seconds")
