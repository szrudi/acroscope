# acroscope brand

## Concept

A powerloop seen through a scope. The mark is one amber stroke: a flight path enters level from the left, pulls up
into a round loop, crosses itself and leaves level to the right, inside a reticle ring with ticks at twelve and
six o'clock. The ring is broken where the path passes through it: the session comes in from outside, the trick is
what the scope is looking at. "acro" is the loop, "scope" is the ring, and the level line doubles as the timeline
under the player's video. It is drawn in the same weight as the player's UI lines, nothing filled, so it sits on
the dark panels as a line element rather than a badge.

## Palette

| role | hex | use |
|---|---|---|
| amber (brand) | `#f5a524` | the mark, the only colour the mark uses |
| text | `#e6e8ec` | the wordmark on dark |
| dim | `#9aa3b2` | tagline, secondary text |
| bg | `#14161a` | dark background (touch icon, banner) |
| panel / line | `#1c1f25` / `#2b3039` | banner timeline strip and its rules |
| blue / green / red | `#4cc2ff` / `#5fd68b` / `#ff6b6b` | moment colours, only in the banner's timeline motif |

On a light background the mark stays amber (no inversion); the wordmark becomes the page's text colour.

## Files

| file | what it is |
|---|---|
| `icon.svg` | the mark, 64x64 viewBox, transparent, stroke 4. App icon, README, anything 48 px and up. |
| `favicon.svg` | the mark for 16-32 px: no ticks, stroke 5.5, wider ring gaps, slightly larger loop. |
| `logo.svg` | mark + wordmark, 48 px tall, for a dark header. Wordmark is DejaVu Sans Bold converted to paths, so no font dependency. |
| `banner.svg` | 1200x400 README banner: dark bg, mark, wordmark, tagline, a timeline strip with moments, throttle trace and playhead. |
| `favicon-32.png`, `favicon-16.png` | raster favicons from `favicon.svg`, transparent. |
| `apple-touch-icon.png` | 180x180, mark at 140 px centred on `#14161a`, opaque. |
| `icon-512.png` | the mark at 512, transparent (PWA manifest, app stores, social previews on dark). |
| `banner.png` | the banner rendered at 1200x400. |

## Regenerating the PNGs

ImageMagick 7 (`magick`), from this directory. Its built-in SVG renderer needs an explicit `stroke-opacity="1"` on
every stroked path (it defaults the root to 0); the SVGs carry it, keep it when editing.

```
magick -background none -density 384 favicon.svg -resize 32x32 favicon-32.png
magick -background none -density 384 favicon.svg -resize 16x16 favicon-16.png
magick -background "#14161a" -density 384 icon.svg -resize 140x140 -gravity center -extent 180x180 -alpha off apple-touch-icon.png
magick -background none -density 384 icon.svg -resize 512x512 icon-512.png
magick -density 96 banner.svg banner.png
```

## Usage

- Minimum size: `icon.svg` at 24 px, `favicon.svg` below that. Do not use `icon.svg` under 24 px; the ticks and the
  ring gaps turn to noise.
- Clear space: at least the ring's stroke width times three (12/64 of the mark's width) on every side; in the
  header the mark sits 12 px from the wordmark at 48 px tall.
- Dark is home. On light backgrounds the amber reads but has low contrast (about 1.9:1 on white); use it at 32 px
  or larger there, and prefer the wordmark in dark text next to it.
- One colour only. Do not recolour the mark to blue, green or red; those are the moment colours in the player and
  the mark must stay distinct from them.
- Do not add a fill inside the ring, drop the ticks (except in the favicon), or rotate the mark.
- Wordmark: lowercase "acroscope", DejaVu Sans Bold, letter-spacing about -2% of the size; `logo.svg` has it as
  paths, so use that rather than re-setting the type.
