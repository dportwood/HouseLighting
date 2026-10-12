# HouseLighting

A small real-time player for the house light show. It reads the layout that
xLights already maintains (controllers, channel ranges, models) and sends
frames straight to the Kulp K8-Pi / FPP controllers over DDP, so effects and
colors can change live instead of being baked into `.fseq` files.

xLights stays the tool for layout and for designed/musical sequences; this
player can play those `.fseq` files too.

## Setup

```bash
pip install -e .[dev]
```

The xLights show folder defaults to `Z:\xlights`; override with `--show-dir`
or the `HOUSELIGHTS_SHOW_DIR` environment variable.

## Usage

```bash
python -m houselights info
python -m houselights --skip FPP-Housenew chase --colors 400080,FF3000 --seconds 30
python -m houselights --only FPP-front-left solid --color 400080 --seconds 5
python -m houselights --skip FPP-Housenew play "Z:/xlights/Halloween Chase.fseq"
```

`--only` / `--skip` take a controller name or IP and can be repeated.
The house controller (`FPP-Housenew`, 172.16.17.64) is currently broken, so skip it.

Don't run this while xSchedule is playing the scheduled show (16:50–23:15) –
the remotes would get both their synced local sequence and our DDP frames.

## How it talks to the controllers

* Channel numbers are global, in `xlights_networks.xml` order (house 1–4062,
  front-right 4063–7314, front-left 7315–9114). The controllers use
  *Keep Channel Numbers*, so the DDP offset is the global channel − 1.
* DDP: UDP 4048, ≤1440 data bytes per packet, PUSH flag on the last packet of
  each frame, sequence number advancing per packet (FPP counts repeats as errors).
* Brightness (70%) and color order are applied on the Pi, not here.
* The remotes accept DDP in FPP *remote* mode while idle (verified 2026-10-12;
  check `/api/channel/input/stats` on each Pi for packet/error counters).

## Tests

```bash
python -m pytest
```

See [docs/roadmap.md](docs/roadmap.md) for what's next.
