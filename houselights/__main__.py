"""Command line: python -m houselights <command> ...

  info                       show controllers and models from the xLights show folder
  solid  --color RRGGBB      hold one color
  chase  --colors A,B,...    live chase along every string
  play   FILE.fseq           play a rendered xLights sequence directly over DDP
  locate --model NAME        light the end of one string in color blocks to find a dead pixel
  serve  --config FILE       run the show: schedule, playlist and the tablet web page
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from . import __version__, effects
from .fseq import read_fseq
from .layout import load_layout
from .output import Output

DEFAULT_SHOW_DIR = os.environ.get("HOUSELIGHTS_SHOW_DIR", r"Z:\xlights")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="houselights")
    p.add_argument("--show-dir", default=DEFAULT_SHOW_DIR, help="xLights show folder (default: %(default)s)")
    p.add_argument("--only", action="append", help="send only to this controller (name or IP); repeatable")
    p.add_argument("--skip", action="append", help="never send to this controller (name or IP); repeatable")
    p.add_argument("--fps", type=float, default=20.0)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info")

    s = sub.add_parser("solid")
    s.add_argument("--color", default="400080")
    s.add_argument("--seconds", type=float, default=5.0)

    c = sub.add_parser("chase")
    c.add_argument("--colors", default="400080,FF3000")
    c.add_argument("--band", type=int, default=8, help="pixels per color band")
    c.add_argument("--speed", type=float, default=20.0, help="pixels per second")
    c.add_argument("--seconds", type=float, default=30.0)

    pl = sub.add_parser("play")
    pl.add_argument("file")
    pl.add_argument("--loops", type=int, default=1)

    lo = sub.add_parser("locate")
    lo.add_argument("--model", required=True)
    lo.add_argument("--first", type=int, default=1, help="first pixel to light (1-based)")
    lo.add_argument("--block", type=int, default=5, help="pixels per color block")
    lo.add_argument("--seconds", type=float, default=60.0)

    sv = sub.add_parser("serve")
    sv.add_argument("--config", default="config.toml")
    sv.add_argument("--dry-run", action="store_true", help="run everything but don't send to the controllers")
    sv.add_argument("--log", help="append output and errors to this file (for running in the background)")

    args = p.parse_args(argv)
    if args.cmd == "serve":
        _serve(args)
        return
    layout = load_layout(args.show_dir)

    if args.cmd == "info":
        _info(layout)
        return

    out = Output(layout, only=args.only, skip=args.skip)
    if not out.targets:
        raise SystemExit("No controllers selected")
    print("Sending to: " + ", ".join(f"{c.name} ({c.ip}) ch {c.start}-{c.end}" for c, _ in out.targets))
    frame = np.zeros(layout.total_channels, dtype=np.uint8)
    models = effects.models_for(layout, args.only)

    try:
        if args.cmd == "solid":
            color = effects.parse_color(args.color)
            _run(out, frame, args.fps, args.seconds, lambda t: effects.solid(frame, models, color))
        elif args.cmd == "chase":
            colors = [effects.parse_color(x) for x in args.colors.split(",")]
            _run(out, frame, args.fps, args.seconds,
                 lambda t: effects.chase(frame, models, colors, t, args.band, args.speed))
        elif args.cmd == "locate":
            model = layout.model(args.model)
            key = effects.locate(frame, model, args.first, args.block)
            names = dict(zip(effects.LOCATE_COLORS, ["white", "red", "green", "blue", "yellow", "cyan", "magenta"]))
            for a, b, color in key:
                print(f"  pixels {a:>3}-{b:<3} {names[color]}")
            _run(out, frame, args.fps, args.seconds, lambda t: None)
        elif args.cmd == "play":
            _play(out, frame, Path(args.file), args.loops)
    except KeyboardInterrupt:
        pass
    finally:
        frame[:] = 0
        out.send(frame)
        out.close()


def _run(out: Output, frame: np.ndarray, fps: float, seconds: float, render) -> None:
    period = 1.0 / fps
    start = time.perf_counter()
    n = 0
    while True:
        t = time.perf_counter() - start
        if t >= seconds:
            break
        render(t)
        out.send(frame)
        n += 1
        time.sleep(max(0.0, start + n * period - time.perf_counter()))
    print(f"Sent {n} frames in {time.perf_counter() - start:.1f}s")


def _play(out: Output, frame: np.ndarray, path: Path, loops: int) -> None:
    seq = read_fseq(path)
    count = min(seq.channel_count, len(frame))
    print(f"{path.name}: {seq.frame_count} frames @ {seq.step_ms}ms, {seq.channel_count} channels")
    period = seq.step_ms / 1000.0
    for _ in range(loops):
        start = time.perf_counter()
        for i in range(seq.frame_count):
            frame[:count] = np.frombuffer(seq.frame(i)[:count], dtype=np.uint8)
            out.send(frame)
            time.sleep(max(0.0, start + (i + 1) * period - time.perf_counter()))


def _serve(args) -> None:
    from .config import load_config
    from .output import NullOutput
    from .player import Player
    from .server import make_server, serve_in_thread

    if args.log:
        Path(args.log).parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(args.log, "a", encoding="utf-8", buffering=1)
    print(f"--- {datetime.now():%Y-%m-%d %H:%M:%S} starting houselights {__version__} ---")
    cfg = load_config(args.config)
    layout = load_layout(cfg.show_dir)
    out = NullOutput() if args.dry_run else Output(layout, only=args.only, skip=(args.skip or []) + cfg.skip_controllers)
    if args.dry_run:
        print("Dry run: nothing is sent to the controllers")
    else:
        print("Sending to: " + ", ".join(f"{c.name} ({c.ip})" for c, _ in out.targets))
    player = Player(cfg, layout, out)
    print(f"Loading {sum(i.type == 'fseq' for i in cfg.playlist)} sequence(s)...")
    player.preload()
    server = make_server(player, cfg.http_host, cfg.http_port)
    serve_in_thread(server)
    print(f"Control page: http://localhost:{cfg.http_port}/  (Ctrl+C to stop)")
    try:
        player.run()
    except KeyboardInterrupt:
        pass
    finally:
        player.stop()
        server.shutdown()
        out.send(np.zeros(layout.total_channels, dtype=np.uint8))
        out.close()


def _info(layout) -> None:
    print(f"{layout.total_channels} channels")
    for c in layout.controllers:
        state = "" if c.active else " (inactive)"
        print(f"  {c.name:<18} {c.ip:<15} {c.protocol} ch {c.start}-{c.end}{state}")
        for m in sorted((m for m in layout.models if m.controller == c.name), key=lambda m: m.start):
            print(f"      {m.name:<22} {m.display_as:<12} ch {m.start}-{m.end} ({m.nodes} px)")


if __name__ == "__main__":
    main()
