<h1 align="center">GR3-LUTSmith</h1>

<p align="center">
  <strong>Your own colour look, from a <code>.cube</code> LUT, inside your RICOH GR III or GR IIIx.</strong>
</p>

GR3-LUTSmith takes a colour-grading LUT (`.cube`) and builds a firmware for the
RICOH GR III or GR IIIx in which one **Image Control** look of your choosing
carries the LUT's look. You can shoot with it in stills, in movie mode and in
the viewfinder, and use it in **RAW Development**. Everything else on your
camera stays exactly as it was, and you can go back to the official firmware at
any time.

This repository contains no RICOH firmware. You download the official firmware
from RICOH yourself, and the included command-line tool turns it into the
version with your look, on your own computer.

> [!IMPORTANT]
> **Status of version 1.0.0: checked offline, no severe issue known; not yet
> tested on a real camera.** Every firmware the tool writes is checked before it
> is written, and the camera's own look processing was emulated for every slot
> on both official files. A GR III has accepted a modified firmware and was
> restored by installing the official file again, but no firmware made by this
> version has been installed on a camera yet. See [Testing status](#testing-status).

> [!WARNING]
> **Unofficial, at your own risk.** Not made or endorsed by RICOH IMAGING.
> Installing modified firmware may void your warranty. Please read
> [Before you start](#before-you-start) and [Risks](#risks) first.

| You want to | Go to |
| --- | --- |
| Know whether your camera is supported | [Supported cameras](#supported-cameras) |
| Make your firmware | [Make your firmware](#make-your-firmware) |
| Build a look someone shared with you | [Using a look someone shared](#using-a-look-someone-shared) |
| Find out what a firmware file contains | [Check a firmware file](#check-a-firmware-file) |
| Go back to RICOH's firmware | [Going back to the official firmware](#going-back-to-the-official-firmware) |

**Contents:**
[Supported cameras](#supported-cameras) |
[How it works](#how-it-works) |
[What you get](#what-you-get) |
[Before you start](#before-you-start) |
[Make your firmware](#make-your-firmware) |
[Put it on the camera](#put-it-on-the-camera) |
[Make it yours](#make-it-yours) |
[Getting a closer fit](#getting-a-closer-fit) |
[Good to know](#good-to-know) |
[Risks](#risks) |
[Testing status](#testing-status) |
[For developers](#for-developers)

## Supported cameras

RICOH publishes one firmware file for the GR III series and one for the GR IIIx
series. The tool works with the current version of each, and with every camera
RICOH made that file for:

| Your camera | Official firmware | File |
| --- | --- | --- |
| RICOH GR III, GR III Street Edition, GR III Diary Edition, GR III HDF | GR III **2.10** | `fwdc239b.bin` |
| RICOH GR IIIx, GR IIIx Urban Edition, GR IIIx HDF | GR IIIx **1.60** | `fwdc243b.bin` |

- Your look renders the same on the standard, HDF and edition models of a
  series, and the HDF filter works as usual.
- The firmware you build keeps the official file's name. Install a firmware
  built from the GR III file only on a GR III-series camera, and one built from
  the GR IIIx file only on a GR IIIx-series camera.
- **Not supported:** other firmware versions (older or newer), the GR IV
  family, the GR II and other models. The tool refuses those files.

## How it works

The GR III and GR IIIx do not run LUTs directly. Their looks are built from a
few native controls: a colour mix, tone curves and **hue sectors** (separate
colour changes for 12 ranges of hue). GR3-LUTSmith **fits** your LUT to those
controls, measures how close the camera can get, and only then writes the
firmware.

- **Tonal and overall colour looks** fit very well. That covers film-style
  curves, contrast, faded blacks, warm or cool casts, and overall saturation
  changes.
- **Looks that move single hues separately** (teal-orange, film simulations
  that shift greens or skies, …) are fitted with the **hue sectors**. They get
  much closer than with the colour mix alone. Changes that depend strongly on
  how saturated a colour is (e.g. only the most vivid reds) remain approximate,
  and the tool tells you when that is the case.
- **Log and camera-specific conversion LUTs** (S-Log, V-Log, …) are not
  supported. Use a "creative" LUT made for normal (Rec.709 / sRGB) pictures.
- **Colour looks only** in this version. A black-and-white LUT works on a colour
  slot (your look then has no colour), but the camera's monochrome looks cannot
  carry a look yet.

The fit is judged with **CIEDE2000**, the standard measure of how different
two colours look. As a guide, **below 1 is hard to see side by side** and
around 2 is a small visible difference.

| Result | Meaning |
| --- | --- |
| **good** | average difference below 1, and 95 % of colours below 2: looks the same |
| **approximate** | average below 2: close, with visible differences in some colours |
| **poor** | the camera cannot reproduce the LUT's whole colour range; firmware is still written when **everyday colours** fit (see [When a LUT is "poor"](#when-a-lut-is-poor)) |

Every LUT is measured three ways: across its whole colour range, on the
24 everyday colours of a **ColorChecker** chart (skin, sky, foliage, …), and on
**neutral greys** (where any colour cast is most visible). With a photo of your
own (`--photo`), also on that photo.

## What you get

- Your look **replaces one of the camera's colour looks**. By default that is
  **Cross Processing** (the last entry in the Image Control list); you can choose
  Standard, Vivid, Positive Film or Negative Film instead, or use several (see [Choosing the slot](#choosing-the-slot)). The entry can show
  [your own name](#naming-the-slot) and [your own icon](#your-own-icon).
- It opens with **every slider at 0**. Your LUT's look is built in at those
  settings; the sliders then work as usual, starting from your look.
- Every look you did not replace is unchanged.

## Before you start

- **A supported camera** (see [Supported cameras](#supported-cameras)).
- **Your camera must already be on the current firmware:** GR III series **2.10**,
  GR IIIx series **1.60**. Check the version in the camera's setup menu.
  - Older version: first update the normal way, with RICOH's official file.
  - Newer version: **do not use this tool.** It only supports these versions.
- **The official firmware file.** From RICOH IMAGING's official support page for
  your camera, download that version. Unpack it if needed until you have the file
  **`fwdc239b.bin`** (GR III series) or **`fwdc243b.bin`** (GR IIIx series).
- **A fully charged battery.** A firmware update must never lose power.
- **An SD card** you can format.
- **Your LUT** as a `.cube` file (3D or 1D), and whether it was made for
  **sRGB** (most LUTs) or **Adobe RGB**.
- **Recommended:** a DNG photo taken with your camera. The tool uses it to check
  the look on real content and to make a preview.
- Note down any custom settings (C1, C2, …) you would not want to set up again.

## Make your firmware

### 1. Set up

Install **[uv](https://docs.astral.sh/uv/)** (it installs Python and everything
else the tool needs), open a terminal in this folder and run:

```sh
uv sync --extra photo
```

<details>
<summary>Without uv</summary>

<br>

With Python 3.9 or newer:

```sh
python3 -m pip install numpy rawpy pillow
python3 gr3_lutsmith.py make my_look.cube ~/Downloads/fwdc239b.bin --photo R0001234.DNG
```

`rawpy` and `pillow` are only needed for `--photo`, `--icon` and `preview`.

</details>

### 2. Make it

Use your own paths, and the official file for your camera (`fwdc239b.bin` for
the GR III series, `fwdc243b.bin` for the GR IIIx series):

```sh
uv run gr3_lutsmith.py make my_look.cube ~/Downloads/fwdc239b.bin --photo R0001234.DNG
```

| Option | What it does |
| --- | --- |
| `--space adobe` | the LUT was made for Adobe RGB |
| `--photo R0001234.DNG` | checks the look on your photo too (optional, but recommended) |
| `--slot vivid`, `--slot negative-film`, … | puts your look in place of another look instead of Cross Processing: [Choosing the slot](#choosing-the-slot) |
| `--name "My Look"` | shows the slot under your own name: [Naming the slot](#naming-the-slot) |
| `--icon my_icon.png` | gives the slot your own menu icon: [Your own icon](#your-own-icon) |
| `--turns 3` | runs three fits and keeps the best ones: [More turns](#more-turns) |
| `--jobs 3` | runs up to three of those turns side by side: [More turns](#more-turns) |
| `--after-curves` | also tries a second colour mix: [Colour after the curves](#colour-after-the-curves) |
| `--no-hue` | fits without hue sectors (less room needed: [Several looks](#several-looks-in-one-firmware)) |

The tool:

1. makes sure you gave it a genuine official file (GR III 2.10 or GR IIIx 1.60);
2. fits the LUT (about a minute or a few, depending on the LUT and your computer)
   and shows how well it fits;
3. builds the firmware and checks it.

For example:

```sh
uv run gr3_lutsmith.py make "Kodak Portra 400.cube" ~/Downloads/fwdc239b.bin --slot vivid --name "Portra"
```

ends like this:

```
  result: POOR  (ΔE00 2.355, p95 6.227; without the look 11.746)
  everyday colours: ColorChecker ΔE00 1.421 (skin 0.869, worst white); greys 0.944 (largest cast 1.54)
  ...
wrote output/Kodak Portra 400/fwdc239b.bin  (SHA-256 …)
Copy this file to the root of the SD card and run the camera's firmware update.
Then choose Portra in Image Control: your look is there.
Status of this version: checked offline, no severe issue known; not yet tested on a real camera.
If anything looks wrong on the camera, install the official RICOH file again to go back.
```

(This LUT is "poor" across its whole range but fits everyday colours closely,
so the firmware is written: see [When a LUT is "poor"](#when-a-lut-is-poor).)

The output folder `output/<your LUT>/` also contains `look.json` (the fitted
look) and can take a preview (step 3). You can share `look.json` with other
GR III and GR IIIx owners: they build the same look without fitting it again
(see [Using a look someone shared](#using-a-look-someone-shared)).

If the LUT cannot be reproduced well, or the firmware file is not an official
file the tool supports, it prints `refused: …` and writes **no firmware**.

**Try many LUTs first:** `uv run gr3_lutsmith.py fit my_look.cube --photo R0001234.DNG`
fits and reports without needing the firmware file.

### 3. Preview it (optional)

```sh
uv run gr3_lutsmith.py preview "output/my_look/look.json" R0001234.DNG
```

This writes `preview.jpg` next to `look.json` with three panels: the camera
without the look, the LUT itself, and the camera **with** the look. The LUT
panel needs the `.cube` file: the tool finds it next to `look.json` or in the
current folder, or give it with `--lut my_look.cube`. Colour and tone are what
to compare. Sharpening and noise are not simulated.

Then [put it on the camera](#put-it-on-the-camera).

## Put it on the camera

### 1. Copy it to the SD card

1. **Format the SD card in the camera.**
2. Copy the firmware file the tool wrote (`fwdc239b.bin` or `fwdc243b.bin`) to
   the **top level** of the card: not into a folder, and keep the name exactly
   as it is.

### 2. Update the camera

1. Insert the fully charged battery and the SD card.
2. Start the firmware update from the camera's setup menu, as described in
   RICOH's official firmware update instructions for your camera.
3. **The camera offers an update to the version it already has** (2.10 → 2.10,
   or 1.60 → 1.60). This is expected: the modified firmware keeps the version
   number. Confirm the update.

> [!CAUTION]
> **Do not turn the camera off, open the battery or card door, or remove the
> card** until the update has finished and the camera has restarted.

### 3. Shoot

- Open **Image Control** and choose your look: **Cross Processing** (or the
  slot you chose, or the name you gave it).
- The firmware version still shows 2.10 (or 1.60). That is normal.
- Delete the firmware file from the card (or format the card) so the update is
  not started again by accident.

To change to another LUT later, make a new firmware from the **official** file
and install it the same way.

### Going back to the official firmware

Repeat steps 1–2 with the **official** file from RICOH instead. Confirm the
update, and your camera is back to stock.

## Make it yours

### Choosing the slot

Your look replaces one of the camera's colour looks. You choose which one with
`--slot` (`make` and `build`); `uv run gr3_lutsmith.py slots` shows the same list:

| `--slot` | Shown as | Replaces | Still | Movie | Name up to | Tone curves |
| --- | --- | --- | :---: | :---: | --- | --- |
| `cross-processing` (default) | Cross Processing | RICOH's Cross Processing look | ✓ | ✓ | 19 characters | built in |
| `standard` | Standard | RICOH's Standard look | ✓ | ✓ | (keeps "Standard") | extra step |
| `vivid` | Vivid | RICOH's Vivid look | ✓ | ✓ | 13 characters | extra step |
| `positive-film` | Positive Film | RICOH's Positive Film look | ✓ | ✓ | 19 characters | extra step |
| `negative-film` | Negative Film | RICOH's Negative Film look | ✓ | ✓ | 23 characters | extra step |

- The colours are the same in every slot: the camera processes your look the
  same way there, so the fit and its scores do not change with the slot.
- Your look opens with every slider at 0 in every slot. Saturation, Hue and the
  other sliders work on top of your look.
- **Tone curves.** Cross Processing is the one look that already has tone curves
  of its own, so a look there changes the camera's settings data only. On the
  other slots, the firmware also gets a small extra processing step that gives
  the slot its own tone curves ("extra step"). Both were checked offline in the
  same way; see [Testing status](#testing-status).
- **Cross Processing:** its Color Tone setting has no effect on your look.
- A slot you do not use keeps RICOH's look.
- **Not available in this version:**
  - **Monotone, Soft Monotone, Hard Monotone, Hi-Contrast B&W:** the camera keeps
    showing these in black and white, whatever the look's colours.
  - **Retro:** the camera keeps Retro's own tone effect and colour cast on top
    of your look.
  - **Bleach Bypass:** the camera adds its Toning colour cast on top of your
    look.
  - **HDR Tone:** it uses processing of its own.
- `check` lists which slots carry a personal look.

### Several looks in one firmware

Fit each LUT with `fit`, then build them together, each look file followed by
its own `--slot` (and `--name`, `--icon` if you like):

```sh
uv run gr3_lutsmith.py fit look_a.cube --photo R0001234.DNG -o output/a
uv run gr3_lutsmith.py fit look_b.cube --photo R0001234.DNG -o output/b
uv run gr3_lutsmith.py build output/a/look.json output/b/look.json --slot vivid --name "Look B" ~/Downloads/fwdc239b.bin -o output/both
```

**How many at once:** the firmware has a limited amount of room for looks.
A look **with hue sectors** takes most of it, so it is the only look in its
firmware. Looks **without** hue sectors (fit them with `--no-hue`) fit on all
five slots together. If they do not fit, the tool says so and writes nothing.

### Naming the slot

`--name` (`make` and `build`) gives the slot your own name, in the Image Control
menu and its screens, in all of the camera's languages:

```sh
uv run gr3_lutsmith.py make my_look.cube ~/Downloads/fwdc239b.bin --slot vivid --name "Portra"
```

- Each slot takes names up to a certain length (see the table above, or
  `slots`). Standard cannot be renamed: its name is shared with other screens.
- **Plain ASCII** (English letters, digits, spaces and punctuation) shows in
  every camera language. This is the safe choice.
- **Other letters and characters** work in the camera languages that use them:
  a Japanese name shows when the camera is set to Japanese, `Müller` in the
  European languages, `Плёнка` in Russian, and so on. Characters that none of
  the camera's languages use (emoji, Arabic, Hebrew, …) are refused.
- The tool checks the name against your **camera language**, which is your
  computer's language unless you set `--camera-language` (for example
  `--camera-language Japanese`; `--help` lists all 21). If the name uses
  characters that language does not, the tool asks whether to use it anyway;
  `--accept-name` agrees in advance.
- Long names may be cut off on some screens.
- Without `--name`, the slot keeps its own name. The name is not saved in
  `look.json`: give `--name` again when you `build`.

### Your own icon

`--icon` (`make` and `build`) replaces the slot's icon in the Image Control menu
with your own picture:

```sh
uv run gr3_lutsmith.py make my_look.cube ~/Downloads/fwdc239b.bin --slot vivid --name "Portra" --icon portra.png
```

- Any picture works (PNG, JPEG, …). The menu icon is 40×40 pixels: a larger
  or smaller picture is resized, and if it is not square, its **centre** is
  used. Transparent parts stay transparent.
- `make` saves the result as `icon.png` next to the firmware, so you can see
  exactly what the camera will show. Simple, bold shapes read best at this size.
- It needs Pillow (`uv sync --extra photo`). Without `--icon`, the slot keeps
  its own icon. The icon is not saved in `look.json`: give `--icon` again when
  you `build`.

### Using a look someone shared

`look.json` holds only the look itself: its colour settings and how well they
fit the LUT, and the LUT's file name. Whoever has the file can build the
firmware directly, without the LUT and without fitting, for a GR III or a
GR IIIx:

```sh
uv run gr3_lutsmith.py build portra-400.json ~/Downloads/fwdc243b.bin --slot vivid --name "Portra" --icon portra.png
```

- The tool first shows how well the look fits its LUT (the results its maker saw).
- Slot, name and icon are yours to choose. Without `--slot`, the look goes on
  Cross Processing.
- The file's contents are checked before anything is built: a damaged file,
  or one with values the camera cannot take, is refused and nothing is written.

## Getting a closer fit

### Hue sectors

After fitting the colour mix and tone curves, the tool tries to improve the look
with **hue sectors**. They are kept **only when they clearly help** and nothing
gets worse (your photo, the greys, the ColorChecker). The result line says
`hue sectors: yes` or `no`. Greys are never changed by them, and neighbouring
sectors always meet without a gap. `--no-hue` turns them off.

### More turns

Where the fit starts often decides how close it gets. With `--turns`, the tool
runs several fits ("turns"), each from a new random starting point, and keeps
the good ones:

```sh
uv run gr3_lutsmith.py make my_look.cube ~/Downloads/fwdc239b.bin --photo R0001234.DNG --turns 3 --jobs 3
```

- Turn 1 always uses the usual starting point. `--jobs 3` runs up to three turns
  side by side, one processor core each; the results are the same as one after
  another.
- A new result is kept unless an earlier one is at least as good on every
  measure. The tool recommends the one with the best overall score; every kept
  result gets its own folder under `turns/`.
- At the end the tool asks `More turns?`: type a number to run more (`2`, or `2a`
  / `2b` with or without colour after the curves), or press Enter.
- Every turn's seed is saved in `turns.json`; `--seed 1234` repeats a turn.

### Colour after the curves

With `--after-curves`, the tool also tries a second colour mix applied **after**
the tone curves, which can follow film looks that treat shadows and highlights
differently. The version with it is kept next to the usual one when it is
better on anything; preview both before choosing. It makes every turn several
minutes longer, and more turns of the usual fit are usually the bigger gain.

### When a LUT is "poor"

Many film-simulation LUTs reshape very saturated colours in ways the camera's
controls cannot follow, so they are "poor" across their whole colour range even
though everyday pictures look right. The firmware is still written when:

- the **ColorChecker** colours differ by less than 2 on average, the **greys**
  by less than 2, and no grey gets a visible colour cast; and, with `--photo`,
  the look is not poor on your photo; or
- with `--photo`, the look is **good on your photo** (and at least halves the
  difference there), with the same grey and colour-cast limits.

The tool says which rule applied. Otherwise it writes no firmware; add
`--force` to build anyway, and check the preview first.

## Good to know

- **An official RICOH update removes your look.** It replaces the whole
  firmware.
- **Before sending the camera for service,** go back to the official firmware.
- **New default slider values** may only show after resetting the Image Control
  settings in the camera.

### Check a firmware file

Before copying any firmware file to a card:

```sh
uv run gr3_lutsmith.py check path/to/fwdc239b.bin
```

It tells you whether the file is an official RICOH file (and which one), a
GR3-LUTSmith firmware, or something else. For a GR3-LUTSmith
firmware, it also lists which slots carry a look, their names and icons, and
whether the looks use tone curves, hue sectors and the colour mix after the
curves.

To check the tool itself against your official file:

```sh
uv run gr3_lutsmith.py selftest ~/Downloads/fwdc239b.bin
```

This builds the bundled example (`examples/film-1d.cube`) and compares the
result with the reference build for that file. It prints
`selftest (GR III 2.10): PASSED` (or `GR IIIx 1.60`).

## Risks

- Your camera cannot tell a good firmware file from a damaged one. Only copy a
  file the tool has written (or confirmed with `check`) to your card.
- If an update is interrupted, or a firmware file cannot start, the camera may
  not reach the update function again, and it would then need RICOH service.
  This cannot be ruled out.
- No firmware made by this version has been installed on a camera yet (see
  [Testing status](#testing-status)).
- Your camera's own calibration data is not touched.

## Testing status

**Version 1.0.0: checked offline, no severe issue known; not yet tested on a
real camera.**

| What | Status |
| --- | --- |
| A GR III accepts a modified firmware, and installing the official file again restores it | tested on a GR III (with an earlier test file) |
| Reading and rebuilding both official files | checked offline: an unchanged rebuild is byte-identical; every written file is decoded again and compared before it is written |
| Your look on each of the five slots (colour mix, tone curves, hue sectors, colour after the curves) | checked offline by emulating the camera's own look processing: the slot renders the look as fitted, at every setting the camera offers, and every other look is unchanged (still and movie/viewfinder, both Image Control banks); not yet tested on a camera |
| The extra step for tone curves (every slot but Cross Processing) | checked offline as above; not yet tested on a camera |
| GR III and GR IIIx; standard, HDF and edition models | checked offline: the look processing does not depend on the camera model; not yet tested on a camera |
| Your own name (`--name`) and icon (`--icon`) | checked offline: read back in all 21 languages; only the slot's own name and icon change; not yet tested on a camera |
| Building from a shared `look.json` | checked offline: the same firmware as `make` with the same look, slot, name and icon |
| Turns side by side (`--jobs`) | checked offline: the same results as one after another |
| Hue-sector ranges and colour after the curves as the camera's image processor applies them | assumed equal to the RICOH GR IV, where they were tested; not yet tested on a GR III or GR IIIx |
| Lens shading in the photo check (`--photo`) | not yet measured for these cameras |

If you install a firmware made by this tool, a report of what you saw (camera,
firmware file, slot, and a JPEG next to the LUT applied to the same DNG) helps
move these lines to "tested".

## For developers

```
GR3-LUTSmith/
  gr3_lutsmith.py        the tool (fit, make, build, preview, slots, check, selftest)
  examples/film-1d.cube  a small synthetic example LUT, and its fitted look (film-1d.look.json)
  tests/test_cli.py      checks that need no RICOH file (CI); with GR3_FIRMWARE=… also builds
```

```sh
uv run python tests/test_cli.py
GR3_FIRMWARE=~/Downloads/fwdc239b.bin uv run python tests/test_cli.py
```

| File | SHA-256 |
| --- | --- |
| official RICOH GR III 2.10 `fwdc239b.bin` | `d6a3c9c288d784080e7b283222ecdd8752adfeed6faa36dab44f73115354bfd4` |
| official RICOH GR IIIx 1.60 `fwdc243b.bin` | `708e79f3b7b96a7e1581b4acf4c412233050a1832718ef7f06c8397db1b431c7` |
| example firmware (`selftest`, GR III 2.10) | `e627de366d85d6eb998054b145dc3ad18d0176aa2dda296d31385cd5dcf6892e` |
| example firmware (`selftest`, GR IIIx 1.60) | `1da3b1a3acfc17bde7989247af965e7528780efd8998bdf90f86b18848f25717` |
