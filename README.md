# Burn scar precip monitor

A local dashboard for rain around current wildfire perimeters. It runs on your machine and opens in a browser. Nothing is hosted.

The page is a screening view: which active perimeters have heavy observed or forecast rain inside a buffer. It does not estimate debris-flow or flood probability. Burn severity and slope are not part of this view, and short bursts can matter more than a 6-hour or 24-hour total.

## Data

- **Perimeters:** NIFC WFIGS current interagency perimeters. This layer is fires that have not been declared out. It refreshes about every 5 minutes. Fires that have been out for days are not included.
- **Station precipitation:** Synoptic `/v2/stations/precip` with `pmode=last` and `units=english` (inches). Stations are kept only when they fall inside the buffer.
- **Gridded observed precipitation:** NCEP MRMS radar-only QPE GRIB2 grids for 1, 3, 6, 12, 24, 48, and 72 hours. Source values are millimeters and are converted to inches. Negative cells are missing data and are ignored. One grid is downloaded per interval, and the value shown is the maximum cell whose center is inside the buffer.
- **Forecast:** National Blend of Models deterministic quantitative precipitation from the CONUS GRIB2 files. Only the QPF message is downloaded from each file. Source values are kilograms per square meter and are converted to inches. The app uses the newest cycle it can find. This is the default. The page shows the wettest total of that length inside the forecast horizon, using the maximum cell in the buffer. Six-hour and longer totals are built from the 6-hour amounts. One-hour and three-hour totals use the hourly amounts. The NWS forecast grid is still available in the forecast source list. Alaska, Hawaii, and Puerto Rico are outside the CONUS grid.

## Run it

Use the existing ArcGIS Pro Python environment. Do not create a separate virtual environment and do not install another Python.

```powershell
cd C:\Users\chad.kahler\Documents\Projects\burnscar-precip-monitor
copy .env.example .env
# Put your Synoptic token in .env as SYNOPTIC_TOKEN=...
& "C:\Users\chad.kahler\AppData\Local\ESRI\conda\envs\arcgispro-py3-pro\python.exe" -m app
```

Then open http://127.0.0.1:8765 if the browser does not open on its own.

That environment already has `httpx`, `shapely`, `pyproj`, `pyyaml`, `numpy`, and GDAL. If `fastapi`, `uvicorn`, or `python-dotenv` are missing, install only those, with the same interpreter:

```powershell
& "C:\Users\chad.kahler\AppData\Local\ESRI\conda\envs\arcgispro-py3-pro\python.exe" -m pip install fastapi==0.115.12 uvicorn==0.34.2 python-dotenv==1.1.1
```

Do not upgrade `numpy`, `shapely`, `pyproj`, `gdal`, `fiona`, or `arcpy`. MRMS GRIB2 files are read with the GDAL already in this environment (`osgeo.gdal`). Rasterio is not required and was not installed.

You can activate `arcgispro-py3-pro` instead of calling that `python.exe` path, as long as `python` then points at this environment.

## What you can change

The sidebar sets state abbreviations (blank means all states), minimum acres, buffer distance in miles (tenths of a mile, so 10.0 is allowed), accumulation intervals, forecast source, forecast horizon, and how often the data refresh. Selecting a fire opens a chart over the map. The shaded side is before the current time: MRMS 1-hour QPE for the past 24 hours, one line for each station inside the buffer, and the highest station total. NBM is drawn at the valid time of each forecast hour, so an older cycle shows up to the left of the observations. Grid values are the maximum in the buffer. Only the QPF message is downloaded from each NBM GRIB2 file. MRMS files still arrive whole, and only the block covering the fire buffers is decoded. A radar switch adds the national NEXRAD reflectivity composite and can play the last two hours in 5-minute steps. Choices are saved in `data/settings.json` on that computer. `config.yaml` holds the defaults for a new install. The map uses Esri Leaflet basemaps: imagery, topographic, streets, terrain, USA topo, and dark gray, chosen from a dropdown, with an optional place-name overlay. While a refresh is running, the page shows that status and the refresh buttons stay disabled.

The Synoptic token stays in `.env` only. Do not commit it.

A refresh downloads perimeters first, then stations, then MRMS grids, then the selected forecast. The table fills in as each step finishes. Source delays are roughly 5–15 minutes for perimeters, minutes to an hour for stations, about an hour for MRMS, and about six hours for a new NBM cycle.
