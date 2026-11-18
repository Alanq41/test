# Race Track Globe

An interactive 3D globe showing 200+ motorsport circuits around the world:
the 2026 Formula 1 calendar, former F1 venues, MotoGP, IndyCar, NASCAR, endurance (Le Mans, Daytona, Sebring, Bathurst…),
Formula E street circuits, touring/GT/national tracks, and legendary road courses like the Isle of Man TT and Pikes Peak.

- Drag to spin, scroll/pinch to zoom, click a pin (or a list entry) to fly to the track
- Filter by series with the coloured chips, search by track or country
- Each track card links to Wikipedia and a satellite view

## Run it

No build step. Serve the folder with any static server and open `index.html`:

```sh
python3 -m http.server 8000
# then open http://localhost:8000
```

All libraries (three.js, d3-geo, topojson-client) and the Natural Earth country map are vendored in `vendor/`, so it works offline.

## Adding tracks

Edit `tracks.js` — each row is `[name, country, lat, lon, lengthKm, "SERIES,CODES", note]`.
