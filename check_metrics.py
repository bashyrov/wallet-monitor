import urllib.request
r = urllib.request.urlopen("http://go-fetcher:8090/internal/metrics", timeout=3)
print(r.read().decode())
