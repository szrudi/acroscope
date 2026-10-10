# acroscope mark: variants to compare

Three alternatives next to the current mark, all 64x64 viewBox, one amber (`#f5a524`), same stroke language. The
contact sheet `variants.png` (1600x900) shows each at 128 px, at 32 and 16 px as true rasters upscaled 4x with
nearest-neighbour (so you see the pixels a tab bar gets), 1:1 inside a light and a dark tab strip, at 32 px on white,
and at 96 px on a light page. The sketches that lost (crosshair ticks at the crossing, a gyro sine that loops once, a
whoop duct with a flip arc) are in the session scratchpad, not here. Nothing existing was changed.

## A: `variant-a.svg`, the current mark on a dark tile
- Idea: the current mark unchanged, at 84%, on a `#14161a` rounded square (radius 14); the tile is the mark's own
  dark background travelling with it.
- Carries the favicon cut (no ticks, stroke 5.5) because the tile exists for 16-32 px; at 48 px and up the icon cut
  with ticks goes on the same tile (bottom row of the sheet), exactly as `apple-touch-icon.png` already does.
- Better: the only one of the four that holds contrast in a light tab bar and on white at 32 px (amber on the tile is
  about 9:1 instead of 1.9:1 on white); the identity does not change.
- Worse: it is a badge, which `BRAND.md` deliberately avoided; the mark shrinks by a sixth inside the tile, so at 16 px
  the loop is one pixel smaller than the current favicon; on dark pages the tile vanishes and nothing is gained.
- At 16 px it reads as "dark square with an amber ring", which is a recognisable tab even when the loop is not.

## B: `variant-b.svg`, the loop alone with a reticle dot
- Idea: no ring; the whole box goes to the flight path (level entry, loop, crossing, level exit, stroke 5) and the
  scope is reduced to a single dot in the loop's eye: the thing the scope is looking at.
- The dot sits at the loop's centre (r 4), not on the crossing, where it would merge with the stroke; the crossing is
  already an X and needed nothing.
- Better: the loop is 25% larger than inside the ring, so it is the most legible of the four at 16 px on dark; the
  figure is simpler to remember and draw, and it looks like a trace, which is what the tool shows.
- Worse: "scope" is nearly gone (a dot has to carry it); on white it is the thinnest of the four at 16 px because the
  amber stroke has nothing dark behind it; the level line no longer meets a ring, so it is less of a timeline.
- At 16 px it reads as "loop with a dot", on light and dark; it is the one that would survive a monochrome stamp.

## C: `variant-c.svg`, OSD corner brackets with the loop inside
- Idea: the goggle view instead of a scope: four corner brackets (stroke 3.5, like a camera or OSD frame) with the loop
  (stroke 4.5) framed inside; the trace passes out through the frame's open sides.
- Quad-centric without drawing a quad: the brackets say "what the pilot sees", the loop says what they did.
- Better: distinct from any other ring-and-line mark; the brackets stay readable at 16 px as four corner dots and the
  open sides keep the "session comes in from outside" idea from the current mark.
- Worse: two elements of different weight compete; the loop gets the least room of the four, so at 16 px it is a
  blob between corners; brackets are a stock UI idiom (screenshot, focus, scan) and could be read as such.
- At 16 px it reads as "framed something"; the frame is recognised before the loop.

## Recommendation
Keep the current mark and adopt A's tile only for the favicon and app icon, so light tab bars and white pages get
contrast without changing the identity; B is the one to take if Rudi wants a simpler mark rather than a fix.
