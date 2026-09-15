#!/usr/bin/env python
"""Build every media asset of the SplashSplat project page into static/.

Run from anywhere (paths below are absolute or resolved against this file):

    conda activate fluid-splat
    python ext/splashsplat-web/tools/build_assets.py            # everything
    python ext/splashsplat-web/tools/build_assets.py --only compare interp

Inputs (never modified):
  ext/Splashsplat/assets/teaser.png                       -> static/images/teaser.jpg, og.jpg, favicons
  ext/video/*_interp_8x*.mp4                              -> static/videos/interp_<scene>_<split>.mp4
  ext/video/*_style.mp4                                   -> static/videos/style_<scene>.mp4
  ext/scene_grid/scene_grid_wipe_gap.mp4                  -> static/videos/scene_grid.mp4
  /scratch/izar/pliu/render_bundle/final_bullet_ds2_a40_crop/<scene>_cam<c>/bullet_time.mp4
        -> static/videos/teaser_bullet.mp4: the three BULLET_PICKS bullet-time renders joined into
           ONE looping clip (0.6 s cross-fades, incl. at the loop seam) for the abstract teaser
  output/crop_showcase_split_video/<split>/<scene>_cam<c>/<method>/NNN.{jpg,png}
        -> static/videos/compare_<split>_<scene>_cam<c>.mp4
           five pixel-aligned panels (Captured | Deformable-3DGS | SpacetimeGaussians |
           4D-Scaffold-GS | Ours) at a common height with a serif label strip on top,
           the same convention the interp/style videos already use.

Every output is H.264 High, yuv420p, even dimensions, `+faststart` (moov before mdat so
the browser can start playback before the whole file is downloaded), width <= 1920.
A poster JPEG (40% into the clip, <=1200 px wide) is written for each video, and
static/videos/manifest.json records width/height/fps/duration/bytes for the HTML.

ffmpeg: uses the one on PATH, else the static build shipped with imageio-ffmpeg
(available in the fluid-splat env). The static build has no drawtext filter, so label
strips are rendered with PIL and composited frame by frame before encoding.
"""
from __future__ import annotations

import argparse, json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
SITE = HERE.parent                                   # ext/splashsplat-web
REPO = SITE.parent.parent                            # splash-splat
EXT = REPO / "ext"
OUT_V = SITE / "static" / "videos"
OUT_I = SITE / "static" / "images"
POSTERS = OUT_I / "posters"

COMPARE_ROOT = REPO / "output" / "crop_showcase_split_video"
BULLET_ROOT = Path("/scratch/izar/pliu/render_bundle/final_bullet_ds2_a40_crop")
BULLET_PICKS = ["bowl_009_cam4", "bowl_006_cam2", "bowl_004_cam2"]      # Peiyu's picks, 2026-09-15 (play order)
TEASER_SIZE = (936, 1040)     # 9:10 canvas; each clip is scaled to cover it and centre-cropped
TEASER_XFADE = 0.6            # seconds of cross-fade between clips and at the loop seam
TEASER_HEAD = 20              # frames of clip 1 moved to the END of the file, so the loop point is a cross-fade too
TEASER_POSTER_T = 2.2         # poster + playback start, seconds into the file (clip 1 mid-pour)
METHODS = [("gt", "Captured"), ("d3g", "Deformable-3DGS"), ("stg", "SpacetimeGaussians"),
           ("4dsg", "4D-Scaffold-GS"), ("ours", "Ours")]
FONTS = ["/usr/share/fonts/urw-base35/NimbusRoman-Regular.otf",      # URW Times, matches the paper figures
         "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf"]
MAX_W = 1920
CRF = "22"


def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:  # pragma: no cover
        raise SystemExit("no ffmpeg on PATH and imageio_ffmpeg not importable "
                         "(conda activate fluid-splat)") from e


FF = None


def run(cmd: list[str]) -> None:
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr[-4000:])
        raise SystemExit(f"command failed: {' '.join(cmd[:6])} ...")


def probe(path: Path) -> dict:
    """width/height/fps/duration via `ffmpeg -i` (the static build ships no ffprobe)."""
    import re
    r = subprocess.run([FF, "-hide_banner", "-i", str(path)], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True)
    txt = r.stderr
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5}).*?([\d.]+) fps", txt)
    d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", txt)
    dur = int(d.group(1)) * 3600 + int(d.group(2)) * 60 + float(d.group(3)) if d else None
    return {"width": int(m.group(1)), "height": int(m.group(2)), "fps": float(m.group(3)),
            "duration": round(dur, 3) if dur is not None else None,
            "bytes": path.stat().st_size}


CODEC_ARGS = ["-c:v", "libx264", "-profile:v", "high", "-preset", "slow",
              "-crf", CRF, "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an"]


def encode_args(extra_vf: str | None = None) -> list[str]:
    vf = ["scale=trunc(iw/2)*2:trunc(ih/2)*2"] if extra_vf is None else [extra_vf]
    return ["-vf", ",".join(vf)] + CODEC_ARGS


def label_strip_height(src: Path, probe_frame: int = 20) -> int:
    """Height of the white label strip burned into the top of a source clip: the first row
    whose pixels are mostly NOT white (image content), scanned on one decoded frame."""
    import cv2, numpy as np
    c = cv2.VideoCapture(str(src)); c.set(cv2.CAP_PROP_POS_FRAMES, probe_frame); ok, im = c.read(); c.release()
    if not ok:
        raise SystemExit(f"cannot decode {src}")
    white = im.min(axis=2) > 235
    rows = white.mean(axis=1)
    h = 0
    while h < im.shape[0] and rows[h] > 0.5:
        h += 1
    return h


def transcode(src: Path, dst: Path, max_w: int = MAX_W, crop_top: int = 0) -> None:
    # optional top crop (burned-in labels), then scale to max_w (never upscale), even dims
    vf = (f"crop=iw:ih-{crop_top}:0:{crop_top}," if crop_top > 0 else "") + f"scale='min({max_w},iw)':-2"
    run([FF, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src)] + encode_args(vf) + [str(dst)])


def poster(video: Path, dst: Path, t: float = 0.0) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    run([FF, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.3f}", "-i", str(video),
         "-frames:v", "1", "-vf", "scale='min(1200,iw)':-2", "-q:v", "3", str(dst)])


def load_font(size: int):
    for f in FONTS:
        if Path(f).exists():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default()


# ---------------------------------------------------------------- comparison videos
def build_compare(split: str, scene_cam: str, fps: float = 10.0, gutter: int = 10,
                  labels: bool = False) -> Path:
    src = COMPARE_ROOT / split / scene_cam
    if not src.exists():
        raise SystemExit(f"missing {src}")
    n = len(list((src / "ours").glob("*.png")))
    # native panel size (all five methods share one crop window)
    w0, h0 = Image.open(sorted((src / "gt").glob("*.jpg"))[0]).size
    # panel height so that 5 panels + 4 gutters fill MAX_W
    pw = (MAX_W - gutter * (len(METHODS) - 1)) / len(METHODS)
    ph = int(round(pw * h0 / w0))
    pw = int(round(pw))
    ph -= ph % 2
    label_h = 56 if labels else 0          # method names live in the HTML by default
    W = pw * len(METHODS) + gutter * (len(METHODS) - 1)
    W -= W % 2
    H = ph + label_h
    font = load_font(30)
    dst = OUT_V / f"compare_{split}_{scene_cam}.mp4"
    with tempfile.TemporaryDirectory(prefix="cmp_") as td:
        for t in range(n):
            canvas = Image.new("RGB", (W, H), "white")
            draw = ImageDraw.Draw(canvas)
            for k, (m, label) in enumerate(METHODS):
                ext = "jpg" if m == "gt" else "png"
                im = Image.open(src / m / f"{t:03d}.{ext}").convert("RGB")
                im = im.resize((pw, ph), Image.LANCZOS)
                x = k * (pw + gutter)
                canvas.paste(im, (x, label_h))
                if labels:
                    tw = draw.textlength(label, font=font)
                    draw.text((x + (pw - tw) / 2, 11), label, fill=(20, 20, 20), font=font)
            canvas.save(f"{td}/{t:03d}.png", compress_level=1)
        run([FF, "-y", "-hide_banner", "-loglevel", "error", "-framerate", str(fps),
             "-i", f"{td}/%03d.png"] + encode_args() + [str(dst)])
    return dst


# ---------------------------------------------------------------- bullet-time teaser loop
def build_bullet_loop(dst: Path) -> dict:
    """Join the BULLET_PICKS clips A, B, C into one seamless loop: A[head:] -> B -> C -> A[:head],
    every cut a cross-fade. Because the file ends on A's first frames and starts right after
    them, the browser's loop point is continuous too (no hard cut anywhere)."""
    import cv2
    srcs = [BULLET_ROOT / sc / "bullet_time.mp4" for sc in BULLET_PICKS]
    fps, nframes = None, []
    for src in srcs:
        if not src.exists():
            raise SystemExit(f"missing {src}")
        c = cv2.VideoCapture(str(src))
        f, n = c.get(cv2.CAP_PROP_FPS), int(c.get(cv2.CAP_PROP_FRAME_COUNT))
        c.release()
        if fps is not None and abs(f - fps) > 1e-3:
            raise SystemExit(f"bullet clips differ in fps: {fps} vs {f} ({src})")
        fps = f
        nframes.append(n)
    W, H = TEASER_SIZE
    d, head = TEASER_XFADE, TEASER_HEAD
    norm = (f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            f"fps={fps:g},format=yuv420p,setsar=1")
    # trim+setpts leaves the branch with an unknown frame rate, which xfade rejects: re-apply fps
    parts = [f"[0:v]{norm},split=2[a0][a1]",
             f"[a0]trim=end_frame={head},setpts=PTS-STARTPTS,fps={fps:g}[head]",
             f"[a1]trim=start_frame={head},setpts=PTS-STARTPTS,fps={fps:g}[s0]"]
    for i in range(1, len(srcs)):
        parts.append(f"[{i}:v]{norm}[s{i}]")
    lens = [(nframes[0] - head) / fps] + [n / fps for n in nframes[1:]]
    cur, total = "s0", lens[0]
    for i in range(1, len(srcs)):
        parts.append(f"[{cur}][s{i}]xfade=transition=fade:duration={d}:offset={total - d:.4f}[x{i}]")
        cur, total = f"x{i}", total + lens[i] - d
    parts.append(f"[{cur}][head]xfade=transition=fade:duration={d}:offset={total - d:.4f}[v]")
    total += head / fps - d
    cmd = [FF, "-y", "-hide_banner", "-loglevel", "error"]
    for src in srcs:
        cmd += ["-i", str(src)]
    cmd += ["-filter_complex", ";".join(parts), "-map", "[v]"] + CODEC_ARGS + [str(dst)]
    run(cmd)
    return {"kind": "teaser", "clips": list(BULLET_PICKS), "xfade_s": d, "head_frames": head,
            "expected_duration": round(total, 3), "poster_t": TEASER_POSTER_T,
            "source": str(BULLET_ROOT), "panels": 1}


def main() -> None:
    global FF
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None,
                    choices=["teaser", "pipeline", "interp", "style", "grid", "compare", "bullet", "posters", "thumbs"],
                    help="subset of steps (default: all)")
    ap.add_argument("--compare_labels", action="store_true",
                    help="burn the method names into the comparison clips (default: names live in the HTML)")
    ap.add_argument("--pipeline_pdf", default=str(EXT / "pipeline_final.pdf"))
    ap.add_argument("--grid_variant", default="gap", choices=["gap", "nogap"],
                    help="which scene_grid wipe video to use")
    ap.add_argument("--compare_fps", type=float, default=10.0,
                    help="playback rate of the 50-frame comparison clips "
                         "(captures were sampled at ~9-12 fps)")
    a = ap.parse_args()
    steps = set(a.only or ["teaser", "pipeline", "interp", "style", "grid", "compare", "bullet", "posters", "thumbs"])
    FF = ffmpeg_exe()
    OUT_V.mkdir(parents=True, exist_ok=True); OUT_I.mkdir(parents=True, exist_ok=True)
    print(f"[build] ffmpeg = {FF}")

    videos: dict[str, dict] = {}

    if "teaser" in steps:
        src = EXT / "Splashsplat" / "assets" / "teaser.png"
        im = Image.open(src).convert("RGB")
        im.save(OUT_I / "teaser.jpg", quality=90, optimize=True, progressive=True)
        # OpenGraph card: 1200x630, the teaser letterboxed on the page background
        og = im.resize((1200, int(round(1200 * im.height / im.width))), Image.LANCZOS)
        card = Image.new("RGB", (1200, 630), (251, 251, 250))
        card.paste(og, (0, (630 - og.height) // 2))
        card.save(OUT_I / "og.jpg", quality=85, optimize=True)
        # favicons: navy rounded square with a white drop (Safari ignores SVG favicons)
        for name, size in (("apple-touch-icon.png", 180), ("favicon-32.png", 32)):
            S = 8 * size                                             # draw big, downsample
            ic = Image.new("RGBA", (S, S), (0, 0, 0, 0))
            d = ImageDraw.Draw(ic)
            d.rounded_rectangle((0, 0, S - 1, S - 1), radius=S // 5, fill=(31, 78, 140, 255))
            cx, r = S / 2, S * 0.23
            cy = S * 0.62
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill="white")
            d.polygon([(cx, S * 0.14), (cx - r * 0.9, cy - r * 0.42), (cx + r * 0.9, cy - r * 0.42)], fill="white")
            ic.resize((size, size), Image.LANCZOS).save(OUT_I / name)
        print(f"[teaser] {im.size} -> teaser.jpg, og.jpg (1200x630), favicons")

    if "pipeline" in steps:
        # vector PDF -> PNG at 2400 px wide (diagram text stays crisp; jpeg would ring on the arrows)
        import fitz  # PyMuPDF
        doc = fitz.open(a.pipeline_pdf)
        page = doc[0]
        zoom = 2400.0 / page.rect.width
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        pix.save(OUT_I / "pipeline.png")
        im = Image.open(OUT_I / "pipeline.png").convert("RGB")
        im.save(OUT_I / "pipeline.png", optimize=True)
        print(f"[pipeline] {a.pipeline_pdf} -> pipeline.png {im.size} "
              f"{(OUT_I / 'pipeline.png').stat().st_size/1e6:.2f} MB")

    if "interp" in steps:
        for src in sorted((EXT / "video").glob("*_interp_8x*.mp4")):
            stem = src.stem.replace("_interp_8x", "")          # bowl_006, bowl_006_test, bowl_001_train
            scene = stem[:8]
            split = "test" if stem.endswith("_test") else "train"
            dst = OUT_V / f"interp_{scene}_{split}.mp4"
            h = label_strip_height(src)
            transcode(src, dst, crop_top=h)
            videos[dst.name] = {"kind": "interp", "scene": scene, "split": split, "source": str(src),
                                "cropped_top": h, "panels": 5, "gutter_px": 9, "source_width": 2886}
            print(f"[interp] {src.name} -> {dst.name} (label strip {h}px cropped)")

    if "style" in steps:
        for src in sorted((EXT / "video").glob("*_style.mp4")):
            scene = src.stem[:8]
            dst = OUT_V / f"style_{scene}.mp4"
            h = label_strip_height(src)
            transcode(src, dst, crop_top=h)
            videos[dst.name] = {"kind": "style", "scene": scene, "source": str(src),
                                "cropped_top": h, "panels": 4, "gutter_px": 8, "source_width": 2632}
            print(f"[style] {src.name} -> {dst.name} (label strip {h}px cropped)")

    if "grid" in steps:
        src = EXT / "scene_grid" / f"scene_grid_wipe_{a.grid_variant}.mp4"
        dst = OUT_V / "scene_grid.mp4"
        transcode(src, dst)                                   # re-encode: even height + faststart guaranteed
        videos[dst.name] = {"kind": "grid", "variant": a.grid_variant, "source": str(src)}
        print(f"[grid] {src.name} -> {dst.name}")

    if "compare" in steps:
        man = json.loads((COMPARE_ROOT / "manifest.json").read_text())
        for split, scene_cams in man["scene_order"].items():
            for sc in scene_cams:
                dst = build_compare(split, sc, fps=a.compare_fps, labels=a.compare_labels)
                videos[dst.name] = {"kind": "compare", "split": split, "scene": sc[:8],
                                    "cam": int(sc.split("cam")[1]), "source": str(COMPARE_ROOT / split / sc),
                                    "panels": 5, "gutter_px": 10, "source_width": 1920}
                print(f"[compare] {split}/{sc} -> {dst.name}")

    if "bullet" in steps:
        dst = OUT_V / "teaser_bullet.mp4"
        videos[dst.name] = build_bullet_loop(dst)
        print(f"[bullet] {' -> '.join(BULLET_PICKS)} -> {dst.name} "
              f"(~{videos[dst.name]['expected_duration']:.1f}s loop)")

    # ---- posters + manifest (always refreshed for every video present) ----
    manifest_p = OUT_V / "manifest.json"
    old = json.loads(manifest_p.read_text()) if manifest_p.exists() else {}
    for v in sorted(OUT_V.glob("*.mp4")):
        meta = probe(v)
        entry = {**old.get(v.name, {}), **videos.get(v.name, {}), **meta,
                 "poster": f"static/images/posters/{v.stem}.jpg"}
        if entry.get("source", "").startswith(str(REPO)):
            entry["source"] = str(Path(entry["source"]).relative_to(REPO))
        if "posters" in steps or not (POSTERS / f"{v.stem}.jpg").exists():
            # frame 0 of a pour is an empty bowl; take the poster 40% in (grid: frame 0 = RGB state;
            # teaser loop: fixed poster_t, and the page starts playback there via data-start)
            if entry.get("kind") == "grid":
                t_poster = 0.0
            elif "poster_t" in entry:
                t_poster = float(entry["poster_t"])
            else:
                t_poster = 0.4 * (meta["duration"] or 0.0)
            entry["poster_t"] = round(t_poster, 3)
            if meta["duration"]:
                entry["data_start"] = round(t_poster / meta["duration"], 4)   # fraction for the HTML
            poster(v, POSTERS / f"{v.stem}.jpg", t=t_poster)
        videos[v.name] = entry
    if "thumbs" in steps:
        THUMBS = OUT_I / "thumbs"; THUMBS.mkdir(parents=True, exist_ok=True)
        for v in sorted(OUT_V.glob("*.mp4")):
            im = Image.open(POSTERS / f"{v.stem}.jpg").convert("RGB")
            im = im.resize((420, int(round(420 * im.height / im.width))), Image.LANCZOS)
            im.save(THUMBS / f"{v.stem}.jpg", quality=82, optimize=True)
            videos[v.name]["thumb"] = f"static/images/thumbs/{v.stem}.jpg"
    manifest_p.write_text(json.dumps(videos, indent=1, sort_keys=True))
    total = sum(v["bytes"] for v in videos.values())
    print(f"[manifest] {len(videos)} videos, {total/1e6:.1f} MB total -> {manifest_p}")
    for k, v in sorted(videos.items()):
        print(f"   {k:40s} {v['width']}x{v['height']} {v['fps']:.0f}fps {v['duration']:.1f}s {v['bytes']/1e6:.1f}MB")


if __name__ == "__main__":
    main()
