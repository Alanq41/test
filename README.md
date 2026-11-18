# TrackLine Circuit Globe

An interactive 3D globe with the 12 circuits in the TrackLine tee collection. Click a pin (or a name in the list) to fly to the circuit
and open a card with a "Shop the tee" link to `https://tracklinewear.com/products/<handle>` (opens in the top window, so it works when embedded in an iframe).

## Run it

No build step. Serve the folder with any static server and open `index.html`:

```sh
python3 -m http.server 8000
```

All libraries (three.js, d3-geo, topojson-client) and the Natural Earth country map are in `vendor/`, so it works offline.

## Editing circuits

Edit `tracks.js`: each row is `[name, country, lat, lon, shopHandle, note]`.
