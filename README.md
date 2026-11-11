# Monument: card build pipeline

Edit the cards in a spreadsheet, run one command, and load the result in Tabletop Simulator.

```
cards/cards.csv   the card list, one row per card design
style.toml        how cards look: colours, fonts, sizes, card backs
config.toml       game name, player count, your GitHub repo, TTS folder
build.py          renders the cards and writes the TTS saved object
out/images/       card sheets TTS downloads from GitHub (committed)
out/tts/          the TTS saved object (also copied into TTS for you)
out/preview/      one PNG per card for a quick look (not committed)
```

## Everyday workflow

1. Edit `cards/cards.csv` (Excel, VS Code, anything that saves CSV).
2. Run `python build.py --push`.
3. In Tabletop Simulator: **Objects > Saved Objects > Monument**. A bag spawns holding the monument and each player's starting deck and draft pool.

Only the host needs to spawn the bag; everyone in the room sees the cards because the images load from GitHub.

For solo testing without pushing, run `python build.py --local`. That points TTS at the images on this PC, so nobody else in a room would see them.

## First-time setup

1. Install Python 3.11 or newer, then in this folder run:
   ```
   python -m pip install -r requirements.txt
   ```
2. Create an empty **public** GitHub repo called `monument`. It has to be public because TTS downloads the images from `raw.githubusercontent.com` without logging in.
3. Put your GitHub username in `config.toml` under `[github]`.
4. In this folder run:
   ```
   git init -b main
   git add -A
   git commit -m "Card pipeline"
   git remote add origin https://github.com/YOUR-GITHUB-USERNAME/monument.git
   git push -u origin main
   ```
5. Run `python build.py --push` and load it in TTS.

## The card list

| Column | Meaning |
| --- | --- |
| `id` | Unique code printed on the card, e.g. `E03`. |
| `name` | Card name. |
| `deck` | `Starting`, `Draft` or `Shared`. Starting and Draft are given to every player; Shared cards appear once. |
| `type` | `Basic`, `Action`, `Event` or `Monument`. Any new type needs a colour in `style.toml`. |
| `copies` | Copies per player (Starting, Draft) or in total (Shared). `0` leaves a card out. |
| `cost` | Idea cost, shown in the circle. Empty for none. |
| `build` | Build cost, e.g. `2 stone, 1 iron`. Empty for none. |
| `text` | Card text. `Start:`, `Activate:`, `Build:` and `Choose one:` print in bold. Write `\n` for a line break. |
| `role` | Designer note. Not printed; shows in the card's TTS notes. |
| `version` | Printed in the corner so testers can tell printings apart. Empty uses `config.toml`'s version. |

Excel note: save as **CSV UTF-8**.

## Good to know

- Image names include a fingerprint of their contents, so every change gets a new URL and TTS never shows a stale cached card. Old images are removed on each build.
- Events get a band across the bottom, edge to edge, so cards laid side by side form one track. Which types get it is set in `style.toml` under `[track]`.
- Card text shrinks to fit. If a card still doesn't fit, the build prints a warning.
- If TTS's Saved Objects folder isn't found automatically, set `saved_objects_dir` in `config.toml`.
- Fonts are Inter, under the SIL Open Font License (`fonts/LICENSE-Inter.txt`).
