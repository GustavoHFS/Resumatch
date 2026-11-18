"""Generates multi-size Windows icon `assets/resumatch.ico` (`16x16` through `256x256`)
and web favicons (`frontend/public/favicon.ico`, `frontend/public/apple-touch-icon.png`)
from the minimalist monochrome Chibi mascot design using Pillow supersampling.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = REPO_ROOT / "assets"
PUBLIC_DIR = REPO_ROOT / "frontend" / "public"
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def render_chibi_master_canvas(size: int = 1024) -> Image.Image:
    """Draws the minimalist monochrome Chibi mascot at 4x supersampled resolution."""
    s = size / 256.0
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    bg_deep = (17, 17, 17, 255)       # #111111
    bg_surface = (24, 24, 24, 255)    # #181818
    border_subtle = (50, 50, 50, 255) # #323232
    fg_white = (238, 238, 238, 255)   # #eeeeee
    fg_muted = (164, 161, 155, 255)   # #a4a19b

    # 1. Rounded emblem card
    d.rounded_rectangle(
        [int(8 * s), int(8 * s), int(248 * s), int(248 * s)],
        radius=int(48 * s),
        fill=bg_deep,
        outline=border_subtle,
        width=max(2, int(6 * s)),
    )

    # 2. Chibi hair tuft (ahoge) at top
    d.arc(
        [int(116 * s), int(20 * s), int(154 * s), int(56 * s)],
        start=195,
        end=355,
        fill=fg_white,
        width=int(9 * s),
    )

    # 3. Shoulders / suit silhouette
    d.pieslice(
        [int(76 * s), int(158 * s), int(180 * s), int(266 * s)],
        start=180,
        end=360,
        fill=bg_surface,
        outline=fg_white,
        width=int(10 * s),
    )
    # Mask bottom line of pieslice cleanly inside card
    d.rectangle([int(70 * s), int(213 * s), int(186 * s), int(238 * s)], fill=bg_deep)

    # Neat mini tie
    d.polygon(
        [
            (int(128 * s), int(164 * s)),
            (int(117 * s), int(184 * s)),
            (int(128 * s), int(208 * s)),
            (int(139 * s), int(184 * s)),
        ],
        fill=fg_white,
    )

    # 4. Chibi head circle
    d.ellipse(
        [int(72 * s), int(48 * s), int(184 * s), int(160 * s)],
        fill=bg_surface,
        outline=fg_white,
        width=int(10 * s),
    )

    # Hair bangs
    d.arc(
        [int(74 * s), int(58 * s), int(132 * s), int(112 * s)],
        start=195,
        end=345,
        fill=fg_white,
        width=int(8 * s),
    )
    d.arc(
        [int(124 * s), int(58 * s), int(182 * s), int(112 * s)],
        start=195,
        end=345,
        fill=fg_white,
        width=int(8 * s),
    )

    # 5. Expressive Chibi eyes + subtle catchlights
    for cx in (106, 150):
        d.ellipse(
            [int((cx - 8) * s), int(100 * s), int((cx + 8) * s), int(120 * s)],
            fill=fg_white,
        )
        d.ellipse(
            [int((cx + 1) * s), int(103 * s), int((cx + 6) * s), int(108 * s)],
            fill=bg_deep,
        )

    # Gentle smile arc
    d.arc(
        [int(118 * s), int(120 * s), int(138 * s), int(136 * s)],
        start=15,
        end=165,
        fill=fg_white,
        width=int(6 * s),
    )

    # 6. Mini A4 Resume Card in foreground corner
    d.rounded_rectangle(
        [int(148 * s), int(154 * s), int(206 * s), int(224 * s)],
        radius=int(9 * s),
        fill=bg_deep,
        outline=fg_white,
        width=int(8 * s),
    )
    d.line(
        [(int(162 * s), int(173 * s)), (int(192 * s), int(173 * s))],
        fill=fg_white,
        width=int(6 * s),
    )
    d.line(
        [(int(162 * s), int(189 * s)), (int(192 * s), int(189 * s))],
        fill=fg_muted,
        width=int(5 * s),
    )
    d.line(
        [(int(162 * s), int(204 * s)), (int(182 * s), int(204 * s))],
        fill=fg_muted,
        width=int(5 * s),
    )

    return img


def main() -> int:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)

    master = render_chibi_master_canvas(1024)
    base_256 = master.resize((256, 256), Image.Resampling.LANCZOS)

    ico_path = ASSETS_DIR / "resumatch.ico"
    base_256.save(ico_path, format="ICO", sizes=ICO_SIZES)

    favicon_path = PUBLIC_DIR / "favicon.ico"
    base_256.save(favicon_path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])

    touch_path = PUBLIC_DIR / "apple-touch-icon.png"
    master.resize((180, 180), Image.Resampling.LANCZOS).save(touch_path, format="PNG")

    print(f"Generated {ico_path} ({ico_path.stat().st_size} bytes) + {favicon_path} + {touch_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
